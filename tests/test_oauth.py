"""Unit tests for OAuth2 flows + token cache. All HTTP is mocked via
httpx.MockTransport; the authorization-code test drives the real
loopback redirect server on an ephemeral port."""
from __future__ import annotations

import json
import stat
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

from lakesh.config import OAuthConfig, Profile
from lakesh.oauth import (
    AuthRequired,
    OAuthError,
    Token,
    TokenCache,
    authorization_code_flow,
    default_token_endpoint,
    device_code_flow,
    fetch_client_credentials,
    get_token,
    refresh_grant,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _token_json(**kw) -> dict:
    return {"access_token": "AT", "token_type": "Bearer", **kw}


# --------------------------------------------------------------------------
# client_credentials

def test_client_credentials_form_fields():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = dict(urllib.parse.parse_qsl(request.content.decode()))
        return httpx.Response(200, json=_token_json(expires_in=3600))

    oauth = OAuthConfig(
        client_id="cid", client_secret="cs",
        scope="a b", audience="https://api", extra={"resource": "urn:x"},
    )
    tok = fetch_client_credentials(oauth, "https://idp/token", _client(handler))
    assert tok.access_token == "AT"
    assert tok.expires_at is not None and tok.expires_at > time.time()
    assert seen["url"] == "https://idp/token"
    assert seen["form"] == {
        "grant_type": "client_credentials",
        "client_id": "cid",
        "client_secret": "cs",
        "scope": "a b",
        "audience": "https://api",
        "resource": "urn:x",
    }


def test_default_token_endpoint_iceberg_rest():
    prof = Profile(
        name="p", uri="http://cat:8181/", warehouse="w",
        oauth=OAuthConfig(client_id="c", client_secret="s"),
    )
    assert default_token_endpoint(prof) == "http://cat:8181/v1/oauth/tokens"


def test_default_token_endpoint_explicit_wins():
    prof = Profile(
        name="p", uri="http://cat:8181", warehouse="w",
        oauth=OAuthConfig(
            client_id="c", client_secret="s", token_endpoint="https://idp/token"
        ),
    )
    assert default_token_endpoint(prof) == "https://idp/token"


def test_oauth_error_body_surfaced():
    def handler(request):
        return httpx.Response(
            400, json={"error": "invalid_client", "error_description": "nope"}
        )

    with pytest.raises(OAuthError, match="invalid_client: nope"):
        fetch_client_credentials(
            OAuthConfig(client_id="c", client_secret="s"),
            "https://idp/token", _client(handler),
        )


# --------------------------------------------------------------------------
# refresh

def test_refresh_grant_keeps_old_refresh_token_if_omitted():
    def handler(request):
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "RT"
        return httpx.Response(200, json=_token_json(expires_in=60))

    tok = refresh_grant(
        OAuthConfig(client_id="c"), "https://idp/token", "RT", _client(handler)
    )
    assert tok.access_token == "AT"
    assert tok.refresh_token == "RT"  # preserved


# --------------------------------------------------------------------------
# device_code

def test_device_code_flow_polls_until_approved():
    calls = {"n": 0}
    sleeps: list[float] = []
    prompts: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/device":
            return httpx.Response(200, json={
                "device_code": "DC", "user_code": "ABCD-1234",
                "verification_uri": "https://idp/activate",
                "interval": 1, "expires_in": 600,
            })
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        if calls["n"] == 2:
            return httpx.Response(400, json={"error": "slow_down"})
        return httpx.Response(200, json=_token_json(
            expires_in=3600, refresh_token="RT"
        ))

    oauth = OAuthConfig(
        grant="device_code", client_id="cid",
        device_authorization_endpoint="https://idp/device",
        token_endpoint="https://idp/token",
    )
    tok = device_code_flow(
        oauth, _client(handler),
        prompt=lambda *a: prompts.append(a),
        sleep=sleeps.append,
        clock=lambda: 0.0,   # never hits the deadline
    )
    assert tok.access_token == "AT"
    assert tok.refresh_token == "RT"
    assert prompts == [("ABCD-1234", "https://idp/activate", None)]
    # 1s interval, then slow_down bumps by +5
    assert sleeps == [1.0, 1.0, 6.0]


def test_device_code_flow_denied():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/device":
            return httpx.Response(200, json={
                "device_code": "DC", "user_code": "X",
                "verification_uri": "https://idp/activate",
                "interval": 1, "expires_in": 600,
            })
        return httpx.Response(400, json={"error": "access_denied"})

    oauth = OAuthConfig(
        grant="device_code", client_id="cid",
        device_authorization_endpoint="https://idp/device",
        token_endpoint="https://idp/token",
    )
    with pytest.raises(OAuthError, match="access_denied"):
        device_code_flow(
            oauth, _client(handler),
            prompt=lambda *a: None, sleep=lambda s: None, clock=lambda: 0.0,
        )


def test_device_code_flow_expires():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/device":
            return httpx.Response(200, json={
                "device_code": "DC", "user_code": "X",
                "verification_uri": "https://idp/activate",
                "interval": 5, "expires_in": 10,
            })
        return httpx.Response(400, json={"error": "authorization_pending"})

    clock = {"t": 0.0}

    def tick() -> float:
        clock["t"] += 6.0
        return clock["t"]

    oauth = OAuthConfig(
        grant="device_code", client_id="cid",
        device_authorization_endpoint="https://idp/device",
        token_endpoint="https://idp/token",
    )
    with pytest.raises(OAuthError, match="expired"):
        device_code_flow(
            oauth, _client(handler),
            prompt=lambda *a: None, sleep=lambda s: None, clock=tick,
        )


# --------------------------------------------------------------------------
# authorization_code + PKCE (drives the real loopback server)

def test_authorization_code_flow_pkce_roundtrip():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        captured["exchange"] = form
        return httpx.Response(200, json=_token_json(
            expires_in=3600, refresh_token="RT"
        ))

    def fake_browser(url: str) -> bool:
        captured["auth_url"] = url
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        # Simulate the IdP redirecting back with a code + our state.
        redirect = (
            f"{q['redirect_uri']}?code=THECODE&state={q['state']}"
        )
        threading.Thread(
            target=lambda: httpx.get(redirect, timeout=5.0), daemon=True
        ).start()
        return True

    oauth = OAuthConfig(
        grant="authorization_code", client_id="cid",
        authorization_endpoint="https://idp/authorize",
        token_endpoint="https://idp/token",
        scope="openid",
    )
    tok = authorization_code_flow(
        oauth, _client(handler), open_browser=fake_browser, timeout=10.0
    )
    assert tok.access_token == "AT"

    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(captured["auth_url"]).query))
    assert q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256"
    assert q["scope"] == "openid"
    # The verifier sent on exchange must hash to the challenge in the URL.
    import base64
    import hashlib
    verifier = captured["exchange"]["code_verifier"]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=").decode()
    )
    assert q["code_challenge"] == challenge
    assert captured["exchange"]["code"] == "THECODE"
    assert captured["exchange"]["grant_type"] == "authorization_code"


def test_authorization_code_flow_state_mismatch():
    def handler(request):
        return httpx.Response(200, json=_token_json())

    def evil_browser(url: str) -> bool:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        redirect = f"{q['redirect_uri']}?code=X&state=WRONG"
        threading.Thread(
            target=lambda: httpx.get(redirect, timeout=5.0), daemon=True
        ).start()
        return True

    oauth = OAuthConfig(
        grant="authorization_code", client_id="cid",
        authorization_endpoint="https://idp/authorize",
        token_endpoint="https://idp/token",
    )
    with pytest.raises(OAuthError, match="state mismatch"):
        authorization_code_flow(
            oauth, _client(handler), open_browser=evil_browser, timeout=10.0
        )


# --------------------------------------------------------------------------
# TokenCache

def test_cache_roundtrip_and_permissions(tmp_path: Path):
    cache = TokenCache(tmp_path / "tokens.json")
    tok = Token(access_token="AT", refresh_token="RT", expires_at=time.time() + 100)
    cache.store("p", "key1", tok)
    loaded = cache.load("p", "key1")
    assert loaded is not None
    assert loaded.access_token == "AT"
    assert loaded.refresh_token == "RT"
    mode = stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode)
    assert mode == 0o600


def test_cache_config_key_mismatch_discards(tmp_path: Path):
    cache = TokenCache(tmp_path / "tokens.json")
    cache.store("p", "key1", Token(access_token="AT"))
    assert cache.load("p", "OTHER") is None


def test_cache_clear(tmp_path: Path):
    cache = TokenCache(tmp_path / "tokens.json")
    cache.store("a", "k", Token(access_token="A"))
    cache.store("b", "k", Token(access_token="B"))
    cache.clear("a")
    assert cache.load("a", "k") is None
    assert cache.load("b", "k") is not None
    cache.clear()
    assert cache.load("b", "k") is None


def test_cache_garbage_json_is_empty(tmp_path: Path):
    p = tmp_path / "tokens.json"
    p.write_text("{not json")
    cache = TokenCache(p)
    assert cache.load("p", "k") is None
    cache.store("p", "k", Token(access_token="AT"))  # recovers
    assert cache.load("p", "k") is not None


# --------------------------------------------------------------------------
# get_token orchestration

def _cc_profile(**oauth_kw) -> Profile:
    return Profile(
        name="p", uri="http://cat:8181", warehouse="w",
        oauth=OAuthConfig(client_id="cid", client_secret="cs", **oauth_kw),
    )


def test_get_token_no_oauth_returns_none(tmp_path: Path):
    prof = Profile(name="p", uri="http://cat:8181", warehouse="w")
    assert get_token(prof, interactive=True, cache=TokenCache(tmp_path / "t.json")) is None


def test_get_token_cc_caches_when_expiry_known(tmp_path: Path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=_token_json(expires_in=3600))

    cache = TokenCache(tmp_path / "t.json")
    prof = _cc_profile()
    with _client(handler) as c:
        assert get_token(prof, interactive=True, cache=cache, http=c) == "AT"
        assert get_token(prof, interactive=True, cache=cache, http=c) == "AT"
    assert calls["n"] == 1  # second call served from cache


def test_get_token_cc_no_expiry_not_cached(tmp_path: Path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=_token_json())  # no expires_in

    cache = TokenCache(tmp_path / "t.json")
    prof = _cc_profile()
    with _client(handler) as c:
        get_token(prof, interactive=True, cache=cache, http=c)
        get_token(prof, interactive=True, cache=cache, http=c)
    assert calls["n"] == 2


def test_get_token_refresh_path(tmp_path: Path):
    def handler(request):
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert form["grant_type"] == "refresh_token"
        return httpx.Response(200, json=_token_json(expires_in=3600))

    cache = TokenCache(tmp_path / "t.json")
    prof = Profile(
        name="p", type="adbc", driver="sqlite", uri="/tmp/x.db",
        token_option="auth_token",
        oauth=OAuthConfig(
            grant="device_code", client_id="cid",
            device_authorization_endpoint="https://idp/device",
            token_endpoint="https://idp/token",
        ),
    )
    from lakesh.oauth import _config_key, default_token_endpoint as dte
    key = _config_key(prof.oauth, dte(prof))
    cache.store("p", key, Token(
        access_token="OLD", refresh_token="RT", expires_at=time.time() - 10
    ))
    with _client(handler) as c:
        assert get_token(prof, interactive=False, cache=cache, http=c) == "AT"


def test_get_token_auth_required_when_not_interactive(tmp_path: Path):
    prof = Profile(
        name="snow", type="adbc", driver="snowflake", token_option="t",
        oauth=OAuthConfig(
            grant="device_code", client_id="cid",
            device_authorization_endpoint="https://idp/device",
            token_endpoint="https://idp/token",
        ),
    )
    cache = TokenCache(tmp_path / "t.json")
    with pytest.raises(AuthRequired, match="lakesh auth login -p snow"):
        get_token(prof, interactive=False, cache=cache)


def test_get_token_force_bypasses_cache(tmp_path: Path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=_token_json(expires_in=3600))

    cache = TokenCache(tmp_path / "t.json")
    prof = _cc_profile()
    with _client(handler) as c:
        get_token(prof, interactive=True, cache=cache, http=c)
        get_token(prof, interactive=True, cache=cache, http=c, force=True)
    assert calls["n"] == 2
