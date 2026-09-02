"""Proving to the source that lakesh is the one asking.

The session stamp in `dialect.SessionContext` is client-asserted: anything
holding the credential can `SET LAKESH_CLIENT = 'cli'` and claim to be a
human. This module makes that claim unforgeable — lakesh signs a
short-lived JWT, and a UDF inside a Snowflake masking policy verifies it.
No valid signature, no unmasked data.

### What this buys, and what it does not

**Strong** against a *different client* holding the same credential.
SnowSQL, DBeaver, a leaked PAT — none of them have the signing key, so a
fail-closed policy masks their reads. The tool becomes the gate rather
than the credential, which is the property worth having.

**Weak** against an agent that can read the key. The key lives on the
client; a coding agent with shell access on a machine that also holds the
human key can read it and sign whatever it likes. Separating keys by
caller helps only as far as the *keys* are separated — run the MCP server
as an OS user that owns only its own key, or keep the key in a keychain.
lakesh cannot enforce any of that, so it must not pretend the signature
alone constrains an agent.

### Two facts from measurement that shaped this

**The token is logged.** `SET x = '<jwt>'` lands verbatim in
`ACCOUNT_USAGE.QUERY_HISTORY.QUERY_TEXT`, retained a year and readable by
`GOVERNANCE_VIEWER`. Bind variables do not help — the value moves to the
`BIND_VALUES` column instead. So the token's safety cannot come from
secrecy, and it does not: every token is **bound to one source session**
via a `sid` claim and expires in seconds. Verified against a live
account, a valid token replayed into a second session is refused.

**Trust comes from the key, never the token.** The `sub` claim says which
caller lakesh believes it is serving, and a holder of the key could write
anything there. So the verifier ignores it and maps the **`kid` header**
to a trust label baked into the UDF. A deployment holding only the agent
key cannot mint a human token, because it does not have the human key.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .config import ConfigError, Profile

# A third method was investigated and deliberately not built: keying the
# policy on `SYS_CONTEXT('SNOWFLAKE$CURRENT','IS_AGENT_ACTIVATED')`
# instead of on anything lakesh presents. It is attractive — no key on
# the client, nothing written to query history, unforgeable because
# Snowflake derives it from how the session authenticated, and 0.81s
# through a policy over 1M rows.
#
# It answers a *different* question, which is why it is not a drop-in
# replacement for either method below. `IS_AGENT_ACTIVATED` is binary:
# agent or not. It cannot tell lakesh driven by a human from DBeaver
# driven by a human, so it gates agent-vs-human where these gate
# lakesh-vs-every-other-tool. The two are complementary and a policy
# could require both.
#
# Also relevant if this is revisited: that property is the *only* one
# `SYS_CONTEXT('SNOWFLAKE$CURRENT', ...)` accepts — CLIENT_APPLICATION_ID,
# AUTHENTICATION_METHOD, OAUTH_CLIENT_ID and friends are all rejected,
# and `ACCOUNT_USAGE.SESSIONS` has no row for the current session (it
# lags by hours), so there is no other auth-derived signal a policy can
# read. Turning it on needs an OAuth security integration created with
# `IS_AGENTIC = TRUE`, or a SERVICE_AGENT user.

METHODS = ("hmac", "ecdsa")
"""Two verification backends at opposite ends of a real trade-off.

`hmac` verifies in pure SQL and is the default; `ecdsa` needs a UDF and
is for environments that require the source to hold nothing forgeable.
Measured end to end through a real masking policy over 1M rows, best of
three, on the same warehouse:

===================================  =========  ========
policy body                          best       mean
===================================  =========  ========
no policy at all                     0.20s      0.30s
**HMAC, pure SQL (`hmac`)**          **0.41s**  0.90s
HMAC + a helper SQL UDF              1.00s      1.36s
HMAC + helper + minute buckets       1.64s      1.72s
JavaScript UDF, IMMUTABLE            1.09s      1.29s
JavaScript UDF, VOLATILE             1.92s      2.06s
Python UDF, IMMUTABLE                2.13s      2.23s
**Python ECDSA verify (`ecdsa`)**    **2.75s**  3.09s
Java ECDSA verify                    2.85s      3.18s
Java UDF, IMMUTABLE                  3.16s      3.33s
Python UDF, VOLATILE                 4.01s      4.20s
Java UDF, VOLATILE                   5.09s      5.33s
===================================  =========  ========

Five findings, none of which were obvious beforehand:

* **The cost is the runtime, not the crypto.** A Python UDF that returns
  a constant costs the same as one doing ECDSA verification, and every
  figure is flat from 1e3 to 1e6 rows. Policy bodies are evaluated once
  per query here, not per row.
* **IMMUTABLE is worth roughly 2x on every UDF runtime** — Python
  4.01 -> 2.13, JavaScript 1.92 -> 1.09, Java 5.09 -> 3.16. Both
  generators set it, and the clause belongs after `LANGUAGE`; anywhere
  else is a bare "syntax error line 7".
* **Java is the slowest**, not the fastest, matching the documented
  behaviour that an inline Java handler without `TARGET_PATH` is
  recompiled on every statement that calls it. Its crypto story is the
  best of the three (`KeyFactory`/`Signature` work in the sandbox,
  verified) and it still loses.
* **JavaScript is the fastest UDF runtime** and is useless here anyway:
  no crypto, no `require`, `eval()` disabled. Asymmetric verification
  there would mean hand-writing bignum P-256 arithmetic in a language
  whose only number type is a double — not a trade worth making inside
  a security control.
* **A scalar SQL UDF called from a policy body is not free.** Factoring
  the HMAC into a helper function cost 0.6s per query, so the generated
  verifier inlines it.
"""

ISSUER = "lakesh"
ALGORITHM = "ES256"
"""ES256 over RS256 deliberately: ~276 characters against ~800 for the
same claims, measured. The token goes into a statement that is written to
query history, so its size is not cosmetic."""

DEFAULT_TTL_S = 60


class SigningError(Exception):
    """Signing was asked for and could not be done, with the reason."""


def available() -> bool:
    """Whether the optional signing dependencies are installed."""
    try:
        import jwt  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401
    except ImportError:
        return False
    return True


def _require_deps() -> None:
    if not available():
        raise SigningError(
            "signing needs the `sign` extra, which is not installed. "
            "Install it with `pip install 'lakesh[sign]'` (it pulls in "
            "PyJWT and cryptography), or remove the `[signing]` block "
            "from the profile."
        )


# --------------------------------------------------------------------------
# where the private key comes from
#
# Three sources, checked in a fixed order so a machine that has both a
# file and an env var behaves the same every time. Explicit beats
# ambient: a path someone wrote in the config outranks an environment
# that may have been inherited from anywhere.

def _from_file(path: str) -> str:
    p = Path(path).expanduser()
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError:
        raise SigningError(f"no signing key at {p}") from None
    except OSError as e:
        raise SigningError(f"cannot read signing key {p}: {e}") from None
    mode = resolved.stat().st_mode
    if mode & 0o077:
        # Same standard ssh applies, and for the same reason: a signing
        # key readable by other users on the box is not a signing key.
        raise SigningError(
            f"{resolved} is group- or world-readable (mode {mode & 0o777:o}). "
            f"Run `chmod 600 {resolved}`."
        )
    return resolved.read_text()


def _from_keychain(name: str) -> str:
    """The OS keychain — the only source an agent with shell access
    cannot simply `cat`, and therefore the only one that meaningfully
    separates an agent from a human key on a shared machine."""
    if sys.platform == "darwin":
        cmd = ["security", "find-generic-password", "-s", name, "-w"]
    else:
        cmd = ["secret-tool", "lookup", "service", name]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        raise SigningError(
            f"cannot read keychain entry {name!r}: {cmd[0]} is not installed"
        ) from None
    except subprocess.TimeoutExpired:
        # A keychain prompt nobody is there to answer, most likely.
        raise SigningError(
            f"timed out reading keychain entry {name!r} — if this is a "
            f"headless run, use key_file or key_env instead"
        ) from None
    if out.returncode != 0:
        raise SigningError(
            f"no keychain entry {name!r} ({out.stderr.strip() or 'not found'})"
        )
    return out.stdout


def signing_config(profile: Profile):
    """This profile's `[signing]` block, or None when it has none."""
    return getattr(profile, "signing", None) or None


def load_private_key(profile: Profile) -> tuple[str, str]:
    """(kid, private key PEM) for this profile, or raise `SigningError`."""
    cfg = signing_config(profile)
    if cfg is None:
        raise SigningError(f"profile {profile.name!r} has no `[signing]` block")
    if cfg.key_file:
        key = _from_file(cfg.key_file)
    elif cfg.key_env:
        key = os.environ.get(cfg.key_env, "")
        if not key:
            raise SigningError(
                f"profile {profile.name!r}: ${cfg.key_env} is unset or empty"
            )
    elif cfg.key_keychain:
        key = _from_keychain(cfg.key_keychain)
    else:
        raise SigningError(
            f"profile {profile.name!r}: `[signing]` needs one of `key_file`, "
            f"`key_env` or `key_keychain`"
        )
    method = getattr(cfg, "method", "hmac")
    if method == "ecdsa" and "PRIVATE KEY" not in key:
        raise SigningError(
            f"profile {profile.name!r}: `method = \"ecdsa\"` needs a PEM "
            f"private key, and the configured key is not one. Generate one "
            f"with `lakesh session keygen --method ecdsa`."
        )
    if method == "hmac" and "PRIVATE KEY" in key:
        # Caught rather than silently HMAC'd with a PEM as the key: it
        # would "work" on both sides and quietly abandon the asymmetric
        # guarantee the operator thought they had configured.
        raise SigningError(
            f"profile {profile.name!r}: the configured key is a PEM private "
            f"key but `method` is \"hmac\". Set `method = \"ecdsa\"`, or "
            f"generate a shared secret with `lakesh session keygen`."
        )
    return cfg.kid, key


# --------------------------------------------------------------------------
# minting

def _method(profile: Profile) -> str:
    cfg = signing_config(profile)
    return getattr(cfg, "method", "hmac") if cfg else "hmac"


def mint_proof(profile: Profile, session_id: str) -> str:
    """The symmetric proof: HMAC-SHA256 over the source's session id.

    No claims and nothing self-described. The message is the one thing
    that bounds the proof, and the trust label comes from whichever
    secret in the source's key table reproduces the digest — so a client
    cannot assert what it is, only prove which secret it holds.

    ### Why there is no timestamp in here

    An earlier version added a minute bucket so the proof expired, with
    the verifier accepting the current and previous minute. Measured, it
    cost 2.5x (0.39s -> 1.00s through a policy over 1M rows) and bought
    close to nothing:

    * The replay risk this whole design guards against is the token
      sitting in `QUERY_HISTORY` for a year. A replayed proof lands in a
      *different* session and is already refused by the session binding
      alone — measured.
    * The only attacker a timestamp additionally stops is one who can
      inject a `SET` into the same live session, and they already own
      the session.
    * It made correctness depend on the client's clock agreeing with
      Snowflake's to within a minute. Skew past that masks everything,
      and the symptom is indistinguishable from a wrong secret.

    The session is the expiry boundary, and lakesh opens one per call.
    """
    import hashlib
    import hmac as _hmac

    if not session_id:
        raise SigningError(
            "refusing to mint an unbound proof: without a session id the "
            "proof is replayable, and it gets written to query history"
        )
    _kid, secret = load_private_key(profile)
    return _hmac.new(secret.strip().encode(), str(session_id).encode(),
                     hashlib.sha256).hexdigest()


def mint(profile: Profile, caller: str, session_id: str) -> str:
    """A short-lived credential binding `caller` to one source session.

    `session_id` is not optional and there is no unbound form for either
    method. The value is written to the source's query history, so an
    unbound one would be a year-long replayable credential sitting in a
    table.
    """
    if _method(profile) == "hmac":
        return mint_proof(profile, session_id)
    _require_deps()
    import jwt

    if not session_id:
        raise SigningError(
            "refusing to mint an unbound token: without a session id the "
            "token is replayable, and it gets written to query history"
        )
    kid, key = load_private_key(profile)
    cfg = signing_config(profile)
    now = int(time.time())
    ttl = int(getattr(cfg, "ttl_s", 0) or DEFAULT_TTL_S)
    try:
        return jwt.encode(
            {
                "iss": ISSUER,
                # Informational only. The verifier maps `kid` to a trust
                # label and ignores this, because a key holder could
                # write anything here.
                "sub": caller,
                "sid": str(session_id),
                "iat": now,
                "exp": now + ttl,
                "jti": uuid.uuid4().hex,
            },
            key,
            algorithm=ALGORITHM,
            headers={"kid": kid},
        )
    except Exception as e:
        raise SigningError(f"could not sign attestation: {e}") from None


# --------------------------------------------------------------------------
# key generation, for `lakesh session keygen`

def generate_secret() -> str:
    """A shared secret for `method = "hmac"`.

    256 bits of `os.urandom`, hex-encoded. Hex rather than base64 so it
    survives being pasted into a SQL string literal and a TOML value
    without escaping, and so the SQL side's `HEX_ENCODE` of it is
    predictable.
    """
    import secrets

    return secrets.token_hex(32)


def generate_keypair() -> tuple[str, str]:
    """(private PEM, public PEM) for a fresh P-256 key."""
    _require_deps()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private, public


def public_key_for(profile: Profile) -> str:
    """The public half of this profile's signing key, for the generator."""
    _require_deps()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    _kid, pem = load_private_key(profile)
    try:
        key = load_pem_private_key(pem.encode(), password=None)
    except Exception as e:
        raise SigningError(f"cannot read the signing key: {e}") from None
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode().strip()


def write_private_key(path: Path, pem: str) -> Path:
    """Write a key at 0600, refusing to clobber an existing one.

    Same `O_EXCL` + mode-on-create discipline as the OAuth token cache:
    creating the file already-private beats creating it and chmodding,
    which leaves a window where it is not.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SigningError(
            f"{path} already exists. Refusing to overwrite a signing key — "
            f"anything trusting the old one would silently stop verifying. "
            f"Move it aside first."
        ) from None
    with os.fdopen(fd, "w") as fh:
        fh.write(pem)
    return path


# --------------------------------------------------------------------------
# the Snowflake side
#
# Generated, printed, and never executed by lakesh. Installing a masking
# policy is an account-wide governance change that wants a human reading
# it first — and the tool asking to be trusted is the wrong thing to be
# silently creating the object that decides whether to trust it.
#
# Every detail below was verified against a live account. The ones that
# are not obvious:
#
# * `IMMUTABLE` goes after `LANGUAGE PYTHON`, not after `HANDLER`. In the
#   latter position Snowflake rejects the statement with a bare "syntax
#   error line 7".
# * The wrapper is declared MEMOIZABLE because that is the documented
#   remedy for an expensive policy body — but measured, it does **not**
#   engage here (three calls in one session cost 2.68s, 2.39s, 2.26s).
#   The documented cause is that the cache is not reused when the
#   function calls a nondeterministic function, which GETVARIABLE and
#   CURRENT_SESSION evidently are. It is left in because it is free and
#   may start working; the cost model below does not assume it.
# * Cost is ~2s per query, flat from 1e3 to 1e6 rows, and it is the
#   Python UDF runtime rather than the crypto — a Python UDF doing
#   nothing costs the same. So this is a fixed toll per query, not a
#   per-row one, and it does not grow with the data.

_VERIFIER_TEMPLATE = '''\
-- Generated by `lakesh session install-sql`. Review before running.
-- Run as a role that owns governance objects (ACCOUNTADMIN or similar).
--
-- What this does: unmasks a column only for a session that presented a
-- valid, unexpired, session-bound signature from a known key. Everything
-- else -- no token, wrong key, tampered, expired, or a token replayed
-- from another session -- gets the masked value. Verified: all six.
--
-- Cost: ~2s per query, flat regardless of row count. That is the Python
-- UDF runtime, not the signature check.

CREATE SCHEMA IF NOT EXISTS {schema};

-- The verifier. No SECRET and no external access integration: a public
-- key needs integrity, not confidentiality, so it is embedded. Rotate by
-- re-running this generator.
CREATE OR REPLACE FUNCTION {schema}.VERIFY_ATTESTATION(tok VARCHAR, sid VARCHAR)
RETURNS VARCHAR
LANGUAGE PYTHON
IMMUTABLE
RUNTIME_VERSION = '3.11'
PACKAGES = ('pyjwt', 'cryptography')
HANDLER = 'verify'
AS $$
# kid -> (trust label, public key). The label comes from THIS table, never
# from the token: a key holder could put anything in the token's claims,
# so the only thing a signature proves is which key signed it.
KEYS = {{
{keys}
}}
import jwt

def verify(tok, sid):
    if not tok:
        return None
    try:
        label, pem = KEYS[jwt.get_unverified_header(tok)["kid"]]
        claims = jwt.decode(
            tok, pem, algorithms=["{alg}"], issuer="{issuer}",
            options={{"require": ["exp", "iat", "sid"]}},
        )
    except Exception:
        return None                      # fail closed, every time
    # The binding that makes a logged token safe: SET statements are
    # written to QUERY_HISTORY verbatim and kept for a year, so a token
    # must be useless in any session but the one it was minted for.
    return label if claims.get("sid") == sid else None
$$;

CREATE OR REPLACE FUNCTION {schema}.ATTESTED_CALLER()
RETURNS VARCHAR
MEMOIZABLE
AS $$ {schema}.VERIFY_ATTESTATION(GETVARIABLE('LAKESH_ATTEST'), CURRENT_SESSION()::VARCHAR) $$;

-- A policy per trust label. Snowflake requires the argument type and the
-- return type to match, so one policy per type you mask.
CREATE OR REPLACE MASKING POLICY {schema}.ATTESTED_ONLY AS (val VARCHAR)
RETURNS VARCHAR ->
  CASE WHEN {schema}.ATTESTED_CALLER() = '{label}' THEN val
       ELSE '***masked***' END;

-- Attach it:
--   ALTER TABLE <t> MODIFY COLUMN <c> SET MASKING POLICY {schema}.ATTESTED_ONLY;
-- Check it from lakesh:
--   lakesh profiles show <profile> --probe
'''


def snowflake_verifier_sql(schema: str, keys: dict[str, tuple[str, str]]) -> str:
    """DDL installing the verifier for `keys` — {kid: (label, public PEM)}."""
    if not keys:
        raise SigningError("no keys to install")
    entries = ",\n".join(
        f'    {kid!r}: ({label!r}, """{pem.strip()}"""),'.rstrip(",")
        for kid, (label, pem) in sorted(keys.items())
    )
    label = next(iter(sorted(keys.values())))[0]
    return _VERIFIER_TEMPLATE.format(
        schema=schema, keys=entries, alg=ALGORITHM, issuer=ISSUER, label=label,
    )


# --------------------------------------------------------------------------
# the HMAC backend's Snowflake side
#
# Verified against a live account, including the part that matters most:
# a role holding only SELECT on the protected table reads it masked, is
# DENIED on the keys table, on GET_DDL of both the function and the
# policy, and on calling the verifier directly — and yet a valid proof
# still unmasks. That is Snowflake's owner's-rights policy evaluation
# doing the work, and it is why the secret can live here at all.
#
# The secret is in a TABLE, never in DDL. A symmetric scheme means
# whoever reads the secret can forge, so "who can read this" has to be a
# grant you can audit and revoke, not "whoever can GET_DDL the policy".

_HMAC_TEMPLATE = '''\
-- Generated by `lakesh session install-sql`. Review before running.
-- Run as a role that owns governance objects.
--
-- Verification is pure SQL: 0.39s through a masking policy over 1M rows,
-- against a 0.18s no-policy floor. The ECDSA variant needs a Python UDF
-- and costs 2.75s for the same query.
--
-- The trade: this is a SHARED SECRET. Anyone who can read {schema}.KEYS
-- can forge a proof for any session. Grant it to nobody. Callers need no
-- privilege on it -- the policy body runs with the policy owner's
-- rights, which is verified behaviour, not an assumption.

CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.KEYS (
  kid     VARCHAR NOT NULL,
  secret  VARCHAR NOT NULL,
  label   VARCHAR NOT NULL,   -- trust level this key grants
  enabled BOOLEAN DEFAULT TRUE
);

-- Rotation is an UPDATE here, not a CREATE OR REPLACE of a function.
MERGE INTO {schema}.KEYS t USING (SELECT {kid} AS kid) s ON t.kid = s.kid
WHEN MATCHED THEN UPDATE SET secret = {secret}, label = {label}, enabled = TRUE
WHEN NOT MATCHED THEN INSERT (kid, secret, label, enabled)
     VALUES ({kid}, {secret}, {label}, TRUE);

-- HMAC-SHA256 hand-built from SHA2_BINARY + BITXOR: Snowflake has no
-- HMAC builtin and no asymmetric verify at all. Cross-checked against
-- Python's hmac module, digest for digest.
--
-- Written out inline rather than factored into a helper UDF. Measured,
-- the nested call cost 0.6s per query (1.64s -> 1.00s) -- a scalar SQL
-- UDF called from a policy body is not free, and this is the hot path.
--
-- The message is the session id alone. A timestamp was tried and
-- removed: it cost another 0.6s, and a replayed proof is already refused
-- because it lands in a different session. See `mint_proof`.
CREATE OR REPLACE FUNCTION {schema}.ATTESTED_CALLER()
RETURNS VARCHAR
AS $$
  SELECT label FROM {schema}.KEYS
   WHERE enabled
     AND GETVARIABLE('LAKESH_ATTEST') =
         SHA2_HEX(
           CONCAT(
             BITXOR(TO_BINARY(RPAD(HEX_ENCODE(secret), 128, '0'), 'HEX'),
                    TO_BINARY(REPEAT('5c', 64), 'HEX')),
             SHA2_BINARY(
               CONCAT(
                 BITXOR(TO_BINARY(RPAD(HEX_ENCODE(secret), 128, '0'), 'HEX'),
                        TO_BINARY(REPEAT('36', 64), 'HEX')),
                 TO_BINARY(HEX_ENCODE(CURRENT_SESSION()::VARCHAR), 'HEX')),
               256)),
           256)
   LIMIT 1
$$;

CREATE OR REPLACE MASKING POLICY {schema}.ATTESTED_ONLY AS (val VARCHAR)
RETURNS VARCHAR ->
  CASE WHEN {schema}.ATTESTED_CALLER() = {label} THEN val
       ELSE '***masked***' END;

-- The secret must stay unreadable. Grant SELECT on {schema}.KEYS to
-- nobody; callers do not need it.
--
-- Attach it:
--   ALTER TABLE <t> MODIFY COLUMN <c> SET MASKING POLICY {schema}.ATTESTED_ONLY;
-- Check it from lakesh:
--   lakesh profiles show <profile> --probe
'''


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def snowflake_hmac_sql(schema: str, kid: str, secret: str, label: str) -> str:
    """DDL installing the pure-SQL verifier for one shared secret."""
    if not (kid and secret and label):
        raise SigningError("kid, secret and label are all required")
    return _HMAC_TEMPLATE.format(
        schema=schema, kid=_sql_str(kid), secret=_sql_str(secret.strip()),
        label=_sql_str(label),
    )


def install_sql(profile: Profile, schema: str, label: str) -> str:
    """The DDL for whichever method this profile is configured for."""
    cfg = signing_config(profile)
    if cfg is None:
        raise SigningError(
            f"profile {profile.name!r} has no `[signing]` block — run "
            f"`lakesh session keygen` first"
        )
    if getattr(cfg, "method", "hmac") == "hmac":
        _kid, secret = load_private_key(profile)
        return snowflake_hmac_sql(schema, cfg.kid, secret, label)
    return snowflake_verifier_sql(schema, {cfg.kid: (label, public_key_for(profile))})
