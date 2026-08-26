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
    """The hole `is_read_only`'s own docstring conceded. This statement is
    accepted by the leading-keyword gate today."""
    sql = "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x"
    assert is_read_only(sql) is True        # the old gate waves it through
    assert find_write(sql) == "INSERT"      # the new one does not


def test_find_write_catches_stacked_statements():
    sql = "SELECT 1; DROP TABLE t"
    assert is_read_only(sql) is True
    assert find_write(sql) == "DROP"


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
