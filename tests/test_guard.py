"""Tests for the write gate and the session ratchet.

The two properties worth protecting here are that a write cannot be
smuggled past the gate, and that a restriction cannot be lifted once set.
The second is tested partly by asserting an API does *not* exist — that is
deliberate, because "cannot be relaxed" is enforced by the absence of a
widening method rather than by a check something could route around.
"""
from __future__ import annotations

import inspect

import pytest

from lakesh import guard
from lakesh.config import Profile
from lakesh.guard import (
    POLICY,
    _leads_like_read,
    USER,
    Restriction,
    Session,
    blocks_write,
    find_write,
    is_read_only,
    refusal,
    strip_literals,
)


@pytest.fixture(autouse=True)
def _fresh_session():
    guard.SESSION.reset_for_tests()
    yield
    guard.SESSION.reset_for_tests()


# --------------------------------------------------------------------------
# strip_literals


@pytest.mark.parametrize("sql,gone", [
    ("SELECT * FROM t WHERE note = 'please delete'", "delete"),
    ('SELECT "drop" FROM t', "drop"),
    ("SELECT 1 -- drop table t", "drop"),
    ("SELECT 1 /* insert into t */", "insert"),
    ("SELECT `truncate` FROM t", "truncate"),
])
def test_strip_literals_blanks_the_dangerous_lookalikes(sql, gone):
    assert gone not in strip_literals(sql).lower()


def test_strip_literals_preserves_length():
    """Length-preserving so any offset computed against the result still
    lines up with the original."""
    sql = "SELECT * FROM t WHERE a = 'xx' -- note"
    assert len(strip_literals(sql)) == len(sql)


def test_strip_literals_handles_escaped_quotes():
    assert "delete" not in strip_literals("SELECT 'it''s delete time' FROM t").lower()


# --------------------------------------------------------------------------
# find_write — the holes this closes


def test_find_write_catches_cte_smuggling():
    """A statement that *begins* like a read but is not one. The
    leading-keyword test alone accepts it, which is why `is_read_only`
    consults `find_write` too."""
    sql = "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x"
    assert _leads_like_read(sql) is True    # looks fine at the head
    assert find_write(sql) == "INSERT"      # ...but it is not
    assert is_read_only(sql) is False       # so the public answer is no


def test_find_write_catches_stacked_statements():
    sql = "SELECT 1; DROP TABLE t"
    assert _leads_like_read(sql) is True
    assert find_write(sql) == "DROP"
    assert is_read_only(sql) is False


@pytest.mark.parametrize("sql,verb", [
    ("INSERT INTO t VALUES (1)", "INSERT"),
    ("UPDATE t SET a = 1", "UPDATE"),
    ("DELETE FROM t", "DELETE"),
    ("CREATE TABLE t (id int)", "CREATE"),
    ("DROP TABLE t", "DROP"),
    ("ALTER TABLE t ADD COLUMN c int", "ALTER"),
    ("TRUNCATE t", "TRUNCATE"),
    ("GRANT SELECT ON t TO r", "GRANT"),
    # Reaching outside the session is a write in every sense that matters:
    # a read-only session that can ATTACH a writable database is not one.
    ("ATTACH 'other.db'", "ATTACH"),
    ("COPY t TO 'out.csv'", "COPY"),
    ("INSTALL httpfs", "INSTALL"),
])
def test_find_write_catches_the_verbs(sql, verb):
    assert find_write(sql) == verb


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT delete_flag, created_at FROM t",       # column named like a verb
    "SELECT * FROM updates",                        # table named like a verb
    "SELECT * FROM t WHERE note = 'please delete everything'",
    "SELECT 1 -- drop table t",
    "SELECT 1 /* insert into t */",
    'SELECT "drop" FROM t',
    "SHOW DATABASES",
    "EXPLAIN SELECT 1",
    "SELECT * FROM a.b QUALIFY row_number() OVER (ORDER BY 1) = 1",
])
def test_find_write_leaves_reads_alone(sql):
    """False positives here break legitimate queries, which is the failure
    mode that gets a safety feature switched off."""
    assert find_write(sql) is None


def test_blocks_write_combines_both_checks():
    assert blocks_write("WITH x AS (INSERT INTO t VALUES (1)) SELECT * FROM x") == "INSERT"
    assert blocks_write("SELECT 1") is None
    assert blocks_write("EXPLAIN SELECT 1") is None
    # Something that neither carries a listed verb nor looks like a read.
    assert blocks_write("MAINTAIN t") is not None


# --------------------------------------------------------------------------
# the ratchet


def test_narrow_has_no_inverse():
    """'Cannot be relaxed' is enforced by the absence of an API, not by a
    check. If someone adds a widening method, this fails."""
    assert not hasattr(Session, "widen")
    assert not hasattr(Session, "relax")
    params = inspect.signature(Session.narrow).parameters
    assert list(params) == ["self", "set_by"]
    assert all(p.annotation is not bool for p in params.values())


def test_narrow_latches_and_is_idempotent():
    s = Session()
    first = s.narrow("--read-only")
    second = s.narrow("something else")
    assert first.read_only and second.read_only
    assert second.set_by == "--read-only"        # the first wins; no re-labelling


def test_user_narrowing_survives_a_config_that_stops_restricting():
    """The operator loosening config mid-session must not undo a caller's
    latch — that is the whole point of the ratchet."""
    s = Session()
    open_profile = Profile(name="p")
    assert s.effective(open_profile).read_only is False
    s.narrow("set_read_only tool")
    assert s.effective(open_profile).read_only is True


def test_policy_label_wins_when_both_apply():
    """The caller needs to know it would still be restricted in a fresh
    session, so policy owns the label."""
    s = Session()
    s.narrow("set_read_only tool")
    eff = s.effective(Profile(name="p", read_only=True))
    assert eff.source == POLICY
    assert "read_only" in eff.set_by


def test_profile_read_only_is_policy():
    eff = Session().effective(Profile(name="snow", read_only=True))
    assert eff.read_only and eff.source == POLICY
    assert "snow" in eff.set_by


def test_env_read_only_is_policy(monkeypatch):
    monkeypatch.setenv("LAKESH_READ_ONLY", "1")
    eff = Session().effective(Profile(name="p"))
    assert eff.read_only and eff.source == POLICY
    assert eff.set_by == "LAKESH_READ_ONLY"


def test_launch_flag_is_policy():
    s = Session()
    s.set_policy_flag("lakesh mcp --read-only")
    eff = s.effective(Profile(name="p"))
    assert eff.source == POLICY and eff.set_by == "lakesh mcp --read-only"


def test_unrestricted_by_default(monkeypatch):
    monkeypatch.delenv("LAKESH_READ_ONLY", raising=False)
    assert Session().effective(Profile(name="p")).read_only is False


# --------------------------------------------------------------------------
# reporting


def test_refusal_names_the_verb_and_the_source():
    policy = refusal(Restriction(True, POLICY, "profile 'snow' read_only"), "DROP")
    assert policy["error_type"] == "read_only_blocked"
    assert policy["blocked"] == "DROP"
    assert "profile 'snow' read_only" in policy["error"]
    assert "LAKESH_MCP_WRITE does not override it" in policy["error"]
    assert policy["restriction"]["relaxable"] is False

    user = refusal(Restriction(True, USER, "set_read_only tool"), "INSERT")
    assert "set_read_only tool" in user["error"]
    assert "start a new session" in user["error"]


def test_describe_says_where_it_came_from():
    assert Restriction().describe() == "unrestricted"
    assert "operator's config" in Restriction(True, POLICY, "x").describe()
    assert "this session" in Restriction(True, USER, "y").describe()


# --------------------------------------------------------------------------
# the native-SQL corpus
#
# lakesh is a universal tool, so the gate has to cope with each engine's
# own spelling. Every row here was refused (or wrongly allowed) at some
# point; naming them individually means a regression is caught by name
# rather than by a vague "reads still work" assertion.


READS = [
    # Scalar functions whose names collide with DDL verbs. These exist on
    # every engine and were all refused, because the character before the
    # word was `(` and the old rule read that as "start of a subquery".
    ("SELECT coalesce(replace(name,'a','b'),'') FROM t", "REPLACE() is a scalar fn"),
    ("SELECT round(truncate(x,2)) FROM t",               "TRUNCATE() is numeric on MySQL/Snowflake"),
    ("SELECT CASE WHEN x THEN replace(a,'a','b') ELSE a END FROM t", "the same, after THEN"),
    ("SELECT insert(s,1,2,'x') FROM t",                  "INSERT() is a MySQL string fn"),
    ("SELECT * FROM tbl AS load",                        "an alias colliding with a verb"),
    # Two spellings of one statement that used to get opposite verdicts.
    ("EXPLAIN (ANALYZE, FORMAT JSON) SELECT 1",          "the Postgres/DuckDB spelling"),
    ("EXPLAIN ANALYZE SELECT 1",                         "the bare spelling"),
    # A commented statement is still a statement.
    ("/* note */ SELECT 1",                              "leading block comment"),
    ("-- note\nSELECT 1",                                "leading line comment"),
    # Per-engine read spellings.
    ("FROM my_table",                                    "DuckDB FROM-first"),
    ("TABLE my_table",                                   "Postgres/DuckDB TABLE"),
    ("LIST @my_stage",                                   "Snowflake stage listing"),
    ("SHOW TERSE TABLES IN SCHEMA x",                    "Snowflake SHOW"),
    ("DESCRIBE TABLE t",                                 "Snowflake DESCRIBE"),
    ("SELECT * FROM a.b QUALIFY row_number() OVER (ORDER BY 1) = 1", "Snowflake QUALIFY"),
    # The transaction form a read-only session most wants.
    ("BEGIN READ ONLY",                                  "read-only transaction"),
    ("START TRANSACTION READ ONLY",                      "the same, ANSI spelling"),
    # Session settings are a prerequisite for many read workloads.
    ("SET SESSION statement_timeout = '5s'",             "session setting"),
    ("SET TIME ZONE 'UTC'",                              "session setting"),
    # Literals and names that merely look dangerous.
    ("SELECT * FROM t WHERE note = 'please delete'",     "a literal"),
    ("SELECT delete_flag FROM updates",                  "verb-like identifiers"),
    ("SELECT $q$it's fine$q$",                           "tagged dollar literal"),
]

WRITES = [
    ("DO $$ BEGIN DELETE FROM t; END $$",                "DO", "Postgres DO block"),
    ("CREATE FUNCTION f() AS $BODY$ DROP TABLE x; $BODY$ LANGUAGE sql",
     "CREATE", "tagged $BODY$ dollar-quoting"),
    ("EXECUTE IMMEDIATE $$ DELETE FROM t $$",            "EXECUTE", "Snowflake scripting"),
    ("WITH x AS (INSERT INTO t VALUES (1)) SELECT * FROM x", "INSERT", "CTE smuggling"),
    ("SELECT 1; DROP TABLE t",                           "DROP", "stacked statements"),
    ("BEGIN",                                            "BEGIN", "a writable transaction"),
    ("SET x = 1",                                        "SET", "a non-session SET"),
    ("ATTACH 'other.db'",                                "ATTACH", "reaching outside"),
]


@pytest.mark.parametrize("sql,why", READS, ids=[s[:38] for s, _ in READS])
def test_native_reads_are_allowed(sql, why):
    assert blocks_write(sql) is None, why


@pytest.mark.parametrize("sql,verb,why", WRITES, ids=[s[:38] for s, _, _ in WRITES])
def test_native_writes_are_caught(sql, verb, why):
    assert blocks_write(sql) == verb, why


def test_dollar_bodies_are_visible_to_the_gate_but_not_to_masking():
    """The same text is a literal for masking and executable code for the
    gate. One function cannot serve both, which is why `keep_bodies`
    exists — blanking the body is how `DO $$ … DELETE … $$` passed."""
    sql = "DO $$ BEGIN DELETE FROM t; END $$"
    assert "DELETE" in strip_literals(sql, keep_bodies=True)
    assert "DELETE" not in strip_literals(sql)


def test_tagged_dollar_quoting_is_recognised():
    """`$BODY$ … $BODY$` is the standard Postgres CREATE FUNCTION form and
    was not handled at all."""
    assert "secret" not in strip_literals("SELECT $BODY$secret$BODY$").lower()


def test_hash_comments_are_stripped():
    """MySQL's and BigQuery's second line-comment syntax."""
    assert "drop" not in strip_literals("SELECT 1 # drop table t").lower()


# --------------------------------------------------------------------------
# CALL, which cannot be classified from the statement


def test_call_is_a_write_unless_vouched_for():
    """Snowflake exposes no way to know what a procedure does: volatility
    keywords are deprecated for procedures, bodies build SQL at runtime,
    and procedures are not atomic. lakesh never guesses."""
    guard.set_read_procedures([])
    assert blocks_write("CALL anything()") == "CALL"


def test_vouched_procedures_are_reads():
    guard.set_read_procedures(["ducklake_snapshots", "ducklake_table_info"])
    assert blocks_write("CALL ducklake_snapshots('lake')") is None
    assert blocks_write("CALL ducklake_table_info('lake')") is None
    # ...and the write procedures in the same family still are not.
    assert blocks_write("CALL ducklake_merge_adjacent_files('lake')") == "CALL"
    guard.set_read_procedures([])


def test_vouching_is_case_insensitive_and_ignores_qualification():
    guard.set_read_procedures(["my_proc"])
    assert blocks_write("CALL MY_PROC()") is None
    assert blocks_write("CALL db.schema.my_proc()") is None
    guard.set_read_procedures([])


def test_the_table_function_spelling_was_always_allowed():
    """`SELECT * FROM ducklake_snapshots(…)` is the same read, and it
    worked while the CALL spelling did not — which is what made the
    inconsistency obvious."""
    assert blocks_write("SELECT * FROM ducklake_snapshots('lake')") is None
