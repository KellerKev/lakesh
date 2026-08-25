"""Tests for credential redaction.

Every assertion here is about the *absence* of a secret, not about the
exact formatting of what replaces it — formatting is allowed to change,
a leak is not.

The PAT below is JWT-*shaped* but fake. Never put a real credential in a
test file.
"""
from __future__ import annotations

import json

import pytest

from lakesh.config import Profile, S3Config, OAuthConfig
from lakesh.redact import (
    MASK,
    profile_secrets,
    redact_option,
    redact_uri,
    scrub,
    uri_password,
)


PAT = (
    "eyJraWQiOiIwMDAwMDAwMDAwMDAwMDAiLCJhbGciOiJFUzI1NiJ9"
    ".eyJwIjoiMDAwMDAwMDA6MDAwMDAwMDAwMCIsImlzcyI6IlNGOjAwMDAiLCJleHAiOjF9"
    ".ZmFrZS1zaWduYXR1cmUtZm9yLXRlc3RzLW9ubHktbm90LWEtcmVhbC10b2tlbg"
)
SF_DSN = f"user.name@corp.example.com:{PAT}@MYORG-ACCOUNT"
PG_URI = "postgresql://reporting:hunter2sekrit@db.example.com:5432/appdb"
LIBPQ = "dbname=ducklake host=/tmp/.pgsock port=55432 user=ducklake password=pgsekrit"


# --------------------------------------------------------------------------
# redact_option

@pytest.mark.parametrize("key", [
    "password", "PASSWORD", "adbc.snowflake.sql.client_secret",
    "auth_token", "access_key", "passcode",
])
def test_redact_option_masks_credential_shaped_keys(key):
    assert redact_option(key, "s3kr1t") == MASK


@pytest.mark.parametrize("key", [
    "adbc.snowflake.sql.account", "adbc.snowflake.sql.warehouse", "role", "db",
])
def test_redact_option_keeps_ordinary_keys(key):
    assert redact_option(key, "PUBLIC") == "PUBLIC"


# --------------------------------------------------------------------------
# redact_uri

def test_redact_uri_drops_snowflake_pat():
    out = redact_uri(SF_DSN)
    assert PAT not in out


def test_redact_uri_splits_on_last_at_so_an_email_username_survives():
    # A Snowflake login name is routinely an email address. Splitting on
    # the first '@' would treat "user.name" as the whole userinfo and
    # leave the PAT sitting in what it thinks is the host.
    out = redact_uri(SF_DSN)
    assert out == f"user.name@corp.example.com:{MASK}@MYORG-ACCOUNT"


def test_redact_uri_keeps_identifying_parts():
    # Username and host identify the connection without authenticating
    # it; dropping them makes the output useless for debugging.
    out = redact_uri(PG_URI)
    assert "hunter2sekrit" not in out
    assert "reporting" in out and "db.example.com:5432" in out and "appdb" in out


def test_redact_uri_handles_uri_without_password():
    uri = "postgresql://appuser@127.0.0.1:5432/appdb"
    assert redact_uri(uri) == uri


def test_redact_uri_handles_uri_without_userinfo():
    uri = "http://127.0.0.1:8181"
    assert redact_uri(uri) == uri


def test_redact_uri_masks_credential_query_params():
    out = redact_uri("postgresql://u@h:5432/db?sslmode=require&sslpassword=abc123")
    assert "abc123" not in out
    assert "sslmode=require" in out


def test_redact_uri_handles_libpq_keyword_dsn():
    # DuckLake profiles authenticate with a keyword DSN, which has no
    # userinfo to split on.
    out = redact_uri(LIBPQ)
    assert "pgsekrit" not in out
    assert "dbname=ducklake" in out and "user=ducklake" in out


def test_redact_uri_passes_through_empty():
    assert redact_uri("") == ""


# --------------------------------------------------------------------------
# uri_password

def test_uri_password_extracts_from_each_shape():
    assert uri_password(SF_DSN) == PAT
    assert uri_password(PG_URI) == "hunter2sekrit"
    assert uri_password(LIBPQ) == "pgsekrit"


def test_uri_password_none_when_absent():
    assert uri_password("postgresql://u@h/db") is None
    assert uri_password("") is None


# --------------------------------------------------------------------------
# profile_secrets + scrub

def _snowflake_profile() -> Profile:
    return Profile(
        name="snowflake", type="adbc", driver="/x/libadbc_driver_snowflake.so",
        uri=SF_DSN, catalog="snow", read_only=True,
        options={
            "adbc.snowflake.sql.account": "MYORG-ACCOUNT",
            "adbc.snowflake.sql.password": "another-sekrit-value",
        },
    )


def test_profile_secrets_collects_every_credential():
    secrets = profile_secrets(_snowflake_profile())
    assert PAT in secrets
    assert "another-sekrit-value" in secrets
    assert "MYORG-ACCOUNT" not in secrets   # identifying, not authenticating


def test_profile_secrets_covers_s3_and_oauth():
    prof = Profile(
        name="p",
        s3=S3Config(access_key="AKIAEXAMPLE", secret_key="s3-secret-value"),
        oauth=OAuthConfig(client_id="cid", client_secret="oauth-secret-value"),
    )
    secrets = profile_secrets(prof)
    assert "s3-secret-value" in secrets
    assert "oauth-secret-value" in secrets


def test_profile_secrets_covers_ducklake_dsn():
    prof = Profile(name="lake", type="ducklake", postgres_dsn=LIBPQ,
                   data_path="s3://b/p/")
    assert "pgsekrit" in profile_secrets(prof)


def test_profile_secrets_ignores_trivially_short_values():
    # Scrubbing a 2-character "secret" would turn the output into confetti.
    prof = Profile(name="p", type="adbc", driver="d",
                   uri="postgresql://u:ab@h/db")
    assert profile_secrets(prof) == set()


def test_scrub_removes_a_dsn_quoted_in_an_error():
    # The path this closes: a driver error quotes the whole statement.
    err = f"ATTACH '{SF_DSN}' AS snow (TYPE adbc, ...) failed: 260001"
    out = scrub(err, profile_secrets(_snowflake_profile()))
    assert PAT not in out
    assert "260001" in out          # the diagnostic itself survives


def test_scrub_is_a_noop_without_secrets():
    assert scrub("plain text", set()) == "plain text"


# --------------------------------------------------------------------------
# the two paths that reach a model

def test_list_profiles_does_not_leak(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""
default = "snowflake"

[profiles.snowflake]
type   = "adbc"
driver = "/x/libadbc_driver_snowflake.so"
uri    = "{SF_DSN}"

[profiles.pg]
type   = "adbc"
driver = "/x/libadbc_driver_postgresql.so"
uri    = "{PG_URI}"
""")
    monkeypatch.setenv("LAKESH_CONFIG", str(cfg))
    from lakesh import mcp as lakesh_mcp

    out = lakesh_mcp.list_profiles()
    assert PAT not in out
    assert "hunter2sekrit" not in out
    # Still useful: the agent can tell the two profiles apart.
    names = {p["name"] for p in json.loads(out)}
    assert names == {"snowflake", "pg"}


def test_mcp_error_scrubs_known_secrets(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""
default = "snowflake"

[profiles.snowflake]
type   = "adbc"
driver = "/x/libadbc_driver_snowflake.so"
uri    = "{SF_DSN}"
""")
    monkeypatch.setenv("LAKESH_CONFIG", str(cfg))
    from lakesh import mcp as lakesh_mcp

    lakesh_mcp._load_or_raise()          # populates the deny-list
    out = lakesh_mcp._error(RuntimeError(f"connect failed for '{SF_DSN}'"))
    assert PAT not in out
    assert "connect failed" in json.loads(out)["error"]


def test_search_objects_does_not_leak(tmp_path, monkeypatch):
    """search_objects echoes `pattern` and the profile name into its
    output and reaches the network, so both its success envelope and its
    error path are routes to the model."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""
default = "snowflake"

[profiles.snowflake]
type   = "adbc"
driver = "/x/libadbc_driver_snowflake.so"
uri    = "{SF_DSN}"
""")
    monkeypatch.setenv("LAKESH_CONFIG", str(cfg))
    from lakesh import mcp as lakesh_mcp

    # No driver at /x/, so this fails inside connect_native — the error
    # path, which is the one that quotes connection details.
    out = lakesh_mcp.search_objects("revenue")
    assert PAT not in out

    # And the same via the all_profiles envelope, which reports the
    # failure per profile rather than raising.
    out = lakesh_mcp.search_objects("revenue", all_profiles=True)
    assert PAT not in out
    assert "snowflake" in out          # still names the profile that failed
