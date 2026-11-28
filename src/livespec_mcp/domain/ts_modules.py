"""TypeScript / JavaScript module resolution for imports that go through
barrels: which indexed file DEFINES the name an import binds?

The extractor stores, for an imported name, the module it was imported from as
a dotted path from the project root with `index` and the extension dropped
(`../interfaces` from `common/pipes/x.ts` -> `common.interfaces`; `../index` at
the root -> ``""``). A barrel (`index.ts`) defines nothing itself: it says
`export * from './pipe-transform.interface'` or
`export { A, B as C } from './x'`. Following those statements, to a bounded
depth and with cycle protection, lands on the one file that declares the name.

Precision rules:

* the answer is a single symbol, or nothing: two files that both export the
  name through different chains (a conflicting `export *`) yield no hit;
* only a top-level declaration of the defining file counts (its qualified name
  is exactly ``<module>.<name>``), never a method that shares the name;
* only relative re-exports are followed (`./x`, `../y`); a bare specifier
  (`@scope/pkg`, a tsconfig path alias) names nothing indexed.

`import { A } from './x'; export { A };` (a re-export through a local import)
and `export default` are not followed. Pure functions of the indexed file list
plus the barrel files' source: no DB, no MCP.
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: How many `export ... from` hops a barrel chain may take.
MAX_BARREL_DEPTH = 5

_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)
_STAR = re.compile(r"""\bexport\s+(?:type\s+)?\*\s*(?:as\s+\w+\s*)?from\s*['"]([^'"]+)['"]""")
_NAMED = re.compile(r"""\bexport\s+(?:type\s+)?\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]""")


def module_name(path: str) -> str:
    """The dotted prefix of every symbol of the file at *path* (what the
    extractor was handed: the path without its last suffix)."""
    parts = path.split("/")
    stem = parts[-1]
    for ext in _EXTS:
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    return ".".join([*parts[:-1], stem])


def module_key(path: str) -> str:
    """The key an import's dotted module path is looked up by: the module name,
    with a trailing `index` collapsed into its directory."""
    name = module_name(path)
    return name[: -len("index")].rstrip(".") if name == "index" or name.endswith(".index") else name


def _relative(spec: str, current_dir: list[str]) -> str | None:
    if not spec.startswith(("./", "../")):
        return None
    parts = list(current_dir)
    for seg in spec.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    if parts and parts[-1] == "index":
        parts.pop()
    if parts:
        for ext in _EXTS:
            if parts[-1].endswith(ext):
                parts[-1] = parts[-1][: -len(ext)]
                break
        if parts[-1] == "index":
            parts.pop()
    return ".".join(parts)


class TsModules:
    def __init__(
        self,
        paths: dict[int, str],
        read_source: Callable[[int], str | None] | None = None,
    ) -> None:
        """*paths*: file id -> project-relative posix path, JS/TS files only."""
        self._paths = paths
        self._read = read_source
        self._by_key: dict[str, list[int]] = {}
        for fid, path in sorted(paths.items()):
            self._by_key.setdefault(module_key(path), []).append(fid)
        self._exports: dict[int, list[tuple[str, str, str | None]]] = {}

    def module_file(self, key: str) -> int | None:
        """The one file a module key names (`x.ts` beats `x/index.ts`, as in
        node resolution); None when nothing or two files claim it."""
        claims = self._by_key.get(key, [])
        direct = [f for f in claims if not self._paths[f].rsplit("/", 1)[-1].startswith("index.")]
        pick = direct or claims
        return pick[0] if len(pick) == 1 else None

    def _reexports(self, fid: int) -> list[tuple[str, str, str | None]]:
        """(`*` or the exported name, source module key, the original name)."""
        if fid in self._exports:
            return self._exports[fid]
        out: list[tuple[str, str, str | None]] = []
        text = self._read(fid) if self._read is not None else None
        if text:
            text = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))
            cur = self._paths[fid].split("/")[:-1]
            for m in _STAR.finditer(text):
                key = _relative(m.group(1), cur)
                if key is not None:
                    out.append(("*", key, None))
            for m in _NAMED.finditer(text):
                key = _relative(m.group(2), cur)
                if key is None:
                    continue
                for item in m.group(1).split(","):
                    words = [w for w in item.replace("type ", "", 1).split() if w]
                    if not words or words[0] == "default":
                        continue
                    if len(words) == 3 and words[1] == "as":
                        out.append((words[2], key, words[0]))
                    elif len(words) == 1:
                        out.append((words[0], key, words[0]))
        self._exports[fid] = out
        return out

    def _find(
        self,
        fid: int,
        name: str,
        candidates_of: Callable[[str], list[tuple[int, str, int]]],
        depth: int,
        seen: frozenset[tuple[int, str]],
    ) -> list[tuple[tuple[int, str, int], int]]:
        """Candidates for *name* exported by file *fid*, with the hops taken."""
        own = f"{module_name(self._paths[fid])}.{name}"
        direct = [c for c in candidates_of(name) if c[2] == fid and c[1] == own]
        if direct:
            return [(direct[0], depth)]
        if depth >= MAX_BARREL_DEPTH or (fid, name) in seen:
            return []
        seen = seen | {(fid, name)}
        found: list[tuple[tuple[int, str, int], int]] = []
        for exported, key, original in self._reexports(fid):
            if exported != "*" and exported != name:
                continue
            target = self.module_file(key)
            if target is None or target == fid:
                continue
            found.extend(self._find(target, original or name, candidates_of, depth + 1, seen))
        return found

    def lookup(
        self,
        scope: str,
        name: str,
        candidates_of: Callable[[str], list[tuple[int, str, int]]],
    ) -> tuple[tuple[int, str, int], bool] | None:
        """The symbol *name* imported from module *scope*: (candidate, came
        through a barrel); *candidates_of* gives the symbols carrying a name. None when the module is not indexed, the name is not
        reachable from it, or two different symbols are."""
        fid = self.module_file(scope)
        if fid is None:
            return None
        hits = self._find(fid, name, candidates_of, 0, frozenset())
        distinct = {h[0][0] for h in hits}
        if len(distinct) != 1:
            return None
        cand, depth = min(hits, key=lambda h: h[1])
        return cand, depth > 0
