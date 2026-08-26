"""Tests for freshness evaluation and duration parsing.

Pure functions, no fixtures and no connections. The property most worth
protecting is that `unknown` never renders as `fresh`: an agent that
reads a missing signal as a passing one is exactly the failure this
module exists to prevent.
"""
from __future__ import annotations

import datetime as dt

import pytest

from lakesh import freshness
from lakesh.config import ConfigError, Profile, TableAnnotation, parse_duration


NOW = dt.datetime(2026, 8, 25, 12, 0, 0, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# parse_duration


@pytest.mark.parametrize("text,seconds", [
    ("45s", 45),
    ("45m", 2700),
    ("24h", 86400),
    ("7d", 604800),
    ("1w", 604800),
    ("1h30m", 5400),
    ("  2h ", 7200),
    ("1d12h", 129600),
    ("24H", 86400),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["24", "", "abc", "24 hours", "1.5h", "-2h", "h"])
def test_parse_duration_rejects_ambiguous_input(text):
    """A bare number especially: "24" is 24 hours to whoever is writing a
    freshness threshold and 24 seconds to time.sleep. Guessing makes
    either every table stale or every table fresh — both of which look
    like the feature working."""
    with pytest.raises(ConfigError) as e:
        parse_duration(text, "profile 'p'")
    assert "duration" in str(e.value)


def test_parse_duration_error_names_its_context():
    with pytest.raises(ConfigError) as e:
        parse_duration("nope", "profile 'snow' table 'A.B'")
    assert "profile 'snow' table 'A.B'" in str(e.value)


# --------------------------------------------------------------------------
# evaluate


def test_unknown_is_not_fresh():
    """The load-bearing distinction. A source that cannot report a
    timestamp must not look like one reporting a recent one."""
    state, age = freshness.evaluate(None, 3600, now=NOW)
    assert state == freshness.STATE_UNKNOWN
    assert age is None


def test_unrated_when_no_threshold_configured():
    state, age = freshness.evaluate(NOW - dt.timedelta(hours=5), None, now=NOW)
    assert state == freshness.STATE_UNRATED
    assert age == 18000


@pytest.mark.parametrize("hours,expected", [
    (1, freshness.STATE_FRESH),
    (6, freshness.STATE_FRESH),      # exactly at the threshold is still fresh
    (7, freshness.STATE_STALE),
])
def test_fresh_and_stale_around_the_threshold(hours, expected):
    at = NOW - dt.timedelta(hours=hours)
    state, _age = freshness.evaluate(at, 6 * 3600, now=NOW)
    assert state == expected


def test_naive_timestamps_are_treated_as_utc():
    """Snowflake's TIMESTAMP_LTZ arrives tz-aware, but a driver or
    session-timezone change must not turn this into a TypeError."""
    naive = dt.datetime(2026, 8, 25, 11, 0, 0)
    state, age = freshness.evaluate(naive, 7200, now=NOW)
    assert state == freshness.STATE_FRESH and age == 3600


def test_future_timestamps_clamp_to_zero():
    """Clock skew between the source and here; a table is never fresher
    than now, and a negative age would read as nonsense."""
    _state, age = freshness.evaluate(NOW + dt.timedelta(hours=3), 3600, now=NOW)
    assert age == 0


@pytest.mark.parametrize("value", ["not a timestamp", True, object()])
def test_unparseable_timestamps_degrade_to_unknown(value):
    """Metadata columns are not a contract — a driver may hand back a
    string where another gives a datetime. A freshness field is an
    advisory extra on a listing, so a surprise degrades rather than
    taking the tool call down."""
    assert freshness.evaluate(value, 3600, now=NOW)[0] == freshness.STATE_UNKNOWN


def test_iso_strings_are_accepted():
    state, age = freshness.evaluate("2026-08-25T11:00:00+00:00", 7200, now=NOW)
    assert state == freshness.STATE_FRESH and age == 3600


# --------------------------------------------------------------------------
# source_dialect


@pytest.mark.parametrize("driver,expected", [
    ("snowflake", freshness.DIALECT_SNOWFLAKE),
    ("/x/site-packages/adbc_driver_snowflake/libadbc_driver_snowflake.so",
     freshness.DIALECT_SNOWFLAKE),
    ("postgresql", freshness.DIALECT_POSTGRES),
    ("/x/libadbc_driver_postgresql.so", freshness.DIALECT_POSTGRES),
    ("trino", freshness.DIALECT_ANSI),
    ("sqlite", freshness.DIALECT_ANSI),
])
def test_source_dialect_matches_paths_not_just_names(driver, expected):
    prof = Profile(name="p", type="adbc", driver=driver)
    assert freshness.source_dialect(prof) == expected


def test_non_adbc_profiles_are_duckdb():
    assert freshness.source_dialect(Profile(name="p")) == freshness.DIALECT_DUCKDB


def test_only_snowflake_claims_a_last_modified():
    assert freshness.reports_last_modified(freshness.DIALECT_SNOWFLAKE)
    for other in (freshness.DIALECT_POSTGRES, freshness.DIALECT_ANSI,
                  freshness.DIALECT_DUCKDB):
        assert not freshness.reports_last_modified(other)


def test_listing_columns_are_empty_where_there_is_nothing_to_ask_for():
    assert "last_altered" in freshness.listing_columns(freshness.DIALECT_SNOWFLAKE)
    assert freshness.listing_columns(freshness.DIALECT_ANSI) == ""
    assert freshness.listing_columns(freshness.DIALECT_DUCKDB) == ""
    # Postgres contributes size but explicitly no timestamp.
    pg = freshness.listing_columns(freshness.DIALECT_POSTGRES)
    assert "pg_total_relation_size" in pg
    assert "CAST(NULL AS TIMESTAMP)" in pg
    # ...and needs a join to reach pg_class, which only it does.
    assert "pg_class" in freshness.listing_join(freshness.DIALECT_POSTGRES)
    assert freshness.listing_join(freshness.DIALECT_SNOWFLAKE) == ""


# --------------------------------------------------------------------------
# describe


def test_describe_omits_everything_when_there_is_nothing_to_say():
    """A catalog with no annotations against a source with no timestamps
    must produce exactly the output it did before this feature — per-row
    payload is what actually costs an agent context."""
    assert freshness.describe(Profile(name="p"), None) == {}


def test_describe_carries_the_annotation():
    prof = Profile(name="p")
    ann = TableAnnotation(pattern="A.B", status="deprecated",
                          superseded_by="A.C", note="use A.C")
    out = freshness.describe(prof, ann)
    assert out["status"] == "deprecated"
    assert out["superseded_by"] == "A.C"
    assert out["note"] == "use A.C"


def test_describe_falls_back_to_the_profile_threshold():
    prof = Profile(name="p", max_staleness_seconds=86400)
    out = freshness.describe(prof, None, last_modified=NOW - dt.timedelta(hours=1),
                             dialect=freshness.DIALECT_SNOWFLAKE, now=NOW)
    assert out["freshness"]["max_staleness_seconds"] == 86400
    assert out["freshness"]["state"] == freshness.STATE_FRESH
    assert out["freshness"]["source"] == "LAST_ALTERED"


def test_table_threshold_beats_the_profile_one():
    prof = Profile(name="p", max_staleness_seconds=86400)
    ann = TableAnnotation(pattern="A.B", max_staleness_seconds=3600)
    out = freshness.describe(prof, ann, last_modified=NOW - dt.timedelta(hours=5),
                             dialect=freshness.DIALECT_SNOWFLAKE, now=NOW)
    assert out["freshness"]["max_staleness_seconds"] == 3600
    assert out["freshness"]["state"] == freshness.STATE_STALE


def test_describe_marks_postgres_row_counts_as_estimates():
    prof = Profile(name="p", type="adbc", driver="postgresql")
    out = freshness.describe(prof, None, row_count=1234, size_bytes=49152,
                             dialect=freshness.DIALECT_POSTGRES)
    fresh = out["freshness"]
    assert fresh["row_count"] == 1234
    assert fresh["row_count_is_estimate"] is True
    assert fresh["bytes"] == 49152
    # No timestamp is available, and that has to be visible.
    assert fresh["state"] == freshness.STATE_UNKNOWN
    assert "last_modified" not in fresh


def test_postgres_never_analyzed_is_not_an_empty_table():
    """pg_class.reltuples is -1 on PG14+ for "never analyzed". Reporting
    that as 0 rows would be a lie about the data, not just the stats."""
    prof = Profile(name="p", type="adbc", driver="postgresql")
    out = freshness.describe(prof, None, row_count=-1,
                             dialect=freshness.DIALECT_POSTGRES)
    assert "row_count" not in out.get("freshness", {})
