"""Profile + config loading.

Config lives at `$LAKESH_CONFIG` if set, else `$XDG_CONFIG_HOME/lakesh/config.toml`,
else `~/.config/lakesh/config.toml`. The file holds named *profiles* plus a
`default` pointer.

Two profile types are supported:

**Iceberg REST catalog** (`type = "iceberg-rest"`, default):

    [profiles.local]
    uri       = "http://127.0.0.1:8181"
    warehouse = "lake"

    [profiles.local.s3]
    endpoint    = "http://127.0.0.1:9000"
    access_key  = "minioadmin"
    secret_key  = "minioadmin"

    [profiles.local.oauth]           # optional
    client_id     = "demo-client"
    client_secret = "demo-secret"

**DuckLake direct** (`type = "ducklake"`) — bypasses the Iceberg REST layer
and talks to DuckLake's Postgres metadata + S3 data path directly:

    [profiles.lake_direct]
    type         = "ducklake"
    postgres_dsn = "dbname=ducklake host=/tmp/.pgsock port=55432 user=ducklake"
    data_path    = "s3://lakehouse/data/"
    catalog      = "lake"            # the `AS <name>` in ATTACH

    [profiles.lake_direct.s3]
    endpoint   = "http://127.0.0.1:9000"
    access_key = "minioadmin"
    secret_key = "minioadmin"

Env-var indirection (`*_env` keys) keeps secrets out of the file. The
`--uri` and `--warehouse` CLI overrides apply to Iceberg REST profiles
only; DuckLake profiles use `postgres_dsn`, `data_path`, and `catalog`.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised for missing / malformed config files and profile lookup failures."""


@dataclass
class S3Config:
    endpoint: str | None = None
    region: str = "us-east-1"
    access_key: str | None = None
    secret_key: str | None = None
    session_token: str | None = None
    path_style: bool = True
    """For MinIO and most on-prem S3. Flip off for AWS S3 proper."""


@dataclass
class OAuthConfig:
    client_id: str | None = None
    client_secret: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)


SUPPORTED_TYPES = ("iceberg-rest", "ducklake")


@dataclass
class Profile:
    name: str
    type: str = "iceberg-rest"
    # iceberg-rest fields
    uri: str = ""
    warehouse: str = ""
    # ducklake fields
    postgres_dsn: str = ""
    data_path: str = ""
    catalog: str = "lake"          # ATTACH alias for ducklake profiles
    # shared
    s3: S3Config = field(default_factory=S3Config)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)

    def validate(self) -> None:
        if self.type not in SUPPORTED_TYPES:
            raise ConfigError(
                f"profile {self.name!r}: unknown type {self.type!r} "
                f"(supported: {', '.join(SUPPORTED_TYPES)})"
            )
        if self.type == "iceberg-rest":
            if not self.uri:
                raise ConfigError(f"profile {self.name!r}: missing `uri`")
            if not self.warehouse:
                raise ConfigError(f"profile {self.name!r}: missing `warehouse`")
        elif self.type == "ducklake":
            if not self.postgres_dsn:
                raise ConfigError(
                    f"profile {self.name!r}: ducklake profile requires "
                    f"`postgres_dsn` (e.g. "
                    f'"dbname=ducklake host=/tmp/.pgsock port=55432 user=ducklake")'
                )
            if not self.data_path:
                raise ConfigError(
                    f"profile {self.name!r}: ducklake profile requires "
                    f"`data_path` (e.g. \"s3://bucket/prefix/\")"
                )


@dataclass
class Config:
    profiles: dict[str, Profile]
    default: str | None = None
    source_path: Path | None = None

    def get(self, name: str | None) -> Profile:
        name = name or self.default
        if not name:
            raise ConfigError(
                "no profile specified and no `default` set in config"
            )
        if name not in self.profiles:
            available = ", ".join(sorted(self.profiles)) or "<none>"
            raise ConfigError(
                f"profile {name!r} not found (available: {available})"
            )
        return self.profiles[name]


# --------------------------------------------------------------------------
# path discovery

def default_config_path() -> Path:
    """Where we look for config if the caller doesn't pass one.

    Precedence:
      1. `$LAKESH_CONFIG` (full path — explicit override)
      2. `$XDG_CONFIG_HOME/lakesh/config.toml`
      3. `~/.config/lakesh/config.toml`
    """
    env = os.environ.get("LAKESH_CONFIG")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "lakesh" / "config.toml"


# --------------------------------------------------------------------------
# load + parse

def _resolve_env(value: Any, env_key: Any) -> Any:
    """Given a pair of (literal, literal_from_env), return whichever is set.
    Used for `client_id` / `client_id_env` pairs — literal wins if both
    are present (explicit-over-implicit)."""
    if value not in (None, ""):
        return value
    if env_key:
        return os.environ.get(str(env_key))
    return None


def _parse_profile(name: str, raw: dict) -> Profile:
    s3_raw = raw.get("s3") or {}
    s3 = S3Config(
        endpoint=s3_raw.get("endpoint"),
        region=s3_raw.get("region", "us-east-1"),
        access_key=_resolve_env(s3_raw.get("access_key"), s3_raw.get("access_key_env")),
        secret_key=_resolve_env(s3_raw.get("secret_key"), s3_raw.get("secret_key_env")),
        session_token=_resolve_env(
            s3_raw.get("session_token"), s3_raw.get("session_token_env")
        ),
        path_style=bool(s3_raw.get("path_style", True)),
    )
    oauth_raw = raw.get("oauth") or {}
    oauth = OAuthConfig(
        client_id=_resolve_env(oauth_raw.get("client_id"), oauth_raw.get("client_id_env")),
        client_secret=_resolve_env(
            oauth_raw.get("client_secret"), oauth_raw.get("client_secret_env")
        ),
    )
    # DuckLake profiles can stash the Postgres password in an env var
    # rather than baking it into the DSN string committed to config.
    postgres_dsn = _resolve_env(raw.get("postgres_dsn"), raw.get("postgres_dsn_env")) or ""
    p = Profile(
        name=name,
        type=str(raw.get("type", "iceberg-rest")),
        uri=str(raw.get("uri", "")),
        warehouse=str(raw.get("warehouse", "")),
        postgres_dsn=str(postgres_dsn or ""),
        data_path=str(raw.get("data_path", "")),
        catalog=str(raw.get("catalog", "lake")),
        s3=s3,
        oauth=oauth,
    )
    p.validate()
    return p


def load_config(path: Path | None = None) -> Config:
    """Load and parse the config file. Raises `ConfigError` if it doesn't exist
    or can't be parsed."""
    path = Path(path) if path else default_config_path()
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}\n"
            f"create one with `lakesh config init` or set LAKESH_CONFIG."
        )
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"failed to parse {path}: {e}") from e

    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ConfigError(f"{path}: `profiles` must be a table")
    profiles = {n: _parse_profile(n, raw) for n, raw in raw_profiles.items()}

    default = data.get("default")
    if default and default not in profiles:
        raise ConfigError(
            f"{path}: `default = {default!r}` but no such profile"
        )
    return Config(profiles=profiles, default=default, source_path=path)


def write_example_config(path: Path) -> None:
    """Drop a working example config file. Used by `lakesh config init`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_EXAMPLE)


_EXAMPLE = """\
# lakesh config — profiles for Iceberg REST catalogs and/or DuckLake
# metastores. Switch between them via `lakesh -p <name>` or by setting
# `default` below.

default = "local"

# --- Iceberg REST profile (default) --------------------------------------

[profiles.local]
# Local duckicelake (or any Iceberg REST catalog) running on the laptop.
uri       = "http://127.0.0.1:8181"
warehouse = "lake"

[profiles.local.s3]
endpoint   = "http://127.0.0.1:9000"
region     = "us-east-1"
access_key = "minioadmin"
secret_key = "minioadmin"
path_style = true

# Uncomment if your local catalog has DUCKICELAKE_OAUTH_CLIENTS configured.
# [profiles.local.oauth]
# client_id     = "demo-client"
# client_secret = "demo-secret"

# --- DuckLake direct profile (bypass the Iceberg REST layer) -------------
# Useful for INSERT/UPDATE/DELETE workloads that the iceberg-ext can't do.
# Same underlying data as `local`, just via the ducklake extension.

# [profiles.lake_direct]
# type         = "ducklake"
# postgres_dsn = "dbname=ducklake host=/tmp/.pgsock port=55432 user=ducklake"
# data_path    = "s3://lakehouse/data/"
# catalog      = "lake"
#
# [profiles.lake_direct.s3]
# endpoint   = "http://127.0.0.1:9000"
# access_key = "minioadmin"
# secret_key = "minioadmin"

# --- Production example with env-backed secrets --------------------------
# [profiles.prod]
# uri       = "https://catalog.prod.example.com"
# warehouse = "prod"
#
# [profiles.prod.s3]
# region = "us-west-2"
#
# [profiles.prod.oauth]
# client_id_env     = "LAKESH_PROD_CLIENT_ID"
# client_secret_env = "LAKESH_PROD_CLIENT_SECRET"
"""
