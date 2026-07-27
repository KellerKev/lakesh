"""OAuth2 token acquisition + caching, per profile.

Three grant types (RFC 6749 / 8628 / 7636):

- **client_credentials** — machine-to-machine. For iceberg-rest profiles
  the token endpoint defaults to the catalog's own `/v1/oauth/tokens`
  (the pre-existing lakesh behavior); any profile may point
  `token_endpoint` at an external IdP instead.
- **device_code** — prints a user code + verification URL, polls the
  token endpoint until the user approves in a browser. For CLIs on
  machines without a usable browser.
- **authorization_code** with PKCE — opens the system browser against
  `authorization_endpoint` and captures the code on a localhost loopback
  redirect. Public client friendly (no client_secret required).

Tokens (access + refresh + expiry) are cached per profile in
`$XDG_STATE_HOME/lakesh/tokens.json` (0600, atomic writes) so
interactive grants only prompt when no valid or refreshable token
exists. `get_token()` is the single entry point; non-interactive
contexts (MCP, piped `exec`) pass `interactive=False` and get
`AuthRequired` instead of a surprise browser popup.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as _secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import OAuthConfig, Profile


class OAuthError(Exception):
    """OAuth2 protocol failure (error response, timeout, denial)."""


class AuthRequired(Exception):
    """An interactive grant is needed but the caller is non-interactive."""

    def __init__(self, profile_name: str, grant: str):
        self.profile_name = profile_name
        self.grant = grant
        super().__init__(
            f"profile {profile_name!r} uses the {grant} grant and has no "
            f"valid cached token — run `lakesh auth login -p {profile_name}` "
            f"in a terminal, then retry"
        )


_EXPIRY_SKEW = 60.0  # refresh this many seconds before actual expiry


@dataclass
class Token:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None      # epoch seconds; None = unknown
    token_type: str = "Bearer"
    scope: str | None = None

    def is_expired(self, skew: float = _EXPIRY_SKEW) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at - skew

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Token":
        return cls(
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token"),
            expires_at=d.get("expires_at"),
            token_type=d.get("token_type", "Bearer"),
            scope=d.get("scope"),
        )


def _parse_token_response(data: dict) -> Token:
    if "access_token" not in data:
        raise OAuthError(f"token response missing access_token: {data!r}")
    expires_at = None
    if data.get("expires_in") is not None:
        expires_at = time.time() + float(data["expires_in"])
    return Token(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=expires_at,
        token_type=data.get("token_type", "Bearer"),
        scope=data.get("scope"),
    )


def default_token_endpoint(profile: Profile) -> str | None:
    """The endpoint used when `token_endpoint` is unset: iceberg-rest
    catalogs expose their own `/v1/oauth/tokens` (pre-existing behavior)."""
    if profile.oauth.token_endpoint:
        return profile.oauth.token_endpoint
    if profile.type == "iceberg-rest" and profile.uri:
        return f"{profile.uri.rstrip('/')}/v1/oauth/tokens"
    return None


# --------------------------------------------------------------------------
# token cache

def _default_cache_path() -> Path:
    env = os.environ.get("LAKESH_TOKEN_CACHE")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "lakesh" / "tokens.json"


def _config_key(oauth: OAuthConfig, token_endpoint: str | None) -> str:
    """Fingerprint of the IdP config — a changed endpoint/client_id/grant
    invalidates cached tokens automatically."""
    raw = f"{oauth.grant}|{token_endpoint or ''}|{oauth.client_id or ''}|{oauth.scope or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class TokenCache:
    def __init__(self, path: Path | None = None):
        self.path = path or _default_cache_path()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "profiles": {}}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def load(self, profile_name: str, config_key: str) -> Token | None:
        entry = self._read()["profiles"].get(profile_name)
        if not entry or entry.get("key") != config_key:
            return None
        try:
            return Token.from_dict(entry["token"])
        except (KeyError, TypeError):
            return None

    def store(self, profile_name: str, config_key: str, token: Token) -> None:
        data = self._read()
        data["profiles"][profile_name] = {
            "key": config_key,
            "token": token.to_dict(),
            "obtained_at": time.time(),
        }
        self._write(data)

    def clear(self, profile_name: str | None = None) -> None:
        data = self._read()
        if profile_name is None:
            data["profiles"] = {}
        else:
            data["profiles"].pop(profile_name, None)
        self._write(data)

    def status(self) -> dict[str, dict]:
        return dict(self._read()["profiles"])


# --------------------------------------------------------------------------
# individual grants

def _post_token(
    http: httpx.Client, endpoint: str, form: dict[str, Any]
) -> dict:
    r = http.post(endpoint, data=form, timeout=15.0)
    if r.status_code >= 400:
        # OAuth errors come back as JSON bodies on 4xx — surface them.
        try:
            body = r.json()
        except ValueError:
            r.raise_for_status()
            raise  # unreachable
        err = body.get("error", f"http {r.status_code}")
        desc = body.get("error_description", "")
        raise OAuthError(f"{err}: {desc}" if desc else err)
    return r.json()


def _base_form(oauth: OAuthConfig) -> dict[str, Any]:
    form: dict[str, Any] = {"client_id": oauth.client_id}
    if oauth.client_secret:
        form["client_secret"] = oauth.client_secret
    if oauth.scope:
        form["scope"] = oauth.scope
    if oauth.audience:
        form["audience"] = oauth.audience
    form.update(oauth.extra)
    return form


def fetch_client_credentials(
    oauth: OAuthConfig, token_endpoint: str, http: httpx.Client
) -> Token:
    form = _base_form(oauth)
    form["grant_type"] = "client_credentials"
    return _parse_token_response(_post_token(http, token_endpoint, form))


def refresh_grant(
    oauth: OAuthConfig, token_endpoint: str, refresh_token: str, http: httpx.Client
) -> Token:
    form: dict[str, Any] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": oauth.client_id,
    }
    if oauth.client_secret:
        form["client_secret"] = oauth.client_secret
    data = _post_token(http, token_endpoint, form)
    token = _parse_token_response(data)
    # IdPs may omit the refresh token on refresh — keep the old one.
    if token.refresh_token is None:
        token.refresh_token = refresh_token
    return token


def _default_prompt(user_code: str, verification_uri: str, complete: str | None) -> None:
    print(
        f"\nTo sign in, open:  {complete or verification_uri}\n"
        + ("" if complete else f"and enter code:    {user_code}\n"),
        file=sys.stderr,
        flush=True,
    )


def device_code_flow(
    oauth: OAuthConfig,
    http: httpx.Client,
    *,
    prompt: Callable[[str, str, str | None], None] = _default_prompt,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Token:
    """RFC 8628. Blocks until the user approves, denies, or the device
    code expires."""
    form = _base_form(oauth)
    start = _post_token(http, oauth.device_authorization_endpoint, form)  # type: ignore[arg-type]
    device_code = start["device_code"]
    interval = float(start.get("interval", 5))
    expires_in = float(start.get("expires_in", 600))
    prompt(
        start["user_code"],
        start["verification_uri"],
        start.get("verification_uri_complete"),
    )

    deadline = clock() + expires_in
    poll: dict[str, Any] = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": oauth.client_id,
    }
    if oauth.client_secret:
        poll["client_secret"] = oauth.client_secret
    while clock() < deadline:
        sleep(interval)
        try:
            return _parse_token_response(
                _post_token(http, oauth.token_endpoint, poll)  # type: ignore[arg-type]
            )
        except OAuthError as e:
            msg = str(e)
            if msg.startswith("authorization_pending"):
                continue
            if msg.startswith("slow_down"):
                interval += 5
                continue
            raise
    raise OAuthError("device code expired before the login was approved")


class _LoopbackHandler(BaseHTTPRequestHandler):
    """Captures ?code=&state= from the IdP redirect and shows a tiny
    close-this-tab page."""

    result: dict[str, str] = {}
    expected_state = ""
    event: threading.Event

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        params = {k: v[0] for k, v in query.items()}
        type(self).result = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = params.get("state") == type(self).expected_state and "code" in params
        body = (
            "<html><body style='font-family:sans-serif'><h2>lakesh</h2><p>"
            + ("Signed in — you can close this tab." if ok
               else "Login failed — check the terminal.")
            + "</p></body></html>"
        )
        self.wfile.write(body.encode())
        type(self).event.set()

    def log_message(self, *args):  # silence request logging
        pass


def authorization_code_flow(
    oauth: OAuthConfig,
    http: httpx.Client,
    *,
    open_browser: Callable[[str], bool] = webbrowser.open,
    timeout: float = 300.0,
) -> Token:
    """Authorization-code grant with PKCE (S256) over a localhost
    loopback redirect."""
    verifier = _secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = _secrets.token_urlsafe(16)

    handler = type(
        "_Handler", (_LoopbackHandler,),
        {"result": {}, "expected_state": state, "event": threading.Event()},
    )
    server = HTTPServer(("127.0.0.1", oauth.redirect_port or 0), handler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    params = {
        "response_type": "code",
        "client_id": oauth.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if oauth.scope:
        params["scope"] = oauth.scope
    if oauth.audience:
        params["audience"] = oauth.audience
    params.update(oauth.extra)
    auth_url = f"{oauth.authorization_endpoint}?{urllib.parse.urlencode(params)}"

    try:
        print(
            f"\nOpening browser for login. If nothing opens, visit:\n  {auth_url}\n",
            file=sys.stderr,
            flush=True,
        )
        open_browser(auth_url)
        if not handler.event.wait(timeout):
            raise OAuthError("timed out waiting for the browser login")
        result = handler.result
    finally:
        server.shutdown()
        server.server_close()

    if result.get("state") != state:
        raise OAuthError("state mismatch on redirect — possible CSRF, aborting")
    if "error" in result:
        desc = result.get("error_description", "")
        raise OAuthError(f"{result['error']}: {desc}" if desc else result["error"])
    if "code" not in result:
        raise OAuthError(f"redirect carried no code: {result!r}")

    form: dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "client_id": oauth.client_id,
        "code_verifier": verifier,
    }
    if oauth.client_secret:
        form["client_secret"] = oauth.client_secret
    return _parse_token_response(_post_token(http, oauth.token_endpoint, form))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# orchestrator

def get_token(
    profile: Profile,
    *,
    interactive: bool,
    cache: TokenCache | None = None,
    http: httpx.Client | None = None,
    force: bool = False,
) -> str | None:
    """Return a bearer token for the profile, or None when the profile
    has no OAuth configured.

    Resolution order: valid cached token → refresh grant →
    client_credentials (non-interactive by nature) → interactive flow
    (device_code / authorization_code), which raises `AuthRequired`
    when `interactive` is False.
    """
    oauth = profile.oauth
    if not oauth.enabled:
        return None
    cache = cache or TokenCache()
    endpoint = default_token_endpoint(profile)
    key = _config_key(oauth, endpoint)
    own_http = http is None
    client = http or httpx.Client()
    try:
        cached = None if force else cache.load(profile.name, key)
        if cached and not cached.is_expired():
            return cached.access_token
        if cached and cached.refresh_token and endpoint:
            try:
                token = refresh_grant(oauth, endpoint, cached.refresh_token, client)
                cache.store(profile.name, key, token)
                return token.access_token
            except (OAuthError, httpx.HTTPError):
                cache.clear(profile.name)  # dead refresh token — start over

        if oauth.grant == "client_credentials":
            if not endpoint:
                return None
            token = fetch_client_credentials(oauth, endpoint, client)
            # Without expiry info we can't know staleness — don't cache,
            # just re-fetch next time (pre-caching lakesh behavior).
            if token.expires_at is not None:
                cache.store(profile.name, key, token)
            return token.access_token

        if not interactive:
            raise AuthRequired(profile.name, oauth.grant)
        if oauth.grant == "device_code":
            token = device_code_flow(oauth, client)
        else:
            token = authorization_code_flow(oauth, client)
        cache.store(profile.name, key, token)
        return token.access_token
    finally:
        if own_http:
            client.close()
