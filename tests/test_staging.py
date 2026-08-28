"""Tests for upload path validation.

This is the first caller-supplied filesystem path lakesh accepts, and
over MCP the caller is a model. The checks are therefore tested
adversarially: the question is not "does the happy path work" but "what
gets through".
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from lakesh.config import Profile
from lakesh.staging import (
    DEFAULT_MAX_UPLOAD_BYTES,
    StagingError,
    max_upload_bytes,
    resolve_upload_path,
    upload_roots,
)


@pytest.fixture
def sandboxed(tmp_path):
    """An allowed root with a file in it, and a secret outside it."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "export.csv").write_text("id\n1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("shh")
    profile = Profile(name="p", upload_roots=(str(allowed),))
    return profile, allowed, outside


# --------------------------------------------------------------------------
# the fence


def test_a_file_inside_an_allowed_root_is_accepted(sandboxed):
    profile, allowed, _ = sandboxed
    resolved = resolve_upload_path(profile, str(allowed / "export.csv"))
    assert resolved == (allowed / "export.csv").resolve()


def test_a_file_outside_every_root_is_refused(sandboxed):
    profile, _, outside = sandboxed
    with pytest.raises(StagingError) as e:
        resolve_upload_path(profile, str(outside / "secret.txt"))
    assert "outside this profile's upload_roots" in str(e.value)


def test_dot_dot_traversal_is_refused(sandboxed):
    profile, allowed, outside = sandboxed
    sneaky = str(allowed / ".." / "outside" / "secret.txt")
    with pytest.raises(StagingError):
        resolve_upload_path(profile, sneaky)


def test_a_symlink_out_of_an_allowed_root_is_refused(sandboxed):
    """The classic bypass. A prefix test on the *unresolved* string
    accepts this, which is why the path is resolved first."""
    profile, allowed, outside = sandboxed
    link = allowed / "innocent.csv"
    link.symlink_to(outside / "secret.txt")
    with pytest.raises(StagingError) as e:
        resolve_upload_path(profile, str(link))
    assert "symlinks are resolved" in str(e.value)


def test_a_symlink_within_the_root_is_still_fine(sandboxed):
    profile, allowed, _ = sandboxed
    link = allowed / "alias.csv"
    link.symlink_to(allowed / "export.csv")
    assert resolve_upload_path(profile, str(link)).name == "export.csv"


def test_no_roots_configured_means_uploads_are_off():
    """An unconfigured allow-list is not an empty one — the feature is
    off, rather than everything being permitted."""
    with pytest.raises(StagingError) as e:
        resolve_upload_path(Profile(name="p"), "/etc/passwd")
    assert "no `upload_roots` configured" in str(e.value)


def test_the_working_directory_is_never_implicitly_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "local.csv").write_text("x")
    with pytest.raises(StagingError):
        resolve_upload_path(Profile(name="p"), "local.csv")


# --------------------------------------------------------------------------
# what may be uploaded


def test_a_missing_file_says_so(sandboxed):
    profile, allowed, _ = sandboxed
    with pytest.raises(StagingError) as e:
        resolve_upload_path(profile, str(allowed / "nope.csv"))
    assert "no such file" in str(e.value)


def test_a_directory_is_refused(sandboxed):
    profile, allowed, _ = sandboxed
    (allowed / "subdir").mkdir()
    with pytest.raises(StagingError) as e:
        resolve_upload_path(profile, str(allowed / "subdir"))
    assert "not a regular file" in str(e.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
def test_a_fifo_is_refused(sandboxed):
    """A FIFO would block the driver forever, or stream something the
    operator never intended."""
    profile, allowed, _ = sandboxed
    fifo = allowed / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(StagingError) as e:
        resolve_upload_path(profile, str(fifo))
    assert "not a regular file" in str(e.value)


def test_an_oversized_file_is_refused(sandboxed):
    profile, allowed, _ = sandboxed
    big = allowed / "big.bin"
    big.write_bytes(b"x" * 2048)
    profile = Profile(name="p", upload_roots=profile.upload_roots,
                      max_upload_bytes=1024)
    with pytest.raises(StagingError) as e:
        resolve_upload_path(profile, str(big))
    assert "max_upload_bytes" in str(e.value)


def test_the_size_cap_has_a_default():
    assert max_upload_bytes(Profile(name="p")) == DEFAULT_MAX_UPLOAD_BYTES
    assert max_upload_bytes(Profile(name="p", max_upload_bytes=10)) == 10


# --------------------------------------------------------------------------
# roots


def test_roots_are_expanded_and_resolved(tmp_path):
    profile = Profile(name="p", upload_roots=(str(tmp_path / "." / "sub"),))
    (tmp_path / "sub").mkdir()
    assert upload_roots(profile) == [(tmp_path / "sub").resolve()]


def test_an_unresolvable_root_allows_nothing_rather_than_everything(tmp_path):
    profile = Profile(name="p", upload_roots=("/definitely/not/here/xyz",))
    (tmp_path / "f.csv").write_text("x")
    with pytest.raises(StagingError):
        resolve_upload_path(profile, str(tmp_path / "f.csv"))


def test_multiple_roots_are_all_honoured(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (b / "f.csv").write_text("x")
    profile = Profile(name="p", upload_roots=(str(a), str(b)))
    assert resolve_upload_path(profile, str(b / "f.csv")).parent == b.resolve()


# --------------------------------------------------------------------------
# the capability seam


def test_only_engines_that_can_stage_report_a_way_to(tmp_path):
    """The registry's contract: an engine without the capability says so
    rather than emitting a statement it does not have."""
    from lakesh import dialect

    sf = Profile(name="s", type="adbc", driver="snowflake", uri="x")
    assert dialect.stage_ops(sf) is not None
    for driver in ("postgresql", "trino"):
        assert dialect.stage_ops(
            Profile(name="p", type="adbc", driver=driver, uri="x")) is None
    assert dialect.stage_ops(Profile(name="l", uri="u", warehouse="w")) is None


def test_an_engine_without_staging_refuses_with_a_reason(tmp_path):
    from lakesh import staging

    (tmp_path / "f.csv").write_text("x")
    pg = Profile(name="pg", type="adbc", driver="postgresql", uri="x",
                 upload_roots=(str(tmp_path),))
    with pytest.raises(staging.StagingError) as e:
        staging.upload(pg, str(tmp_path / "f.csv"), "@~/t")
    assert "no file staging" in str(e.value) and "postgres" in str(e.value)


def test_the_put_statement_quotes_its_path(tmp_path):
    from lakesh.dialect import stage_ops

    ops = stage_ops(Profile(name="s", type="adbc", driver="snowflake", uri="x"))
    sql = ops.put("/tmp/it's a file.csv", "@~/t")
    assert sql.startswith("PUT 'file:///tmp/it\\'s a file.csv'")
    # Compression off, or the staged name gains .gz and the verifying
    # LIST cannot find what it just uploaded.
    assert "AUTO_COMPRESS=FALSE" in sql


def test_snowflake_requires_verification_after_put():
    """A PUT through adbc_scan returns columns and no rows, so its own
    response cannot report success."""
    from lakesh.dialect import stage_ops

    ops = stage_ops(Profile(name="s", type="adbc", driver="snowflake", uri="x"))
    assert ops.verify_after_put is True


def test_upload_fails_loudly_when_the_file_is_not_there_afterwards(tmp_path, monkeypatch):
    """The listing is the only evidence either way, so a missing file
    must be an error rather than a cheerful success."""
    from lakesh import staging

    (tmp_path / "f.csv").write_text("x")
    prof = Profile(name="s", type="adbc", driver="snowflake", uri="x",
                   upload_roots=(str(tmp_path),))
    monkeypatch.setattr(staging, "_run", lambda p, sql: (["name"], []))
    with pytest.raises(staging.StagingError) as e:
        staging.upload(prof, str(tmp_path / "f.csv"), "@~/t")
    assert "treat the upload as failed" in str(e.value)


def test_upload_confirms_by_listing(tmp_path, monkeypatch):
    from lakesh import staging

    (tmp_path / "f.csv").write_text("hello")
    prof = Profile(name="s", type="adbc", driver="snowflake", uri="x",
                   upload_roots=(str(tmp_path),))
    monkeypatch.setattr(
        staging, "_run",
        lambda p, sql: (["name", "size"], [("t/f.csv", 5)]) if sql.startswith("LIST")
        else (["source"], []))
    result = staging.upload(prof, str(tmp_path / "f.csv"), "@~/t")
    assert result["verified"] is True
    assert result["local_bytes"] == 5
