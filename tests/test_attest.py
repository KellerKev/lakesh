"""Signed session attestation.

The properties here were each verified against a live Snowflake account
before being pinned as unit tests; the comments say which.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from lakesh import attest
from lakesh.config import ConfigError, Profile, SigningConfig

jwt = pytest.importorskip("jwt")


@pytest.fixture
def keypair():
    return attest.generate_keypair()


@pytest.fixture
def signed_profile(tmp_path, keypair):
    """An ECDSA profile. `method` must be explicit — the default is
    `hmac`, because a 2.5s-per-query default is the wrong one."""
    priv, _pub = keypair
    path = attest.write_private_key(tmp_path / "k.pem", priv)
    return Profile(
        name="snow", type="adbc", driver="snowflake", uri="u",
        signing=SigningConfig(method="ecdsa", kid="agent-1", key_file=str(path)),
    )


@pytest.fixture
def hmac_profile(tmp_path):
    path = attest.write_private_key(tmp_path / "s.key", attest.generate_secret())
    return Profile(
        name="snow", type="adbc", driver="snowflake", uri="u",
        signing=SigningConfig(kid="agent-1", key_file=str(path)),
    )


# --------------------------------------------------------------------------
# minting

def test_a_token_carries_kid_caller_and_session(signed_profile):
    tok = attest.mint(signed_profile, "mcp", "12345")
    assert jwt.get_unverified_header(tok)["kid"] == "agent-1"
    claims = jwt.decode(tok, options={"verify_signature": False})
    assert claims["sub"] == "mcp" and claims["sid"] == "12345"
    assert claims["iss"] == attest.ISSUER
    assert 0 < claims["exp"] - int(time.time()) <= attest.DEFAULT_TTL_S


def test_an_unbound_token_is_refused(signed_profile):
    """There is deliberately no unbound form. `SET` writes the token to
    QUERY_HISTORY verbatim, kept a year — an unbound one would be a
    long-lived replayable credential sitting in a table."""
    with pytest.raises(attest.SigningError, match="replayable"):
        attest.mint(signed_profile, "cli", "")


def test_each_token_is_unique(signed_profile):
    a = attest.mint(signed_profile, "cli", "1")
    b = attest.mint(signed_profile, "cli", "1")
    assert a != b            # jti differs, so a replay is identifiable


def test_the_token_verifies_against_the_public_half(signed_profile):
    tok = attest.mint(signed_profile, "cli", "42")
    pub = attest.public_key_for(signed_profile)
    claims = jwt.decode(tok, pub, algorithms=[attest.ALGORITHM],
                        issuer=attest.ISSUER)
    assert claims["sid"] == "42"


def test_a_different_key_does_not_verify(signed_profile, tmp_path):
    """The property the whole design rests on."""
    tok = attest.mint(signed_profile, "cli", "42")
    other_priv, other_pub = attest.generate_keypair()
    with pytest.raises(Exception):
        jwt.decode(tok, other_pub, algorithms=[attest.ALGORITHM],
                   issuer=attest.ISSUER)


def test_es256_keeps_the_token_small(signed_profile):
    """It goes into a logged statement, so size is not cosmetic.
    Measured against the live account at 276 characters."""
    assert len(attest.mint(signed_profile, "cli", "1")) < 400


# --------------------------------------------------------------------------
# key sources

def test_env_key_is_used_when_no_file(monkeypatch, keypair):
    priv, _ = keypair
    monkeypatch.setenv("LAKESH_TEST_KEY", priv)
    p = Profile(name="s", type="adbc", driver="snowflake", uri="u",
                signing=SigningConfig(method="ecdsa", kid="k",
                                      key_env="LAKESH_TEST_KEY"))
    assert attest.load_private_key(p) == ("k", priv)


def test_file_outranks_env(monkeypatch, tmp_path, keypair):
    """Explicit beats ambient: an env var may have been inherited from
    anywhere, a path was written down on purpose."""
    priv, _ = keypair
    other, _ = attest.generate_keypair()
    monkeypatch.setenv("LAKESH_TEST_KEY", other)
    path = attest.write_private_key(tmp_path / "k.pem", priv)
    p = Profile(name="s", type="adbc", driver="snowflake", uri="u",
                signing=SigningConfig(method="ecdsa", kid="k",
                                      key_file=str(path),
                                      key_env="LAKESH_TEST_KEY"))
    assert attest.load_private_key(p)[1] == priv


def test_a_world_readable_key_is_refused(tmp_path, keypair):
    priv, _ = keypair
    path = tmp_path / "loose.pem"
    path.write_text(priv)
    path.chmod(0o644)
    p = Profile(name="s", type="adbc", driver="snowflake", uri="u",
                signing=SigningConfig(kid="k", key_file=str(path)))
    with pytest.raises(attest.SigningError, match="readable"):
        attest.load_private_key(p)


def test_a_missing_env_key_says_which_variable(monkeypatch):
    monkeypatch.delenv("LAKESH_NOPE", raising=False)
    p = Profile(name="s", type="adbc", driver="snowflake", uri="u",
                signing=SigningConfig(kid="k", key_env="LAKESH_NOPE"))
    with pytest.raises(attest.SigningError, match="LAKESH_NOPE"):
        attest.load_private_key(p)


def test_ecdsa_with_a_non_key_file_is_refused(tmp_path):
    path = tmp_path / "notakey.pem"
    path.write_text("hello")
    path.chmod(0o600)
    p = Profile(name="s", type="adbc", driver="snowflake", uri="u",
                signing=SigningConfig(method="ecdsa", kid="k", key_file=str(path)))
    with pytest.raises(attest.SigningError, match="needs a PEM"):
        attest.load_private_key(p)


def test_hmac_with_a_pem_key_is_refused(tmp_path, keypair):
    """It would "work" on both sides while quietly abandoning the
    asymmetric guarantee the operator thought they had configured."""
    priv, _ = keypair
    path = attest.write_private_key(tmp_path / "k.pem", priv)
    p = Profile(name="s", type="adbc", driver="snowflake", uri="u",
                signing=SigningConfig(method="hmac", kid="k", key_file=str(path)))
    with pytest.raises(attest.SigningError, match="hmac"):
        attest.load_private_key(p)


def test_keygen_refuses_to_clobber(tmp_path, keypair):
    priv, _ = keypair
    attest.write_private_key(tmp_path / "k.pem", priv)
    with pytest.raises(attest.SigningError, match="already exists"):
        attest.write_private_key(tmp_path / "k.pem", priv)


def test_a_generated_key_is_private(tmp_path, keypair):
    priv, _ = keypair
    path = attest.write_private_key(tmp_path / "k.pem", priv)
    assert path.stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------
# the key never leaks

def test_the_signing_key_is_scrubbed_from_errors(signed_profile):
    from lakesh.redact import profile_secrets, scrub

    _kid, priv = attest.load_private_key(signed_profile)
    leaked = f"driver blew up while running: {priv}"
    assert priv not in scrub(leaked, profile_secrets(signed_profile))


# --------------------------------------------------------------------------
# the generated Snowflake DDL
#
# Shape verified by actually running it against a live account: the
# clause order below is not stylistic, Snowflake rejects the alternative.

def test_immutable_comes_after_language(keypair):
    """Measured: with IMMUTABLE after HANDLER, Snowflake rejects the
    statement with a bare 'syntax error line 7'."""
    sql = attest.snowflake_verifier_sql("G", {"k": ("human", keypair[1])})
    assert sql.index("LANGUAGE PYTHON") < sql.index("IMMUTABLE")
    assert sql.index("IMMUTABLE") < sql.index("HANDLER")


def test_the_verifier_binds_to_the_session(keypair):
    sql = attest.snowflake_verifier_sql("G", {"k": ("human", keypair[1])})
    assert "CURRENT_SESSION()" in sql
    assert 'claims.get("sid") == sid' in sql


def test_the_label_comes_from_the_key_not_the_token(keypair):
    """A key holder could put anything in `sub`, so the verifier must
    never read it."""
    sql = attest.snowflake_verifier_sql("G", {"k": ("human", keypair[1])})
    assert '"sub"' not in sql and "'sub'" not in sql
    assert "return label if" in sql


def test_the_verifier_needs_no_secret_or_external_access(keypair):
    """A public key needs integrity, not confidentiality."""
    sql = attest.snowflake_verifier_sql("G", {"k": ("human", keypair[1])})
    assert "EXTERNAL_ACCESS_INTEGRATIONS" not in sql and "SECRETS" not in sql


def test_multiple_keys_map_to_their_own_labels(keypair):
    _p, pub_a = keypair
    _q, pub_b = attest.generate_keypair()
    sql = attest.snowflake_verifier_sql(
        "G", {"agent-1": ("agent", pub_a), "human-1": ("human", pub_b)})
    assert "'agent-1': ('agent'" in sql and "'human-1': ('human'" in sql


def test_no_keys_is_an_error():
    with pytest.raises(attest.SigningError):
        attest.snowflake_verifier_sql("G", {})


# --------------------------------------------------------------------------
# config validation

def _cfg(**kw):
    return Profile(name="s", type="adbc", driver="snowflake", uri="u",
                   signing=SigningConfig(**kw))


def test_an_empty_signing_block_is_refused():
    """It would leave signing off while looking configured."""
    with pytest.raises(ConfigError, match="key_file"):
        _cfg(kid="k").validate()


def test_signing_without_a_kid_is_refused():
    with pytest.raises(ConfigError, match="kid"):
        _cfg(key_env="X").validate()


def test_a_negative_ttl_is_refused():
    with pytest.raises(ConfigError, match="ttl_s"):
        _cfg(kid="k", key_env="X", ttl_s=-1).validate()


# --------------------------------------------------------------------------
# the HMAC backend
#
# Chosen as the default on measurement: 0.29s through a real masking
# policy over 1M rows against a 0.24s no-policy floor, where the ECDSA
# path costs 2.75s for the same query.


def test_hmac_is_the_default_method():
    """2.5s on every query is the wrong default for an agentic tool."""
    assert SigningConfig().method == "hmac"


def test_the_proof_is_a_sha256_digest(hmac_profile):
    proof = attest.mint(hmac_profile, "cli", "12345")
    assert len(proof) == 64 and int(proof, 16) >= 0


def test_the_proof_matches_the_reference_construction(hmac_profile):
    """Cross-checked digest-for-digest against Python's hmac, which is
    also how the hand-built SQL version was validated."""
    import hashlib
    import hmac as H

    _kid, secret = attest.load_private_key(hmac_profile)
    proof = attest.mint(hmac_profile, "cli", "999")
    assert proof == H.new(secret.strip().encode(), b"999",
                          hashlib.sha256).hexdigest()


def test_a_proof_is_bound_to_its_session(hmac_profile):
    """The property everything rests on: `SET` writes the proof verbatim
    into QUERY_HISTORY, so it must be useless in any other session."""
    assert attest.mint(hmac_profile, "cli", "1") != attest.mint(hmac_profile, "cli", "2")


def test_an_unbound_proof_is_refused(hmac_profile):
    with pytest.raises(attest.SigningError, match="replayable"):
        attest.mint(hmac_profile, "cli", "")


def test_a_different_secret_gives_a_different_proof(tmp_path):
    a = Profile(name="a", type="adbc", driver="snowflake", uri="u",
                signing=SigningConfig(kid="k", key_file=str(
                    attest.write_private_key(tmp_path / "a", attest.generate_secret()))))
    b = Profile(name="b", type="adbc", driver="snowflake", uri="u",
                signing=SigningConfig(kid="k", key_file=str(
                    attest.write_private_key(tmp_path / "b", attest.generate_secret()))))
    assert attest.mint(a, "cli", "1") != attest.mint(b, "cli", "1")


def test_the_proof_carries_no_claims(hmac_profile):
    """A client proves which secret it holds and nothing else — the trust
    label comes from the row in the source's key table that reproduces
    the digest, so a client cannot assert what it is."""
    proof = attest.mint(hmac_profile, "mcp", "1")
    assert proof == attest.mint(hmac_profile, "cli", "1")   # caller is not in it


def test_the_proof_carries_no_timestamp(hmac_profile):
    """Removed deliberately. It cost 0.6s per query, a replayed proof is
    already refused by the session binding, and it made correctness
    depend on the client clock matching Snowflake's to within a minute —
    where skew masks everything and looks like a wrong secret."""
    assert attest.mint(hmac_profile, "cli", "1") == attest.mint(hmac_profile, "cli", "1")
    assert not hasattr(attest, "minute_buckets")


def test_generated_secrets_are_unique_and_long_enough():
    seen = {attest.generate_secret() for _ in range(50)}
    assert len(seen) == 50
    assert all(len(s) == 64 for s in seen)


# --------------------------------------------------------------------------
# the HMAC DDL
#
# Verified live: a role with only SELECT on the protected table is DENIED
# on the keys table, on GET_DDL of the function and the policy, and on
# calling the verifier — and a valid proof still unmasks, because the
# policy body runs with the policy owner's rights.

def test_the_secret_goes_in_a_table_not_the_ddl():
    sql = attest.snowflake_hmac_sql("G", "k", "s3cr3t", "human")
    assert "CREATE TABLE IF NOT EXISTS G.KEYS" in sql
    # It appears once, in the MERGE that seeds the row — never inside a
    # function or policy body, which are what GET_DDL exposes.
    body = sql[sql.index("CREATE OR REPLACE FUNCTION"):]
    assert "s3cr3t" not in body


def _executable(sql: str) -> str:
    """The statements only. The templates carry a lot of explanation, and
    a check for a function name has to ignore prose that names it."""
    return "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))


def test_the_verifier_binds_to_the_session_hmac():
    sql = attest.snowflake_hmac_sql("G", "k", "s", "human")
    assert "CURRENT_SESSION()" in sql


def test_rotation_does_not_replace_the_function():
    """Rotating a secret should be an UPDATE, not DDL."""
    assert "MERGE INTO G.KEYS" in attest.snowflake_hmac_sql("G", "k", "s", "human")


def test_install_sql_follows_the_profile_method(hmac_profile, signed_profile):
    hmac_ddl = attest.install_sql(hmac_profile, "G", "human")
    assert "KEYS" in hmac_ddl and "LANGUAGE PYTHON" not in hmac_ddl
    ecdsa_ddl = attest.install_sql(signed_profile, "G", "human")
    assert "LANGUAGE PYTHON" in ecdsa_ddl and "CREATE TABLE" not in ecdsa_ddl


def test_a_quote_in_a_secret_cannot_break_out():
    sql = attest.snowflake_hmac_sql("G", "k", "it's", "human")
    assert r"\'" in sql


def test_the_keys_table_precedes_the_function_that_reads_it():
    """Snowflake resolves a UDF body's references at creation time. An
    earlier draft emitted a helper function after its caller and failed
    with "unknown function" — and because the policy then masks
    everything, that looked exactly like a wrong secret."""
    sql = attest.snowflake_hmac_sql("G", "k", "s", "human")
    assert sql.index("CREATE TABLE IF NOT EXISTS G.KEYS") < sql.index(
        "FUNCTION G.ATTESTED_CALLER")
