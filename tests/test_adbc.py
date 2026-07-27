"""Unit tests for the ADBC connection builders. The secret/ATTACH SQL is
asserted against a recording fake connection — no adbc_scanner install
or driver needed. A live end-to-end test is gated on the sqlite ADBC
driver being present."""
from __future__ import annotations

import os

import duckdb
import pytest

from lakesh.config import ConfigError, Profile
from lakesh.duck import (
    _adbc_attach_sql,
    _adbc_options,
    _install_adbc_secret,
    _split_adbc_options,
    load_adbc_scanner,
)


class _RecordingCon:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple[str, list | None]] = []
        self.fail_on = fail_on

    def execute(self, sql: str, params: list | None = None):
        if self.fail_on and self.fail_on in sql:
            raise duckdb.Error(f"boom: {sql}")
        self.calls.append((sql, params))
        return self


def _adbc_profile(**kw) -> Profile:
    defaults = dict(
        name="pg", type="adbc", driver="postgresql",
        uri="postgresql://h:5432/db", catalog="pg",
    )
    defaults.update(kw)
    return Profile(**defaults)


# --------------------------------------------------------------------------
# option merging + token injection

def test_adbc_options_token_option_key():
    prof = _adbc_profile(
        options={"username": "u"}, token_option="auth_token"
    )
    assert _adbc_options(prof, "TOK") == {"username": "u", "auth_token": "TOK"}


def test_adbc_options_placeholder():
    prof = _adbc_profile(
        options={"authorization_header": "Bearer {token}"}
    )
    assert _adbc_options(prof, "TOK") == {"authorization_header": "Bearer TOK"}


def test_adbc_options_no_token_passthrough():
    prof = _adbc_profile(options={"username": "u"}, token_option="auth_token")
    assert _adbc_options(prof, None) == {"username": "u"}


# --------------------------------------------------------------------------
# option splitting: credential-shaped keys → SECRET, the rest → ATTACH

def test_split_adbc_options():
    secret, attach = _split_adbc_options({
        "username": "u",
        "password": "pw",
        "database": "db",
        "entrypoint": "ep",
        "adbc.snowflake.sql.account": "acct",
        "batch_size": "1000",
    })
    assert secret == {
        "username": "u", "password": "pw", "database": "db", "entrypoint": "ep",
    }
    assert attach == {"adbc.snowflake.sql.account": "acct", "batch_size": "1000"}


# --------------------------------------------------------------------------
# secret SQL construction

def test_install_adbc_secret_binds_values_as_params():
    con = _RecordingCon()
    prof = _adbc_profile()
    _install_adbc_secret(con, prof, {"username": "u", "password": "s3cret"})
    assert len(con.calls) == 1
    sql, params = con.calls[0]
    assert "CREATE OR REPLACE SECRET adbc_pg" in sql
    assert "TYPE adbc" in sql
    assert '"username" ?' in sql
    assert '"password" ?' in sql
    # secrets are only ever in the params list, never in SQL text
    assert "s3cret" not in sql
    assert params == [
        "postgresql://h:5432/db",   # SCOPE
        "postgresql",               # driver
        "postgresql://h:5432/db",   # uri
        "u",
        "s3cret",
    ]


def test_install_adbc_secret_rejects_non_secret_key():
    con = _RecordingCon()
    prof = _adbc_profile()
    with pytest.raises(ConfigError, match="not storable in an adbc secret"):
        _install_adbc_secret(con, prof, {"adbc.some.key": "v"})


# --------------------------------------------------------------------------
# ATTACH SQL construction

def test_attach_sql_with_dotted_options():
    prof = _adbc_profile(read_only=True)
    sql = _adbc_attach_sql(prof, {"adbc.postgresql.some_key": "v"})
    assert sql == (
        "ATTACH 'postgresql://h:5432/db' AS pg "
        "(TYPE adbc, driver 'postgresql', \"adbc.postgresql.some_key\" 'v', "
        "READ_ONLY)"
    )


def test_attach_sql_escapes_quotes():
    prof = _adbc_profile(uri="post'gres")
    sql = _adbc_attach_sql(prof, {"k": "va'lue"})
    assert "post''gres" in sql
    assert "'va''lue'" in sql


def test_attach_sql_rejects_bad_option_key():
    prof = _adbc_profile()
    with pytest.raises(ConfigError, match="invalid adbc option key"):
        _adbc_attach_sql(prof, {'x" (TYPE S3); --': "v"})


# --------------------------------------------------------------------------
# extension loading

def test_load_adbc_scanner_graceful_failure(capsys):
    import lakesh.duck as duck
    duck._adbc_warned = False
    con = _RecordingCon(fail_on="INSTALL")
    assert load_adbc_scanner(con) is False
    err = capsys.readouterr().err
    assert "adbc_scanner extension unavailable" in err
    # warns only once per process
    assert load_adbc_scanner(con) is False
    assert "adbc_scanner" not in capsys.readouterr().err


def test_load_adbc_scanner_required_reraises():
    con = _RecordingCon(fail_on="INSTALL")
    with pytest.raises(duckdb.Error):
        load_adbc_scanner(con, required=True)


# --------------------------------------------------------------------------
# live end-to-end. Uses the sqlite driver shipped by the
# `adbc-driver-sqlite` PyPI package (a dev dependency), so it only needs
# network for the community-extension install. `driver` accepts a full
# path to the shared library, same as a `dbc`-installed manifest name.

def _sqlite_driver_path() -> str | None:
    try:
        import adbc_driver_sqlite
        return adbc_driver_sqlite._driver_path()
    except Exception:
        return None


def _adbc_available() -> bool:
    if os.environ.get("LAKESH_SKIP_ADBC_TESTS") == "1":
        return False
    if _sqlite_driver_path() is None:
        return False
    try:
        con = duckdb.connect(":memory:")
        con.execute("INSTALL adbc_scanner FROM community")
        con.execute("LOAD adbc_scanner")
        con.close()
        return True
    except Exception:
        return False


needs_adbc = pytest.mark.skipif(
    not _adbc_available(),
    reason="adbc_scanner extension or sqlite ADBC driver unavailable",
)


@needs_adbc
def test_adbc_sqlite_end_to_end(tmp_path):
    import sqlite3

    from lakesh.duck import catalog_alias, connect

    db = tmp_path / "t.sqlite"
    with sqlite3.connect(db) as sq:
        sq.execute("CREATE TABLE items (id INTEGER, name TEXT)")
        sq.execute("INSERT INTO items VALUES (1, 'a'), (2, 'b')")

    prof = Profile(
        name="sq", type="adbc", driver=_sqlite_driver_path(),
        uri=str(db), catalog="sq",
    )
    con = connect(prof)
    rows = con.execute(
        f"SELECT COUNT(*) FROM {catalog_alias(prof)}.main.items"
    ).fetchone()
    assert rows[0] == 2
    con.close()
