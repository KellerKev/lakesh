"""What each source engine can and cannot do.

lakesh is a universal query tool that specialises in DuckLake, Iceberg
and duckicelake — it is not a front end for any one warehouse. But the
layers that page, estimate, search and time out were all written against
DuckDB-shaped SQL, and each of them made a lowest-common-denominator
choice with the reasoning written in English in a docstring right next to
it. This module is where those facts live as data instead.

Before this existed there were **three independent re-implementations of
the same driver-name substring test** (in `freshness`, `duck` and `mcp`)
and **four copies of the system-schema list**, which had already drifted
— `pg_toast` was in one of them and none of the others.

### The contract

Every capability degrades to "unavailable" rather than to a wrong answer.
A dialect that cannot `EXPLAIN` returns `None` and the caller reports
that it cannot, rather than sending Snowflake's spelling to Trino and
surfacing a syntax error. That property is inherited from
`freshness.listing_columns`, which already returned `''` for engines with
nothing to contribute, and it is the reason this shape was grown rather
than replaced.

### Why only four profiles

DuckDB/DuckLake/Iceberg, Postgres and Snowflake are the engines that can
actually be tested here. MySQL, Trino, BigQuery, SQL Server and SQLite
get the ANSI profile, which claims nothing it cannot deliver. Writing
their profiles from documentation is how a "universal" tool acquires
quietly-wrong behaviour — a Trino profile that pages with
`LIMIT … OFFSET` instead of `OFFSET … LIMIT` is worse than one that says
paging is unavailable, because the first fails at runtime in the user's
face and the second fails honestly at the API.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from .config import Profile


def _sf_quote(value: str) -> str:
    """Single-quoted literal for a Snowflake statement."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


@dataclass(frozen=True)
class StageOps:
    """How an engine moves a local file to where it can read it.

    `put` takes an already-validated local path — `staging` owns deciding
    whether lakesh is willing to read it, because that judgement is the
    same on every engine.
    """

    put: Callable[[str, str], str]
    list: Callable[[str], str]
    remove: Callable[[str], str]
    target_hint: str = "@stage or @~/path"
    load: "Callable[[str, str, str], str] | None" = None
    """(table, stage_target, file_format) -> SQL that loads a staged file
    into an existing table. None means the engine cannot."""
    default_format: str = ""
    infer_create: "Callable[[str, str, str], str] | None" = None
    """(table, stage_target, named_format) -> SQL creating the table from
    the staged file's inferred schema. Opt-in only."""
    verify_after_put: bool = True
    """Whether the caller must confirm by listing.

    True for Snowflake, and not as belt-and-braces: measured, a PUT
    through `adbc_scan` returns its column names and **no rows**, so the
    response cannot tell you whether the transfer happened. Snowflake's
    own docs separately warn that a successful EXECUTION_STATUS does not
    mean files were transferred.
    """


# Up to `db.schema.table`, each part a plain identifier. A table name
# cannot be a bound parameter, so it is interpolated — and therefore has
# to be validated rather than escaped.
QUALIFIED_NAME_RE = __import__("re").compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}$")


_SNOWFLAKE_STAGE = StageOps(
    # AUTO_COMPRESS=FALSE keeps the staged name equal to the local name,
    # so the follow-up LIST can actually find it; with compression on,
    # Snowflake appends .gz and the verification has to guess.
    put=lambda local, target: (
        f"PUT {_sf_quote('file://' + local)} {target} "
        f"AUTO_COMPRESS=FALSE OVERWRITE=TRUE"),
    list=lambda target: f"LIST {target}",
    remove=lambda target: f"REMOVE {target}",
    target_hint="@~/path, @my_stage or @db.schema.stage/path",
    load=lambda table, target, fmt: (
        f"COPY INTO {table} FROM {target} "
        f"FILE_FORMAT=({fmt}) PURGE=FALSE"),
    default_format="TYPE=CSV SKIP_HEADER=1",
    # INFER_SCHEMA takes a NAMED file format object — inline specs are
    # not supported — so auto-create only works when the operator names
    # one they have already created. Verified against the docs and by a
    # syntax error from the inline form.
    infer_create=lambda table, target, named_fmt: (
        f"CREATE TABLE IF NOT EXISTS {table} USING TEMPLATE ("
        f"SELECT ARRAY_AGG(OBJECT_CONSTRUCT(*)) FROM TABLE(INFER_SCHEMA("
        f"LOCATION=>{_sf_quote(target)}, FILE_FORMAT=>{_sf_quote(named_fmt)})))"),
)


@dataclass(frozen=True)
class Dialect:
    """One engine's capabilities. Absent capability = `None` or empty."""

    name: str
    system_schemas: tuple[str, ...] = ("main", "information_schema", "pg_catalog")
    ilike: bool = False
    """Whether `ILIKE` exists. MySQL, SQL Server, Trino, BigQuery and
    SQLite have no such operator; their `LIKE` is either already
    case-insensitive or needs `LOWER()` on both sides."""
    like_escape: bool = True
    """BigQuery's LIKE has no ESCAPE clause."""
    explain: str | None = None
    """Format string for a plan, or None when the engine has no EXPLAIN
    reachable over this path."""
    statement_timeout: Callable[[float], str] | None = None
    read_procedures: frozenset[str] = frozenset()
    """Procedures known to be reads. `CALL` cannot be classified from the
    statement, so this is the only way a CALL is ever allowed in a
    read-only session — plus whatever the operator vouches for."""
    unwrappable: tuple[str, ...] = (
        "explain", "pragma", "show", "desc", "describe", "list", "call", "use")
    """Statements that are not relations, so they cannot be wrapped in a
    derived table for paging or a count probe."""
    freshness_columns: str = ""
    freshness_join: str = ""
    stage: "StageOps | None" = None
    """How to stage a local file. None means the engine has no such
    concept reachable over this path — DuckLake and Iceberg would stage
    to object storage instead, which is a different implementation of the
    same capability and not yet written."""

    def is_system_schema(self, name: str) -> bool:
        return str(name).lower() in self.system_schemas


# --------------------------------------------------------------------------
# profiles

_DUCKDB = Dialect(
    name="duckdb",
    system_schemas=("main", "information_schema", "pg_catalog", "pg_toast"),
    ilike=True,
    explain="EXPLAIN (FORMAT json) {sql}",
    # DuckLake's read procedures are a closed, knowable set — which is
    # what makes vouching for them honest here and impossible for a
    # Snowflake procedure.
    read_procedures=frozenset({
        "ducklake_snapshots", "ducklake_table_info", "ducklake_list_files",
        "ducklake_options", "ducklake_current_snapshot",
        "ducklake_last_committed_snapshot", "ducklake_table_insertions",
        "ducklake_table_deletions",
    }),
)

_POSTGRES = Dialect(
    name="postgres",
    system_schemas=("information_schema", "pg_catalog", "pg_toast"),
    ilike=True,
    # No EXPLAIN over the native path: the ADBC driver wraps every
    # statement in COPY (…) TO STDOUT, which rejects it. Measured.
    explain=None,
    statement_timeout=lambda s: (
        f"SELECT set_config('statement_timeout', '{int(s * 1000)}', false)"),
    freshness_columns=(
        "CAST(NULL AS TIMESTAMP) AS last_modified, "
        "CAST(c.reltuples AS BIGINT) AS row_count, "
        "CASE WHEN c.oid IS NULL THEN NULL "
        "     ELSE pg_total_relation_size(c.oid) END AS bytes"),
    freshness_join=(
        " LEFT JOIN pg_namespace n ON n.nspname = t.table_schema"
        " LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = t.table_name"),
)

_SNOWFLAKE = Dialect(
    name="snowflake",
    # Returned upper-cased, and matched case-insensitively — a
    # case-sensitive comparison put ~60-70 views per database back into
    # every listing.
    system_schemas=("information_schema",),
    ilike=True,
    explain="EXPLAIN USING JSON {sql}",
    statement_timeout=lambda s: (
        f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {int(s)}"),
    # Deliberately empty. Snowflake exposes no way to know whether a
    # procedure writes, so only the operator's allow-list applies.
    read_procedures=frozenset(),
    unwrappable=(
        "explain", "pragma", "show", "desc", "describe", "list", "call",
        "use", "get", "put", "remove", "execute"),
    stage=_SNOWFLAKE_STAGE,
)

_ANSI = Dialect(name="ansi")

_REGISTRY = {d.name: d for d in (_DUCKDB, _POSTGRES, _SNOWFLAKE, _ANSI)}

# Longest first, so a driver living under `/opt/snowflake/` that is
# actually the Trino driver resolves on its own filename rather than on
# its parent directory. The old substring test got that wrong.
_DRIVER_HINTS = tuple(sorted(
    (("snowflake", "snowflake"), ("postgresql", "postgres"),
     ("postgres", "postgres"), ("duckdb", "duckdb")),
    key=lambda pair: -len(pair[0]),
))


def known_dialects() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def get(name: str) -> Dialect:
    return _REGISTRY.get(str(name).lower(), _ANSI)


def for_profile(profile: Profile) -> Dialect:
    """Which engine this profile talks to.

    An explicit `dialect = "..."` on the profile wins — the guess is a
    guess, and an operator with an unusual driver path needs a way to
    correct it. Otherwise the driver's *basename* is matched, not the
    whole path.
    """
    declared = getattr(profile, "dialect", "") or ""
    if declared:
        return get(declared)
    if profile.type != "adbc":
        # DuckLake and Iceberg REST are both read through DuckDB, so they
        # share its capabilities. They differ in where their freshness
        # signal lives, which is tracked separately.
        return _DUCKDB
    basename = os.path.basename(profile.driver or "").lower()
    for hint, name in _DRIVER_HINTS:
        if hint in basename:
            return _REGISTRY[name]
    return _ANSI


# --------------------------------------------------------------------------
# capability helpers
#
# Each returns SQL, or None/'' when the engine cannot do the thing. The
# caller reports "unavailable with a reason" rather than emitting a
# statement the engine will reject.

def page_sql(dialect: Dialect, inner: str, limit: int, offset: int) -> str | None:
    """A derived-table page, or None when this engine cannot express one.

    Only the ANSI `LIMIT … OFFSET` spelling is emitted. Trino wants
    `OFFSET … LIMIT` and SQL Server has no `LIMIT` at all; rather than
    guess at spellings that cannot be tested here, those fall to the ANSI
    profile and the caller pages client-side instead.
    """
    if dialect.name == "ansi":
        return None
    return (f"SELECT * FROM ({inner}) AS _lakesh_page "
            f"LIMIT {int(limit)} OFFSET {int(offset)}")


def ilike_expr(dialect: Dialect, column: str, literal: str, escape: str) -> str:
    """Case-insensitive match, in whatever spelling the engine has.

    Case-insensitivity is not optional: Snowflake upper-cases unquoted
    identifiers and Postgres lower-cases them, so a case-sensitive search
    finds nothing on one of them.
    """
    if dialect.ilike:
        clause = f"{column} ILIKE {literal}"
        return f"{clause} ESCAPE {escape}" if dialect.like_escape else clause
    clause = f"LOWER({column}) LIKE LOWER({literal})"
    return f"{clause} ESCAPE {escape}" if dialect.like_escape else clause


def explain_sql(dialect: Dialect, sql: str) -> str | None:
    return dialect.explain.format(sql=sql) if dialect.explain else None


def timeout_sql(dialect: Dialect, seconds: float) -> str | None:
    if not dialect.statement_timeout or seconds <= 0:
        return None
    return dialect.statement_timeout(seconds)


def read_procedures_for(profile: Profile) -> frozenset[str]:
    """Built-ins the dialect vouches for, plus whatever the operator did.

    The operator's half is a *declaration*, not a verification: lakesh
    cannot check what a procedure does, and says so in the docs.
    """
    dialect = for_profile(profile)
    declared = tuple(getattr(profile, "read_procedures", ()) or ())
    return frozenset(dialect.read_procedures | {n.lower() for n in declared})


def stage_ops(profile: Profile) -> "StageOps | None":
    """How to stage a file for this profile, or None if it cannot.

    The caller reports "unavailable" with a reason rather than emitting a
    `PUT` to an engine that has no such statement.
    """
    return for_profile(profile).stage
