"""Is this table the one I should be using, and is it current?

An agent can run perfectly correct SQL against the wrong table and never
know. This module supplies the two halves of the answer:

* **Declared** — what the operator asserted in config: canonical or
  deprecated, what supersedes it, how stale it is allowed to get. Works
  identically on every source, because it is just config.
* **Observed** — what the source itself can tell us about when the table
  last changed and how big it is. This is where sources diverge sharply,
  and the divergence is not something we can paper over.

What each source can honestly report
------------------------------------

============  ==================  ================================
Source        Timestamp?          Reported
============  ==================  ================================
Snowflake     yes, LAST_ALTERED   last_modified, row_count, bytes
Postgres      **no**              row_count estimate, bytes
ANSI ADBC     no                  nothing
Iceberg/lake  not yet             nothing
============  ==================  ================================

Two honesty constraints follow, and both are load-bearing.

**Postgres has no freshness signal, and must not be given a fake one.**
Its ANSI ``information_schema.tables`` has twelve columns and none of
them are temporal (measured, not assumed). The only timestamps anywhere
are ``pg_stat_user_tables.last_analyze`` / ``last_autovacuum``, which are
statistics-collector artifacts: NULL until autovacuum happens to fire,
wiped by ``pg_stat_reset()``, and completely decoupled from when a row
last landed. Reporting one as ``last_modified`` would hand an agent a
vacuum timestamp labelled as data freshness — worse than reporting
nothing, because "unknown" makes an agent go and ask while a wrong
timestamp makes it confidently proceed.

**Snowflake's LAST_ALTERED is a signal, not a proof.** It moves on DDL
and metadata operations as well as DML, and for a view it tracks the
definition changing rather than the data. It is labelled in the output
so a caller who knows Snowflake can calibrate. ``ROW_COUNT`` and
``BYTES`` are NULL for views and external tables.

Hence four states rather than three: ``unknown`` and ``fresh`` must never
be confusable, because an agent reading a missing signal as a passing one
is precisely the failure this exists to prevent.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .config import Profile, TableAnnotation

# --------------------------------------------------------------------------
# which dialect are we talking to

DIALECT_SNOWFLAKE = "snowflake"
DIALECT_POSTGRES = "postgres"
DIALECT_ANSI = "ansi"
DIALECT_DUCKDB = "duckdb"


def source_dialect(profile: Profile) -> str:
    """Which metadata vocabulary this profile's source speaks.

    A substring test, because `driver` is usually a path to a shared
    library (`/…/libadbc_driver_snowflake.so`) rather than a bare name.
    """
    if profile.type != "adbc":
        return DIALECT_DUCKDB
    driver = (profile.driver or "").lower()
    if "snowflake" in driver:
        return DIALECT_SNOWFLAKE
    if "postgres" in driver:
        return DIALECT_POSTGRES
    return DIALECT_ANSI


def reports_last_modified(dialect: str) -> bool:
    return dialect == DIALECT_SNOWFLAKE


# --------------------------------------------------------------------------
# the extra columns each dialect can contribute to a table listing
#
# These are spliced into the SELECT list of the listing query that is
# already being sent, so on Snowflake freshness costs zero extra round
# trips. Column order is fixed: last_modified, row_count, bytes.

_SNOWFLAKE_COLUMNS = "last_altered, row_count, bytes"

# Postgres has nothing temporal, but size is still worth having — "is
# this the real table or an empty shell" is a question an agent asks.
# Driving the listing from pg_class instead of information_schema would
# be simpler SQL and a privilege regression: information_schema is
# filtered to what the current role can access and pg_class is not.
_POSTGRES_COLUMNS = (
    "CAST(NULL AS TIMESTAMP) AS last_modified, "
    "CAST(c.reltuples AS BIGINT) AS row_count, "
    "CASE WHEN c.oid IS NULL THEN NULL "
    "     ELSE pg_total_relation_size(c.oid) END AS bytes"
)
_POSTGRES_JOIN = (
    " LEFT JOIN pg_namespace n ON n.nspname = t.table_schema"
    " LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = t.table_name"
)


def listing_columns(dialect: str) -> str:
    """Extra SELECT-list columns, or '' when the source has none."""
    if dialect == DIALECT_SNOWFLAKE:
        return _SNOWFLAKE_COLUMNS
    if dialect == DIALECT_POSTGRES:
        return _POSTGRES_COLUMNS
    return ""


def listing_join(dialect: str) -> str:
    return _POSTGRES_JOIN if dialect == DIALECT_POSTGRES else ""


# --------------------------------------------------------------------------
# evaluation

STATE_UNKNOWN = "unknown"     # the source cannot tell us
STATE_UNRATED = "unrated"     # we know the age, nobody said what is acceptable
STATE_FRESH = "fresh"
STATE_STALE = "stale"


def _as_aware(value: dt.datetime) -> dt.datetime:
    """Naive timestamps are assumed UTC. Snowflake's TIMESTAMP_LTZ
    arrives tz-aware through ADBC, but a driver or session-timezone
    change should not turn a freshness check into a TypeError."""
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _coerce_datetime(value: Any) -> dt.datetime | None:
    """Whatever the driver handed back, as a datetime or nothing.

    Metadata columns are not a contract: a driver may return an ISO
    string where another returns a timestamp. A freshness field is an
    advisory extra on a listing, so a surprise here must degrade to
    "unknown" rather than take the whole tool call down with it.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day,
                           tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def evaluate(
    last_modified: dt.datetime | None,
    max_staleness_seconds: int | None,
    now: dt.datetime | None = None,
) -> tuple[str, int | None]:
    """(state, age_seconds).

    `unknown` when the source cannot report a timestamp at all — never
    `fresh`, which is the distinction the whole module exists for.
    """
    last_modified = _coerce_datetime(last_modified)
    if last_modified is None:
        return STATE_UNKNOWN, None
    now = now or dt.datetime.now(dt.timezone.utc)
    age = int((_as_aware(now) - _as_aware(last_modified)).total_seconds())
    age = max(0, age)          # clock skew; a table is never fresher than now
    if not max_staleness_seconds:
        return STATE_UNRATED, age
    return (STATE_FRESH if age <= max_staleness_seconds else STATE_STALE), age


def threshold_for(
    profile: Profile, annotation: TableAnnotation | None
) -> int | None:
    """The table's own threshold, falling back to the profile's."""
    if annotation is not None and annotation.max_staleness_seconds:
        return annotation.max_staleness_seconds
    return profile.max_staleness_seconds


def status_for(profile: Profile, annotation: TableAnnotation | None) -> str:
    if annotation is not None and annotation.status != "unknown":
        return annotation.status
    return profile.status


# --------------------------------------------------------------------------
# output

def describe(
    profile: Profile,
    annotation: TableAnnotation | None,
    last_modified: dt.datetime | None = None,
    row_count: Any = None,
    size_bytes: Any = None,
    dialect: str = DIALECT_ANSI,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """The annotation/freshness fields for one table.

    Keys are **omitted when there is nothing to say**, so a catalog with
    no annotations against a source with no timestamps produces exactly
    the output it did before this feature existed. The tool list is
    short on purpose and the same restraint applies to per-row payload,
    which is what actually costs an agent context.
    """
    out: dict[str, Any] = {}

    status = status_for(profile, annotation)
    if status != "unknown":
        out["status"] = status
    if annotation is not None:
        if annotation.note:
            out["note"] = annotation.note
        if annotation.superseded_by:
            out["superseded_by"] = annotation.superseded_by

    threshold = threshold_for(profile, annotation)
    observed = _coerce_datetime(last_modified)
    state, age = evaluate(observed, threshold, now=now)

    fresh: dict[str, Any] = {}
    if state != STATE_UNKNOWN and observed is not None:
        fresh["state"] = state
        fresh["age_seconds"] = age
        fresh["last_modified"] = _as_aware(observed).isoformat()
        if reports_last_modified(dialect):
            # LAST_ALTERED moves on DDL too; name the source so a caller
            # who knows Snowflake can discount it appropriately.
            fresh["source"] = "LAST_ALTERED"
    if threshold:
        fresh["max_staleness_seconds"] = threshold
    count = _coerce_int(row_count)
    # Postgres reports -1 for "never analyzed", which is an absence of
    # information rather than an empty table.
    if count is not None and count >= 0:
        fresh["row_count"] = count
        if dialect == DIALECT_POSTGRES:
            fresh["row_count_is_estimate"] = True
    size = _coerce_int(size_bytes)
    if size is not None:
        fresh["bytes"] = size
    if fresh and "state" not in fresh:
        fresh["state"] = STATE_UNKNOWN
    if fresh:
        out["freshness"] = fresh
    return out
