"""Tests for the dialect registry.

lakesh is a universal query tool that specialises in DuckLake/Iceberg; it
is not a front end for any one warehouse. The property these protect is
that a capability the engine does not have degrades to "unavailable"
rather than to a statement that engine will reject.
"""
from __future__ import annotations

import pytest

from lakesh import dialect
from lakesh.config import ConfigError, Profile


def _adbc(driver: str, **kw) -> Profile:
    return Profile(name="p", type="adbc", driver=driver, uri="x", **kw)


# --------------------------------------------------------------------------
# resolution


@pytest.mark.parametrize("driver,expected", [
    ("snowflake", "snowflake"),
    ("/x/site-packages/adbc_driver_snowflake/libadbc_driver_snowflake.so", "snowflake"),
    ("postgresql", "postgres"),
    ("/x/libadbc_driver_postgresql.so", "postgres"),
    ("trino", "ansi"),
    ("mysql", "ansi"),
    ("", "ansi"),
])
def test_driver_resolves_to_a_dialect(driver, expected):
    assert dialect.for_profile(_adbc(driver)).name == expected


def test_resolution_reads_the_basename_not_the_path():
    """A Trino driver living under /opt/snowflake/ is a Trino driver. The
    old substring test over the whole path got this wrong."""
    prof = _adbc("/opt/snowflake/drivers/libadbc_driver_trino.so")
    assert dialect.for_profile(prof).name == "ansi"


def test_an_explicit_dialect_overrides_the_guess():
    """The guess is a guess; an operator with an unusual layout needs a
    way to correct it."""
    prof = _adbc("/opt/weird/libdriver.so", dialect="snowflake")
    assert dialect.for_profile(prof).name == "snowflake"


def test_unknown_dialect_is_rejected_at_config_time():
    with pytest.raises(ConfigError) as e:
        _adbc("x", dialect="oracle").validate()
    assert "oracle" in str(e.value)


def test_non_adbc_profiles_are_duckdb():
    """DuckLake and Iceberg REST are both read through DuckDB, so they
    share its capabilities."""
    assert dialect.for_profile(Profile(name="p", uri="u", warehouse="w")).name == "duckdb"


# --------------------------------------------------------------------------
# capabilities degrade rather than lie


def test_ansi_claims_nothing_it_cannot_deliver():
    ansi = dialect.get("ansi")
    assert dialect.explain_sql(ansi, "SELECT 1") is None
    assert dialect.timeout_sql(ansi, 5) is None
    assert dialect.page_sql(ansi, "SELECT 1", 10, 5) is None
    assert ansi.read_procedures == frozenset()


def test_explain_uses_each_engines_own_spelling():
    assert "USING JSON" in dialect.explain_sql(dialect.get("snowflake"), "SELECT 1")
    assert "FORMAT json" in dialect.explain_sql(dialect.get("duckdb"), "SELECT 1")
    # Postgres over ADBC cannot EXPLAIN at all — the driver wraps every
    # statement in COPY (…) TO STDOUT, which rejects it.
    assert dialect.explain_sql(dialect.get("postgres"), "SELECT 1") is None


def test_timeout_uses_each_engines_own_spelling():
    assert "set_config" in dialect.timeout_sql(dialect.get("postgres"), 3)
    assert "STATEMENT_TIMEOUT_IN_SECONDS = 3" in dialect.timeout_sql(
        dialect.get("snowflake"), 3)
    assert dialect.timeout_sql(dialect.get("duckdb"), 3) is None


def test_ilike_falls_back_to_lower_where_the_operator_does_not_exist():
    """MySQL, SQL Server, Trino, BigQuery and SQLite have no ILIKE."""
    have = dialect.ilike_expr(dialect.get("postgres"), "c", "'x'", "'!'")
    lack = dialect.ilike_expr(dialect.get("ansi"), "c", "'x'", "'!'")
    assert "ILIKE" in have
    assert "ILIKE" not in lack and "LOWER(c)" in lack


def test_system_schemas_are_matched_case_insensitively():
    """Snowflake and SQL Server return them upper-cased."""
    assert dialect.get("snowflake").is_system_schema("INFORMATION_SCHEMA")
    assert dialect.get("postgres").is_system_schema("PG_CATALOG")
    assert not dialect.get("snowflake").is_system_schema("ANALYTICS")


def test_each_engine_has_its_own_system_schema_set():
    """A shared constant was the DuckDB+Postgres union and nothing else."""
    assert "main" in dialect.get("duckdb").system_schemas
    assert "main" not in dialect.get("snowflake").system_schemas
    assert "pg_catalog" in dialect.get("postgres").system_schemas


# --------------------------------------------------------------------------
# read procedures


def test_ducklake_read_procedures_ship_as_known():
    """A closed, knowable set — which is what makes vouching for them
    honest here and impossible for a Snowflake procedure."""
    known = dialect.get("duckdb").read_procedures
    assert "ducklake_snapshots" in known
    assert "ducklake_merge_adjacent_files" not in known


def test_snowflake_vouches_for_nothing_by_default():
    """Snowflake exposes no way to know what a procedure does."""
    assert dialect.get("snowflake").read_procedures == frozenset()


def test_the_operator_can_vouch_for_their_own():
    prof = _adbc("snowflake", read_procedures=("My_Reporting_Proc",))
    allowed = dialect.read_procedures_for(prof)
    assert "my_reporting_proc" in allowed          # normalised to lower case


def test_operator_additions_join_the_builtins():
    prof = Profile(name="lake", uri="u", warehouse="w",
                   read_procedures=("my_proc",))
    allowed = dialect.read_procedures_for(prof)
    assert "my_proc" in allowed and "ducklake_snapshots" in allowed
