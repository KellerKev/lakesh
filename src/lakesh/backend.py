"""How lakesh reaches a source, behind one small interface.

For its whole life lakesh had exactly one way to run a query: DuckDB's
`adbc_scanner` loading an ADBC driver `.so`. That grew two shapes that
every consumer had to know about — a native path that returns
`(con, handle)` and speaks through `adbc_scan`/`adbc_execute`, and an
attached-catalog path that returns a bare `DuckDBPyConnection` — plus a
`handle is None` sentinel threaded through a dozen call sites to tell them
apart.

This module collapses that to one type. A `Session` is "something lakesh
can run SQL against and fetch rows from," and the native/attached
distinction becomes two implementations of it. Adding a *third* way to
reach a source — a Python driver — is then a third implementation rather
than a third branch at every call site.

### The interface is deliberately PEP 249

Every consumer (`mcp`, `cli`, `repl`, `staging`) needs only a tiny slice
of a connection: run a statement, read column names off `description`,
`fetchmany`/`fetchall`, and cancel. That slice *is* the Python DB-API
(PEP 249) cursor — which `python-duckdb`, `snowflake-connector-python`
and `psycopg` all already implement. So the standard a Python backend
must meet is not lakesh-invented; it is the one every SQL driver already
speaks, and the DB-API backend is a thin adapter with no per-driver code.

### `run` vs `execute`

`run(sql)` is the high-level call: it routes reads and writes (a write
that went through `adbc_scan` would execute twice — see
`duck.adbc_native_scan`) and returns `(columns, rows)`. `execute(sql,
params)` hands back a live cursor for the one consumer that needs
incremental paging (`mcp`'s `query` tool) and for the bound-parameter
metadata queries on the DuckDB path.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import duckdb

from .config import Profile


@runtime_checkable
class Cursor(Protocol):
    """The PEP 249 cursor slice lakesh actually reads."""

    description: Any

    def fetchall(self) -> list[tuple]: ...
    def fetchmany(self, size: int) -> list[tuple]: ...
    def fetchone(self) -> tuple | None: ...


class Session(Protocol):
    """One open connection to a source, however lakesh reached it."""

    profile: Profile
    dialect_name: str

    def execute(self, sql: str, params: list | None = None) -> Cursor:
        """A live cursor. Params are for the DuckDB path's bound metadata
        queries; a native/ADBC session interpolates literals instead and
        does not accept them."""

    def run(self, sql: str) -> tuple[list[str], list[tuple]]:
        """Execute `sql`, routing reads vs writes, and return
        `(columns, rows)`. A write returns `([], [])` — it has no result
        set — and runs exactly once."""

    def cancel(self) -> None:
        """Best-effort interrupt of the in-flight statement, for
        `duck.deadline`."""

    def close(self) -> None: ...

    def probe(self) -> dict | None:
        """What the source says about this session (who it thinks we are,
        agent-activation), or None if it cannot say."""

    queries_source_directly: bool
    """Whether SQL runs on the source in its own dialect (native ADBC and
    Python drivers) or through DuckDB's attached catalog. The metadata
    tools build different SQL for the two — source `information_schema`
    with interpolated literals, vs catalog-qualified DuckDB SQL with bound
    params — so they branch on this rather than on `profile.type`."""

    def metadata(self) -> "SourceMetadata | None":
        """A catalog API for sources that expose namespaces/tables/columns
        without SQL (pyiceberg, a custom REST catalog), or None when
        metadata comes from `information_schema` like everything else. The
        metadata tools prefer this when present."""


class SourceMetadata(Protocol):
    """Namespaces/tables/columns from a catalog API rather than SQL.

    For sources that have no `information_schema` — an Iceberg catalog is
    the motivating case — the four metadata tools read from this instead
    of querying. Column tuples match the SQL path's shape
    `(name, type, nullable, position)` so the tool bodies converge again
    after the branch.
    """

    def namespaces(self) -> list[str]: ...
    def tables(self, namespace: str | None) -> list[tuple[str, str]]: ...
    def columns(self, namespace: str, table: str) -> list[tuple[str, str, bool, int]]: ...


# --------------------------------------------------------------------------
# implementations over today's primitives
#
# These wrap `duck.py` rather than move it: the extraction is meant to be
# behaviour-preserving, and the existing test suite is the guard. Once the
# consumers all speak `Session`, the `handle is None` branches inside
# `duck` become dead and can be folded in.


class DuckSession:
    """A source reached through a local DuckDB connection — the
    attached-catalog path (Iceberg REST, DuckLake) and DuckDB itself.

    DuckDB executes reads and writes identically and has no
    double-execution problem, so `run` is a plain `execute`; no routing is
    needed the way it is for ADBC.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, profile: Profile):
        self._con = con
        self.profile = profile
        from . import dialect as _dialect

        self.dialect_name = _dialect.for_profile(profile).name

    queries_source_directly = False

    def execute(self, sql: str, params: list | None = None) -> Cursor:
        return self._con.execute(sql, params) if params else self._con.execute(sql)

    def run(self, sql: str) -> tuple[list[str], list[tuple]]:
        cur = self._con.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        return columns, cur.fetchall()

    def cancel(self) -> None:
        self._con.interrupt()

    def close(self) -> None:
        self._con.close()

    def probe(self) -> dict | None:
        from . import duck as _duck

        return _duck.session_probe(self._con, None, self.profile)

    def metadata(self):
        return None    # DuckDB has information_schema; the SQL path is used

    # Escape hatch for the few call sites that still need the raw
    # connection (the sandbox helpers, the REPL). Removed as they migrate.
    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._con


class AdbcSession:
    """A source reached through native ADBC passthrough — `(con, handle)`,
    speaking `adbc_scan` for reads and `adbc_execute` for writes."""

    def __init__(
        self, con: duckdb.DuckDBPyConnection, handle: int, profile: Profile
    ):
        self._con = con
        self._handle = handle
        self.profile = profile
        from . import dialect as _dialect

        self.dialect_name = _dialect.for_profile(profile).name

    queries_source_directly = True

    def execute(self, sql: str, params: list | None = None) -> Cursor:
        if params:
            # The native path interpolates literals (see the metadata
            # tools' `_lit`-built SQL); it has no bound-parameter form.
            raise ValueError(
                "the native ADBC path does not take bound parameters; "
                "interpolate literals into the SQL instead"
            )
        from . import duck as _duck

        return _duck.adbc_native_scan(self._con, self._handle, sql)

    def run(self, sql: str) -> tuple[list[str], list[tuple]]:
        from . import duck as _duck

        return _duck.adbc_native_stmt(self._con, self._handle, sql)

    def cancel(self) -> None:
        self._con.interrupt()

    def close(self) -> None:
        self._con.close()

    def probe(self) -> dict | None:
        from . import duck as _duck

        return _duck.session_probe(self._con, self._handle, self.profile)

    def metadata(self):
        return None    # the source has information_schema; the SQL path is used

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._con

    @property
    def handle(self) -> int:
        return self._handle


class DBAPISession:
    """A source reached through any PEP 249 (DB-API 2.0) connection — the
    Python-driver path.

    This is the whole point of the abstraction: `python-duckdb`,
    `snowflake-connector-python` and `psycopg` all implement DB-API, so
    this one adapter serves every one of them with no per-driver code. A
    user's own driver joins the same way — it need only be a DB-API
    connection (or a factory returning one).

    Unlike the ADBC path there is no double-execution to route around
    (that was an `adbc_scan` table-function quirk). `run` still routes on
    read-vs-write, but only to decide **fetch vs commit**: a read returns
    its rows, a write commits and returns none.
    """

    def __init__(self, conn: Any, profile: Profile, *, dialect_name: str):
        self._conn = conn
        self.profile = profile
        self.dialect_name = dialect_name

    queries_source_directly = True

    def execute(self, sql: str, params: list | None = None) -> Cursor:
        cur = self._conn.cursor()
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur

    def run(self, sql: str) -> tuple[list[str], list[tuple]]:
        from . import guard

        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            if guard.is_read_only(sql):
                columns = [d[0] for d in cur.description] if cur.description else []
                return columns, cur.fetchall()
            # A write. DB-API runs it once (no adbc_scan double-exec), but
            # commit is needed for drivers that default autocommit off; it
            # is a harmless no-op where autocommit is on.
            commit = getattr(self._conn, "commit", None)
            if callable(commit):
                commit()
            return [], []
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def cancel(self) -> None:
        # DB-API does not standardise cancellation; try the common
        # spellings and give up quietly if none exist.
        for target in (self._conn, ):
            fn = getattr(target, "cancel", None)
            if callable(fn):
                try:
                    fn()
                    return
                except Exception:
                    pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def probe(self) -> dict | None:
        from . import dialect as _dialect

        sql = _dialect.session_probe_sql(self.profile)
        if not sql:
            return None
        try:
            cur = self._conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            if not rows:
                return None
            names = [d[0].lower() for d in cur.description]
            return {k: v for k, v in zip(names, rows[0]) if v is not None}
        except Exception:
            return None

    def metadata(self):
        return None    # a DB-API SQL source uses information_schema


# --------------------------------------------------------------------------
# the Python-backend registry
#
# A backend factory takes the profile and returns either a DB-API
# connection (which we wrap in DBAPISession) or a fully-formed Session
# (for a source that needs custom governance or is not SQL at all). The
# shipped factories cover the common drivers; a `"module:callable"` name
# lets a user supply their own without any packaging ceremony.


def _duckdb_backend(profile: Profile, *, caller: str | None = None):
    """python-duckdb as a DB-API backend — the reference implementation.

    `options.database` picks the database (default in-memory); it is a
    real DB-API connection, so it exercises `DBAPISession` for real
    rather than short-cutting through `DuckSession`.
    """
    con = duckdb.connect(profile.options.get("database", ":memory:"))
    return con


def _snowflake_backend(profile: Profile, *, caller: str | None = None):
    """snowflake-connector-python — pure Python, no ADBC `.so`.

    Two things this backend owns, both established by measurement:
    it bypasses the frozen ADBC driver, and its `application` reaches
    Snowflake's `CLIENT_ENVIRONMENT.APPLICATION` verbatim. The default
    activates agent-masking for the MCP caller (`cortex_code_cli`), stays
    honest for the CLI, and an explicit `options.application` overrides
    either — see the README on the audit-trail tradeoff.
    """
    try:
        import snowflake.connector as sfc
    except ImportError:
        raise _configerror(
            "the snowflake python backend needs snowflake-connector-python: "
            "pip install 'lakesh[snowflake-python]'"
        ) from None
    opts = _coerce_options(profile.options)
    opts.setdefault("application", _default_application(caller))
    return sfc.connect(**opts)


def _postgres_backend(profile: Profile, *, caller: str | None = None):
    """psycopg (v3) as a DB-API backend."""
    try:
        import psycopg
    except ImportError:
        raise _configerror(
            "the postgres python backend needs psycopg: "
            "pip install 'lakesh[postgres-python]'"
        ) from None
    opts = _coerce_options(profile.options)
    # psycopg labels the session with application_name, the same signal
    # lakesh stamps on the ADBC Postgres path.
    opts.setdefault("application_name", f"lakesh/{_version()} {caller or 'cli'}")
    if profile.uri:
        return psycopg.connect(profile.uri, **opts)
    return psycopg.connect(**opts)


def _pyiceberg_backend(profile: Profile, *, caller: str | None = None):
    """pyiceberg — the data-pull family, for a catalog that speaks no SQL.

    This is the shape that motivated the `SourceMetadata` seam: an Iceberg
    catalog has no `information_schema`, so metadata comes from the catalog
    API, and data comes back as Arrow which lakesh runs SQL over in an
    in-process DuckDB. That reuses lakesh's own engine over Python-fetched
    data rather than depending on DuckDB's iceberg extension reaching the
    catalog — the value for catalogs (Glue, Hive, SQL) it cannot.

    Catalog props come from the profile's `[s3]` block (reused, not
    re-specified) plus any extra pyiceberg props in `options`; `uri` and
    `warehouse` fall back to the profile's own fields.
    """
    try:
        from pyiceberg.catalog import load_catalog
    except ImportError:
        raise _configerror(
            "the pyiceberg backend needs pyiceberg: "
            "pip install 'lakesh[iceberg-python]'"
        ) from None

    opts = dict(profile.options)
    props: dict[str, Any] = {}
    s3 = profile.s3
    if s3.endpoint:
        props["s3.endpoint"] = s3.endpoint
    if s3.access_key:
        props["s3.access-key-id"] = s3.access_key
    if s3.secret_key:
        props["s3.secret-access-key"] = s3.secret_key
    if s3.region:
        props["s3.region"] = s3.region
    if s3.path_style:
        props["s3.path-style-access"] = "true"
    props["uri"] = opts.pop("uri", None) or profile.uri
    props["warehouse"] = opts.pop("warehouse", None) or profile.warehouse
    name = opts.pop("name", None) or profile.catalog or "lake"
    # remaining options are pyiceberg catalog props (credential, token, …)
    props.update(opts)
    catalog = load_catalog(name, **{"type": "rest", **props})
    return PyicebergSession(catalog, profile)


class _PyicebergMetadata:
    """Catalog-API metadata for `PyicebergSession`. Cached per session —
    a tool run that lists then describes should not re-list."""

    def __init__(self, catalog):
        self._c = catalog
        self._ns: list[str] | None = None

    def namespaces(self) -> list[str]:
        if self._ns is None:
            self._ns = sorted(".".join(ns) for ns in self._c.list_namespaces())
        return self._ns

    def tables(self, namespace: str | None) -> list[tuple[str, str]]:
        targets = [tuple(namespace.split("."))] if namespace else \
            self._c.list_namespaces()
        out = []
        for ns in targets:
            for ident in self._c.list_tables(ns):
                out.append((".".join(ident[:-1]), ident[-1]))
        return sorted(out)

    def columns(self, namespace: str, table: str):
        schema = self._c.load_table(f"{namespace}.{table}").schema()
        return [
            (f.name, str(f.field_type), not f.required, i)
            for i, f in enumerate(schema.fields, 1)
        ]


class PyicebergSession:
    """Runs SQL over an Iceberg catalog by pulling Arrow into DuckDB.

    Metadata is served from the catalog API (`metadata()`); query
    execution registers the referenced tables as DuckDB views (scanned via
    pyiceberg to Arrow) and runs the SQL locally. Registration is lazy and
    keyed on the statement's table references, so a query touches only the
    tables it names.
    """

    queries_source_directly = False   # DuckDB executes locally

    def __init__(self, catalog, profile: Profile):
        self._catalog = catalog
        self.profile = profile
        from . import dialect as _dialect

        self.dialect_name = _dialect.for_profile(profile).name
        self._con = duckdb.connect(":memory:")
        self._registered: set[str] = set()
        self._md = _PyicebergMetadata(catalog)

    def _register_referenced(self, sql: str) -> None:
        import re

        low = sql.lower()
        for ns, tbl in self._md.tables(None):
            key = f"{ns}.{tbl}"
            if key in self._registered:
                continue
            # Register when the statement names the table, qualified or
            # bare. A word-boundary match keeps `orders` from also pulling
            # `orders_archive`.
            hit = (key.lower() in low
                   or re.search(rf"\b{re.escape(tbl.lower())}\b", low))
            if not hit:
                continue
            arrow = self._catalog.load_table(key).scan().to_arrow()
            rel = "_lakesh_" + re.sub(r"\W", "_", key)   # a bare identifier
            self._con.register(rel, arrow)
            self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{ns}"')
            self._con.execute(
                f'CREATE OR REPLACE VIEW "{ns}"."{tbl}" AS SELECT * FROM {rel}'
            )
            self._registered.add(key)

    def execute(self, sql: str, params: list | None = None) -> Cursor:
        self._register_referenced(sql)
        return self._con.execute(sql, params) if params else self._con.execute(sql)

    def run(self, sql: str) -> tuple[list[str], list[tuple]]:
        cur = self.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        return columns, cur.fetchall()

    def cancel(self) -> None:
        self._con.interrupt()

    def close(self) -> None:
        self._con.close()

    def probe(self) -> dict | None:
        return None

    def metadata(self) -> _PyicebergMetadata:
        return self._md


_SHIPPED_BACKENDS = {
    "duckdb": _duckdb_backend,
    "snowflake": _snowflake_backend,
    "postgres": _postgres_backend,
    "pyiceberg": _pyiceberg_backend,
}


def _default_application(caller: str | None) -> str:
    """The Snowflake `application` string lakesh sends by default.

    Agent (MCP) sessions default to the marker that activates
    agent-masking policies; a human at the CLI stays honestly labelled and
    is *not* marked as an agent (it would otherwise be masked as one). An
    operator overrides either via `options.application`.
    """
    if caller == "mcp":
        return "cortex_code_cli"      # activates IS_AGENT_ACTIVATED
    return f"lakesh/{_version()} {caller or 'cli'}"


def _version() -> str:
    from . import duck as _duck

    return _duck.__version__


def _coerce_options(options: dict) -> dict:
    """Driver kwargs. TOML gives strings; a few connect() kwargs want
    other types, but drivers generally coerce, so this is a passthrough
    today and the seam for per-driver coercion if one is ever needed."""
    return dict(options)


def _resolve_backend(name: str):
    """A backend factory from a shipped name or a `module:callable` path."""
    if ":" in name:
        import importlib

        module_name, _, attr = name.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            raise _configerror(
                f"cannot import backend module {module_name!r}: {e}"
            ) from None
        factory = getattr(module, attr, None)
        if not callable(factory):
            raise _configerror(
                f"backend {name!r}: {module_name!r} has no callable {attr!r}"
            )
        return factory
    if name in _SHIPPED_BACKENDS:
        return _SHIPPED_BACKENDS[name]
    raise _configerror(
        f"unknown backend {name!r} — shipped: "
        f"{', '.join(sorted(_SHIPPED_BACKENDS))}; or a \"module:callable\" path"
    )


def _open_python_session(profile: Profile, *, caller: str | None) -> Session:
    from . import dialect as _dialect

    factory = _resolve_backend(profile.backend)
    obj = factory(profile, caller=caller)
    # A factory may return a ready Session (full custom control) or a bare
    # DB-API connection for lakesh to wrap.
    if hasattr(obj, "run") and hasattr(obj, "dialect_name"):
        return obj
    return DBAPISession(
        obj, profile, dialect_name=_dialect.for_profile(profile).name
    )


# --------------------------------------------------------------------------
# factory


def open_session(
    profile: Profile,
    *,
    caller: str | None = None,
    timeout_s: float | None = None,
    interactive: bool = True,
    native: bool | None = None,
) -> Session:
    """Open the right `Session` for a profile.

    `native` selects the ADBC passthrough path for an adbc profile; when
    unset it defaults to native for adbc profiles (their fast path) and
    the attached path otherwise. Non-adbc profiles are always attached.
    """
    from . import duck as _duck

    if caller is None:
        caller = _duck.CALLER

    if profile.type == "python":
        if native:
            raise _configerror(
                f"profile {profile.name!r}: --native is an ADBC concept and "
                f"does not apply to a python backend"
            )
        return _open_python_session(profile, caller=caller)

    want_native = (profile.type == "adbc") if native is None else native
    if want_native:
        if profile.type != "adbc":
            raise _configerror(
                f"profile {profile.name!r}: native passthrough requires an "
                f"adbc profile (this one is {profile.type!r})"
            )
        con, handle = _duck.connect_native(
            profile, interactive=interactive, timeout_s=timeout_s, caller=caller
        )
        return AdbcSession(con, handle, profile)
    con = _duck.connect(profile, interactive=interactive)
    return DuckSession(con, profile)


def _configerror(msg: str):
    from .config import ConfigError

    return ConfigError(msg)
