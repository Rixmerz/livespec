"""Data-access extraction: which tables does each function touch?

Read at index time, from source alone, for Python, TypeScript/JavaScript and
Java. Two kinds of evidence:

- SQL (or JPQL) in string literals, recognised by ``sql_access.sql_tables``.
  The table name is in the source, so the access carries it.
- ORM / query-builder calls in a fixed list of unambiguous shapes (SQLAlchemy,
  Django, Prisma, Knex, Supabase, TypeORM, Sequelize, JPA / Spring Data). Those
  name a model class, an entity or a repository type; the table is resolved
  after indexing from the model declarations this module also extracts
  (``data_access_store.resolve_data_access``). A table name is never guessed.

Every hit is attributed to the innermost symbol spanning its line. A module-
or class-level ``NAME = "<sql>"`` constant produces no access where it is
defined and one access at each use of ``NAME`` (an execute-like call, an
f-string, a concatenation) inside the same file, at the line of the use; its
``CREATE TABLE`` statements are DECLARATIONS at the line of the constant. A
constant imported with ``from .schema import NAME`` is a placeholder resolved
after indexing (``data_access_store.resolve_data_access``), so editing the
defining file never leaves stale rows. A string variable assigned once in a
function and used later in it is read through (``base + where``).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from livespec_mcp.domain.extractors import (
    ExtractedAccess,
    ExtractedModel,
    ExtractedSymbol,
    ExtractResult,
)
from livespec_mcp.domain.languages import GrammarUnavailableError, get_parser
from livespec_mcp.domain.sql_access import (
    DDL,
    HOLE,
    READ,
    WRITE,
    SqlTable,
    looks_like_sql,
    sql_analyse,
)

DATA_ACCESS_LANGUAGES = frozenset({"python", "typescript", "tsx", "javascript", "java"})

# A file with none of these cannot produce a row; skipping it saves a parse.
_HINT_RE = re.compile(
    r"""\b(?:select|insert|update|delete|merge|upsert|replace|truncate)\b
      | (?:create|alter|drop)\s+table
      | \.objects\b | __tablename__ | db_table | \bTable\( | \.query\b | session
      | prisma | knex | supabase | getRepository | Repository | @Entity | EntityManager
      | sequelize | \.find(?:All|ByPk|OrCreate|AndCountAll)\b | \.bulkCreate\b
      | \bexecute(?:script|many)?\b | SQLAlchemy | WriteOnlyMapped | DynamicMapped | \bdb\.Model\b | _or_404""",
    re.I | re.X,
)


@dataclass
class _Hit:
    line: int
    access: str
    mechanism: str
    table: str | None = None
    schema: str | None = None
    ref_name: str | None = None
    ref_kind: str | None = None


@dataclass
class _Model:
    line: int
    name: str
    role: str
    mechanism: str
    table: str | None = None
    schema: str | None = None
    entity: str | None = None
    table_source: str | None = None
    owner: str | None = None
    secondary: str | None = None
    detail: str | None = None


_analyse = lru_cache(maxsize=512)(sql_analyse)

#: Operations a write-only / dynamic SQLAlchemy collection answers, by mode.
REL_OPS: dict[str, dict[str, str]] = {
    "write_only": {"select": READ, "add": WRITE, "remove": WRITE, "delete": WRITE},
    "dynamic": {
        "filter": READ,
        "filter_by": READ,
        "count": READ,
        "all": READ,
        "first": READ,
        "order_by": READ,
        "add": WRITE,
        "remove": WRITE,
        "delete": WRITE,
    },
}
_REL_OP_NAMES = frozenset(op for ops in REL_OPS.values() for op in ops)
#: add / remove touch the association table of a many-to-many collection.
REL_EDGE_OPS = frozenset({"add", "remove"})


def flask_table_name(class_name: str) -> str:
    """Flask-SQLAlchemy's default table name (``camel_to_snake_case``)."""
    spaced = re.sub(r"((?<=[a-z0-9])[A-Z]|(?!^)[A-Z](?=[a-z]))", r"_\1", class_name)
    return spaced.lower().lstrip("_")


@dataclass
class _Found:
    hits: list[_Hit] = field(default_factory=list)
    models: list[_Model] = field(default_factory=list)
    skips: dict[int, int] = field(default_factory=dict)  # line -> interpolated table positions

    def sql(
        self,
        text: str,
        line: int,
        *,
        jpql: bool = False,
        exact: bool = False,
        declare: bool = False,
    ) -> None:
        """Rows for one string. JPQL names are entities, resolved later.

        `exact`: every row sits on `line` (the use of a constant or a variable,
        whose text lives elsewhere); otherwise a row sits on the line of its
        table name inside a multi-line literal. `declare`: a ``CREATE TABLE``
        in this text is also a declaration of the table."""
        tables, dynamic = _analyse(text, jpql=jpql)
        for t in tables:
            at = line if exact else line + text.count("\n", 0, t.offset)
            if jpql:
                self.hits.append(_Hit(at, t.access, "jpa", ref_name=t.name, ref_kind="model"))
            else:
                self.hits.append(_Hit(at, t.access, "sql", table=t.name, schema=t.schema))
                if declare:
                    self._declared(at, t)
        if dynamic:
            self.skips[line] = max(self.skips.get(line, 0), dynamic)

    def declare_sql(self, text: str, line: int) -> None:
        """Declarations only: the ``CREATE TABLE`` statements of a constant."""
        for t in _analyse(text)[0]:
            self._declared(line + text.count("\n", 0, t.offset), t)

    def _declared(self, line: int, t: SqlTable, mechanism: str = "sql") -> None:
        if t.access == DDL and t.verb == "CREATE":
            self.models.append(
                _Model(line, t.name.lower(), "table", mechanism, t.name, t.schema,
                       table_source="declared")
            )  # fmt: skip

    def constref(self, line: int, module: str, name: str) -> None:
        """A use of a constant imported from `module`, resolved after indexing."""
        key = f"{module}:{name}"
        if not any(m.role == "constref" and m.line == line and m.name == key for m in self.models):
            self.models.append(_Model(line, key, "constref", "sql"))

    def relref(self, line: int, owner: str, attr: str, op: str) -> None:
        """`owner.attr.op(...)` on what may be a write-only / dynamic collection."""
        self.models.append(_Model(line, attr, "relref", "sqlalchemy", owner=owner, detail=op))

    def model(self, line: int, access: str, mechanism: str, name: str) -> None:
        self.hits.append(_Hit(line, access, mechanism, ref_name=name, ref_kind="model"))

    def table(
        self, line: int, access: str, mechanism: str, table: str, schema: str | None = None
    ) -> None:
        self.hits.append(_Hit(line, access, mechanism, table=table, schema=schema))


def collect_data_access(source: str, language: str, module_name: str, out: ExtractResult) -> None:
    """Fill ``out.accesses`` / ``out.models`` for one already-extracted file."""
    if language not in DATA_ACCESS_LANGUAGES or not _HINT_RE.search(source):
        return
    found = _Found()
    try:
        if language == "python":
            _python(source, module_name, found)
        elif language == "java":
            _java(source, found)
        else:
            _js(source, language, found)
    except (SyntaxError, RecursionError, GrammarUnavailableError):
        # The symbol extractor already reported an unparsable file or a missing
        # grammar; a pathologically deep expression only costs this file's rows.
        return
    if not found.hits and not found.models and not found.skips:
        return
    owner = _SpanIndex(out.symbols)
    seen: set[tuple[Any, ...]] = set()
    for h in found.hits:
        qname = owner.at(h.line)
        key = (qname, h.access, h.mechanism, h.line, h.table, h.schema, h.ref_name)
        if qname is None or key in seen:
            continue
        seen.add(key)
        out.accesses.append(
            ExtractedAccess(
                src_qname=qname,
                access=h.access,
                mechanism=h.mechanism,
                line=h.line,
                table=h.table.lower() if h.table else None,
                schema=h.schema.lower() if h.schema else None,
                ref_name=h.ref_name,
                ref_kind=h.ref_kind,
            )
        )
    for m in found.models:
        out.models.append(
            ExtractedModel(
                src_qname=owner.at(m.line) or "",
                name=m.name,
                role=m.role,
                mechanism=m.mechanism,
                line=m.line,
                table=m.table.lower() if m.table else None,
                schema=m.schema.lower() if m.schema else None,
                entity=m.entity,
                table_source=m.table_source if m.table else None,
                owner=m.owner,
                secondary=m.secondary,
                detail=m.detail,
            )
        )
    for line, n in found.skips.items():
        for _ in range(n):
            out.models.append(
                ExtractedModel(
                    src_qname=owner.at(line) or "",
                    name="dynamic_table",
                    role="skip",
                    mechanism="sql",
                    line=line,
                )
            )


class _SpanIndex:
    """Innermost symbol spanning a line (narrowest span; later start wins)."""

    def __init__(self, symbols: list[ExtractedSymbol]) -> None:
        self._spans = sorted(
            ((s.start_line, s.end_line, s.qualified_name) for s in symbols),
            key=lambda x: (x[1] - x[0], -x[0]),
        )
        self._memo: dict[int, str | None] = {}

    def at(self, line: int) -> str | None:
        if line not in self._memo:
            self._memo[line] = next(
                (q for start, end, q in self._spans if start <= line <= end), None
            )
        return self._memo[line]


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------

_SA_MODULES = frozenset({"sqlalchemy", "sqlmodel", "flask_sqlalchemy"})
_SESSION_RE = re.compile(r"(?:^|_)(?:session|sess|db|s)$|session$", re.I)
_DJ_WRITE = frozenset(
    """create update delete bulk_create bulk_update get_or_create update_or_create
    acreate aupdate adelete abulk_create abulk_update aget_or_create aupdate_or_create""".split()
)
_SA_WRITE_FUNCS = frozenset({"insert", "update", "delete"})
_SA_SESSION_WRITE = frozenset({"add", "merge", "delete", "add_all", "bulk_save_objects"})
_SA_GET = frozenset({"get", "get_or_404", "get_one"})


def _cap(name: str) -> bool:
    return bool(name) and name[0].isupper() and not name.isupper()


def _py_last(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _py_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _Py:
    def __init__(self, tree: ast.Module, module_name: str, found: _Found) -> None:
        self.found = found
        self.module_name = module_name
        mods: set[str] = set()
        self.flask_classes: set[str] = set()  # `from flask_sqlalchemy import SQLAlchemy [as X]`
        self.flask_modules: set[str] = set()  # `import flask_sqlalchemy [as X]`
        self.imported: dict[str, tuple[str, str]] = {}  # local name -> (module, name)
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
                for a in n.names:
                    if a.name == "flask_sqlalchemy":
                        self.flask_modules.add(a.asname or a.name)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
                if n.module.split(".")[0] == "flask_sqlalchemy":
                    self.flask_classes.update(
                        a.asname or a.name for a in n.names if a.name == "SQLAlchemy"
                    )
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                target = self._import_target(n)
                if target is not None:
                    for a in n.names:
                        if a.name != "*":
                            self.imported[a.asname or a.name] = (target, a.name)
        self.sa = bool(mods & _SA_MODULES)
        # Django-style file: imports django or a `models` module, or uses a manager.
        self.django = (
            "django" in mods
            or any(
                isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[-1] == "models"
                for n in ast.walk(tree)
            )
            or any(
                isinstance(n, ast.Attribute)
                and n.attr == "objects"
                and isinstance(n.value, ast.Name)
                and _cap(n.value.id)
                for n in ast.walk(tree)
            )
        )
        self.strs: dict[str, str] = {}  # module-level string constants (any text)
        self.cls_strs: dict[str, dict[str, str]] = {}  # class -> its string constants
        self.tables: set[str] = set()
        self.dj_models: set[str] = set()
        self.sa_models: set[str] = set()  # same-file classes that map to a table
        self.env: dict[str, str] = {}  # string variables of the current function
        self.shadow: set[str] = set()  # every name bound in the current function
        self.skip_assign: set[str] = set()  # variables counted where they are used
        self.cur_cls: str | None = None
        self.in_func = False
        self._subst = False
        self._defining = True  # collecting constants: uses of imports are not recorded
        self._models(tree)
        self._consts(tree)
        self._defining = False
        self.ctx: dict[str, tuple[str, str]] = {}
        self.block(tree.body, top=True)

    def _import_target(self, n: ast.ImportFrom) -> str | None:
        """Absolute dotted module of a `from X import ...` (relative levels
        resolved against this file's own module name)."""
        if n.level == 0:
            return n.module
        parts = self.module_name.split(".")[:-1]  # the package holding this file
        if n.level - 1 > len(parts):
            return None
        pkg = parts[: len(parts) - (n.level - 1)]
        return ".".join([*pkg, n.module] if n.module else pkg) or None

    # -- declarations -----------------------------------------------------

    def _consts(self, tree: ast.Module) -> None:
        """String constants of the module body and of every class body, in
        source order (a constant may be built from earlier ones). A name bound
        twice at one level is ambiguous and dropped. SQL-like module constants
        also produce, at their definition, declarations (`CREATE TABLE`) and
        the `sqlconst` rows other modules resolve imports against."""
        for scope, body, strs in self._const_scopes(tree):
            bound: dict[str, int] = {}
            for st in body:
                for name in _bound_names(st):
                    bound[name] = bound.get(name, 0) + 1
            for st in body:
                target = value = None
                if (
                    isinstance(st, ast.Assign)
                    and len(st.targets) == 1
                    and isinstance(st.targets[0], ast.Name)
                ):
                    target, value = st.targets[0].id, st.value
                elif (
                    isinstance(st, ast.AnnAssign)
                    and isinstance(st.target, ast.Name)
                    and st.value is not None
                ):
                    target, value = st.target.id, st.value
                if target is None or value is None or bound.get(target) != 1:
                    continue
                self.cur_cls = scope
                text = self._text(value)
                self.cur_cls = None
                if text is None:
                    continue
                strs[target] = text
                if looks_like_sql(text):
                    self.found.declare_sql(text, getattr(value, "lineno", st.lineno))
                    if scope is None:
                        self._sqlconst(target, text, getattr(value, "lineno", st.lineno))

    def _const_scopes(self, tree: ast.Module) -> Iterator[tuple[str | None, list[ast.stmt], dict]]:
        yield None, tree.body, self.strs
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                yield n.name, n.body, self.cls_strs.setdefault(n.name, {})

    def _sqlconst(self, name: str, text: str, line: int) -> None:
        """One row per (access, table) of an importable SQL constant."""
        tables, _ = _analyse(text)
        for t in tables:
            self.found.models.append(
                _Model(
                    line + text.count("\n", 0, t.offset),
                    f"{self.module_name}:{name}",
                    "sqlconst",
                    "sql",
                    t.name,
                    t.schema,
                    detail=t.access,
                )
            )

    def _app_label(self) -> str | None:
        parts = self.module_name.split(".")
        if len(parts) >= 2 and parts[-1] == "models":
            return parts[-2]
        if len(parts) >= 3 and parts[-2] == "models":
            return parts[-3]
        return None

    def _is_flask_db(self, value: ast.AST) -> bool:
        if not isinstance(value, ast.Call):
            return False
        f = value.func
        return (isinstance(f, ast.Name) and f.id in self.flask_classes) or (
            isinstance(f, ast.Attribute)
            and f.attr == "SQLAlchemy"
            and isinstance(f.value, ast.Name)
            and f.value.id in self.flask_modules
        )

    def _models(self, tree: ast.Module) -> None:
        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        classes.sort(key=lambda c: c.lineno)
        by_name = {c.name: c for c in classes}
        dj: dict[str, ast.ClassDef] = {}
        changed = True
        while changed:  # same-file inheritance from an (abstract) model base
            changed = False
            for c in classes:
                if c.name in dj:
                    continue
                for b in c.bases:
                    is_model = (
                        isinstance(b, ast.Attribute)
                        and b.attr == "Model"
                        and _py_last(b.value) == "models"
                    ) or (
                        isinstance(b, ast.Name)
                        and (b.id in dj or (self.django and b.id == "Model"))
                    )
                    if is_model:
                        dj[c.name] = c
                        changed = True
                        break
        flask_rows: dict[str, _Model] = {}
        for c in classes:
            tablename = schema = db_table = app_label = None
            abstract = custom_name = False
            for st in c.body:
                if isinstance(st, ast.Assign) and len(st.targets) == 1:
                    tgt = st.targets[0]
                    if isinstance(tgt, ast.Name) and tgt.id == "__tablename__":
                        tablename = _py_str(st.value)
                        custom_name = custom_name or tablename is None
                    elif isinstance(tgt, ast.Name) and tgt.id == "__table_args__":
                        schema = _table_args_schema(st.value)
                    elif isinstance(tgt, ast.Name) and tgt.id in ("__abstract__", "__table__"):
                        abstract = abstract or tgt.id == "__table__" or _truthy(st.value)
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    if st.target.id == "__tablename__":
                        tablename = _py_str(st.value)
                        custom_name = custom_name or tablename is None
                elif isinstance(st, ast.FunctionDef) and st.name == "__tablename__":
                    custom_name = True  # @declared_attr: the name is computed
                elif isinstance(st, ast.ClassDef) and st.name == "Meta":
                    for m in st.body:
                        if isinstance(m, ast.Assign) and len(m.targets) == 1:
                            key = _py_last(m.targets[0])
                            if key == "db_table":
                                db_table = _py_str(m.value)
                            elif key == "app_label":
                                app_label = _py_str(m.value)
                            elif key == "abstract":
                                abstract = isinstance(m.value, ast.Constant) and bool(m.value.value)
            self._relationships(c)
            if tablename:
                self.sa_models.add(c.name)
                self.found.models.append(
                    _Model(c.lineno, c.name, "model", "sqlalchemy", tablename, schema,
                           table_source="declared")
                )  # fmt: skip
                continue
            if c.name not in dj and db_table is None:
                row = (
                    None if (abstract or custom_name) else self._flask_model(c, by_name, flask_rows)
                )
                if row is not None:
                    flask_rows[c.name] = row
                    self.sa_models.add(c.name)
                    self.found.models.append(row)
                continue
            self.dj_models.add(c.name)
            if abstract:
                continue
            if db_table:
                schema_part, _, table = db_table.rpartition(".")
                self.found.models.append(
                    _Model(c.lineno, c.name, "model", "django", table.strip('"'),
                           schema_part.strip('"') or None, table_source="declared")
                )  # fmt: skip
                continue
            label = app_label or self._app_label()
            self.found.models.append(
                _Model(
                    c.lineno,
                    c.name,
                    "model",
                    "django",
                    f"{label}_{c.name.lower()}" if label else None,
                    table_source="convention",
                )
            )
        for n in ast.walk(tree):  # users = Table("users", metadata, ...)
            if (
                isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Call)
                and _py_last(n.value.func) == "Table"
                and len(n.value.args) >= 2
                and _py_str(n.value.args[0])
            ):
                schema = next(
                    (_py_str(k.value) for k in n.value.keywords if k.arg == "schema"), None
                )
                name = n.targets[0].id
                self.tables.add(name)
                self.found.models.append(
                    _Model(n.lineno, name, "model", "sqlalchemy", _py_str(n.value.args[0]),
                           schema, table_source="declared")
                )  # fmt: skip
            elif (  # db = SQLAlchemy(...) from flask_sqlalchemy
                isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and self._is_flask_db(n.value)
            ):
                self.found.models.append(
                    _Model(n.lineno, n.targets[0].id, "flask_db", "flask_sqlalchemy")
                )

    def _flask_model(
        self, c: ast.ClassDef, by_name: dict[str, ast.ClassDef], rows: dict[str, _Model]
    ) -> _Model | None:
        """A Flask-SQLAlchemy model without `__tablename__`.

        The default name is the CamelCase -> snake_case of the class name, but
        only when the base is a Flask-SQLAlchemy ``db.Model``: plain declarative
        SQLAlchemy has no default. The base `db` is verified after indexing
        (``flask_candidate``). A subclass of a same-file model without a primary
        key of its own is single-table inheritance and shares its parent's
        table; one with its own key is joined inheritance and gets its own
        name; a subclass whose parent is not a known model stays unresolved."""
        for b in c.bases:
            if (
                isinstance(b, ast.Attribute)
                and b.attr == "Model"
                and isinstance(b.value, ast.Name)
                and b.value.id != "models"
            ):
                if self._mixin_names_table(c, by_name):
                    return None
                return _Model(c.lineno, c.name, "model", "sqlalchemy", flask_table_name(c.name),
                              entity=b.value.id, table_source="flask_candidate")  # fmt: skip
        parents = [rows[b.id] for b in c.bases if isinstance(b, ast.Name) and b.id in rows]
        if len(parents) != 1 or self._mixin_names_table(c, by_name):
            return None
        parent = parents[0]
        if _has_primary_key(c):
            return _Model(c.lineno, c.name, "model", "sqlalchemy", flask_table_name(c.name),
                          entity=parent.entity, table_source="flask_candidate")  # fmt: skip
        return _Model(c.lineno, c.name, "model", "sqlalchemy", parent.table, parent.schema,
                      entity=parent.entity, table_source=parent.table_source)  # fmt: skip

    @staticmethod
    def _mixin_names_table(c: ast.ClassDef, by_name: dict[str, ast.ClassDef]) -> bool:
        """Does a same-file base class pick the table name itself?"""
        for b in c.bases:
            base = by_name.get(b.id) if isinstance(b, ast.Name) else None
            if base is None:
                continue
            for st in base.body:
                if (isinstance(st, ast.FunctionDef) and st.name == "__tablename__") or (
                    isinstance(st, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in st.targets)
                ):
                    return True
        return False

    def _relationships(self, c: ast.ClassDef) -> None:
        """`posts: so.WriteOnlyMapped['Post'] = so.relationship(...)` and
        `tasks = relationship('Task', lazy='dynamic')`: collections whose target
        model the class states. Anything else is not recorded."""
        for st in c.body:
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                attr, ann, value = st.target.id, st.annotation, st.value
            elif (
                isinstance(st, ast.Assign)
                and len(st.targets) == 1
                and isinstance(st.targets[0], ast.Name)
            ):
                attr, ann, value = st.targets[0].id, None, st.value
            else:
                continue
            mode = target = None
            if isinstance(ann, ast.Subscript):
                kind = _py_last(ann.value)
                if kind in ("WriteOnlyMapped", "DynamicMapped"):
                    mode = "write_only" if kind == "WriteOnlyMapped" else "dynamic"
                    target = _rel_target(ann.slice)
            secondary = None
            if isinstance(value, ast.Call) and _py_last(value.func) == "relationship":
                kws = {k.arg: k.value for k in value.keywords if k.arg}
                if mode is None and _py_str(kws.get("lazy")) == "dynamic":
                    mode = "dynamic"
                if target is None and value.args:
                    target = _rel_target(value.args[0])
                if target is None and "argument" in kws:
                    target = _rel_target(kws["argument"])
                sec = kws.get("secondary")
                if isinstance(sec, ast.Name):
                    secondary = sec.id
                elif _py_str(sec):
                    secondary = "=" + str(_py_str(sec))
            if mode and target:
                self.found.models.append(
                    _Model(st.lineno, attr, "relationship", "sqlalchemy", entity=target,
                           owner=c.name, secondary=secondary, detail=mode)
                )  # fmt: skip

    # -- string values ------------------------------------------------------

    def _lookup(self, name: str, line: int) -> str | None:
        """Text of the string a bare name holds in the current scope.

        A local variable resolved once wins; a name bound anywhere else in the
        function (a parameter, a rebinding) is unknown; then the class body's
        constants (only while in the class body, as Python scopes them), the
        module's, and last a constant imported from another module, which is
        recorded for the resolver and stands as an opaque hole here."""
        if name in self.env:
            self._subst = True
            return self.env[name]
        if name in self.shadow:
            return None
        if not self.in_func and self.cur_cls and name in self.cls_strs.get(self.cur_cls, {}):
            self._subst = True
            return self.cls_strs[self.cur_cls][name]
        if name in self.strs:
            self._subst = True
            return self.strs[name]
        if name in self.imported:
            module, orig = self.imported[name]
            if not self._defining:
                self.found.constref(line, module, orig)
            return HOLE
        return None

    def _attr_text(self, node: ast.Attribute) -> str | None:
        """`self.SQL` / `cls.SQL` / `ClassName.SQL`: a constant of a same-file class."""
        base = node.value
        if not isinstance(base, ast.Name):
            return None
        cls = self.cur_cls if base.id in ("self", "cls") else base.id
        text = self.cls_strs.get(cls or "", {}).get(node.attr)
        if text is not None:
            self._subst = True
        return text

    def _text(self, node: ast.AST) -> str | None:
        """SQL-ish text of a string expression with holes, or None. Names that
        hold a string in scope are substituted (and set ``_subst``)."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return self._lookup(node.id, node.lineno) if isinstance(node.ctx, ast.Load) else None
        if isinstance(node, ast.Attribute):
            return self._attr_text(node)
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                elif isinstance(v, ast.FormattedValue):
                    inner = self._text(v.value)
                    parts.append(HOLE if inner is None else inner)
                else:
                    parts.append(HOLE)
            return "".join(parts)
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Add):
                left, right = self._text(node.left), self._text(node.right)
                if left is None and right is None:
                    return None
                return (HOLE if left is None else left) + (HOLE if right is None else right)
            if isinstance(node.op, ast.Mod):
                return self._text(node.left)
            return None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
        ):
            return self._text(node.func.value)
        return None

    def _sql_text(self, node: ast.AST) -> tuple[str, bool] | None:
        """(text, substituted) of a string expression, or None."""
        self._subst = False
        text = self._text(node)
        return None if text is None else (text, self._subst)

    # -- walking ----------------------------------------------------------

    def block(self, body: list[ast.stmt], *, top: bool = False) -> None:
        for st in body:
            if (
                isinstance(st, ast.Expr)
                and isinstance(st.value, ast.Constant)
                and isinstance(st.value.value, str)
            ):
                continue  # docstring / bare string
            if top and _skipped_const(st, self):
                continue  # a SQL constant counts where it is used
            self.visit(st)

    def _scope(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Local string variables of `fn` (call after saving the outer scope).

        A name read through is bound exactly once by a plain assignment (and
        only appended to with `+=`) and is not a parameter. Anything else (two
        assignments, a loop target, an unpack, a parameter) is unknown, so a
        variable that may hold another table's SQL yields nothing."""
        binds: dict[str, list[ast.AST]] = {}
        stores: dict[str, int] = {}
        args = fn.args
        params = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
        params.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
        uses: dict[str, int] = {}
        for n in ast.walk(fn):  # uses count nested functions too
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                uses[n.id] = uses.get(n.id, 0) + 1
        for n in _scope_walk(fn):
            if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
                stores[n.id] = stores.get(n.id, 0) + 1
            elif isinstance(n, ast.Import | ast.ImportFrom):
                for al in n.names:
                    nm = (al.asname or al.name).split(".")[0]
                    stores[nm] = stores.get(nm, 0) + 1
                    binds.setdefault(nm, [])
            elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                stores[n.name] = stores.get(n.name, 0) + 1
                binds.setdefault(n.name, [])
            elif isinstance(n, ast.ExceptHandler) and n.name:
                stores[n.name] = stores.get(n.name, 0) + 1
                binds.setdefault(n.name, [])
            if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
                binds.setdefault(n.id, [])
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        binds.setdefault(t.id, []).append(n if len(n.targets) == 1 else t)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                binds.setdefault(n.target.id, []).append(n if n.value is not None else n.target)
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                binds.setdefault(n.target.id, []).append(n)
            elif isinstance(n, ast.Global | ast.Nonlocal):
                for name in n.names:
                    stores[name] = stores.get(name, 0) + 2
                    binds.setdefault(name, [])
        local = set(binds) | params
        env = {k: v for k, v in self.env.items() if k not in local}
        shadow = (self.shadow | local) - set(env)
        self.env, self.shadow, self.skip_assign = env, shadow, set()
        self.in_func = True
        order = sorted(
            (
                (min(getattr(x, "lineno", 0) for x in stmts), name)
                for name, stmts in binds.items()
                if name not in params
                and stmts
                and hasattr(stmts[0], "lineno")
                and stores.get(name, 0) == len(stmts)
            ),
        )
        for _, name in order:
            stmts = sorted(binds[name], key=lambda x: getattr(x, "lineno", 0))
            first = stmts[0]
            if not isinstance(first, ast.Assign | ast.AnnAssign) or first.value is None:
                continue
            rest = stmts[1:]
            if not all(isinstance(s, ast.AugAssign) and isinstance(s.op, ast.Add) for s in rest):
                continue
            text = self._text(first.value)
            if text is None:
                continue
            for aug in rest:
                extra = self._text(aug.value)  # type: ignore[attr-defined]
                text += HOLE if extra is None else extra
            self.env[name] = text
            self.shadow.discard(name)
            if uses.get(name) and looks_like_sql(text):
                self.skip_assign.add(name)

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            saved = (self.ctx, self.env, self.shadow, self.skip_assign, self.in_func)
            self.ctx = self._bindings(node)
            self._scope(node)
            for d in node.decorator_list:
                self.visit(d)
            self.block(node.body)
            self.ctx, self.env, self.shadow, self.skip_assign, self.in_func = saved
            return
        if isinstance(node, ast.ClassDef):
            saved_cls, saved_func = self.cur_cls, self.in_func
            self.cur_cls, self.in_func = node.name, False
            self.block(node.body, top=True)
            self.cur_cls, self.in_func = saved_cls, saved_func
            return
        if isinstance(node, ast.Module):
            self.block(node.body, top=True)
            return
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id in self.skip_assign:
                return  # read through where the variable is used
        if isinstance(node, ast.Constant | ast.JoinedStr | ast.BinOp):
            got = self._sql_text(node)
            if got is not None:
                text, subst = got
                self.found.sql(text, getattr(node, "lineno", 1), exact=subst, declare=not subst)
                return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                got = self._sql_text(node)
                if got is not None and looks_like_sql(got[0]):
                    self.found.sql(got[0], node.lineno, exact=True)
            return
        if isinstance(node, ast.Call | ast.Attribute):
            self.chain(node)
            return
        for field_name, value in ast.iter_fields(node):
            if field_name in ("body", "orelse", "finalbody") and isinstance(value, list):
                self.block(value)
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, ast.AST):
                        self.visit(v)
            elif isinstance(value, ast.AST):
                self.visit(value)

    def _bindings(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, tuple[str, str]]:
        """Local name -> (origin, model) for `v = Model(...)`, `v = Model.objects...`,
        `v = session.get(Model, ...)` and parameters annotated with a class."""
        out: dict[str, tuple[str, str]] = {}
        for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
            if isinstance(a.annotation, ast.Name) and _cap(a.annotation.id):
                out[a.arg] = ("ann", a.annotation.id)
        for n in ast.walk(fn):
            if not (
                isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
            ):
                continue
            value = n.value.value if isinstance(n.value, ast.Await) else n.value
            bound = self._bound_model(value)
            if bound is not None:
                out.setdefault(n.targets[0].id, bound)
        return out

    def _bound_model(self, value: ast.AST) -> tuple[str, str] | None:
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and _cap(value.func.id):
            return ("ctor", value.func.id)
        root, names, calls = _py_spine(value)
        if names and names[0] == "objects":
            model = _py_last(root)
            if model and _cap(model):
                return ("django", model)
        for name, call in calls:
            if name in ("query", "get") and call.args:
                model = self._model_arg(call.args[0])
                if model:
                    return ("sa", model)
        return None

    def _model_arg(self, arg: ast.AST) -> str | None:
        """`User`, `User.id`, `User.__table__` or a same-file `Table` variable."""
        if isinstance(arg, ast.Name):
            return arg.id if _cap(arg.id) or arg.id in self.tables else None
        root = arg
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and isinstance(arg, ast.Attribute):
            return root.id if _cap(root.id) or root.id in self.tables else None
        return None

    def _instance_model(self, arg: ast.AST) -> tuple[str, str] | None:
        if isinstance(arg, ast.Name):
            if arg.id == "self" and self.cur_cls in self.sa_models:
                return ("self", self.cur_cls or "")
            return self.ctx.get(arg.id)
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and _cap(arg.func.id):
            return ("ctor", arg.func.id)
        return None

    def chain(self, node: ast.Call | ast.Attribute) -> None:
        root, names, calls = _py_spine(node)
        line = node.lineno
        f = self.found
        root_name = _py_last(root)
        # `self.SQL` / `cls.SQL` / `ClassName.SQL`: a class-level SQL constant.
        if (
            isinstance(root, ast.Name)
            and names
            and (root.id in ("self", "cls") or root.id not in self.shadow)
        ):
            cls = self.cur_cls if root.id in ("self", "cls") else root.id
            text = self.cls_strs.get(cls or "", {}).get(names[0])
            if text is not None and looks_like_sql(text):
                f.sql(text, line, exact=True)
        # Django: Model.objects.<...>
        if names and names[0] == "objects" and root_name and _cap(root_name):
            write = any(n in _DJ_WRITE for n in names[1:])
            f.model(line, WRITE if write else READ, "django", root_name)
        # Flask-SQLAlchemy: Model.query.<...>
        elif (
            len(names) > 1
            and names[0] == "query"
            and self.sa
            and isinstance(root, ast.Name)
            and _cap(root.id)
        ):
            write = any(n in ("delete", "update") for n in names[1:])
            f.model(line, WRITE if write else READ, "sqlalchemy", root.id)
        # Django: instance.save() / instance.delete()
        elif (
            isinstance(root, ast.Name)
            and len(names) == 1
            and names[0] in ("save", "delete", "asave", "adelete")
            and calls
            and not calls[-1][1].args
            and root.id in self.ctx
        ):
            origin, model = self.ctx[root.id]
            if origin == "django" or (
                origin in ("ctor", "ann") and (self.django or model in self.dj_models)
            ):
                f.model(line, WRITE, "django", model)
        for i, (name, call) in enumerate(calls):
            recv = names[i - 1] if i > 0 else root_name
            is_attr = isinstance(call.func, ast.Attribute)
            session = bool(recv and _SESSION_RE.search(recv))
            if name == "query" and is_attr and (self.sa or session):
                for a in call.args:
                    model = self._model_arg(a)
                    if model:
                        f.model(call.lineno, READ, "sqlalchemy", model)
            elif (
                name in ("select", "insert", "update", "delete")
                and self.sa
                and recv in ("sa", "sqlalchemy")
            ):
                args = call.args if name == "select" else call.args[:1]
                access = WRITE if name in _SA_WRITE_FUNCS else READ
                for a in args:
                    model = self._model_arg(a)
                    if model:
                        f.model(call.lineno, access, "sqlalchemy", model)
            elif name == "select_from" and is_attr and self.sa and call.args:
                model = self._model_arg(call.args[0])
                if model:
                    f.model(call.lineno, READ, "sqlalchemy", model)
            elif name in _SA_SESSION_WRITE and is_attr and session and call.args:
                arg = call.args[0]
                items = arg.elts if isinstance(arg, ast.List | ast.Tuple) else [arg]
                for item in items:
                    bound = self._instance_model(item)
                    if bound is not None:
                        f.model(call.lineno, WRITE, "sqlalchemy", bound[1])
            elif (
                name in _SA_GET
                and is_attr
                and session
                and len(call.args) >= (2 if name == "get" else 1)
            ):
                first = call.args[0]
                if isinstance(first, ast.Name) and _cap(first.id):
                    f.model(call.lineno, READ, "sqlalchemy", first.id)
            elif name in _REL_OP_NAMES and is_attr and (self.sa or name == "select"):
                holder = call.func.value  # type: ignore[attr-defined]
                if isinstance(holder, ast.Attribute):
                    base = holder.value
                    owner = (
                        self.cur_cls
                        if isinstance(base, ast.Name) and base.id == "self" and self.cur_cls
                        else "*"
                    )
                    f.relref(call.lineno, owner or "*", holder.attr, name)
            for a in call.args:
                self.visit(a.value if isinstance(a, ast.Starred) else a)
            for k in call.keywords:
                self.visit(k.value)
        if not isinstance(root, ast.Name):
            if isinstance(root, ast.Call):  # `select(User).where(...)`, `f(x)`
                self._plain_call(root)
            else:
                self.visit(root)

    def _plain_call(self, call: ast.Call) -> None:
        """A call whose callee is a bare name (the bottom of a chain)."""
        name = _py_last(call.func)
        if name in ("select", "insert", "update", "delete") and self.sa:
            args = call.args if name == "select" else call.args[:1]
            for a in args:
                model = self._model_arg(a)
                if model:
                    self.found.model(
                        call.lineno, WRITE if name in _SA_WRITE_FUNCS else READ, "sqlalchemy", model
                    )
        if not isinstance(call.func, ast.Name):
            self.visit(call.func)
        for a in call.args:
            self.visit(a.value if isinstance(a, ast.Starred) else a)
        for k in call.keywords:
            self.visit(k.value)


def _bound_names(st: ast.stmt) -> list[str]:
    """Names a top-level statement binds (assignments, defs, imports, loops)."""
    out: list[str] = []
    if isinstance(st, ast.Assign):
        for t in st.targets:
            out.extend(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
    elif isinstance(st, ast.AnnAssign | ast.AugAssign):
        if isinstance(st.target, ast.Name):
            out.append(st.target.id)
    elif isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        out.append(st.name)
    elif isinstance(st, ast.Import | ast.ImportFrom):
        out.extend((a.asname or a.name).split(".")[0] for a in st.names)
    elif isinstance(st, ast.For | ast.AsyncFor):
        out.extend(n.id for n in ast.walk(st.target) if isinstance(n, ast.Name))
    return out


def _skipped_const(st: ast.stmt, py: _Py) -> bool:
    """`NAME = "<sql>"` at module / class level: its uses count, not the definition."""
    if not (isinstance(st, ast.Assign) and len(st.targets) == 1):
        return False
    t = st.targets[0]
    if not isinstance(t, ast.Name):
        return False
    scope = py.strs if py.cur_cls is None else py.cls_strs.get(py.cur_cls, {})
    text = scope.get(t.id)
    return text is not None and looks_like_sql(text)


def _scope_walk(fn: ast.AST) -> Iterator[ast.AST]:
    """Nodes of a function body, not descending into nested defs / classes /
    lambdas (their bindings are their own)."""
    stack: list[ast.AST] = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _truthy(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and bool(node.value)


def _has_primary_key(c: ast.ClassDef) -> bool:
    return any(
        isinstance(n, ast.keyword) and n.arg == "primary_key" and _truthy(n.value)
        for n in ast.walk(c)
    )


def _rel_target(node: ast.AST) -> str | None:
    """Model named by a relationship argument or annotation: `'Post'`, `Post`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        name = node.value.strip()
        return name if name.isidentifier() else None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _py_spine(
    node: ast.AST,
) -> tuple[ast.AST, list[str], list[tuple[str, ast.Call]]]:
    """`a.b(x).c(y)` -> (root `a`, ['b', 'c'], [('b', call), ('c', call)]).

    Descends only through attribute access and method calls; a call on a bare
    name (`f(x)`) is a root."""
    names: list[str] = []
    calls: list[tuple[str, ast.Call]] = []
    cur = node
    while True:
        if isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
            calls.append((cur.func.attr, cur))
            names.append(cur.func.attr)
            cur = cur.func.value
        elif isinstance(cur, ast.Attribute):
            names.append(cur.attr)
            cur = cur.value
        else:
            break
    names.reverse()
    calls.reverse()
    # `calls` index into `names` by position of the method name.
    out_calls: list[tuple[str, ast.Call]] = []
    j = 0
    aligned: list[tuple[str, ast.Call] | None] = [None] * len(names)
    for i, n in enumerate(names):
        if j < len(calls) and calls[j][0] == n and calls[j][1].func.attr == n:  # type: ignore[union-attr]
            # A method name can repeat; match calls in order.
            if _is_call_of(calls[j][1], n):
                aligned[i] = calls[j]
                j += 1
    out_calls = [c for c in aligned if c is not None]
    return cur, names, out_calls if len(out_calls) == len(calls) else calls


def _is_call_of(call: ast.Call, name: str) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == name


def _table_args_schema(value: ast.AST) -> str | None:
    dicts = [value] if isinstance(value, ast.Dict) else []
    if isinstance(value, ast.Tuple):
        dicts = [e for e in value.elts if isinstance(e, ast.Dict)]
    for d in dicts:
        if not isinstance(d, ast.Dict):
            continue
        for k, v in zip(d.keys, d.values, strict=True):
            if k is not None and _py_str(k) == "schema":
                return _py_str(v)
    return None


def _python(source: str, module_name: str, found: _Found) -> None:
    _Py(ast.parse(source), module_name, found)


# --------------------------------------------------------------------------
# tree-sitter helpers (TS / JS / Java)
# --------------------------------------------------------------------------


def _txt(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _walk(node) -> Iterator[Any]:
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _same(a, b) -> bool:
    return (
        a is not None
        and b is not None
        and a.start_byte == b.start_byte
        and a.end_byte == b.end_byte
        and a.type == b.type
    )


def _line(node) -> int:
    return int(node.start_point[0]) + 1


_TS_STRING_PARTS = ("string_fragment", "multiline_string_fragment")


def _ts_text(node, src: bytes, plus: str = "binary_expression") -> str | None:
    """Text of a string / template / text block / `+` concatenation, with holes."""
    t = node.type
    if t in ("string", "string_literal", "template_string"):
        parts: list[str] = []
        for c in node.children:
            if c.type in _TS_STRING_PARTS:
                parts.append(_txt(c, src))
            elif c.type == "template_substitution":
                parts.append(HOLE)
            elif c.type == "escape_sequence":
                raw = _txt(c, src)
                parts.append(
                    "\n" if raw == "\\n" else raw[1:] if raw in ('\\"', "\\'", "\\`") else " "
                )
        return "".join(parts)
    if t == plus:
        op = node.child_by_field_name("operator")
        if op is None or _txt(op, src) != "+":
            return None
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        lt = _ts_text(left, src, plus) if left is not None else None
        rt = _ts_text(right, src, plus) if right is not None else None
        if lt is None and rt is None:
            return None
        return (HOLE if lt is None else lt) + (HOLE if rt is None else rt)
    if t == "parenthesized_expression" and node.named_child_count == 1:
        return _ts_text(node.named_children[0], src, plus)
    return None


def _is_stringish(node) -> bool:
    return node.type in ("string", "string_literal", "template_string", "binary_expression")


def _chain_up(call, src: bytes, call_type: str) -> list[str]:
    """Method names called on the result of `call`, innermost first (JS/TS)."""
    names: list[str] = []
    cur = call
    while True:
        p = cur.parent
        if p is None:
            break
        if p.type in ("await_expression", "parenthesized_expression", "non_null_expression"):
            cur = p
            continue
        if p.type == "member_expression" and _same(p.child_by_field_name("object"), cur):
            prop = p.child_by_field_name("property")
            if prop is not None:
                names.append(_txt(prop, src))
            pp = p.parent
            if (
                pp is not None
                and pp.type == call_type
                and _same(pp.child_by_field_name("function"), p)
            ):
                cur = pp
                continue
        break
    return names


# --------------------------------------------------------------------------
# TypeScript / JavaScript
# --------------------------------------------------------------------------

_PRISMA_READ = frozenset(
    """findMany findUnique findUniqueOrThrow findFirst findFirstOrThrow count aggregate
    groupBy""".split()
)
_PRISMA_WRITE = frozenset(
    "create createMany createManyAndReturn update updateMany upsert delete deleteMany".split()
)
# Enough on their own to accept a receiver that is only *probably* a client.
_PRISMA_DISTINCT = frozenset(
    """findMany findUnique findUniqueOrThrow findFirst findFirstOrThrow createMany
    updateMany deleteMany upsert""".split()
)
_PRISMA_LOOSE_RECEIVERS = frozenset({"db", "tx", "trx", "client"})
_KNEX_WRITE = frozenset(
    "insert update del delete truncate increment decrement upsert onConflict merge".split()
)
_KNEX_JOINS = frozenset(
    "join innerJoin leftJoin leftOuterJoin rightJoin rightOuterJoin fullOuterJoin crossJoin".split()
)
_KNEX_DDL = frozenset(
    """createTable createTableIfNotExists createTableLike dropTable dropTableIfExists
    alterTable renameTable hasTable""".split()
)
_KNEX_LOOSE_NAMES = frozenset({"db", "trx", "tx", "qb"})
_SUPABASE_WRITE = frozenset({"insert", "update", "upsert", "delete"})
_TYPEORM_READ = frozenset(
    """find findBy findOne findOneBy findOneOrFail findOneByOrFail findAndCount
    findAndCountBy count countBy exist exists existsBy createQueryBuilder query sum average
    minimum maximum""".split()
)
_TYPEORM_WRITE = frozenset(
    """save insert update delete remove softDelete softRemove restore recover upsert
    increment decrement clear""".split()
)
_TYPEORM_REPO_TYPES = frozenset({"Repository", "MongoRepository", "TreeRepository"})
_SEQ_STRONG = frozenset({"findAll", "findByPk", "findOrCreate", "findAndCountAll", "bulkCreate"})
_SEQ_READ = frozenset({"findAll", "findByPk", "findAndCountAll", "findOne", "count"})
_SEQ_WRITE = frozenset(
    {"findOrCreate", "bulkCreate", "create", "update", "destroy", "upsert", "truncate"}
)
_KNEX_IMPORT_RE = re.compile(r"""['"]knex['"]""")
_SEQ_IMPORT_RE = re.compile(r"""['"]sequelize(?:-typescript)?['"]""")


def _js_last_name(node, src: bytes) -> str | None:
    """`a` -> a, `this.a.b` -> b."""
    if node is None:
        return None
    if node.type in ("identifier", "property_identifier", "this"):
        return _txt(node, src)
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        return _txt(prop, src) if prop is not None else None
    if node.type in ("await_expression", "parenthesized_expression", "non_null_expression"):
        return _js_last_name(node.named_children[0], src) if node.named_children else None
    return None


def _js_root(node, src: bytes):
    """Bottom of a call/member chain: the identifier (or call on one) it starts from."""
    cur = node
    while cur is not None:
        if cur.type == "member_expression":
            cur = cur.child_by_field_name("object")
        elif cur.type == "call_expression":
            fn = cur.child_by_field_name("function")
            if fn is None or fn.type == "identifier":
                return cur
            cur = fn
        elif cur.type in ("await_expression", "parenthesized_expression", "non_null_expression"):
            cur = cur.named_children[0] if cur.named_children else None
        else:
            return cur
    return None


def _js_args(call) -> list[Any]:
    args = call.child_by_field_name("arguments")
    return list(args.named_children) if args is not None and args.type == "arguments" else []


def _js_literal(node, src: bytes) -> str | None:
    """A plain string literal (no interpolation)."""
    if node is None or node.type not in ("string", "template_string"):
        return None
    if any(c.type == "template_substitution" for c in node.children):
        return None
    return _ts_text(node, src)


def _js_object_string(obj, src: bytes, key: str) -> str | None:
    if obj is None or obj.type != "object":
        return None
    for pair in obj.named_children:
        if pair.type != "pair":
            continue
        k = pair.child_by_field_name("key")
        if k is not None and _txt(k, src).strip("'\"") == key:
            return _js_literal(pair.child_by_field_name("value"), src)
    return None


def _split_table(name: str) -> tuple[str | None, str]:
    schema, _, table = name.rpartition(".")
    return (schema or None), table


class _Js:
    def __init__(self, source: str, language: str, found: _Found) -> None:
        self.src = source.encode("utf-8")
        self.found = found
        self.knex_import = bool(_KNEX_IMPORT_RE.search(source))
        self.seq_import = bool(_SEQ_IMPORT_RE.search(source))
        self.repos: dict[str, str] = {}  # variable / field name -> entity
        self.seq_models: set[str] = set()
        self.consts: dict[str, tuple[str, int, int]] = {}  # name -> (text, start, end of decl)
        root = get_parser(language).parse(self.src).root_node
        nodes = list(_walk(root))
        for n in nodes:
            self.declare(n, root)
        skip_until = -1
        for n in nodes:
            if n.start_byte < skip_until and _is_stringish(n):
                continue
            if _is_stringish(n):
                text = _ts_text(n, self.src)
                if text is not None:
                    skip_until = n.end_byte
                    if not self._is_const_decl(n):
                        found.sql(text, _line(n), declare=True)
                    continue
            if n.type == "call_expression":
                self.call(n)
            elif n.type == "identifier":
                self.const_use(n)

    def _is_const_decl(self, node) -> bool:
        return any(s == node.start_byte and e == node.end_byte for _, s, e in self.consts.values())

    def const_use(self, node) -> None:
        name = _txt(node, self.src)
        entry = self.consts.get(name)
        if entry is None:
            return
        p = node.parent
        if (
            p is not None
            and p.type == "variable_declarator"
            and _same(p.child_by_field_name("name"), node)
        ):
            return
        self.found.sql(entry[0], _line(node), exact=True)

    # -- declarations -----------------------------------------------------

    def declare(self, n, root) -> None:
        src = self.src
        t = n.type
        if t == "variable_declarator":
            name = n.child_by_field_name("name")
            value = n.child_by_field_name("value")
            if name is None or value is None or name.type != "identifier":
                return
            decl = n.parent
            holder = decl.parent if decl is not None else None
            top = holder is not None and (
                _same(holder, root)
                or (holder.type == "export_statement" and _same(holder.parent, root))
            )
            if top and _is_stringish(value):
                text = _ts_text(value, src)
                if text is not None and looks_like_sql(text):
                    self.consts[_txt(name, src)] = (text, value.start_byte, value.end_byte)
                    self.found.declare_sql(text, _line(value))
                return
            entity = self._repo_entity(value)
            if entity:
                self.repos[_txt(name, src)] = entity
            self._sequelize_define(value, _txt(name, src))
        elif t == "assignment_expression":
            left = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            entity = self._repo_entity(right) if right is not None else None
            if entity and left is not None:
                name = _js_last_name(left, src)
                if name:
                    self.repos[name] = entity
        elif t in ("required_parameter", "optional_parameter", "public_field_definition"):
            ann = n.child_by_field_name("type")
            name = n.child_by_field_name("pattern") or n.child_by_field_name("name")
            entity = self._repository_type(ann)
            if entity and name is not None:
                self.repos[_txt(name, src)] = entity
        elif t == "class_declaration":
            self._class(n)

    def _repository_type(self, ann) -> str | None:
        """Entity of a `: Repository<Entity>` annotation."""
        if ann is None:
            return None
        for g in _walk(ann):
            if g.type != "generic_type":
                continue
            base = g.child_by_field_name("name")
            args = g.child_by_field_name("type_arguments")
            if base is None or args is None or _txt(base, self.src) not in _TYPEORM_REPO_TYPES:
                continue
            first = args.named_children[0] if args.named_children else None
            if first is not None and first.type == "type_identifier":
                return _txt(first, self.src)
        return None

    def _repo_entity(self, value) -> str | None:
        """Entity of `getRepository(Entity)` (bare or as a method), unchained."""
        while value is not None and value.type in ("await_expression", "parenthesized_expression"):
            value = value.named_children[0] if value.named_children else None
        if value is None or value.type != "call_expression":
            return None
        fn = value.child_by_field_name("function")
        if _js_last_name(fn, self.src) != "getRepository":
            return None
        args = _js_args(value)
        if args and args[0].type == "identifier" and _cap(_txt(args[0], self.src)):
            return _txt(args[0], self.src)
        return None

    def _sequelize_define(self, value, var: str) -> None:
        if value is None or value.type != "call_expression":
            return
        fn = value.child_by_field_name("function")
        if fn is None or fn.type != "member_expression" or _js_last_name(fn, self.src) != "define":
            return
        recv = _js_last_name(fn.child_by_field_name("object"), self.src) or ""
        args = _js_args(value)
        if "sequelize" not in recv.lower() or not args:
            return
        model = _js_literal(args[0], self.src)
        if not model:
            return
        table = _js_object_string(args[2], self.src, "tableName") if len(args) > 2 else None
        for name in {model, var}:
            self.seq_models.add(name)
            self.found.models.append(
                _Model(_line(value), name, "model", "sequelize", table, table_source="declared")
            )

    def _class(self, n) -> None:
        src = self.src
        name_node = n.child_by_field_name("name")
        if name_node is None:
            return
        name = _txt(name_node, src)
        decorators = [c for c in n.children if c.type == "decorator"]
        if n.parent is not None and n.parent.type == "export_statement":
            decorators += [c for c in n.parent.children if c.type == "decorator"]
        for d in decorators:
            expr = d.named_children[0] if d.named_children else None
            if expr is None:
                continue
            callee = (
                expr.child_by_field_name("function") if expr.type == "call_expression" else expr
            )
            dname = _js_last_name(callee, src)
            if dname == "Entity":
                args = _js_args(expr) if expr.type == "call_expression" else []
                table = schema = None
                for a in args:
                    table = table or _js_literal(a, src) or _js_object_string(a, src, "name")
                    schema = schema or _js_object_string(a, src, "schema")
                self.found.models.append(
                    _Model(_line(n), name, "model", "typeorm", table, schema,
                           table_source="declared")
                )  # fmt: skip
            elif dname == "Table":  # sequelize-typescript
                args = _js_args(expr) if expr.type == "call_expression" else []
                table = None
                for a in args:
                    table = table or _js_object_string(a, src, "tableName")
                self.seq_models.add(name)
                self.found.models.append(
                    _Model(_line(n), name, "model", "sequelize", table, table_source="declared")
                )
        heritage = next((c for c in n.children if c.type == "class_heritage"), None)
        if heritage is not None and self.seq_import:
            base = _txt(heritage, src)
            if re.search(r"\bextends\s+Model\b", base) and name not in self.seq_models:
                self.seq_models.add(name)
                self.found.models.append(_Model(_line(n), name, "model", "sequelize"))

    # -- calls ------------------------------------------------------------

    def _knexish(self, name: str | None) -> bool:
        if not name:
            return False
        return name.lower() == "knex" or (self.knex_import and name in _KNEX_LOOSE_NAMES)

    def call(self, c) -> None:
        src = self.src
        f = self.found
        fn = c.child_by_field_name("function")
        if fn is None:
            return
        args = _js_args(c)
        line = _line(c)
        first = _js_literal(args[0], src) if args else None
        if fn.type == "identifier":
            name = _txt(fn, src)
            if self._knexish(name) and first and len(args) == 1:
                up = _chain_up(c, src, "call_expression")
                schema, table = _split_table(first)
                f.table(line, WRITE if _KNEX_WRITE & set(up) else READ, "knex", table, schema)
            elif name == "getRepository":
                self._get_repository(c, args, line)
            return
        if fn.type != "member_expression":
            return
        prop = fn.child_by_field_name("property")
        obj = fn.child_by_field_name("object")
        if prop is None or obj is None:
            return
        method = _txt(prop, src)
        if method == "getRepository":
            self._get_repository(c, args, line)
            return
        # Prisma: <client>.<model>.<method>(...)
        if method in _PRISMA_READ or method in _PRISMA_WRITE:
            if obj.type == "member_expression":
                model_node = obj.child_by_field_name("property")
                recv = (_js_last_name(obj.child_by_field_name("object"), src) or "").lower()
                model = _txt(model_node, src) if model_node is not None else ""
                ok = "prisma" in recv or (
                    recv in _PRISMA_LOOSE_RECEIVERS and method in _PRISMA_DISTINCT
                )
                if ok and model and model_node.type == "property_identifier" and model[0] != "$":
                    f.model(line, WRITE if method in _PRISMA_WRITE else READ, "prisma", model)
                    return
        # TypeORM: a variable / field known to hold a Repository<Entity>
        holder = (
            _js_last_name(obj, src) if obj.type in ("identifier", "member_expression") else None
        )
        if holder in self.repos and (obj.type == "identifier" or self._is_this_member(obj)):
            if method in _TYPEORM_WRITE:
                f.model(line, WRITE, "typeorm", self.repos[holder])
            elif method in _TYPEORM_READ:
                f.model(line, READ, "typeorm", self.repos[holder])
            return
        # Sequelize: Model.findAll(...) and friends
        if obj.type == "identifier" and (method in _SEQ_READ or method in _SEQ_WRITE):
            model = _txt(obj, src)
            if _cap(model) and (method in _SEQ_STRONG or model in self.seq_models):
                f.model(line, WRITE if method in _SEQ_WRITE else READ, "sequelize", model)
                return
        if not first:
            return
        # Query builders: a string table name on a chain rooted in a known client
        root = _js_root(obj, src)
        root_fn = root.child_by_field_name("function") if root.type == "call_expression" else root
        root_name = _js_last_name(root_fn, src) if root_fn is not None else None
        if root_fn is not None and root_fn.type == "this":
            root_name = self._this_root(obj)
        schema, table = _split_table(first)
        if self._knexish(root_name):
            down = self._chain_down(obj)
            up = _chain_up(c, src, "call_expression")
            wrote = bool(_KNEX_WRITE & (set(up) | set(down)))
            if method in _KNEX_DDL:
                f.table(line, DDL, "knex", table, schema)
                if method in ("createTable", "createTableIfNotExists"):
                    f.models.append(
                        _Model(line, table.lower(), "table", "knex", table, schema,
                               table_source="declared")
                    )  # fmt: skip
            elif method == "into":
                f.table(line, WRITE, "knex", table, schema)
            elif method in ("from", "table", "withSchema") and method != "withSchema":
                f.table(line, WRITE if wrote else READ, "knex", table, schema)
            elif method in _KNEX_JOINS:
                f.table(line, READ, "knex", table.split(" ")[0], schema)
        elif root_name and "supabase" in root_name.lower() and method == "from":
            up = _chain_up(c, src, "call_expression")
            f.table(line, WRITE if _SUPABASE_WRITE & set(up) else READ, "supabase", table, schema)

    def _is_this_member(self, obj) -> bool:
        inner = obj.child_by_field_name("object")
        return inner is not None and inner.type == "this"

    def _this_root(self, obj) -> str | None:
        """`this.knex.from(...)` -> 'knex'."""
        cur = obj
        while cur is not None and cur.type in ("member_expression", "call_expression"):
            if cur.type == "member_expression" and self._is_this_member(cur):
                return _js_last_name(cur, self.src)
            cur = cur.child_by_field_name(
                "object" if cur.type == "member_expression" else "function"
            )
        return None

    def _chain_down(self, obj) -> list[str]:
        names: list[str] = []
        cur = obj
        while cur is not None:
            if cur.type == "member_expression":
                prop = cur.child_by_field_name("property")
                if prop is not None:
                    names.append(_txt(prop, self.src))
                cur = cur.child_by_field_name("object")
            elif cur.type == "call_expression":
                cur = cur.child_by_field_name("function")
            else:
                break
        return names

    def _get_repository(self, c, args, line: int) -> None:
        if not args or args[0].type != "identifier" or not _cap(_txt(args[0], self.src)):
            return
        up = _chain_up(c, self.src, "call_expression")
        if not up:
            return  # bound to a variable: counted where the variable is used
        entity = _txt(args[0], self.src)
        if up[0] in _TYPEORM_WRITE:
            self.found.model(line, WRITE, "typeorm", entity)
        elif up[0] in _TYPEORM_READ:
            self.found.model(line, READ, "typeorm", entity)


def _js(source: str, language: str, found: _Found) -> None:
    _Js(source, language, found)


# --------------------------------------------------------------------------
# Java
# --------------------------------------------------------------------------

_JPA_REPO_BASES = frozenset(
    """JpaRepository CrudRepository PagingAndSortingRepository ListCrudRepository
    ListPagingAndSortingRepository Repository JpaSpecificationExecutor
    ReactiveCrudRepository""".split()
)
_REPO_READ_RE = re.compile(r"^(?:find|get|read|count|exists|query|search|stream)")
_REPO_WRITE_RE = re.compile(r"^(?:save|delete|insert|update|remove|persist)")
_REPO_TYPE_RE = re.compile(r"(?:Repository|Repo|Dao|DAO)$")
_EM_WRITE = frozenset({"persist", "merge", "remove"})
_EM_READ = frozenset({"find", "getReference"})
_EM_NAMES = frozenset({"em", "entityManager"})
_JPQL_ANNOTATIONS = frozenset({"Query", "NamedQuery"})


def _java_type_name(node, src: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "type_identifier":
        return _txt(node, src)
    if node.type == "generic_type":
        base = next((c for c in node.children if c.type == "type_identifier"), None)
        return _txt(base, src) if base is not None else None
    if node.type == "scoped_type_identifier":
        return _txt(node, src).rsplit(".", 1)[-1]
    return None


def _java_annotations(node, src: bytes) -> dict[str, Any]:
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    out: dict[str, Any] = {}
    if mods is None:
        return out
    for a in mods.children:
        if a.type in ("marker_annotation", "annotation"):
            name = a.child_by_field_name("name")
            if name is not None:
                out[_txt(name, src).rsplit(".", 1)[-1]] = a
    return out


def _java_annotation_values(ann, src: bytes) -> dict[str, Any]:
    """`@Table(name = "x", schema = "s")` -> {'name': node, 'schema': node};
    a single positional value is under 'value'."""
    args = ann.child_by_field_name("arguments")
    out: dict[str, Any] = {}
    if args is None:
        return out
    for c in args.named_children:
        if c.type == "element_value_pair":
            key = c.child_by_field_name("key")
            value = c.child_by_field_name("value")
            if key is not None and value is not None:
                out[_txt(key, src)] = value
        else:
            out.setdefault("value", c)
    return out


def _java_literal(node, src: bytes) -> str | None:
    if node is None or node.type != "string_literal":
        return None
    return _ts_text(node, src)


class _Java:
    def __init__(self, source: str, found: _Found) -> None:
        self.src = source.encode("utf-8")
        self.found = found
        self.types: dict[str, str] = {}
        self.consts: dict[str, tuple[str, int]] = {}  # name -> (text, start byte of the value)
        self.repo_types: set[str] = set()
        root = get_parser("java").parse(self.src).root_node
        nodes = list(_walk(root))
        for n in nodes:
            self.declare(n)
        const_starts = {start for _, start in self.consts.values()}
        skip_until = -1
        for n in nodes:
            if n.type in ("string_literal", "binary_expression"):
                if n.start_byte < skip_until:
                    continue
                text = _ts_text(n, self.src)
                if text is not None:
                    skip_until = n.end_byte
                    if n.start_byte not in const_starts:
                        found.sql(text, _line(n), jpql=self._jpql(n), declare=True)
                    continue
            if n.type == "method_invocation":
                self.call(n)
            elif n.type == "identifier" and _txt(n, self.src) in self.consts:
                p = n.parent
                if (
                    p is not None
                    and p.type == "variable_declarator"
                    and _same(p.child_by_field_name("name"), n)
                ):
                    continue
                found.sql(
                    self.consts[_txt(n, self.src)][0], _line(n), jpql=self._jpql(n), exact=True
                )

    def _jpql(self, node) -> bool:
        """Is this string a JPQL query (entity names) rather than native SQL?"""
        cur = node.parent
        while cur is not None:
            if cur.type == "annotation":
                name = cur.child_by_field_name("name")
                if name is None or _txt(name, self.src).rsplit(".", 1)[-1] not in _JPQL_ANNOTATIONS:
                    return False
                native = _java_annotation_values(cur, self.src).get("nativeQuery")
                return not (native is not None and _txt(native, self.src) == "true")
            if cur.type == "argument_list":
                call = cur.parent
                if call is not None and call.type == "method_invocation":
                    name = call.child_by_field_name("name")
                    return name is not None and _txt(name, self.src) == "createQuery"
                return False
            if cur.type in ("method_declaration", "class_body", "block", "lambda_expression"):
                return False
            cur = cur.parent
        return False

    def declare(self, n) -> None:
        src = self.src
        t = n.type
        if t in ("field_declaration", "local_variable_declaration"):
            type_node = n.child_by_field_name("type")
            tname = _java_type_name(type_node, src)
            for d in n.children:
                if d.type != "variable_declarator":
                    continue
                name = d.child_by_field_name("name")
                value = d.child_by_field_name("value")
                if name is None:
                    continue
                if (
                    tname == "var"
                    and value is not None
                    and value.type == "object_creation_expression"
                ):
                    tname = _java_type_name(value.child_by_field_name("type"), src)
                if tname:
                    self.types[_txt(name, src)] = tname
                if t == "field_declaration" and value is not None and tname == "String":
                    text = _ts_text(value, src)
                    if text is not None and looks_like_sql(text):
                        self.consts[_txt(name, src)] = (text, value.start_byte)
                        self.found.declare_sql(text, _line(value))
        elif t == "formal_parameter":
            tname = _java_type_name(n.child_by_field_name("type"), src)
            name = n.child_by_field_name("name")
            if tname and name is not None:
                self.types[_txt(name, src)] = tname
        elif t == "class_declaration":
            anns = _java_annotations(n, src)
            name = n.child_by_field_name("name")
            if "Entity" in anns and name is not None:
                table = schema = None
                if "Table" in anns and anns["Table"].type == "annotation":
                    values = _java_annotation_values(anns["Table"], src)
                    table = _java_literal(values.get("name"), src)
                    schema = _java_literal(values.get("schema"), src)
                self.found.models.append(
                    _Model(_line(n), _txt(name, src), "model", "jpa", table, schema,
                           table_source="declared")
                )  # fmt: skip
        elif t == "interface_declaration":
            name = n.child_by_field_name("name")
            ext = next((c for c in n.children if c.type == "extends_interfaces"), None)
            if name is None or ext is None:
                return
            for g in _walk(ext):
                if g.type != "generic_type":
                    continue
                base = next((c for c in g.children if c.type == "type_identifier"), None)
                args = next((c for c in g.children if c.type == "type_arguments"), None)
                if base is None or args is None or _txt(base, src) not in _JPA_REPO_BASES:
                    continue
                entity = (
                    _java_type_name(args.named_children[0], src) if args.named_children else None
                )
                if entity:
                    self.repo_types.add(_txt(name, src))
                    self.found.models.append(
                        _Model(_line(n), _txt(name, src), "repository", "jpa", entity=entity)
                    )
                break

    def call(self, c) -> None:
        src = self.src
        obj = c.child_by_field_name("object")
        name = c.child_by_field_name("name")
        if obj is None or name is None:
            return
        if obj.type == "identifier":
            recv = _txt(obj, src)
        elif obj.type == "field_access":
            fld = obj.child_by_field_name("field")
            recv = _txt(fld, src) if fld is not None else ""
        else:
            return
        method = _txt(name, src)
        tname = self.types.get(recv)
        args_node = c.child_by_field_name("arguments")
        args = list(args_node.named_children) if args_node is not None else []
        line = _line(c)
        if tname == "EntityManager" or (tname is None and recv in _EM_NAMES):
            if method in _EM_WRITE and args:
                entity = self._arg_entity(args[0])
                if entity:
                    self.found.model(line, WRITE, "jpa", entity)
            elif method in _EM_READ and args and args[0].type == "class_literal":
                entity = _java_type_name(
                    args[0].named_children[0] if args[0].named_children else None, src
                )
                if entity:
                    self.found.model(line, READ, "jpa", entity)
            return
        if tname and (tname in self.repo_types or _REPO_TYPE_RE.search(tname)):
            access = (
                WRITE
                if _REPO_WRITE_RE.match(method)
                else READ
                if _REPO_READ_RE.match(method)
                else None
            )
            if access:
                self.found.hits.append(
                    _Hit(line, access, "jpa", ref_name=tname, ref_kind="repository")
                )

    def _arg_entity(self, arg) -> str | None:
        if arg.type == "object_creation_expression":
            return _java_type_name(arg.child_by_field_name("type"), self.src)
        if arg.type == "identifier":
            t = self.types.get(_txt(arg, self.src))
            return t if t and _cap(t) and t not in ("String", "Object", "Long", "Integer") else None
        return None


def _java(source: str, found: _Found) -> None:
    _Java(source, found)
