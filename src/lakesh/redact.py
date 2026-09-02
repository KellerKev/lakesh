"""Credential redaction — one place that knows what a secret looks like.

Two consumers, with different stakes:

* The CLI's `config show` / `profiles show`, which advertise "secrets
  redacted" and are read by a human.
* The MCP server's `list_profiles` and every tool's error payload, which
  are read by an LLM. Whatever goes out there lands in a model's context
  and in whatever transcript or telemetry sits behind it, so a leak is
  not recoverable by deleting a scrollback buffer.

For several ADBC drivers the connection URI *is* the credential — a
gosnowflake DSN is `USER:PAT@ACCOUNT`, a libpq URI is
`postgresql://user:password@host/db` — so printing `profile.uri`
verbatim ships a live token. `redact_uri` is the fix; `scrub` is the
backstop for free text (driver errors love to quote the whole DSN).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Profile

MASK = "***"

# What a credential-shaped option key looks like. Deliberately broad: a
# false positive costs a human one `grep` in their own config file, a
# false negative ships a token to a model.
SECRET_OPTION_RE = re.compile(r"password|secret|token|key|passcode", re.IGNORECASE)

# A libpq keyword/value DSN (`dbname=lake host=/tmp password=hunter2`) —
# no userinfo to split on, so it needs its own pass. DuckLake profiles
# authenticate this way.
_LIBPQ_PAIR_RE = re.compile(r"(\S+?)=(\S*)")


def redact_option(key: str, value: str) -> str:
    """Mask a value whose *key* looks credential-shaped."""
    return MASK if SECRET_OPTION_RE.search(key) else value


def _split_authority(uri: str) -> tuple[str, str, str, str]:
    """(scheme_with_sep, authority, tail, whole_had_scheme).

    Handles both `scheme://…` URIs and bare DSNs like the gosnowflake
    `USER:PAT@ACCOUNT`, which carry no scheme at all.
    """
    scheme, sep, rest = uri.partition("://")
    if not sep:
        scheme, sep, rest = "", "", uri
    cut = len(rest)
    for ch in "/?#":
        i = rest.find(ch)
        if i != -1:
            cut = min(cut, i)
    return f"{scheme}{sep}", rest[:cut], rest[cut:], sep


def _redact_query(tail: str) -> str:
    """Mask credential-shaped query params (`?sslpassword=…&token=…`)."""
    if not tail.startswith("?") and "?" not in tail:
        return tail
    path, sep, query = tail.partition("?")
    if not sep:
        return tail
    parts = []
    for part in query.split("&"):
        key, eq, _value = part.partition("=")
        parts.append(f"{key}{eq}{MASK}" if eq and SECRET_OPTION_RE.search(key) else part)
    return f"{path}{sep}{'&'.join(parts)}"


def _redact_libpq(dsn: str) -> str:
    return _LIBPQ_PAIR_RE.sub(
        lambda m: f"{m.group(1)}={MASK}" if SECRET_OPTION_RE.search(m.group(1))
        else m.group(0),
        dsn,
    )


def _libpq_password(dsn: str) -> str | None:
    for key, value in _LIBPQ_PAIR_RE.findall(dsn):
        if SECRET_OPTION_RE.search(key) and value:
            return value
    return None


def _looks_libpq(uri: str) -> bool:
    return "://" not in uri and "=" in uri


def redact_uri(uri: str) -> str:
    """Strip the userinfo password and credential-shaped query params.

    Keeps the username and host: those identify the connection without
    authenticating it, and dropping them makes the output useless for
    the debugging it exists to support.
    """
    if not uri:
        return uri
    if _looks_libpq(uri):
        return _redact_libpq(uri)

    prefix, authority, tail, _had_scheme = _split_authority(uri)
    if "@" in authority:
        # A Snowflake login name is frequently an email address and so
        # contains an '@' of its own — the userinfo/host boundary is the
        # LAST '@', not the first.
        userinfo, _, host = authority.rpartition("@")
        user, colon, _password = userinfo.partition(":")
        if colon:
            userinfo = f"{user}:{MASK}"
        authority = f"{userinfo}@{host}"
    return f"{prefix}{authority}{_redact_query(tail)}"


def uri_password(uri: str) -> str | None:
    """The password `redact_uri` would mask, so callers can build a
    deny-list for `scrub`. None when the URI carries no password."""
    if not uri:
        return None
    if _looks_libpq(uri):
        return _libpq_password(uri)
    _prefix, authority, _tail, _had_scheme = _split_authority(uri)
    if "@" not in authority:
        return None
    userinfo, _, _host = authority.rpartition("@")
    _user, colon, password = userinfo.partition(":")
    return password if colon and password else None


def profile_secrets(profile: "Profile") -> set[str]:
    """Every literal credential the profile carries, for `scrub`."""
    secrets: set[str] = set()

    def add(value: str | None) -> None:
        # One- and two-character "secrets" would turn scrubbed text into
        # confetti; a real credential is never that short.
        if value and len(value) > 3:
            secrets.add(value)

    add(uri_password(profile.uri))
    add(uri_password(profile.postgres_dsn))
    add(_libpq_password(profile.postgres_dsn) if profile.postgres_dsn else None)
    for key, value in profile.options.items():
        if SECRET_OPTION_RE.search(key):
            add(value)
    add(profile.s3.secret_key)
    add(profile.s3.session_token)
    add(profile.oauth.client_secret)
    signing = getattr(profile, "signing", None)
    if signing is not None:
        # The key itself, not just the path: a driver error can quote a
        # statement, and a signing key that reaches an error string has
        # already left the machine.
        try:
            from .attest import load_private_key

            add(load_private_key(profile)[1])
        except Exception:
            pass          # unreadable key is the caller's problem, not ours
    return secrets


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Replace literal secret values wherever they appear in free text.

    The path this closes: a failed ATTACH or a driver error quotes the
    whole statement, DSN inline, and that string is what we hand back to
    the caller.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    return text
