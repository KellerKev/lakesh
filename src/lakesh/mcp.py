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

from .config import (
    DESCRIBE_TABLE_SHAPES,
    Config,
    ConfigError,
    Profile,
    default_config_path,
    load_config,
)
from .duck import (
    QueryTimeout,
    catalog_alias,
    deadline,
)
from . import freshness
from . import backend as _backend
from .oauth import AuthRequired
from . import dialect as _dialect
from . import duck as _duck
from . import guard
from . import mask as _mask
from .output import _stringify  # type: ignore[attr-defined]
from .redact import profile_secrets, redact_uri, scrub


# The write gate lives in `guard`, so there is one place that knows what a
# write looks like. Re-exported here because this module and its tests have
# always referred to it by these names.
_READ_ONLY_LEADING = guard._READ_ONLY_LEADING
_is_read_only = guard.is_read_only


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

# `pg_toast` is owned by the connecting role on Postgres, so the source's
# own information_schema lists it even though DuckDB's attached catalog
# never did — noise an agent has to reason past for nothing.
_SYSTEM_SCHEMAS = ("main", "information_schema", "pg_catalog", "pg_toast")


def _prefer_native(profile: Profile) -> bool:
    return profile.type == "adbc"


def _metadata(sess):
    """The session's catalog-API metadata provider, or None when metadata
    comes from `information_schema` (the SQL path)."""
    getter = getattr(sess, "metadata", None)
    return getter() if getter else None


def _lit(value: str) -> str:
    """Single-quoted SQL literal. `adbc_scan` takes the statement as one
    string, so values interpolated into it are escaped rather than
    bound — this is the escaping."""
    return "'" + str(value).replace("'", "''") + "'"


def _not_system_schemas(column: str) -> str:
    """Exclude the engine's own schemas, case-insensitively.

    Snowflake and SQL Server return schema names upper-cased, so a
    case-sensitive `NOT IN ('information_schema')` does not match
    `INFORMATION_SCHEMA` — which put ~60-70 views per Snowflake database
    back into every listing, the exact noise the native path exists to
    remove. `LOWER()` is ANSI and available on every engine lakesh
    reaches.
    """
    names = ", ".join(_lit(s.lower()) for s in _SYSTEM_SCHEMAS)
    return f"LOWER({column}) NOT IN ({names})"


def _not_system_schemas_for(profile: Profile | None, column: str) -> str:
    """The same exclusion, using the engine's own system-schema list.

    MySQL's `sys`, SQL Server's `sys` and Snowflake's single
    `INFORMATION_SCHEMA` are all different sets; a shared constant was
    the DuckDB+Postgres union and nothing else.
    """
    if profile is None:
        return _not_system_schemas(column)
    names = ", ".join(_lit(n) for n in _dialect.for_profile(profile).system_schemas)
    return f"LOWER({column}) NOT IN ({names})"


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


def _ilike_for(profile: Profile | None, column: str, like: str) -> str:
    """Case-insensitive match in whatever spelling the engine has.

    `ILIKE` exists on Snowflake, Postgres and DuckDB; MySQL, SQL Server,
    Trino, BigQuery and SQLite have no such operator, so they get
    `LOWER(x) LIKE LOWER(y)` instead of a syntax error.
    """
    if profile is None:
        return _ilike(column, like)
    return _dialect.ilike_expr(
        _dialect.for_profile(profile), column, _lit(like), _lit(_LIKE_ESCAPE))


def _ilike(column: str, like: str) -> str:
    """Case-insensitive match for the native path. ILIKE is available on
    Snowflake, Postgres and DuckDB alike, and case-insensitivity is not
    optional here: Snowflake upper-cases unquoted identifiers and
    Postgres lower-cases them, so the same logical name is spelled two
    ways depending on which source you ask."""
    return f"{column} ILIKE {_lit(like)} ESCAPE {_lit(_LIKE_ESCAPE)}"


@contextmanager
def _session(
    profile_name: str | None, timeout_s: float | None = None,
    native: bool | None = None,
) -> "Iterator[_backend.Session]":
    """One `Session` for a profile, whatever backend serves it.

    Replaces the `_open_native`/`_open` split at the tool call sites: a
    tool asks the session whether it `queries_source_directly` to choose
    which SQL to build (source `information_schema` vs DuckDB catalog),
    then runs it the same way regardless. A python-backed profile takes
    the source-direct branch automatically, which is correct — it queries
    the source's own `information_schema`.

    `native` is passed through for adbc profiles (native passthrough vs
    attached catalog); left None, metadata tools get the adbc default
    (native), matching the old `_prefer_native` behaviour.
    """
    prof = _load_or_raise().get(profile_name)
    sess = _backend.open_session(
        prof, interactive=False, timeout_s=timeout_s, native=native)
    try:
        yield sess
    finally:
        sess.close()


# --------------------------------------------------------------------------
# deadlines and paging

# Chosen to sit below the request timeout of a typical MCP client, so a
# slow query comes back as a labelled error the model can act on rather
# than as `MCP error -32001`, which is indistinguishable from a dead
# server.
_DEFAULT_TIMEOUT_S = 120.0


def _effective_timeout(prof: Profile, requested: float | None) -> float | None:
    """Resolve the deadline. A profile's `query_timeout_s` is a ceiling,
    not a default: the same precedent as `read_only` beating
    `LAKESH_MCP_WRITE`, where config is the operator's binding statement
    of intent and a caller may narrow it but never widen it."""
    if requested is not None:
        base: float | None = float(requested)
    else:
        env = os.environ.get("LAKESH_MCP_TIMEOUT_S")
        try:
            base = float(env) if env else _DEFAULT_TIMEOUT_S
        except ValueError:
            base = _DEFAULT_TIMEOUT_S
    if base is not None and base <= 0:
        base = None                       # 0 means "no deadline"
    if prof.query_timeout_s is not None:
        return prof.query_timeout_s if base is None else min(base, prof.query_timeout_s)
    return base


# Statements that are not relations and so cannot be wrapped in a
# derived table. DuckDB actually tolerates `SHOW`, but the same string
# goes to Snowflake where SHOW is not selectable, so it stays on the
# client-side skip path for both.
_UNWRAPPABLE = re.compile(r"^\s*(explain|pragma|show|desc|describe)\b", re.IGNORECASE)

# Only ever used to *add* a warning, never to reject or rewrite — so it
# does not matter that it cannot tell a top-level ORDER BY from one
# inside a CTE or a window frame.
_HAS_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)

_MAX_OFFSET = 100_000


def _is_unwrappable(sql: str, prof: Profile | None = None) -> bool:
    """Whether `sql` is a statement that cannot sit in a derived table.

    Per dialect, because the set differs: Snowflake adds `get`, `put`,
    `remove` and `execute` to the common ones. Falls back to the shared
    regex when no profile is in hand.
    """
    head = sql.strip().lstrip("(").split(None, 1)
    if not head:
        return False
    if prof is None:
        return bool(_UNWRAPPABLE.match(sql.strip().lstrip("(")))
    return head[0].lower() in _dialect.for_profile(prof).unwrappable


def _paginate(sql: str, limit: int, offset: int,
              prof: Profile | None = None) -> tuple[str, bool]:
    """(statement, wrapped). `wrapped` false means the caller has to skip
    rows client-side, which transfers them and throws them away.

    At offset 0 the statement is returned byte-for-byte unchanged, so the
    common path is exactly what it was before pagination existed.
    """
    if offset <= 0 or _is_unwrappable(sql, prof):
        return sql, False
    # `AS _lakesh_page` is required by Postgres < 16. The trailing `;` has
    # already been stripped by the caller, which is load-bearing: a
    # semicolon inside the subquery is a syntax error.
    #
    # LIMIT is `limit + 1`, matching the caller's fetch: the extra row is
    # the sentinel that makes `has_more` exact. Capping the SQL at
    # `limit` instead makes every full page look like the last one.
    return (
        f"SELECT * FROM ({sql}) AS _lakesh_page "
        f"LIMIT {int(limit) + 1} OFFSET {int(offset)}",
        True,
    )


# --------------------------------------------------------------------------
# pre-flight estimates
#
# What can honestly be produced differs per source, and the differences
# are not small:
#
#   * DuckDB: `EXPLAIN (FORMAT json)` carries an optimizer cardinality on
#     the root node. No byte figure anywhere in the plan.
#   * Postgres over ADBC: no plan at all. The driver wraps every
#     statement in `COPY (…) TO STDOUT`, and Postgres rejects
#     `COPY (EXPLAIN …)` outright. Same for SHOW and SET.
#   * Snowflake over ADBC: `EXPLAIN USING JSON` returns a plan, but its
#     shape is unverified here, so it is passed through verbatim rather
#     than parsed into numbers we cannot vouch for.
#
# Hence the hard rule below: emit `estimated_rows` only when a number was
# genuinely extracted. Never null, never zero, never a regex over a
# box-drawing tree — a model that reads `estimated_rows: 0` concludes the
# query is free.

_ESTIMATE_MODES = ("plan", "count")

_POSTGRES_NO_EXPLAIN = (
    "the postgresql ADBC driver wraps every statement in COPY (...) TO "
    "STDOUT, which rejects EXPLAIN. Re-run with estimate=\"count\" for an "
    "exact count, or native=false to plan through DuckDB."
)


def _estimate_mode(estimate: bool | str) -> str | None:
    """None when no estimate was asked for."""
    if estimate is True:
        return "plan"
    if estimate is False or estimate is None:
        return None
    return str(estimate).lower()


def _duckdb_cardinality(plan_rows: list[tuple]) -> int | None:
    """Pull `Estimated Cardinality` off the root node of DuckDB's JSON
    plan. Returns None rather than a guess if the shape has moved —
    "Estimated Cardinality" is an unpinned DuckDB internal."""
    for row in plan_rows:
        for cell in row:
            if not isinstance(cell, str) or "Estimated Cardinality" not in cell:
                continue
            try:
                node = json.loads(cell)
            except (TypeError, ValueError):
                continue
            # DuckDB emits the plan as a list holding the root node.
            if isinstance(node, list):
                node = node[0] if node else None
            while isinstance(node, dict):
                info = node.get("extra_info") or {}
                value = info.get("Estimated Cardinality")
                if value is not None:
                    try:
                        return int(str(value))
                    except (TypeError, ValueError):
                        return None
                children = node.get("children") or []
                node = children[0] if children else None
    return None


def _count_probe(sql: str) -> str:
    return f"SELECT count(*) AS n FROM ({sql}) AS _lakesh_est"


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
        "read_only refuses them either way. `stage_upload` puts a local "
        "file where the source can read it, limited to directories the "
        "operator allow-listed. Call `session_status` to see "
        "what this session is allowed to do, and `set_read_only` to give "
        "up write access for the rest of it."
    ),
)


@server.tool()
def list_profiles() -> str:
    """List all configured catalog profiles. Returns JSON: each entry has
    `name`, `type` (`iceberg-rest`, `ducklake`, `adbc`, or `python`), and a
    one-line `description` of where it points. Snowflake profiles also carry
    an `agent_activation` flag (whether Cortex-Code agent-masking can be /
    is enabled); call `session_status` for the full explanation."""
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
        entry = {
            "name": name,
            "type": p.type,
            "default": (name == cfg.default),
            "description": desc,
        }
        if p.status != "unknown":
            entry["status"] = p.status
        if p.max_staleness_seconds:
            entry["max_staleness_seconds"] = p.max_staleness_seconds
        if _is_snowflake_profile(p):
            # Let an agent enumerating profiles see the option up front —
            # session_status(profile) returns the full explanation.
            enabled = _backend.agent_activation_opted_in(p)
            entry["agent_activation"] = {
                "available": True,
                "enabled": bool(enabled),
                "note": (
                    "Snowflake agent-masking can be activated for MCP "
                    "sessions (impersonates Cortex Code); "
                    + ("currently ON. " if enabled else "currently OFF. ")
                    + "Call session_status for what it means and how to "
                    "enable it."
                ),
            }
        out.append(entry)
    return json.dumps(out)


@server.tool()
def set_read_only() -> str:
    """Restrict this session to reads for the rest of its life.

    Takes no argument on purpose: there is no way to turn it back off.
    Once set, every `query` call in this session refuses writes, and the
    only way to regain write access is to start a new session. Call it
    when you are about to explore a source you do not intend to modify —
    it costs nothing and removes a whole class of accident.

    Idempotent. Returns the effective restriction and where it came from.
    """
    guard.SESSION.narrow("set_read_only tool")
    restriction = guard.SESSION.effective()
    return json.dumps({
        "ok": True,
        "restriction": restriction.as_dict(),
        "note": restriction.describe(),
    })


@server.tool()
def session_status(profile: str = "") -> str:
    """What this session is currently allowed to do.

    Worth checking before assuming a failure was the source's fault: a
    refused write may be a restriction the operator set, in which case
    retrying will not help.

    Name a `profile` to additionally ask that source who it thinks you
    are — on Snowflake that includes `agent_activated`, which reports
    whether the account will apply agent-specific masking policies to
    this connection. Expect `FALSE` unless an admin has set up an OAuth
    security integration with `IS_AGENTIC = TRUE`: lakesh labels its
    sessions but cannot make itself agent-activated, and believing
    otherwise means believing a policy applies when it does not. Omit
    `profile` and no connection is opened.
    """
    restriction = guard.SESSION.effective()
    out = {
        "restriction": restriction.as_dict(),
        "summary": restriction.describe(),
        "writes_enabled_by_env": _writes_enabled(),
        "masking": {"mode": _SESSION_MASK["mode"], "relaxable": False},
        "sandbox": {
            # A sandbox you believe is on but isn't is worse than none,
            # so report the skip reason rather than just a boolean.
            "local_files_blocked": restriction.read_only and not _duck.ALLOW_LOCAL_FILES,
            "skipped_because": _duck.LAST_SANDBOX_SKIP,
        },
        "caller": _duck.CALLER,
    }
    if profile:
        out["source_session"] = _source_session(profile)
    return json.dumps(out, default=str)


def _is_snowflake_profile(p: Profile) -> bool:
    """A profile that reaches Snowflake, either backend — python snowflake
    or the ADBC snowflake driver. Agent activation is offered on both, but
    only the python backend can actually deliver it (the ADBC driver
    mangles the application name)."""
    return (p.type == "python" and p.backend == "snowflake") or \
           (p.type == "adbc" and p.driver == "snowflake")


def _agent_activation_hint(prof: Profile, activated: bool | None) -> dict:
    """Tell the caller they have the option to make a Snowflake session
    agent-active — and, plainly, what enabling it means.

    This is the "let the user know they have the option" surface: it
    appears in `session_status` for any Snowflake profile, whether or not
    it is currently activated.
    """
    opted_in = _backend.agent_activation_opted_in(prof)
    is_python_sf = prof.type == "python" and prof.backend == "snowflake"
    hint: dict = {
        "available": True,
        "enabled": bool(opted_in),
        "currently_active": activated,
        "what_it_does": (
            "sends application=cortex_code_cli, which makes Snowflake treat "
            "the session as its own Cortex Code client "
            "(IS_AGENT_ACTIVATED=TRUE, AGENT_TYPE=CORTEX_LITE_AGENT) so "
            "agent-masking policies apply. This is IMPERSONATION of a "
            "first-party client on an undocumented allowlist — it lands in "
            "the account's audit trail as Cortex Code, not lakesh."
        ),
        "honest_alternative": (
            "for honest activation (AGENT_TYPE=EXTERNAL_AGENT) have an admin "
            "create an OAuth integration with IS_AGENTIC=TRUE, or a "
            "SERVICE_AGENT user — no client string can do it honestly."
        ),
    }
    if not is_python_sf:
        hint["requires"] = (
            "the python snowflake backend (type=\"python\", "
            "backend=\"snowflake\") — the ADBC driver mangles the "
            "application name so it cannot activate. Switch this profile to "
            "the python backend to use it."
        )
    if not opted_in:
        hint["how_to_enable"] = (
            "set agent_activation = true on the profile, or "
            "LAKESH_SNOWFLAKE_AGENT_ACTIVATION=1 for the whole MCP server."
        )
    return hint


def _source_session(profile: str) -> dict:
    """What the named source reports about a session lakesh opens to it.

    Errors are reported in-band rather than raised: this is a diagnostic,
    and "I could not ask" is a useful answer where a failed tool call is
    not.
    """
    try:
        prof = _profile_of(profile)
    except Exception as e:
        return {"error": str(e)}
    try:
        sess = _backend.open_session(prof, interactive=False)
    except Exception as e:
        # A failed connect quotes the statement with the DSN inline.
        return {"error": scrub(str(e), profile_secrets(prof))}
    try:
        reported = sess.probe()
    finally:
        sess.close()

    is_snowflake = _dialect.for_profile(prof).name == "snowflake"
    if reported is None:
        out: dict = {
            "supported": False,
            "reason": f"{_dialect.for_profile(prof).name} cannot report on "
                      f"its own session over this path",
        }
        if is_snowflake:
            out["agent_activation"] = _agent_activation_hint(prof, None)
        return out
    out = {"supported": True, "reported": reported}
    if "agent_activated" in reported:
        activated = str(reported["agent_activated"]).upper() == "TRUE"
        out["agent_activated"] = activated
        if is_snowflake:
            out["agent_activation"] = _agent_activation_hint(prof, activated)
    if "lakesh_client" in reported:
        # The label lakesh set, echoed back from the source rather than
        # from local state — the round trip is the only thing that proves
        # the stamp actually stuck.
        out["stamp_confirmed"] = True
        out["stamp_is_client_asserted"] = True
    attested = (_duck.LAST_STAMP or {}).get("attested")
    if attested:
        # Signed, so unlike the stamp above a policy can act on it — as
        # far as it trusts the key, which is the caveat that belongs
        # wherever this is reported.
        out["attestation"] = dict(attested, client_asserted=False)
        out["stamp_is_client_asserted"] = False
    return out


_SESSION_MASK: dict[str, str] = {"mode": "off"}


@server.tool()
def set_masking() -> str:
    """Mask recognisable PII in every result for the rest of this session.

    Takes no argument, and cannot be turned off — including down to
    `audit`, which returns unmasked rows. Read `query`'s note on what
    masking does and does not protect against before relying on it.
    """
    _SESSION_MASK["mode"] = "mask"
    return json.dumps({
        "ok": True, "mode": "mask", "relaxable": False,
        "note": ("Masking applies as values are rendered. It is not access "
                 "control — SQL that transforms a value before lakesh sees "
                 "it defeats it."),
    })


@server.tool()
def stage_upload(local_path: str, target: str, profile: str | None = None) -> str:
    """Upload a local file to the source's staging area.

    Snowflake internal stages are supported; other engines report that
    they cannot, rather than emitting a statement they do not have.

    `local_path` must sit inside one of the profile's configured
    `upload_roots`, and symlinks are resolved before that check. If the
    operator has configured no roots, uploads are refused outright.
    That fence is **not** the filesystem sandbox and does not depend on
    it: the sandbox binds DuckDB's engine, while a stage upload is read
    by the source's own driver, outside it.

    The result is verified by listing the target afterwards, because a
    PUT returns no rows over this path and so cannot report its own
    success.
    """
    from . import staging

    try:
        prof = _profile_of(profile)
        restriction = guard.SESSION.effective(prof)
        if restriction.read_only:
            return json.dumps(guard.refusal(restriction, "PUT"))
        return json.dumps(staging.upload(prof, local_path, target), default=str)
    except staging.StagingError as e:
        return json.dumps({"error": str(e), "error_type": "staging_refused"})
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        return _error(e)


@server.tool()
def stage_load(
    table: str, target: str, profile: str | None = None,
    file_format: str | None = None, create: bool = False,
) -> str:
    """Load a staged file into an existing table.

    Loads FROM a stage INTO a table; the reverse direction (unloading a
    table out to a stage) is deliberately not supported. Refused in a
    read-only session, since it writes.

    `create=True` creates the table first from the staged file's inferred
    schema. It is off by default because a mistyped table name would then
    quietly create a new table rather than failing, and inferred types are
    usually wrong in ways that surface much later. It also needs a named
    file format configured, because INFER_SCHEMA does not accept an
    inline one.

    The row count is reported from a before/after count rather than the
    statement's own output, which over this path may be empty.
    """
    from . import staging

    try:
        prof = _profile_of(profile)
        restriction = guard.SESSION.effective(prof)
        if restriction.read_only:
            return json.dumps(guard.refusal(restriction, "COPY"))
        return json.dumps(
            staging.load(prof, table, target,
                         file_format=file_format or "", create=create),
            default=str)
    except staging.StagingError as e:
        return json.dumps({"error": str(e), "error_type": "staging_refused"})
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        return _error(e)


@server.tool()
def stage_list(target: str, profile: str | None = None) -> str:
    """List what is staged at a target, e.g. `@~/exports`."""
    from . import staging

    try:
        prof = _profile_of(profile)
        return json.dumps({"target": target,
                           "files": staging.listing(prof, target)}, default=str)
    except staging.StagingError as e:
        return json.dumps({"error": str(e), "error_type": "staging_refused"})
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        return _error(e)


@server.tool()
def stage_remove(target: str, profile: str | None = None) -> str:
    """Remove staged files at a target. Refused in a read-only session."""
    from . import staging

    try:
        prof = _profile_of(profile)
        restriction = guard.SESSION.effective(prof)
        if restriction.read_only:
            return json.dumps(guard.refusal(restriction, "REMOVE"))
        return json.dumps(staging.remove(prof, target), default=str)
    except staging.StagingError as e:
        return json.dumps({"error": str(e), "error_type": "staging_refused"})
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        return _error(e)


def _profile_of(profile: str | None) -> Profile:
    return _load_or_raise().get(profile)


def _describe_shape(cfg: Config, requested: str | None) -> str:
    """Resolve `describe_table`'s output shape.

    Precedence is caller, then environment, then config file. Unlike the
    query deadline — where the profile is a ceiling because it is a
    safety property — this is a presentation preference, so the caller
    who knows what it wants to parse gets the last word.
    """
    if requested:
        return str(requested).lower()
    env = os.environ.get("LAKESH_MCP_DESCRIBE_SHAPE")
    if env:
        return env.strip().lower()
    return cfg.describe_table_shape


@server.tool()
def list_namespaces(profile: str | None = None) -> str:
    """List schemas / namespaces in the catalog. `profile` defaults to
    the config's `default`. Returns JSON array of names."""
    try:
        prof = _profile_of(profile)
        with _session(profile) as sess:
            md = _metadata(sess)
            if md is not None:
                rows = [(n,) for n in md.namespaces()]
            elif sess.queries_source_directly:
                _cols, rows = sess.run(
                    "SELECT schema_name FROM information_schema.schemata "
                    f"WHERE {_not_system_schemas_for(prof, 'schema_name')} "
                    "ORDER BY 1"
                )
            else:
                rows = sess.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE catalog_name = ? "
                    "  AND schema_name NOT IN ('main','information_schema','pg_catalog') "
                    "ORDER BY 1",
                    [catalog_alias(sess.profile)],
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
        dialect = freshness.source_dialect(prof)
        with _session(profile) as sess:
            md = _metadata(sess)
            if md is not None:
                # A catalog API with no information_schema (Iceberg):
                # namespaces/tables come from the catalog, freshness does
                # not (the source cannot report it here).
                rows = list(md.tables(namespace))
            elif sess.queries_source_directly:
                # Freshness columns ride on the statement already being
                # sent, so on Snowflake this costs zero extra round trips.
                extra = freshness.listing_columns(dialect)
                sql = (
                    "SELECT t.table_schema, t.table_name"
                    + (f", {extra}" if extra else "")
                    + " FROM information_schema.tables t"
                    + freshness.listing_join(dialect)
                    + f" WHERE {_not_system_schemas_for(prof, 't.table_schema')}"
                )
                if namespace:
                    sql += f" AND t.table_schema = {_lit(namespace)}"
                sql += " ORDER BY 1, 2"
                _cols, rows = sess.run(sql)
            else:
                q = ("SELECT table_schema, table_name FROM information_schema.tables "
                     "WHERE table_catalog = ? "
                     "  AND table_schema NOT IN ('main','information_schema','pg_catalog')")
                params: list = [catalog_alias(sess.profile)]
                if namespace:
                    q += " AND table_schema = ?"
                    params.append(namespace)
                q += " ORDER BY 1, 2"
                rows = sess.execute(q, params).fetchall()
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        return _error(e)

    out = []
    for row in rows:
        entry = {"namespace": row[0], "table": row[1]}
        entry.update(freshness.describe(
            prof, prof.annotation_for(row[0], row[1]),
            last_modified=row[2] if len(row) > 2 else None,
            row_count=row[3] if len(row) > 3 else None,
            size_bytes=row[4] if len(row) > 4 else None,
            dialect=dialect,
        ))
        out.append(entry)
    return json.dumps(out, default=str)


@server.tool()
def describe_table(
    namespace: str, table: str, profile: str | None = None,
    shape: str | None = None,
) -> str:
    """Describe one table: its columns, and whether it is the one you
    should be using.

    Returns an object with `columns` (each `{column, type, nullable,
    position}`) plus, when the operator has said anything about this
    table, `status` (`canonical` / `deprecated`), `note`,
    `superseded_by`, and a `freshness` block. Check `status` before
    building a query on it — a deprecated table will still answer
    perfectly good SQL with the wrong numbers.

    `shape="array"` returns just the bare list of columns instead, which
    is what this tool returned before the envelope existed. It is there
    for callers that already parse that shape; note that it has nowhere
    to report that a table is deprecated or stale, so you lose the
    warning. The default can be set once with `describe_table_shape` in
    the config file or `LAKESH_MCP_DESCRIBE_SHAPE` in the server's
    environment.

    `freshness.state` is one of `fresh`, `stale`, `unrated` (nobody set a
    threshold) or `unknown` (**the source cannot report a
    last-modified time at all** — notably Postgres, which has no honest
    signal for it). `unknown` is not `fresh`; if you need to know, ask.

    For `adbc` profiles `namespace` is the schema alone (`ACCOUNT_USAGE`,
    not `SNOWFLAKE.ACCOUNT_USAGE`) and the types come back in the
    source's own vocabulary — `TEXT` / `NUMBER` rather than DuckDB's
    `VARCHAR` / `BIGINT`. That is the vocabulary you want when writing
    SQL for that source.
    """
    observed: tuple = ()
    try:
        cfg = _load_or_raise()
        resolved_shape = _describe_shape(cfg, shape)
        if resolved_shape not in DESCRIBE_TABLE_SHAPES:
            return json.dumps({
                "error": f"unknown shape {resolved_shape!r} "
                         f"(supported: {', '.join(DESCRIBE_TABLE_SHAPES)})",
            })
        prof = cfg.get(profile)
        dialect = freshness.source_dialect(prof)
        # In `array` shape there is nowhere to put status or freshness,
        # so don't pay for the extra round trip that fetches them.
        want_freshness = resolved_shape == "object"
        with _session(profile) as sess:
            md = _metadata(sess)
            if md is not None:
                # Columns from the catalog API; `is_nullable` as the
                # 'YES'/'NO' the downstream shaping expects.
                rows = [
                    (name, typ, "YES" if nullable else "NO", pos)
                    for name, typ, nullable, pos in md.columns(namespace, table)
                ]
            elif sess.queries_source_directly:
                _cols, rows = sess.run(
                    "SELECT column_name, data_type, is_nullable, ordinal_position "
                    "FROM information_schema.columns "
                    f"WHERE table_schema = {_lit(namespace)} "
                    f"  AND table_name = {_lit(table)} "
                    "ORDER BY ordinal_position"
                )
                # A second statement, but on the already-open session —
                # one more round trip, not a reconnect, and nowhere near
                # the eager-catalog path that cost minutes.
                extra = freshness.listing_columns(dialect) if want_freshness else ""
                if extra:
                    _c, found = sess.run(
                        f"SELECT {extra} FROM information_schema.tables t"
                        + freshness.listing_join(dialect)
                        + f" WHERE t.table_schema = {_lit(namespace)}"
                        f"   AND t.table_name = {_lit(table)}"
                    )
                    observed = tuple(found[0]) if found else ()
            else:
                rows = sess.execute(
                    "SELECT column_name, data_type, is_nullable, ordinal_position "
                    "FROM information_schema.columns "
                    "WHERE table_catalog = ? AND table_schema = ? AND table_name = ? "
                    "ORDER BY ordinal_position",
                    [catalog_alias(sess.profile), namespace, table],
                ).fetchall()
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        return _error(e)

    columns = [
        {"column": r[0], "type": r[1], "nullable": str(r[2]).upper() in ("YES", "TRUE"),
         "position": int(r[3])}
        for r in rows
    ]
    if resolved_shape == "array":
        return json.dumps(columns, default=str)

    out: dict[str, Any] = {"namespace": namespace, "table": table}
    out.update(freshness.describe(
        prof, prof.annotation_for(namespace, table),
        last_modified=observed[0] if len(observed) > 0 else None,
        row_count=observed[1] if len(observed) > 1 else None,
        size_bytes=observed[2] if len(observed) > 2 else None,
        dialect=dialect,
    ))
    out["columns"] = columns
    return json.dumps(out, default=str)


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


def _search_sql_native(like: str, namespace: str | None, match: str,
                       prof: Profile | None = None) -> str:
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
            f"  AND {_ilike_for(prof, 's.schema_name', like)}"
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
            f"WHERE {_not_system_schemas_for(prof, 't.table_schema')}{ns_table} "
            f"  AND {_ilike_for(prof, 't.table_name', like)}"
        ),
        "column": (
            "SELECT 'column' AS matched_on, c.table_schema AS object_schema, "
            "c.table_name AS object_table, c.column_name AS object_column, "
            "c.data_type AS data_type "
            "FROM information_schema.columns c "
            f"WHERE {_not_system_schemas('c.table_schema')}{ns_col} "
            f"  AND {_ilike_for(prof, 'c.column_name', like)}"
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
    like: str, namespace: str | None, match: str, catalog: str,
    prof: Profile | None = None,
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
                f"  AND {_not_system_schemas_for(prof, 'schema_name')} "
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
    with _session(profile_name) as sess:
        md = _metadata(sess)
        if md is not None:
            rows = _search_via_metadata(md, like, namespace, match)
            mode = "catalog"
        elif sess.queries_source_directly:
            sql = _wrap_search(_search_sql_native(like, namespace, match, prof), limit)
            _cols, rows = sess.run(sql)
            mode = "native"
        else:
            body, params = _search_sql_duckdb(
                like, namespace, match, catalog_alias(sess.profile), prof)
            rows = sess.execute(_wrap_search(body, limit), params).fetchall()
            mode = "duckdb"

    truncated = limit if len(rows) > limit else None
    return _group_matches(list(rows)[:limit]), truncated, mode


def _like_matcher(like: str):
    """A predicate for a SQL ILIKE pattern, evaluated in Python for the
    catalog-API path (an Iceberg catalog has no SQL to run the ILIKE in).

    Handles `%`/`_` wildcards and the `!` escape `search_objects` uses.
    """
    import re as _re

    out, i, n = [], 0, len(like)
    while i < n:
        c = like[i]
        if c == "!" and i + 1 < n:      # escaped literal
            out.append(_re.escape(like[i + 1])); i += 2; continue
        if c == "%":
            out.append(".*")
        elif c == "_":
            out.append(".")
        else:
            out.append(_re.escape(c))
        i += 1
    rx = _re.compile("^" + "".join(out) + "$", _re.IGNORECASE)
    return lambda name: bool(rx.match(name or ""))


def _search_via_metadata(md, like: str, namespace: str | None, match: str) -> list[tuple]:
    """Search rows `(matched_on, schema, table, column, type)` from a
    catalog API, in the shape `_group_matches` expects."""
    matches = _like_matcher(like)
    want = lambda kind: match in ("all", kind)
    rows: list[tuple] = []
    for ns, tbl in md.tables(namespace):
        if want("schema") and matches(ns):
            rows.append(("schema", ns, tbl, None, None))
        if want("table") and matches(tbl):
            rows.append(("table", ns, tbl, None, None))
        if want("column"):
            for cname, ctype, _nullable, _pos in md.columns(ns, tbl):
                if matches(cname):
                    rows.append(("column", ns, tbl, cname, ctype))
    return rows


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
        # Annotations only — config lookups, no extra query. Freshness
        # here would mean a second round trip per search.
        prof = cfg.get(target)
        for item in found:
            if all_profiles:
                item["profile"] = target
            if item["table"]:
                ann = prof.annotation_for(item["namespace"], item["table"])
                status = freshness.status_for(prof, ann)
                if status != "unknown":
                    item["status"] = status
                if ann is not None and ann.superseded_by:
                    item["superseded_by"] = ann.superseded_by
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


def _estimate_query(
    profile_name: str | None, prof: Profile, sql: str, mode: str,
    want: str, timeout: float | None, policy: "_mask.Policy | None" = None,
) -> str:
    """Size a statement up without running it (or, for `count`, without
    returning its rows)."""
    policy = policy or _mask.Policy("off")
    out: dict[str, Any] = {"estimate": True, "mode": mode}

    if want == "count":
        if _is_unwrappable(sql, prof):
            # A count probe wraps the statement in a derived table, which
            # a non-relational statement cannot survive. Saying so beats
            # emitting a syntax error.
            out["method"] = "unavailable"
            out["reason"] = (
                "this statement is not a relation, so it cannot be wrapped "
                "in a count probe. Run it and count the rows instead."
            )
            return json.dumps(out)
        probe = _count_probe(sql)
        try:
            with _session(profile_name, timeout) as sess:
                with deadline(sess, timeout):
                    _cols, rows = sess.run(probe)
        except QueryTimeout as e:
            return json.dumps({
                "error": scrub(str(e), _KNOWN_SECRETS), "error_type": "timeout",
                "timeout_s": e.seconds, "elapsed_s": round(e.elapsed, 1),
                "enforced": "hard" if e.hard else "best_effort", "mode": mode,
            })
        except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
            return _error(e)
        out["method"] = "count"
        out["exact_rows"] = _jsonable(rows[0][0]) if rows and rows[0] else None
        out["note"] = ("exact, but the scan ran on the source — this was not "
                       "free on a metered warehouse")
        return json.dumps(out, default=str)

    # want == "plan"
    #
    # Branch on the DIALECT (via the session), not on which path we happen
    # to be on. `EXPLAIN USING JSON` is Snowflake's spelling and nothing
    # else's; a source-direct session (native ADBC or a Python driver)
    # uses the dialect's spelling, an attached DuckDB one uses DuckDB's.
    used_duckdb_explain = False
    try:
        with _session(profile_name, timeout) as sess:
            if sess.queries_source_directly:
                source = _dialect.for_profile(prof)
                explain = _dialect.explain_sql(source, sql)
                if explain is None:
                    out["method"] = "unavailable"
                    out["reason"] = (
                        _POSTGRES_NO_EXPLAIN if source.name == "postgres" else
                        f"the {source.name} profile has no plan available over "
                        f"the native path. Re-run with estimate=\"count\" for "
                        f"an exact count, or native=false to plan through DuckDB."
                    )
                    return json.dumps(out)
            else:
                explain = f"EXPLAIN (FORMAT json) {sql}"
                used_duckdb_explain = True
            with deadline(sess, timeout):
                _cols, rows = sess.run(explain)
    except QueryTimeout as e:
        return json.dumps({
            "error": scrub(str(e), _KNOWN_SECRETS), "error_type": "timeout",
            "timeout_s": e.seconds, "elapsed_s": round(e.elapsed, 1),
            "enforced": "hard" if e.hard else "best_effort", "mode": mode,
        })
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        out["method"] = "unavailable"
        out["reason"] = scrub(str(e), _KNOWN_SECRETS)
        out["hint"] = ("this source could not produce a plan. Try "
                       "estimate=\"count\" for an exact count.")
        return json.dumps(out)

    out["method"] = "explain"
    # Plans embed filter literals verbatim, so `EXPLAIN … WHERE
    # email='alice@corp.com'` ships the value without this.
    out["plan"] = _mask.mask_text(policy, "\n".join(
        str(cell) for row in rows for cell in row if cell is not None
    ))
    if used_duckdb_explain:
        # Parsed only where the shape is known and tested — DuckDB's
        # `EXPLAIN (FORMAT json)`. A source's own plan is passed through
        # verbatim rather than guessed at.
        rows_estimate = _duckdb_cardinality(rows)
        if rows_estimate is not None:
            out["estimated_rows"] = rows_estimate
            out["note"] = "optimizer estimate, not a count"
    return json.dumps(out, default=str)


@server.tool()
def query(
    sql: str,
    profile: str | None = None,
    limit: int = 1000,
    offset: int = 0,
    format: str = "json",
    native: bool | None = None,
    timeout_s: float | None = None,
    estimate: bool | str = False,
    read_only: bool = False,
    mask: str | None = None,
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
        offset: skip this many rows. Each page is a SEPARATE EXECUTION of
                the statement — there is no cursor held between calls —
                so paging a warehouse query N times costs N runs of it.
                Fine for a second page, expensive for a sweep. Pagination
                without a top-level `ORDER BY` is not stable across
                pages; the response says so in `warnings` when it spots
                one missing.
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
        timeout_s: deadline in seconds; 120 by default, 0 to disable. A
                profile's `query_timeout_s` is a ceiling this can lower
                but not raise. How well it is enforced differs by path
                and the response says which applied via `enforced`:
                `hard` on the DuckDB path, where the query is genuinely
                aborted on the deadline; `best_effort` in native mode,
                where a statement blocked waiting on the source cannot be
                interrupted until the driver returns control. In native
                mode lakesh also asks the source to enforce its own
                statement timeout where the driver supports it — on
                Postgres that lands at roughly 2x the requested seconds,
                because the driver applies it once on prepare and once on
                fetch.
        estimate: size the query up INSTEAD of running it — worth doing
                before an expensive one, since on a warehouse execution
                is money. `true` asks the planner: an `estimated_rows`
                figure where one can be had, and the plan verbatim.
                `"count"` instead runs `count(*)` over the statement,
                which is exact but does execute the scan server-side, so
                it is opt-in rather than a silent fallback.
                Not every source can answer: the Postgres ADBC driver
                cannot EXPLAIN at all, and says so with a `reason`
                telling you what to try instead. `estimated_rows` is
                present only when a real number was extracted — never as
                a null or a zero.

        read_only: refuse writes for this call. Narrows only — it cannot
                re-enable writes that policy already forbids, and it does
                NOT latch: use `set_read_only()` to restrict the whole
                session. When any restriction is in force the write check
                is the stronger one, which also catches a write smuggled
                inside a CTE or after a `;`.
        mask: `"mask"` to replace recognisable PII in the results, or
                `"audit"` to report what would be masked without masking
                it. Narrows only — it cannot weaken masking the operator
                configured. Masking applies as values are RENDERED and is
                not access control: `substr(email,1,5)`, `count(*) WHERE
                email LIKE 'a%'`, `md5(ssn)` and `ORDER BY email` all
                defeat it. Use it to avoid pulling PII you don't need into
                context, not to enforce that you cannot read it.

    Writes (INSERT / UPDATE / DELETE / DDL / DROP) are rejected unless
    the server was started with `LAKESH_MCP_WRITE=1` in its environment.
    A profile with `read_only = true` refuses them regardless: the
    profile is the more specific statement of intent, and native mode
    opens its own ADBC connection that the ATTACH's READ_ONLY flag never
    sees. A rejection says which layer imposed the restriction, because
    one that cannot be relaxed from here is worth knowing about before
    retrying.
    """
    sql = sql.strip().rstrip(";")
    if not sql:
        return json.dumps({"error": "empty query"})

    limit = max(1, min(int(limit), 10_000))
    offset = max(0, int(offset))
    if offset > _MAX_OFFSET:
        return json.dumps({
            "error": f"offset {offset} exceeds the {_MAX_OFFSET} cap. Each "
                     f"page re-executes the statement, so walking a source "
                     f"page by page is not the right tool — narrow the query "
                     f"or aggregate instead.",
        })
    if format not in ("json", "table"):
        return json.dumps({"error": f"unknown format {format!r}"})

    try:
        prof = _profile_of(profile)
    except (ConfigError, RuntimeError) as e:
        return _error(e)

    # A per-call `read_only=True` narrows this statement only — it
    # deliberately does not latch, because a routine parameter quietly
    # restricting the rest of the session would surprise the caller.
    # `set_read_only()` is the latch.
    # `CALL` can only be allowed for procedures the dialect or the
    # operator vouches for; install that list before the gate runs.
    guard.set_read_procedures(_dialect.read_procedures_for(prof))
    policy = _mask.resolve(
        _load_or_raise(), prof, requested=mask,
        session_mode=_SESSION_MASK["mode"],
    )
    restriction = guard.SESSION.effective(prof)
    if read_only and not restriction.read_only:
        restriction = guard.Restriction(True, guard.USER, "query(read_only=True)")

    if restriction.read_only:
        # The stronger scan runs only when a restriction is in force, which
        # caps the blast radius of it being imperfect.
        blocked = guard.blocks_write(sql)
        if blocked:
            return json.dumps(guard.refusal(restriction, blocked))
    elif not _is_read_only(sql) and not _writes_enabled():
        return _error(
            "non-SELECT statement rejected. The MCP server is in "
            "read-only mode — set LAKESH_MCP_WRITE=1 in the server's "
            "environment to enable writes."
        )

    # `native` is an ADBC concept (source passthrough vs DuckDB's attached
    # catalog); it only applies to adbc profiles. A python backend always
    # queries its source directly, and iceberg/ducklake always go through
    # DuckDB.
    if prof.type == "adbc":
        use_native = _prefer_native(prof) if native is None else bool(native)
    else:
        use_native = False
    mode = ("native" if use_native
            else "python" if prof.type == "python"
            else "duckdb")
    timeout = _effective_timeout(prof, timeout_s)

    want_estimate = _estimate_mode(estimate)
    if want_estimate is not None:
        if want_estimate not in _ESTIMATE_MODES:
            return json.dumps({
                "error": f"unknown estimate {estimate!r} (expected: true for a "
                         f"planner estimate, or \"count\" for an exact count)",
            })
        if offset:
            return json.dumps({
                "error": "estimate and offset are mutually exclusive — an "
                         "estimate describes the whole statement, not a page.",
            })
        return _estimate_query(profile, prof, sql, mode, want_estimate, timeout, policy)
    # A hard deadline needs DuckDB to be the one blocked. Only the
    # attached-catalog path (DuckDB executes) can be interrupted hard; a
    # source-direct session (native ADBC or a Python driver) sits inside
    # the driver, which does not observe the interrupt until it returns.
    source_direct = use_native or prof.type == "python"
    enforced = "best_effort" if (source_direct and timeout) else (
        "hard" if timeout else "none"
    )
    paged_sql, wrapped = _paginate(sql, limit, offset, prof)
    # One row past the limit, so "exactly `limit` rows" is distinguishable
    # from "more to come" instead of guessed at.
    want = limit + 1
    is_read = _is_read_only(sql)

    try:
        with _session(profile, timeout, native=use_native) as sess:
            with deadline(sess, timeout):
                if is_read:
                    cur = sess.execute(paged_sql)
                    columns = [d[0] for d in cur.description] if cur.description else []
                    if offset and not wrapped:
                        cur.fetchmany(offset)   # unwrappable: skip client-side
                    rows = cur.fetchmany(want)
                else:
                    # A write. run() sends it once — pushing a write through
                    # the paging cursor would double-execute it on the ADBC
                    # path (see duck.adbc_native_scan).
                    columns, rows = sess.run(paged_sql)
    except QueryTimeout as e:
        return json.dumps({
            "error": scrub(str(e), _KNOWN_SECRETS),
            "error_type": "timeout",
            "timeout_s": e.seconds,
            "elapsed_s": round(e.elapsed, 1),
            "enforced": "hard" if e.hard else "best_effort",
            "mode": mode,
            "hint": (
                "the deadline aborted the query"
                if e.hard else
                "the deadline could not abort the in-flight driver call; "
                "narrow the query or add a LIMIT"
            ),
        })
    except (AuthRequired, ConfigError, duckdb.Error, RuntimeError) as e:
        hint = _duck.explain_sandbox_error(e)
        if hint:
            return json.dumps({
                "error": scrub(str(e), _KNOWN_SECRETS),
                "error_type": "sandboxed", "hint": hint,
            })
        return _error(e)

    has_more = len(rows) > limit
    rows = rows[:limit]
    warnings: list[str] = []
    rows, mask_report = _mask.mask_rows(policy, columns, rows)
    warnings.extend(_mask.detect_defeats(policy, sql))
    if offset and not _HAS_ORDER_BY.search(sql):
        warnings.append(
            "no ORDER BY detected: each page is a separate execution of the "
            "statement, so row order is not stable across pages and rows may "
            "be duplicated or skipped. Add an ORDER BY on a unique column."
        )
    if not columns:
        return json.dumps({"ok": True, "rows": 0, "mode": mode, "note": "no result set"})
    if format == "json":
        payload: dict[str, Any] = {
            "columns": columns,
            "rows": [{c: _jsonable(v) for c, v in zip(columns, row)} for row in rows],
            "row_count": len(rows),
            # Kept for compatibility; `has_more` is the accurate signal.
            # This one cannot tell "exactly `limit` rows" from "truncated".
            "truncated_at": limit if len(rows) >= limit else None,
            "has_more": has_more,
            "next_offset": offset + len(rows) if has_more else None,
            "mode": mode,
            "enforced": enforced,
        }
        if offset:
            payload["offset"] = offset
        if warnings:
            payload["warnings"] = warnings
        masking = mask_report.as_dict(policy)
        if masking:
            payload["masking"] = masking
        return json.dumps(payload, default=str)
    footer = f"\n\n({len(rows)} rows, mode={mode}"
    if has_more:
        footer += f", more from offset {offset + len(rows)}"
    return _rows_as_table(columns, rows) + footer + ")"


def serve(config_path: Path | None = None, read_only: bool = False) -> None:
    """Run the MCP server on stdio — the entry point `lakesh mcp` calls.

    `config_path` is exported as `$LAKESH_CONFIG` rather than threaded
    through every tool: the tools each load config on their own (they
    are stateless by design), and `default_config_path()` already reads
    that variable first.
    """
    if config_path is not None:
        os.environ["LAKESH_CONFIG"] = str(config_path)
    if read_only:
        # Policy layer: set before any tool runs, so it applies to every
        # call and cannot be relaxed by one.
        guard.SESSION.set_policy_flag("lakesh mcp --read-only")
    # Every connection this process opens is agent-driven, so the source's
    # audit trail should say so. Set before any tool runs, for the same
    # reason as the policy flag above.
    _duck.CALLER = "mcp"
    server.run()


if __name__ == "__main__":
    serve()
