"""MCP server — exposes lakesh's catalog access to LLM agents.

Run via `lakesh mcp`; the server speaks MCP over stdio (the transport
Claude Desktop, Cline, Continue, etc. use to talk to local MCP servers).

Tools exposed:

    list_profiles()
    list_namespaces(profile=None)
    list_tables(profile=None, namespace=None)
    describe_table(namespace, table, profile=None)
    query(sql, profile=None, limit=1000, format="json")

Each tool reuses the same `config.load_config()` + `duck.connect()` path
the CLI uses, so config + auth + S3 plumbing behaves identically to
`lakesh exec`.

### Safety: read-only by default

LLM-driven SQL is high blast-radius. By default the `query` tool rejects
anything that doesn't start with `SELECT` / `SHOW` / `DESCRIBE` / `WITH`
(case-insensitive). Set `LAKESH_MCP_WRITE=1` in the server's environment
to enable INSERT / UPDATE / DELETE / DDL / DuckLake procedure calls.

A profile marked `read_only = true` wins over `LAKESH_MCP_WRITE`: the
profile is the more specific statement of intent, and native passthrough
(below) opens its own ADBC connection that the ATTACH's READ_ONLY flag
cannot reach.

### Safety: nothing credential-shaped goes to the model

`list_profiles` output and every error payload land in an LLM's context.
For Snowflake and Postgres profiles the connection URI *is* the
credential, so both routes go through `redact` first. See that module.

### Performance: native passthrough for adbc profiles

Each tool call opens + closes a fresh DuckDB connection. That's slower
than a long-lived REPL session but matches the stateless tool-call model
of MCP, and avoids the iceberg extension's known thread-affinity quirks.
For high-frequency querying, prefer the `lakesh` REPL or `lakesh exec`.

For `adbc` profiles that per-call model collides with how
`adbc_scanner` populates DuckDB's catalog: eagerly and serially, one
`DESC TABLE` per object, with nothing cached between connections. Every
Snowflake database carries ~60-70 INFORMATION_SCHEMA views of its own,
so the cost is round-trip latency × object count and has a fixed floor
regardless of data size. Measured against a live account:

    list_tables      through the ATTACH   >240s (MCP client timeout)
                     through the source     5s
    describe_table   through the ATTACH   >240s
                     through the source     6s
    count of information_schema.tables    >600s, killed

Six minutes per call is past any MCP client's timeout, and the client
reports it as `MCP error -32001: Request timed out` — which reads like a
dead server rather than a slow query. So for adbc profiles the three
introspection tools and `query` send SQL to the source instead. Pass
`native=false` to `query` to force the DuckDB path back on; it is the
only way to join a source against a local Parquet file in one statement.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import duckdb
from mcp.server.fastmcp import FastMCP

from .config import Config, ConfigError, Profile, default_config_path, load_config
from .duck import adbc_native_scan, catalog_alias, connect, connect_native
from .oauth import AuthRequired
from .output import _stringify  # type: ignore[attr-defined]
from .redact import profile_secrets, redact_uri, scrub


_READ_ONLY_LEADING = re.compile(r"^\s*(select|show|describe|desc|with|explain|pragma|values)\b", re.IGNORECASE)


def _is_read_only(sql: str) -> bool:
    """Cheap structural check — covers the obvious top-level cases. A
    determined caller can still smuggle writes inside a `WITH … INSERT`
    CTE; the safety net is meant to catch accidental misuse, not a
    motivated attacker."""
    return bool(_READ_ONLY_LEADING.match(sql.strip().lstrip("(")))


def _writes_enabled() -> bool:
    return os.environ.get("LAKESH_MCP_WRITE", "0") in ("1", "true", "yes")


# Every credential seen in a loaded config, so `_error` can scrub free
# text. Populated by `_load_or_raise`; a module-level set because errors
# surface from call sites that no longer have the profile in hand.
_KNOWN_SECRETS: set[str] = set()


def _load_or_raise(config_path: Path | None = None) -> Config:
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        raise RuntimeError(str(e)) from e
    for prof in cfg.profiles.values():
        _KNOWN_SECRETS.update(profile_secrets(prof))
    return cfg


def _error(exc: Exception | str) -> str:
    """The single error path for every tool. Driver errors quote the
    failing statement, DSN inline, so nothing reaches the model without
    passing through `scrub` first."""
    return json.dumps({"error": scrub(str(exc), _KNOWN_SECRETS)})


# --------------------------------------------------------------------------
# native passthrough helpers (see the module docstring)

_SYSTEM_SCHEMAS = ("main", "information_schema", "pg_catalog")


def _prefer_native(profile: Profile) -> bool:
    return profile.type == "adbc"


def _lit(value: str) -> str:
    """Single-quoted SQL literal. `adbc_scan` takes the statement as one
    string, so values interpolated into it are escaped rather than
    bound — this is the escaping."""
    return "'" + str(value).replace("'", "''") + "'"


def _not_system_schemas(column: str) -> str:
    return f"{column} NOT IN ({', '.join(_lit(s) for s in _SYSTEM_SCHEMAS)})"


# --------------------------------------------------------------------------
# pattern matching for search_objects

# `!` rather than the conventional backslash. Snowflake treats a backslash
# as an escape character *inside string literals*, while Postgres (with
# standard_conforming_strings on) and DuckDB treat it as a literal — so
# `ESCAPE '\'` would need to be written two different ways depending on
# the source. `!` has no literal-level meaning in any of the three.
_LIKE_ESCAPE = "!"


def _like_pattern(pattern: str) -> str:
    """Translate a caller's pattern into a LIKE pattern.

    `_` is escaped to a literal underscore: analytic table names are full
    of them and nobody types `fact_revenue` meaning `fact<any char>revenue`.
    `%` is honoured as the wildcard, and its presence suppresses the
    implicit `%…%` wrap so `revenue%` means prefix-match. A bare word is
    wrapped, because the whole point of the tool is that the caller
    doesn't know the naming convention yet.
    """
    escaped = (
        pattern.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return escaped if "%" in escaped else f"%{escaped}%"


def _ilike(column: str, like: str) -> str:
    """Case-insensitive match for the native path. ILIKE is available on
    Snowflake, Postgres and DuckDB alike, and case-insensitivity is not
    optional here: Snowflake upper-cases unquoted identifiers and
    Postgres lower-cases them, so the same logical name is spelled two
    ways depending on which source you ask."""
    return f"{column} ILIKE {_lit(like)} ESCAPE {_lit(_LIKE_ESCAPE)}"


@contextmanager
def _open_native(profile_name: str | None) -> Iterator[tuple[duckdb.DuckDBPyConnection, int, Profile]]:
    cfg = _load_or_raise()
    prof = cfg.get(profile_name)
    con, handle = connect_native(prof, interactive=False)
    try:
        yield con, handle, prof
    finally:
        con.close()


def _native(con: duckdb.DuckDBPyConnection, handle: int, sql: str) -> tuple[list[str], list[tuple]]:
    cur = adbc_native_scan(con, handle, sql)
    columns = [d[0] for d in cur.description] if cur.description else []
    return columns, cur.fetchall()


@contextmanager
def _open(profile_name: str | None, cfg: Config | None = None) -> Iterator[tuple[duckdb.DuckDBPyConnection, str]]:
    """Resolve profile + ATTACH + yield (connection, catalog_alias).
    Closes the connection on exit so we never leak DuckDB handles.

    Connections are opened with `interactive=False`: an MCP server can't
    run a browser or device-code login, so profiles on interactive
    grants need a prior `lakesh auth login` in a terminal. Cached tokens
    (incl. refresh) keep working here without any prompting; the
    `AuthRequired` raised otherwise is surfaced to the caller as a JSON
    error by each tool."""
    cfg = cfg or _load_or_raise()
    prof = cfg.get(profile_name)
    con = connect(prof, interactive=False)
    try:
        yield con, catalog_alias(prof)
    finally:
        con.close()


def _rows_as_json(columns: list[str], rows: list[tuple]) -> str:
    return json.dumps(
        [{c: _jsonable(v) for c, v in zip(columns, row)} for row in rows],
        default=str,
    )


def _rows_as_table(columns: list[str], rows: list[tuple]) -> str:
    """Plain-text fixed-width table, easy for LLMs to reason about."""
    widths = [len(c) for c in columns]
    str_rows = [[_stringify(v) for v in row] for row in rows]
    for r in str_rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))
    sep = "  "
    lines = [sep.join(c.ljust(w) for c, w in zip(columns, widths))]
    lines.append(sep.join("-" * w for w in widths))
    for r in str_rows:
        lines.append(sep.join(v.ljust(w) for v, w in zip(r, widths)))
    return "\n".join(lines)


def _jsonable(v: Any) -> Any:
    """Coerce source-native scalars into something `json.dumps` accepts.

    Snowflake hands back `Decimal` for every NUMBER column and
    `datetime` for every timestamp, so without this a credits sum or any
    timestamp aborts the whole tool call with "Object of type Decimal is
    not JSON serializable".
    """
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, decimal.Decimal):
        # int, not float: float silently rounds a NUMBER(38,0) key past
        # 2^53, and those keys are exactly what an agent joins on.
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, _dt.timedelta):
        return v.total_seconds()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


# --------------------------------------------------------------------------
# server

server = FastMCP(
    "lakesh",
    instructions=(
        "SQL access to Iceberg REST catalogs, DuckLake metastores, and any "
        "ADBC source (Snowflake, Postgres, …). Use `list_profiles` to "
        "discover what's configured, `search_objects` to find a schema, "
        "table or column by name when you don't already know where it "
        "lives, `list_namespaces` / `list_tables` / "
        "`describe_table` to navigate, and `query` to run SELECT "
        "statements. For ADBC profiles `query` sends the source's own SQL "
        "dialect straight through, and table names are the source's own "
        "(e.g. SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY). Writes are disabled "
        "unless the operator set LAKESH_MCP_WRITE=1, and a profile marked "
        "read_only refuses them either way."
    ),
)


@server.tool()
def list_profiles() -> str:
    """List all configured catalog profiles. Returns JSON: each entry has
    `name`, `type` (`iceberg-rest`, `ducklake`, or `adbc`), and a
    one-line `description` of where it points."""
    cfg = _load_or_raise()
    out = []
    for name in sorted(cfg.profiles):
        p = cfg.profiles[name]
        if p.type == "iceberg-rest":
            desc = f"Iceberg REST {redact_uri(p.uri)} (warehouse={p.warehouse})"
        elif p.type == "adbc":
            # redact_uri, not p.uri: for a gosnowflake or libpq DSN the
            # URI is the credential, and this string goes to the model.
            desc = (
                f"ADBC {p.driver} {redact_uri(p.uri) or '(options-configured)'} "
                f"(catalog={p.catalog})"
            )
        else:
            desc = f"DuckLake @ {p.data_path} (catalog={p.catalog})"
        out.append({
            "name": name,
            "type": p.type,
            "default": (name == cfg.default),
            "description": desc,
        })
    return json.dumps(out)


def _profile_of(profile: str | None) -> Profile:
    return _load_or_raise().get(profile)


@server.tool()
def list_namespaces(profile: str | None = None) -> str:
    """List schemas / namespaces in the catalog. `profile` defaults to
    the config's `default`. Returns JSON array of names."""
    try:
        prof = _profile_of(profile)
        if _prefer_native(prof):
            with _open_native(profile) as (con, handle, _prof):
                _cols, rows = _native(con, handle, (
                    "SELECT schema_name FROM information_schema.schemata "
                    f"WHERE {_not_system_schemas('schema_name')} "
                    "ORDER BY 1"
                ))
        else:
            with _open(profile) as (con, catalog):
                rows = con.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE catalog_name = ? "
                    "  AND schema_name NOT IN ('main','information_schema','pg_catalog') "
                    "ORDER BY 1",
                    [catalog],
                ).fetchall()
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        return _error(e)
    return json.dumps([r[0] for r in rows])


@server.tool()
def list_tables(profile: str | None = None, namespace: str | None = None) -> str:
    """List tables. Without `namespace`, returns all (namespace, table)
    pairs. With one, scopes to that namespace. JSON array of objects.

    For `adbc` profiles the listing comes from the source's own
    `information_schema`, scoped to the database the profile connects
    to — so `namespace` is the bare schema name, not `DATABASE.SCHEMA`.
    """
    try:
        prof = _profile_of(profile)
        if _prefer_native(prof):
            sql = (
                "SELECT table_schema, table_name FROM information_schema.tables "
                f"WHERE {_not_system_schemas('table_schema')}"
            )
            if namespace:
                sql += f" AND table_schema = {_lit(namespace)}"
            sql += " ORDER BY 1, 2"
            with _open_native(profile) as (con, handle, _prof):
                _cols, rows = _native(con, handle, sql)
        else:
            q = ("SELECT table_schema, table_name FROM information_schema.tables "
                 "WHERE table_catalog = ? "
                 "  AND table_schema NOT IN ('main','information_schema','pg_catalog')")
            with _open(profile) as (con, catalog):
                params: list = [catalog]
                if namespace:
                    q += " AND table_schema = ?"
                    params.append(namespace)
                q += " ORDER BY 1, 2"
                rows = con.execute(q, params).fetchall()
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        return _error(e)
    return json.dumps([{"namespace": r[0], "table": r[1]} for r in rows])


@server.tool()
def describe_table(namespace: str, table: str, profile: str | None = None) -> str:
    """Return a table's columns + types + nullability. JSON array of
    `{column, type, nullable, position}` objects.

    For `adbc` profiles `namespace` is the schema alone (`ACCOUNT_USAGE`,
    not `SNOWFLAKE.ACCOUNT_USAGE`) and the types come back in the
    source's own vocabulary — `TEXT` / `NUMBER` rather than DuckDB's
    `VARCHAR` / `BIGINT`. That is the vocabulary you want when writing
    SQL for that source.
    """
    try:
        prof = _profile_of(profile)
        if _prefer_native(prof):
            with _open_native(profile) as (con, handle, _prof):
                _cols, rows = _native(con, handle, (
                    "SELECT column_name, data_type, is_nullable, ordinal_position "
                    "FROM information_schema.columns "
                    f"WHERE table_schema = {_lit(namespace)} "
                    f"  AND table_name = {_lit(table)} "
                    "ORDER BY ordinal_position"
                ))
        else:
            with _open(profile) as (con, catalog):
                rows = con.execute(
                    "SELECT column_name, data_type, is_nullable, ordinal_position "
                    "FROM information_schema.columns "
                    "WHERE table_catalog = ? AND table_schema = ? AND table_name = ? "
                    "ORDER BY ordinal_position",
                    [catalog, namespace, table],
                ).fetchall()
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        return _error(e)
    return json.dumps([
        {"column": r[0], "type": r[1], "nullable": str(r[2]).upper() in ("YES", "TRUE"),
         "position": int(r[3])}
        for r in rows
    ])


# --------------------------------------------------------------------------
# search_objects
#
# The three legs are UNIONed into ONE statement rather than issued as
# three queries. Against a remote source the cost of a metadata call is
# round-trip latency, so three calls is three times the wall clock on the
# very source where wall clock is the problem.
#
# Every NULL placeholder is CAST: a bare untyped NULL in a UNION branch is
# a type-resolution hazard on Postgres.

_SEARCH_LEGS = ("schema", "table", "column")

# Per matching object, not per matching column: a table with 300 columns
# matching `%id%` would otherwise bury every other result.
_MAX_COLUMNS_PER_OBJECT = 10


def _search_legs(match: str) -> tuple[str, ...]:
    return _SEARCH_LEGS if match == "all" else (match,)


def _search_sql_native(like: str, namespace: str | None, match: str) -> str:
    """One statement for the source's own information_schema."""
    ns_table = f" AND t.table_schema = {_lit(namespace)}" if namespace else ""
    ns_col = f" AND c.table_schema = {_lit(namespace)}" if namespace else ""
    parts = {
        "schema": (
            "SELECT 'schema' AS matched_on, s.schema_name AS object_schema, "
            "CAST(NULL AS VARCHAR) AS object_table, "
            "CAST(NULL AS VARCHAR) AS object_column, "
            "CAST(NULL AS VARCHAR) AS data_type "
            "FROM information_schema.schemata s "
            f"WHERE {_not_system_schemas('s.schema_name')} "
            f"  AND {_ilike('s.schema_name', like)}"
        ),
        # Every leg carries the full alias list, not just the first. A
        # UNION takes its column names from whichever branch leads, and
        # `match`/`namespace` can drop the schema leg — leaving two
        # unaliased CAST(NULL AS VARCHAR) columns that both come back
        # named "varchar", which the adbc_scan binder rejects as
        # duplicates.
        "table": (
            "SELECT 'table' AS matched_on, t.table_schema AS object_schema, "
            "t.table_name AS object_table, "
            "CAST(NULL AS VARCHAR) AS object_column, "
            "CAST(NULL AS VARCHAR) AS data_type "
            "FROM information_schema.tables t "
            f"WHERE {_not_system_schemas('t.table_schema')}{ns_table} "
            f"  AND {_ilike('t.table_name', like)}"
        ),
        "column": (
            "SELECT 'column' AS matched_on, c.table_schema AS object_schema, "
            "c.table_name AS object_table, c.column_name AS object_column, "
            "c.data_type AS data_type "
            "FROM information_schema.columns c "
            f"WHERE {_not_system_schemas('c.table_schema')}{ns_col} "
            f"  AND {_ilike('c.column_name', like)}"
        ),
    }
    # A namespace filter makes the schema leg meaningless — the caller has
    # already told us which schema they mean.
    legs = [
        parts[leg] for leg in _search_legs(match)
        if not (leg == "schema" and namespace)
    ]
    return " UNION ALL ".join(legs)


def _search_sql_duckdb(
    like: str, namespace: str | None, match: str, catalog: str
) -> tuple[str, list]:
    """Same shape against DuckDB's own catalog, but bound rather than
    interpolated — DuckDB takes real parameters, so use them."""
    legs: list[str] = []
    params: list = []
    for leg in _search_legs(match):
        if leg == "schema":
            if namespace:
                continue
            legs.append(
                "SELECT 'schema' AS matched_on, schema_name AS object_schema, "
                "CAST(NULL AS VARCHAR) AS object_table, "
                "CAST(NULL AS VARCHAR) AS object_column, "
                "CAST(NULL AS VARCHAR) AS data_type "
                "FROM information_schema.schemata "
                "WHERE catalog_name = ? "
                f"  AND {_not_system_schemas('schema_name')} "
                f"  AND schema_name ILIKE ? ESCAPE {_lit(_LIKE_ESCAPE)}"
            )
            params += [catalog, like]
        elif leg == "table":
            ns = " AND table_schema = ?" if namespace else ""
            legs.append(
                "SELECT 'table' AS matched_on, table_schema AS object_schema, "
                "table_name AS object_table, "
                "CAST(NULL AS VARCHAR) AS object_column, "
                "CAST(NULL AS VARCHAR) AS data_type "
                "FROM information_schema.tables "
                f"WHERE table_catalog = ?{ns} "
                f"  AND {_not_system_schemas('table_schema')} "
                f"  AND table_name ILIKE ? ESCAPE {_lit(_LIKE_ESCAPE)}"
            )
            params += [catalog] + ([namespace] if namespace else []) + [like]
        else:
            ns = " AND table_schema = ?" if namespace else ""
            legs.append(
                "SELECT 'column' AS matched_on, table_schema AS object_schema, "
                "table_name AS object_table, column_name AS object_column, "
                "data_type AS data_type "
                "FROM information_schema.columns "
                f"WHERE table_catalog = ?{ns} "
                f"  AND {_not_system_schemas('table_schema')} "
                f"  AND column_name ILIKE ? ESCAPE {_lit(_LIKE_ESCAPE)}"
            )
            params += [catalog] + ([namespace] if namespace else []) + [like]
    return " UNION ALL ".join(legs), params


def _wrap_search(body: str, limit: int) -> str:
    """Order and cap in the source. An unordered LIMIT slice is
    nondeterministic, and capping here means a pathological match never
    streams back over the wire.

    `limit + 1` is a sentinel: fetching one more row than we intend to
    return is how we tell "exactly `limit` matches" from "more than
    `limit` matches" without guessing.
    """
    return f"SELECT * FROM ({body}) x ORDER BY 2, 3, 1, 4 LIMIT {int(limit) + 1}"


def _group_matches(rows: list[tuple]) -> list[dict]:
    """Collapse raw match rows into one entry per object.

    A search for "revenue" against a warehouse can match 60 columns
    across 8 tables; the agent wants the 8 tables.
    """
    grouped: dict[tuple, dict] = {}
    order: list[tuple] = []
    for matched_on, schema, table, column, data_type in rows:
        key = (schema, table)
        entry = grouped.get(key)
        if entry is None:
            entry = grouped[key] = {
                "namespace": schema,
                "table": table,
                "_matched": set(),
                "_columns": [],
                "_column_hits": 0,
            }
            order.append(key)
        entry["_matched"].add(matched_on)
        if matched_on == "column" and column is not None:
            entry["_column_hits"] += 1
            if len(entry["_columns"]) < _MAX_COLUMNS_PER_OBJECT:
                entry["_columns"].append({"column": column, "type": data_type})

    out = []
    for key in sorted(order, key=lambda k: (str(k[0] or ""), str(k[1] or ""))):
        entry = grouped[key]
        item = {
            "namespace": entry["namespace"],
            "table": entry["table"],
            "matched_on": sorted(entry["_matched"]),
        }
        if entry["_columns"]:
            item["columns"] = entry["_columns"]
            if entry["_column_hits"] > len(entry["_columns"]):
                item["columns_truncated"] = True
                item["column_match_count"] = entry["_column_hits"]
        out.append(item)
    return out


def _search_one(
    profile_name: str | None, like: str, namespace: str | None,
    match: str, limit: int,
) -> tuple[list[dict], int | None, str]:
    """(results, truncated_at, mode) for a single profile."""
    prof = _profile_of(profile_name)
    if _prefer_native(prof):
        sql = _wrap_search(_search_sql_native(like, namespace, match), limit)
        with _open_native(profile_name) as (con, handle, _prof):
            _cols, rows = _native(con, handle, sql)
        mode = "native"
    else:
        with _open(profile_name) as (con, catalog):
            body, params = _search_sql_duckdb(like, namespace, match, catalog)
            rows = con.execute(_wrap_search(body, limit), params).fetchall()
        mode = "duckdb"

    truncated = limit if len(rows) > limit else None
    return _group_matches(list(rows)[:limit]), truncated, mode


@server.tool()
def search_objects(
    pattern: str,
    profile: str | None = None,
    namespace: str | None = None,
    match: str = "all",
    limit: int = 200,
    all_profiles: bool = False,
) -> str:
    """Find schemas, tables and columns whose name matches `pattern`.

    Use this first when you don't already know where something lives —
    it is the only way to locate an object by name. `list_namespaces` and
    `list_tables` enumerate; this one searches.

    Args:
        pattern: what to look for. A bare word is matched anywhere in the
                 name (`revenue` finds `FCT_REVENUE` and `revenue_usd`).
                 `%` is a wildcard and switches off the implicit
                 wrap, so `revenue%` is a prefix match. `_` is literal.
                 Matching is always case-insensitive.
        profile: which configured profile to search. Omit for the default.
        namespace: restrict to one schema. Worth passing as soon as you
                 know it — it is the main lever for keeping a search on a
                 large warehouse fast.
        match: `all` (default), or `schema` / `table` / `column` to search
                 only that kind of object. `table` is the cheapest.
        limit: max raw matches to consider, capped at 1000.
        all_profiles: search every configured profile instead of one.
                 Costs a connection per profile inside a single call, so
                 leave it off unless you genuinely don't know which
                 source holds the thing.

    Returns JSON with one entry per matching *object* — a table matching
    on six columns is one entry, not six — each carrying `matched_on`
    (any of `schema`, `table`, `column`) and, for column matches, the
    matching `columns` (at most 10, with `columns_truncated` when there
    were more). `truncated_at` counts raw matches, not grouped entries,
    so it can exceed `result_count`.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return json.dumps({"error": "empty pattern"})
    if "\\" in pattern:
        # `_lit` doubles single quotes but not backslashes, and Snowflake
        # reads a backslash inside a string literal as an escape — so a
        # trailing one would swallow the closing quote there and nowhere
        # else. Rejecting is honest; silently mangling is not.
        return json.dumps({
            "error": "pattern may not contain a backslash — escaping differs "
                     "across sources. Use % and _ instead.",
        })
    if match not in ("all",) + _SEARCH_LEGS:
        return json.dumps({
            "error": f"unknown match {match!r} (expected: all, "
                     f"{', '.join(_SEARCH_LEGS)})",
        })
    if match == "schema" and namespace:
        # Contradictory rather than merely redundant, and the honest
        # answer is to say so — silently returning nothing would read as
        # "no such schema".
        return json.dumps({
            "error": "match='schema' searches schema names, so `namespace` "
                     "(which restricts to one schema) cannot apply. Drop one.",
        })
    limit = max(1, min(int(limit), 1000))
    like = _like_pattern(pattern)

    try:
        cfg = _load_or_raise()
        targets = sorted(cfg.profiles) if all_profiles else [profile]
    except (ConfigError, RuntimeError) as e:
        return _error(e)

    results: list[dict] = []
    errors: list[dict] = []
    searched: list[str] = []
    truncated: int | None = None
    mode = None
    for target in targets:
        try:
            found, trunc, mode = _search_one(target, like, namespace, match, limit)
        except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
            # One profile with a cold token must not fail the whole search;
            # report it alongside whatever the others found.
            errors.append({
                "profile": target or "(default)",
                "error": scrub(str(e), _KNOWN_SECRETS),
            })
            continue
        searched.append(target or cfg.default or "(default)")
        if all_profiles:
            for item in found:
                item["profile"] = target
        results.extend(found)
        truncated = trunc if truncated is None else max(truncated, trunc or 0) or None

    out: dict[str, Any] = {
        "pattern": pattern,
        "like": like,
        "searched": list(_search_legs(match)),
        "results": results,
        "result_count": len(results),
        "truncated_at": truncated,
    }
    if all_profiles:
        out["searched_profiles"] = searched
    else:
        out["profile"] = searched[0] if searched else (profile or "(default)")
        out["mode"] = mode
    if errors:
        out["errors"] = errors
    return json.dumps(out, default=str)


@server.tool()
def query(
    sql: str,
    profile: str | None = None,
    limit: int = 1000,
    format: str = "json",
    native: bool | None = None,
) -> str:
    """Run a SQL statement and return its result.

    Args:
        sql: the statement to run. In native mode (the default for
             `adbc` profiles) this is the *source's* SQL, sent verbatim,
             with tables named as that source names them
             (`SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`). Otherwise it is
             DuckDB SQL and tables in the attached catalog are
             addressable as `<catalog>.<ns>.<table>` — `ice` for Iceberg
             REST profiles, the configured `catalog` (default `lake`)
             for DuckLake, or the profile's `catalog` for adbc.
        profile: which configured profile to run against. Omit for default.
        limit: max rows returned. Hard-capped at 10000 to keep the
               response payload tractable for the LLM.
        format: `json` (default) — array of {col: value} objects,
                machine-readable. `table` — fixed-width text, easier for
                the LLM to summarise.
        native: send the statement straight to the source instead of
                through DuckDB. Defaults to true for `adbc` profiles,
                where the DuckDB path is minutes slower and cannot
                express `SHOW`, `QUALIFY`, cross-database references, or
                a bare `count(*)`. Set false to go back through DuckDB —
                the only way to join the source against a local Parquet
                file in one statement. Ignored for non-adbc profiles.
                The mode that actually ran is reported as `mode`.

    Writes (INSERT / UPDATE / DELETE / DDL / DROP) are rejected unless
    the server was started with `LAKESH_MCP_WRITE=1` in its environment.
    A profile with `read_only = true` refuses them regardless: the
    profile is the more specific statement of intent, and native mode
    opens its own ADBC connection that the ATTACH's READ_ONLY flag never
    sees.
    """
    sql = sql.strip().rstrip(";")
    if not sql:
        return json.dumps({"error": "empty query"})

    limit = max(1, min(int(limit), 10_000))
    if format not in ("json", "table"):
        return json.dumps({"error": f"unknown format {format!r}"})

    try:
        prof = _profile_of(profile)
    except (ConfigError, RuntimeError) as e:
        return _error(e)

    if not _is_read_only(sql):
        if prof.read_only:
            return _error(
                f"non-SELECT statement rejected: profile {prof.name!r} is "
                f"marked read_only in the config. That is deliberate and "
                f"LAKESH_MCP_WRITE does not override it."
            )
        if not _writes_enabled():
            return _error(
                "non-SELECT statement rejected. The MCP server is in "
                "read-only mode — set LAKESH_MCP_WRITE=1 in the server's "
                "environment to enable writes."
            )

    use_native = _prefer_native(prof) if native is None else bool(native)
    if use_native and prof.type != "adbc":
        use_native = False

    try:
        if use_native:
            with _open_native(profile) as (con, handle, _prof):
                cur = adbc_native_scan(con, handle, sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(limit)
        else:
            with _open(profile) as (con, _catalog):
                cur = con.execute(sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(limit)
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        return _error(e)

    mode = "native" if use_native else "duckdb"
    if not columns:
        return json.dumps({"ok": True, "rows": 0, "mode": mode, "note": "no result set"})
    if format == "json":
        return json.dumps({
            "columns": columns,
            "rows": [{c: _jsonable(v) for c, v in zip(columns, row)} for row in rows],
            "row_count": len(rows),
            "truncated_at": limit if len(rows) >= limit else None,
            "mode": mode,
        }, default=str)
    return _rows_as_table(columns, rows) + f"\n\n({len(rows)} rows, mode={mode})"


def serve(config_path: Path | None = None) -> None:
    """Run the MCP server on stdio — the entry point `lakesh mcp` calls.

    `config_path` is exported as `$LAKESH_CONFIG` rather than threaded
    through every tool: the tools each load config on their own (they
    are stateless by design), and `default_config_path()` already reads
    that variable first.
    """
    if config_path is not None:
        os.environ["LAKESH_CONFIG"] = str(config_path)
    server.run()


if __name__ == "__main__":
    serve()
