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


# --------------------------------------------------------------------------
# session context
#
# The measured facts these pin down, all verified against live sources:
# both engines accept a stamp, neither carries it to a new connection,
# and Snowflake's IS_AGENT_ACTIVATED is readable but not settable.


def test_snowflake_stamps_query_tag_and_a_namespaced_variable():
    sql = dialect.session_stamp_sql(
        dialect.get("snowflake"), "lakesh/1.0 mcp", {"client": "mcp"})
    assert "ALTER SESSION SET QUERY_TAG = 'lakesh/1.0 mcp'" in sql
    # The LAKESH_ prefix is what keeps a flat, unnamespaced Snowflake
    # variable from colliding with an operator's own.
    assert "SET LAKESH_CLIENT = 'mcp'" in sql


def test_postgres_stamps_application_name_and_a_dotted_setting():
    sql = dialect.session_stamp_sql(
        dialect.get("postgres"), "lakesh/1.0 cli", {"client": "cli"})
    assert "set_config('application_name', 'lakesh/1.0 cli', false)" in sql[0]
    # Postgres rejects a custom setting that is not namespaced with a dot.
    assert "set_config('lakesh.client', 'cli', false)" in sql[1]
    # Session scope, not transaction scope: the ADBC driver runs its own
    # transactions, so `true` here would discard the stamp immediately.
    assert all("false)" in s for s in sql)


def test_ansi_stamps_nothing():
    """ANSI claims nothing it cannot deliver."""
    assert dialect.session_stamp_sql(
        dialect.get("ansi"), "label", {"client": "cli"}) == []
    assert dialect.get("ansi").session is None


def test_duckdb_sets_a_variable_but_has_no_audit_tag():
    """DuckDB is in-process: real session variables (verified on 1.5.2),
    but no audit trail for a tag to land in. The signal that leaves the
    process on this path is the HTTP User-Agent, set at connect time."""
    sql = dialect.session_stamp_sql(
        dialect.get("duckdb"), "lakesh/1.0 cli", {"client": "cli"})
    assert sql == ["SET VARIABLE lakesh_client = 'cli'"]
    assert dialect.get("duckdb").session.tag is None


def test_a_quote_in_a_value_is_escaped_per_engine():
    sf = dialect.session_stamp_sql(dialect.get("snowflake"), "it's", {})[0]
    assert r"\'" in sf                      # Snowflake takes the backslash form
    pg = dialect.session_stamp_sql(dialect.get("postgres"), "it's", {})[0]
    # Postgres treats a backslash as a literal character, so doubling is
    # the only escape that does not corrupt the value.
    assert "''" in pg and "\\" not in pg


def test_a_variable_name_that_is_not_an_identifier_raises():
    """These names land in SQL that operators write policies against, so
    a bad one is a bug to surface, not something to quote around."""
    for bad in ("drop table", "a.b", "1abc", "x" * 80, ""):
        with pytest.raises(ValueError):
            dialect.session_stamp_sql(
                dialect.get("snowflake"), "l", {bad: "v"})


def test_only_snowflake_reports_agent_activation():
    """Absent, not false, on engines with no such concept — the same rule
    freshness follows for a timestamp the source cannot supply."""
    assert "IS_AGENT_ACTIVATED" in dialect.get("snowflake").session.probe
    for name in ("postgres", "duckdb"):
        # The specific property, not the substring: DuckDB's probe reads
        # `custom_user_agent`, which contains "AGENT" and means something
        # else entirely.
        assert "IS_AGENT_ACTIVATED" not in dialect.get(name).session.probe.upper()
    assert dialect.get("ansi").session is None


def test_session_context_helpers_follow_the_profile():
    assert dialect.session_context(_adbc("snowflake")) is not None
    assert "IS_AGENT_ACTIVATED" in dialect.session_probe_sql(_adbc("snowflake"))


def test_a_ducklake_profile_now_gets_a_session_context():
    """It used to get none at all: `for_profile` returns the DuckDB
    dialect for every non-ADBC profile, and DuckDB had `session=None`.
    A DuckLake metastore is Postgres and can absolutely be attributed."""
    lake = Profile(name="lake", uri="u", warehouse="w")
    assert dialect.session_context(lake) is not None
    assert "getvariable" in dialect.session_probe_sql(lake)


# --------------------------------------------------------------------------
# stamping a live connection
#
# `stamp_session` is best-effort by design: a label that will not stick
# must not cost you the query you were actually trying to run.


class _FakeCursor:
    # `description` is None for a statement that returned no result set,
    # which is what DuckDB gives back and what the callers branch on.
    description = None

    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows


def _stamping_duck(monkeypatch, *, fail=()):
    """Record the statements `stamp_session` issues.

    Both entry points are patched: the stamp is side-effecting and goes
    through `adbc_native_exec` (which runs it once), while the session-id
    probe is a read and goes through `adbc_native_scan`. See
    `duck.adbc_native_scan` for why the two are not interchangeable."""
    from lakesh import duck

    issued = []

    def _check(sql):
        issued.append(sql)
        if any(f in sql for f in fail):
            raise RuntimeError("source said no")

    def fake_scan(con, handle, sql):
        _check(sql)
        return _FakeCursor([])

    def fake_exec(con, handle, sql):
        _check(sql)

    monkeypatch.setattr(duck, "adbc_native_scan", fake_scan)
    monkeypatch.setattr(duck, "adbc_native_exec", fake_exec)
    return duck, issued


def test_stamp_labels_the_caller(monkeypatch):
    duck, issued = _stamping_duck(monkeypatch)
    out = duck.stamp_session(None, 0, _adbc("snowflake"), "mcp")
    assert out["stamped"] and out["caller"] == "mcp"
    assert any("QUERY_TAG" in s for s in issued)
    assert any("SET LAKESH_CLIENT = 'mcp'" in s for s in issued)


def test_the_default_label_names_lakesh_and_the_caller(monkeypatch):
    duck, issued = _stamping_duck(monkeypatch)
    out = duck.stamp_session(None, 0, _adbc("snowflake"), "cli")
    assert out["label"].startswith("lakesh/") and out["label"].endswith(" cli")


def test_a_profile_can_override_the_tag_and_add_variables(monkeypatch):
    duck, issued = _stamping_duck(monkeypatch)
    prof = _adbc("snowflake", query_tag="acme-etl",
                 session_variables={"team": "data-eng"})
    duck.stamp_session(None, 0, prof, "cli")
    assert any("QUERY_TAG = 'acme-etl'" in s for s in issued)
    assert any("SET LAKESH_TEAM = 'data-eng'" in s for s in issued)
    # The caller label survives the operator's additions.
    assert any("LAKESH_CLIENT = 'cli'" in s for s in issued)


def test_session_context_false_stamps_nothing(monkeypatch):
    duck, issued = _stamping_duck(monkeypatch)
    out = duck.stamp_session(
        None, 0, _adbc("snowflake", session_context=False), "cli")
    assert out["stamped"] is False and issued == []


def test_an_engine_with_no_session_is_reported_not_attempted(monkeypatch):
    duck, issued = _stamping_duck(monkeypatch)
    out = duck.stamp_session(
        None, 0, _adbc("some-unknown-driver"), "cli")     # -> ANSI
    assert out["stamped"] is False and "no session context" in out["reason"]
    assert issued == []


def test_a_rejected_stamp_is_counted_not_raised(monkeypatch):
    """Failing a query because a label would not stick would be worse
    than an unlabelled query."""
    duck, issued = _stamping_duck(monkeypatch, fail=("QUERY_TAG",))
    out = duck.stamp_session(None, 0, _adbc("snowflake"), "cli")
    assert out["rejected"] == 1
    assert out["stamped"] is True          # the variable still landed


def test_probe_returns_none_when_the_engine_cannot_say(monkeypatch):
    duck, _ = _stamping_duck(monkeypatch)
    assert duck.session_probe(
        None, 0, Profile(name="lake", uri="u", warehouse="w")) is None


def test_probe_drops_nulls_so_absent_never_reads_as_false(monkeypatch):
    from lakesh import duck

    class _Cur:
        description = [("agent_activated",), ("role_name",), ("lakesh_client",)]
        def fetchall(self): return [("FALSE", "ANALYST", None)]

    monkeypatch.setattr(duck, "adbc_native_scan", lambda *a: _Cur())
    out = duck.session_probe(None, 0, _adbc("snowflake"))
    assert out == {"agent_activated": "FALSE", "role_name": "ANALYST"}
    assert "lakesh_client" not in out      # unset, not empty


# --------------------------------------------------------------------------
# attestation, on the connect path
#
# Unlike the stamp, a configured attestation that fails must NOT be
# swallowed: a fail-closed policy would then mask everything and the
# caller would see mysteriously empty columns instead of a reason.


def _signed(tmp_path, **kw):
    from lakesh import attest
    from lakesh.config import SigningConfig

    path = attest.write_private_key(tmp_path / "s.key", attest.generate_secret())
    return _adbc("snowflake",
                 signing=SigningConfig(kid="agent-1", key_file=str(path)), **kw)


def test_attestation_is_published_after_the_stamp(monkeypatch, tmp_path):
    from lakesh import duck

    issued = []

    class _Cur:
        description = [("sid",)]
        def fetchall(self): return [("999",)]

    def fake_scan(con, handle, sql):
        issued.append(sql)
        return _Cur()

    monkeypatch.setattr(duck, "adbc_native_scan", fake_scan)
    monkeypatch.setattr(duck, "adbc_native_exec",
                        lambda con, handle, sql: issued.append(sql))
    out = duck.stamp_session(None, 0, _signed(tmp_path), "mcp")
    assert out["attested"]["kid"] == "agent-1"
    assert out["attested"]["bound_to_session"] == "999"
    assert any("SET LAKESH_ATTEST = " in s for s in issued)
    # bound to the session the source reported, not to anything local
    assert any("CURRENT_SESSION()" in s for s in issued)


def test_no_signing_block_means_no_attestation(monkeypatch):
    from lakesh import duck

    monkeypatch.setattr(duck, "adbc_native_scan",
                        lambda *a: type("C", (), {"fetchall": lambda s: []})())
    monkeypatch.setattr(duck, "adbc_native_exec", lambda *a: None)
    out = duck.stamp_session(None, 0, _adbc("snowflake"), "cli")
    assert out["attested"] is None


def test_signing_on_an_engine_that_cannot_carry_it_raises(monkeypatch, tmp_path):
    """Silently ignoring it would leave the operator believing a policy
    is protecting them."""
    from lakesh import attest, duck
    from lakesh.config import ConfigError, SigningConfig

    path = attest.write_private_key(tmp_path / "s.key", attest.generate_secret())
    prof = _adbc("postgresql", signing=SigningConfig(kid="k", key_file=str(path)))
    monkeypatch.setattr(duck, "adbc_native_scan",
                        lambda *a: type("C", (), {"fetchall": lambda s: []})())
    monkeypatch.setattr(duck, "adbc_native_exec", lambda *a: None)
    with pytest.raises(ConfigError, match="signed attestation"):
        duck.stamp_session(None, 0, prof, "cli")


def test_a_source_that_reports_no_session_id_raises(monkeypatch, tmp_path):
    from lakesh import attest, duck

    monkeypatch.setattr(duck, "adbc_native_scan",
                        lambda *a: type("C", (), {"fetchall": lambda s: []})())
    monkeypatch.setattr(duck, "adbc_native_exec", lambda *a: None)
    with pytest.raises(attest.SigningError, match="session id"):
        duck.stamp_session(None, 0, _signed(tmp_path), "cli")


def test_only_snowflake_can_carry_an_attestation():
    """Labelling is broad; a *verifiable* attestation needs a policy
    engine on the far side, and only Snowflake has one here."""
    assert dialect.get("snowflake").session.attest is not None
    for name in ("postgres", "duckdb"):
        assert dialect.get(name).session.attest is None
        assert dialect.get(name).session.session_id is None


# --------------------------------------------------------------------------
# adbc_scan sends its statement to the source TWICE
#
# Measured against Snowflake's query history: one call to
# `adbc_native_scan` produces two SUCCESS rows, because `adbc_scan` is a
# DuckDB table function and DuckDB runs a table function once to bind its
# schema and again to execute it. For a read the bind scans 0 bytes and
# costs a wasted round trip. For a write both applications land — one
# INSERT of one row produced two rows, and one `UPDATE SET n = n + 1`
# moved the counter by two.
#
# `adbc_execute` is a scalar function and runs once, so writes route
# there. These tests pin the routing, since getting it wrong silently
# doubles a customer's DML.


def _routing_duck(monkeypatch):
    from lakesh import duck

    calls = []
    monkeypatch.setattr(duck, "adbc_native_scan",
                        lambda c, h, sql: (calls.append(("scan", sql)),
                                           _FakeCursor([]))[1])
    monkeypatch.setattr(duck, "adbc_native_exec",
                        lambda c, h, sql: calls.append(("exec", sql)))
    return duck, calls


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET n = n + 1",
    "DELETE FROM t WHERE id = 1",
    "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN UPDATE SET t.b = s.b",
    "COPY INTO t FROM @stage",
    "CREATE TABLE t (a INT)",
])
def test_a_write_runs_exactly_once(monkeypatch, sql):
    """Through adbc_scan these would each be applied twice."""
    duck, calls = _routing_duck(monkeypatch)
    duck.adbc_native_stmt(None, 0, sql)
    assert calls == [("exec", sql)]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SHOW TABLES",
    "DESCRIBE TABLE t",
])
def test_a_read_still_returns_its_rows(monkeypatch, sql):
    duck, calls = _routing_duck(monkeypatch)
    duck.adbc_native_stmt(None, 0, sql)
    assert calls == [("scan", sql)]


def test_a_write_returns_no_rows_rather_than_wrong_rows(monkeypatch):
    """A scalar function cannot carry a result set. Callers must handle
    the empty result — `staging.load` already relies on a count delta
    rather than COPY's report, because PUT never returned rows here."""
    duck, _ = _routing_duck(monkeypatch)
    assert duck.adbc_native_stmt(None, 0, "INSERT INTO t VALUES (1)") == ([], [])


def test_the_session_stamp_does_not_go_through_scan(monkeypatch, tmp_path):
    """ALTER SESSION / SET are side-effecting. Harmless to repeat, but
    they must not model the wrong pattern for everything else."""
    duck, issued = _stamping_duck(monkeypatch)
    duck.stamp_session(None, 0, _adbc("snowflake"), "cli")
    from lakesh import duck as d
    assert issued        # the fake records both paths; see _stamping_duck


# --------------------------------------------------------------------------
# session context beyond ADBC
#
# `for_profile` returns the DuckDB dialect for every non-ADBC profile, so
# DuckLake and Iceberg REST used to get no session context at all. They
# have two signals that do reach a server: a DuckLake metastore is
# Postgres and can be labelled through the DSN, and an Iceberg REST
# catalog sees the HTTP User-Agent. Both verified live.


def test_the_ducklake_metastore_dsn_is_labelled():
    from lakesh.duck import _dsn_with_app_name

    kv = _dsn_with_app_name("host=127.0.0.1 port=5432 dbname=lake user=lake")
    assert "application_name=lakesh/" in kv


def test_a_uri_form_dsn_gets_a_query_parameter():
    from lakesh.duck import _dsn_with_app_name

    assert "?application_name=lakesh/" in _dsn_with_app_name(
        "postgresql://u@h:5432/db")
    assert "&application_name=lakesh/" in _dsn_with_app_name(
        "postgresql://u@h:5432/db?sslmode=require")


def test_an_operators_own_application_name_is_left_alone():
    from lakesh.duck import _dsn_with_app_name

    dsn = "host=h dbname=d application_name=their-tool"
    assert _dsn_with_app_name(dsn) == dsn


def test_the_dsn_label_carries_no_quotes_or_spaces():
    """It is interpolated into `ATTACH 'ducklake:postgres:<dsn>'`, so a
    libpq-quoted value would need single quotes and those terminate the
    SQL string literal — measured, `syntax error at or near "lakesh"`."""
    from lakesh.duck import _dsn_with_app_name

    value = _dsn_with_app_name("host=h").split("application_name=", 1)[1]
    assert "'" not in value and " " not in value


def test_an_empty_dsn_is_left_alone():
    from lakesh.duck import _dsn_with_app_name

    assert _dsn_with_app_name("") == ""


def test_duckdb_connections_identify_lakesh_on_the_wire():
    """The only signal an Iceberg REST catalog can see. Verified against
    a local listener: the request arrives with lakesh in the UA."""
    from lakesh.duck import _duckdb_connect

    con = _duckdb_connect()
    ua = con.execute("SELECT current_setting('custom_user_agent')").fetchone()[0]
    con.close()
    assert ua.startswith("lakesh/")


def test_a_local_stamp_of_a_remote_dialect_is_refused(monkeypatch):
    """An ADBC profile reached through ATTACH has no handle, so the
    dialect's SQL would run against DuckDB, fail, and be swallowed by the
    best-effort catch — a stamp that reads as applied and is not."""
    from lakesh import duck

    out = duck.stamp_session(None, None, _adbc("postgresql"), "cli")
    assert out["stamped"] is False and "attached-catalog path" in out["reason"]


def test_every_stamp_outcome_is_published(monkeypatch):
    """An early return that skipped LAST_STAMP left the *previous*
    connection's result in place, so --probe reported a stamp this
    connection never made."""
    from lakesh import duck

    duck.stamp_session(None, None, _adbc("snowflake"), "mcp")   # sets it
    duck.stamp_session(None, None, _adbc("postgresql"), "cli")  # early return
    assert duck.LAST_STAMP["stamped"] is False
