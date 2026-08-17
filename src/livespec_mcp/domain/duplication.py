"""Duplication detection, in the two levels that can run before a write.

An agent rewrites a helper that already exists because it has no cheap way to
ask "is this already here?". Grep answers on names, and the whole point is that
the duplicate has a *different* name — that is why it got written.

The plan this implements defines three levels. Two of them live here:

===== ==================== ============ =========================================
level what                 budget       catches
===== ==================== ============ =========================================
0     normalised AST hash  < 10 ms      literal copy with the identifiers renamed
1     winnowed fingerprint < 100 ms     near-duplicate: reordered, edited, padded
2     semantic embedding   seconds      same intent, unrelated code  (NOT here)
===== ==================== ============ =========================================

Level 2 is deliberately absent: it costs seconds, and anything that costs
seconds on the path of a write gets switched off within days. It belongs to the
async post-write pass, feeding search and reports rather than gating.

**Normalisation is the whole of level 0.** Hashing source text finds nothing —
whitespace, comments and one renamed variable all defeat it, and those are
exactly the differences a rewrite introduces. Hashing a *structure* with the
identifiers replaced by their binding position finds the copy that a human
would call the same function with different names.

**A false positive is expensive here in a way a false negative is not.** A
missed duplicate costs some redundancy. A wrong "this already exists" blocks
work the agent was right to do, and two of those teach the user to disable the
check — after which it catches nothing at all. Both levels are therefore tuned
to be quiet: level 0 only fires on structural identity, and level 1 needs a
high overlap on a body long enough for the overlap to mean something.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass

# Below this, structural similarity is noise: every two-line guard clause in a
# codebase has the same shape as every other, and reporting them as duplicates
# is how a checker gets turned off.
MIN_TOKENS_FOR_LEVEL1 = 40

# k-gram width for winnowing. Small enough to survive an edit inside a
# statement, wide enough that a match means shared structure and not a shared
# keyword.
KGRAM = 5
WINDOW = 4

# Overlap above which two bodies are reported as near-duplicates. High on
# purpose — see the module docstring on the cost asymmetry.
NEAR_DUPLICATE_THRESHOLD = 0.80


@dataclass(frozen=True)
class Fingerprint:
    """What has to be stored per symbol to answer both levels."""

    structural_hash: str
    minhashes: tuple[int, ...]
    token_count: int

    @property
    def eligible_for_level1(self) -> bool:
        return self.token_count >= MIN_TOKENS_FOR_LEVEL1


# ---------------------------------------------------------------------------
# level 0 — normalised structure
# ---------------------------------------------------------------------------


class _Normaliser(ast.NodeVisitor):
    """Render a Python AST as a token stream with identifiers made positional.

    `def add(a, b): return a + b` and `def sum2(x, y): return x + y` produce the
    same stream. That is the point: a copy with the names changed is the copy
    this level exists to find.

    Literals are kept as their *type*, not their value — two functions that
    differ only in a magic number are the same code with a different constant,
    and a reviewer wants to see both.
    """

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self._names: dict[str, str] = {}

    def _slot(self, name: str) -> str:
        if name not in self._names:
            self._names[name] = f"v{len(self._names)}"
        return self._names[name]

    def generic_visit(self, node: ast.AST) -> None:
        self.tokens.append(type(node).__name__)
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.tokens.append(f"N:{self._slot(node.id)}")

    def visit_arg(self, node: ast.arg) -> None:
        self.tokens.append(f"A:{self._slot(node.arg)}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # The attribute name is kept: `x.commit()` and `x.rollback()` are not
        # the same code, and erasing that is how level 0 starts lying.
        self.tokens.append(f"AT:{node.attr}")
        self.visit(node.value)

    def visit_Constant(self, node: ast.Constant) -> None:
        self.tokens.append(f"C:{type(node.value).__name__}")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # The function's own name is dropped — renaming is the disguise.
        self.tokens.append("FunctionDef")
        for a in node.args.args:
            self.visit(a)
        for stmt in node.body:
            self.visit(stmt)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]


# Fallback tokenizer for the eight non-Python languages the index covers. No
# parse, so no positional renaming: identifiers are erased wholesale rather
# than numbered. Coarser than the Python path and honest about it — it will
# miss a structural copy that the Python path would catch.
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[^\sA-Za-z0-9_]")
_KEYWORDS = frozenset({
    "if", "else", "for", "while", "return", "function", "func", "def", "class",
    "const", "let", "var", "new", "try", "catch", "finally", "throw", "raise",
    "import", "from", "export", "public", "private", "static", "void", "int",
    "string", "bool", "true", "false", "null", "nil", "None", "async", "await",
    "match", "case", "switch", "break", "continue", "in", "is", "not", "and",
    "or", "fn", "let", "mut", "pub", "impl", "struct", "enum", "type",
})


def _generic_tokens(source: str) -> list[str]:
    out: list[str] = []
    for tok in _WORD.findall(source):
        if tok in _KEYWORDS:
            out.append(tok)
        elif tok[:1].isalpha() or tok[:1] == "_":
            out.append("ID")
        elif tok.isdigit():
            out.append("NUM")
        else:
            out.append(tok)
    return out


def tokenize(source: str, language: str = "python") -> list[str]:
    """Normalised token stream for a symbol body."""
    if language == "python":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # A body sliced out of a file can be a method with its `def` at an
            # indent the parser rejects. Retry dedented before giving up.
            try:
                tree = ast.parse(_dedent(source))
            except SyntaxError:
                return _generic_tokens(source)
        n = _Normaliser()
        n.visit(tree)
        return n.tokens
    return _generic_tokens(source)


def _dedent(source: str) -> str:
    lines = [ln for ln in source.splitlines() if ln.strip()]
    if not lines:
        return source
    pad = min(len(ln) - len(ln.lstrip()) for ln in lines)
    return "\n".join(ln[pad:] for ln in source.splitlines())


def structural_hash(source: str, language: str = "python") -> str:
    toks = tokenize(source, language)
    return hashlib.sha256(" ".join(toks).encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# level 1 — winnowed fingerprint
# ---------------------------------------------------------------------------


def _kgram_hashes(tokens: list[str], k: int = KGRAM) -> list[int]:
    if len(tokens) < k:
        return []
    return [
        int.from_bytes(
            hashlib.blake2b(" ".join(tokens[i:i + k]).encode("utf-8"), digest_size=8).digest(),
            "big",
        )
        for i in range(len(tokens) - k + 1)
    ]


def winnow(tokens: list[str], k: int = KGRAM, window: int = WINDOW) -> tuple[int, ...]:
    """Select a stable subset of k-gram hashes (Schleimer et al.).

    Taking every k-gram would make comparison O(body length) per candidate.
    Winnowing keeps the minimum of each sliding window, which guarantees that
    any shared substring longer than ``k + window - 1`` shares at least one
    selected hash — so the subset can be compared instead of the whole, and a
    match still means something.
    """
    grams = _kgram_hashes(tokens, k)
    if not grams:
        return ()
    if len(grams) <= window:
        return (min(grams),)
    picked: list[int] = []
    prev = -1
    for i in range(len(grams) - window + 1):
        w = grams[i:i + window]
        j = i + w.index(min(w))
        if j != prev:
            picked.append(grams[j])
            prev = j
    return tuple(sorted(set(picked)))


def fingerprint(source: str, language: str = "python") -> Fingerprint:
    toks = tokenize(source, language)
    return Fingerprint(
        structural_hash=hashlib.sha256(
            " ".join(toks).encode("utf-8")
        ).hexdigest()[:32],
        minhashes=winnow(toks),
        token_count=len(toks),
    )


def similarity(a: Fingerprint, b: Fingerprint) -> float:
    """Jaccard overlap of the two winnowed sets."""
    if not a.minhashes or not b.minhashes:
        return 0.0
    sa, sb = set(a.minhashes), set(b.minhashes)
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


# ---------------------------------------------------------------------------
# the question a caller actually asks
# ---------------------------------------------------------------------------


@dataclass
class Match:
    qualified_name: str
    file_path: str
    level: int          # 0 structural identity, 1 near-duplicate
    similarity: float

    @property
    def reason(self) -> str:
        if self.level == 0:
            return "identical structure (only the names differ)"
        return f"{self.similarity:.0%} structural overlap"


def find_duplicates(
    candidate: Fingerprint,
    corpus: list[tuple[str, str, Fingerprint]],
    *,
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
    limit: int = 5,
) -> list[Match]:
    """Everything in *corpus* that this candidate duplicates.

    `corpus` entries are ``(qualified_name, file_path, fingerprint)``.

    Level 0 hits are returned even for short bodies — structural identity is
    identity regardless of length. Level 1 needs `MIN_TOKENS_FOR_LEVEL1`,
    because below that every guard clause looks like every other one and the
    report becomes noise the reader learns to skip.
    """
    exact: list[Match] = []
    near: list[Match] = []
    for qname, path, fp in corpus:
        if fp.structural_hash == candidate.structural_hash:
            exact.append(Match(qname, path, 0, 1.0))
            continue
        if not (candidate.eligible_for_level1 and fp.eligible_for_level1):
            continue
        score = similarity(candidate, fp)
        if score >= threshold:
            near.append(Match(qname, path, 1, score))

    near.sort(key=lambda m: m.similarity, reverse=True)
    return (exact + near)[:limit]
