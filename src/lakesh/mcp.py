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

### Safety: nothing credential-shaped goes to the model

`list_profiles` output and every error payload land in an LLM's context.
For Snowflake and Postgres profiles the connection URI *is* the
credential, so both routes go through `redact` first. See that module.

### Performance: per-call connections

Each tool call opens + closes a fresh DuckDB connection. That's slower
than a long-lived REPL session but matches the stateless tool-call model
of MCP, and avoids the iceberg extension's known thread-affinity quirks.
For high-frequency querying, prefer the `lakesh` REPL or `lakesh exec`.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import duckdb
from mcp.server.fastmcp import FastMCP

from .config import Config, ConfigError, default_config_path, load_config
from .duck import catalog_alias, connect
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
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


# --------------------------------------------------------------------------
# server

server = FastMCP(
    "lakesh",
    instructions=(
        "SQL access to Iceberg REST catalogs and DuckLake metastores via "
        "DuckDB. Use `list_profiles` to discover what's configured, "
        "`list_namespaces` / `list_tables` / `describe_table` to navigate, "
        "and `query` to run SELECT statements. Writes are disabled unless "
        "the operator set LAKESH_MCP_WRITE=1."
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


@server.tool()
def list_namespaces(profile: str | None = None) -> str:
    """List schemas / namespaces in the catalog. `profile` defaults to
    the config's `default`. Returns JSON array of names."""
    try:
        with _open(profile) as (con, catalog):
            rows = con.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE catalog_name = ? "
                "  AND schema_name NOT IN ('main','information_schema','pg_catalog') "
                "ORDER BY 1",
                [catalog],
            ).fetchall()
    except AuthRequired as e:
        return _error(e)
    return json.dumps([r[0] for r in rows])


@server.tool()
def list_tables(profile: str | None = None, namespace: str | None = None) -> str:
    """List tables. Without `namespace`, returns all (namespace, table)
    pairs. With one, scopes to that namespace. JSON array of objects."""
    q = ("SELECT table_schema, table_name FROM information_schema.tables "
         "WHERE table_catalog = ? "
         "  AND table_schema NOT IN ('main','information_schema','pg_catalog')")
    try:
        with _open(profile) as (con, catalog):
            params: list = [catalog]
            if namespace:
                q += " AND table_schema = ?"
                params.append(namespace)
            q += " ORDER BY 1, 2"
            rows = con.execute(q, params).fetchall()
    except AuthRequired as e:
        return _error(e)
    return json.dumps([{"namespace": r[0], "table": r[1]} for r in rows])


@server.tool()
def describe_table(namespace: str, table: str, profile: str | None = None) -> str:
    """Return a table's columns + types + nullability. JSON array of
    `{column, type, nullable, position}` objects."""
    try:
        with _open(profile) as (con, catalog):
            rows = con.execute(
                "SELECT column_name, data_type, is_nullable, ordinal_position "
                "FROM information_schema.columns "
                "WHERE table_catalog = ? AND table_schema = ? AND table_name = ? "
                "ORDER BY ordinal_position",
                [catalog, namespace, table],
            ).fetchall()
    except AuthRequired as e:
        return _error(e)
    return json.dumps([
        {"column": r[0], "type": r[1], "nullable": r[2] == "YES", "position": int(r[3])}
        for r in rows
    ])


@server.tool()
def query(
    sql: str,
    profile: str | None = None,
    limit: int = 1000,
    format: str = "json",
) -> str:
    """Run a SQL statement and return its result.

    Args:
        sql: any DuckDB SQL — typically `SELECT ...`. Tables in the
             attached catalog are addressable as `<catalog>.<ns>.<table>`
             where `<catalog>` is `ice` for Iceberg REST profiles or
             the configured `catalog` (default `lake`) for DuckLake.
        profile: which configured profile to run against. Omit for default.
        limit: max rows returned. Hard-capped at 10000 to keep the
               response payload tractable for the LLM.
        format: `json` (default) — array of {col: value} objects,
                machine-readable. `table` — fixed-width text, easier for
                the LLM to summarise.

    Writes (INSERT / UPDATE / DELETE / DDL / DROP) are rejected unless
    the server was started with `LAKESH_MCP_WRITE=1` in its environment.
    """
    sql = sql.strip().rstrip(";")
    if not sql:
        return json.dumps({"error": "empty query"})

    if not _is_read_only(sql) and not _writes_enabled():
        return json.dumps({
            "error": (
                "non-SELECT statement rejected. The MCP server is in "
                "read-only mode — set LAKESH_MCP_WRITE=1 in the server's "
                "environment to enable writes."
            ),
        })

    limit = max(1, min(int(limit), 10_000))
    if format not in ("json", "table"):
        return json.dumps({"error": f"unknown format {format!r}"})

    try:
        with _open(profile) as (con, _catalog):
            try:
                cur = con.execute(sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(limit)
            except duckdb.Error as e:
                return _error(e)
    except AuthRequired as e:
        return _error(e)

    if not columns:
        return json.dumps({"ok": True, "rows": 0, "note": "no result set"})
    if format == "json":
        return json.dumps({
            "columns": columns,
            "rows": [{c: _jsonable(v) for c, v in zip(columns, row)} for row in rows],
            "row_count": len(rows),
            "truncated_at": limit if len(rows) >= limit else None,
        })
    return _rows_as_table(columns, rows) + f"\n\n({len(rows)} rows)"


def serve() -> None:
    """Run the MCP server on stdio — the entry point `lakesh mcp` calls."""
    server.run()


if __name__ == "__main__":
    serve()
