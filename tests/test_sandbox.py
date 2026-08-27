"""Tests for the filesystem sandbox.

A read-only session is not a sandbox on its own: `read_csv('/etc/passwd')`
is a read, so the write gate lets it through. These tests exist to make
sure that stays closed, and — just as important — that closing it does
not break the connections lakesh actually needs.
"""
from __future__ import annotations

import duckdb
import pytest

from lakesh import duck, guard
from lakesh.config import Profile
from lakesh.duck import (
    apply_sandbox,
    explain_sandbox_error,
    needs_local_files,
    sandbox_wanted,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    guard.SESSION.reset_for_tests()
    monkeypatch.setattr(duck, "ALLOW_LOCAL_FILES", False)
    monkeypatch.delenv("LAKESH_ALLOW_LOCAL_FILES", raising=False)
    yield
    guard.SESSION.reset_for_tests()


def _con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


def _reads_local(con) -> bool:
    try:
        con.execute("SELECT * FROM read_text('/etc/hosts')").fetchall()
        return True
    except duckdb.Error:
        return False


# --------------------------------------------------------------------------
# the hole, and that it closes


def test_local_files_are_readable_without_the_sandbox():
    """The behaviour being fixed. If this ever fails, the test below is
    proving nothing."""
    assert _reads_local(_con()) is True


def test_sandbox_blocks_local_file_reads():
    con = _con()
    assert apply_sandbox(con, Profile(name="pg", type="adbc", driver="d")) is None
    assert _reads_local(con) is False


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_text('/etc/hosts')",
    "SELECT * FROM read_csv('/etc/hosts')",
    "SELECT * FROM glob('/etc/*')",
])
def test_sandbox_blocks_every_local_reader(sql):
    con = _con()
    apply_sandbox(con, Profile(name="pg", type="adbc", driver="d"))
    with pytest.raises(duckdb.Error):
        con.execute(sql).fetchall()


def test_duckdb_itself_refuses_to_unlock_it():
    """The guarantee is DuckDB's, not lakesh's: caller SQL cannot undo it
    either, which is stronger than anything enforced in Python."""
    con = _con()
    apply_sandbox(con, Profile(name="pg", type="adbc", driver="d"))
    with pytest.raises(duckdb.Error):
        con.execute("SET disabled_filesystems=''")
    assert _reads_local(con) is False


def test_ordinary_sql_is_unaffected():
    con = _con()
    apply_sandbox(con, Profile(name="pg", type="adbc", driver="d"))
    assert con.execute("SELECT 1 + 1").fetchone() == (2,)


# --------------------------------------------------------------------------
# profiles that cannot be sandboxed


@pytest.mark.parametrize("profile,skips", [
    (Profile(name="l", type="ducklake", postgres_dsn="d", data_path="/var/lake/"), True),
    (Profile(name="s", type="ducklake", postgres_dsn="d", data_path="s3://b/d/"), False),
    (Profile(name="i", type="iceberg-rest", uri="http://x", warehouse="/var/wh"), True),
    (Profile(name="n", type="iceberg-rest", uri="http://x", warehouse="lake"), False),
    (Profile(name="p", type="adbc", driver="postgresql"), False),
])
def test_local_data_paths_skip_the_sandbox(profile, skips):
    """Locking a profile whose data lives on local disk produces a session
    that connects and then fails on every query — worse than not locking.
    Detect it and say so instead."""
    assert (needs_local_files(profile) is not None) is skips


def test_skip_reason_names_the_profile_and_the_path():
    reason = needs_local_files(
        Profile(name="lake", type="ducklake", postgres_dsn="d", data_path="/var/lake/"))
    assert "lake" in reason and "/var/lake/" in reason


def test_apply_sandbox_returns_the_skip_reason_and_leaves_access_open():
    con = _con()
    profile = Profile(name="lake", type="ducklake", postgres_dsn="d", data_path="/var/lake/")
    assert apply_sandbox(con, profile) is not None
    assert _reads_local(con) is True


# --------------------------------------------------------------------------
# when it engages


def test_sandbox_follows_read_only():
    profile = Profile(name="pg", type="adbc", driver="d")
    assert sandbox_wanted(profile) is False
    guard.SESSION.narrow("--read-only")
    assert sandbox_wanted(profile) is True


def test_allow_local_files_opts_out(monkeypatch):
    profile = Profile(name="pg", type="adbc", driver="d")
    guard.SESSION.narrow("--read-only")
    monkeypatch.setattr(duck, "ALLOW_LOCAL_FILES", True)
    assert sandbox_wanted(profile) is False


def test_allow_local_files_env_opts_out(monkeypatch):
    profile = Profile(name="pg", type="adbc", driver="d")
    guard.SESSION.narrow("--read-only")
    monkeypatch.setenv("LAKESH_ALLOW_LOCAL_FILES", "1")
    assert sandbox_wanted(profile) is False


def test_profile_read_only_also_engages_it():
    """Operator policy, not just the caller's flag."""
    assert sandbox_wanted(Profile(name="pg", type="adbc", driver="d", read_only=True))


# --------------------------------------------------------------------------
# it must not break what lakesh needs


def test_extensions_must_be_loaded_before_locking():
    """Extension loading reads the local extension directory, which is why
    the lockdown sits at the tail of each builder rather than earlier."""
    con = _con()
    apply_sandbox(con, Profile(name="pg", type="adbc", driver="d"))
    with pytest.raises(duckdb.Error):
        con.execute("INSTALL httpfs")


def test_an_already_loaded_extension_keeps_working():
    con = _con()
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    apply_sandbox(con, Profile(name="pg", type="adbc", driver="d"))
    # httpfs is still loaded and usable; only the local filesystem is gone.
    assert con.execute("SELECT 1").fetchone() == (1,)
    assert _reads_local(con) is False


def test_http_is_not_blocked_by_the_local_sandbox():
    """S3 and HTTP are what Iceberg and DuckLake read through, so blocking
    them would make the sandbox unusable. A failed HTTP read must be a
    network error, not a permission error."""
    con = _con()
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    apply_sandbox(con, Profile(name="pg", type="adbc", driver="d"))
    try:
        con.execute("SELECT * FROM read_csv('https://127.0.0.1:1/none.csv')").fetchall()
    except duckdb.Error as e:
        assert "has been disabled" not in str(e), str(e)


# --------------------------------------------------------------------------
# reporting


def test_sandbox_error_gets_an_explanatory_hint():
    """The raw error names only the filesystem, which reads like a bug
    rather than a policy — and DuckDB raises the same thing when it tries
    to autoload an extension it needs."""
    con = _con()
    apply_sandbox(con, Profile(name="pg", type="adbc", driver="d"))
    try:
        con.execute("SELECT * FROM read_text('/etc/hosts')").fetchall()
        raise AssertionError("expected a permission error")
    except duckdb.Error as e:
        hint = explain_sandbox_error(e)
        assert hint and "--allow-local-files" in hint


def test_unrelated_errors_get_no_hint():
    assert explain_sandbox_error(ValueError("something else")) is None
