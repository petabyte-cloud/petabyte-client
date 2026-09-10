"""Environment-based configuration for the Petabyte MCP server.

Nothing here is hardcoded to a production deployment: the API base URL and the API key are
REQUIRED to come from the environment (or an explicit `Settings(...)` in tests). The remaining
knobs are operator safety rails (read-only mode, hour/price caps) and transport settings.

Every limit that mirrors an API constraint is named `API_*` and documented against the server
model it comes from, so a drift between the two is a one-line fix here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

# --- constraints mirrored from the Petabyte API (lumaris_api/main.py) ---------------------------
API_MAX_HOURS = 8760  # RequestVMModel / QuickLaunchModel: hours = Field(gt=0, le=8760)
API_MAX_EXTEND_HOURS = 720  # ExtendModel: hours = Field(gt=0, le=720)
API_MAX_ESTIMATE_HOURS = 720  # EstimateModel: hours = Field(1, ge=1, le=720)
API_MAX_LIST_LIMIT = 200  # /marketplace/specs: limit = Query(200, ge=1, le=200)
API_MAX_BOOKINGS_LIMIT = 200  # /account/bookings: limit = Query(50, le=200)

DEFAULT_MAX_HOURS = 24  # MCP-side cap on hours an agent may escrow in one call
DEFAULT_READ_TIMEOUT_S = 30.0
DEFAULT_WRITE_TIMEOUT_S = 90.0  # /launch books + creates a task + a VM route: slower than a read
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_SCOPE_CACHE_TTL_S = 60
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765

Transport = Literal["stdio", "streamable-http"]
LogFormat = Literal["json", "text"]

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class ConfigError(ValueError):
    """A required setting is missing or a setting has an unsafe / malformed value."""


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


def _env_int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not lo <= val <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {val}")
    return val


def _env_float(
    env: Mapping[str, str], name: str, default: float | None, lo: float, hi: float
) -> float | None:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= val <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {val}")
    return val


def validate_api_url(raw: str, *, allow_insecure_http: bool = False) -> str:
    """Normalise and sanity-check the API base URL.

    Rejects anything that is not http(s), a URL carrying credentials (`user:pass@host`), or a
    plaintext `http://` URL to a non-loopback host unless explicitly allowed — the API key would
    otherwise travel unencrypted. Returns the URL without a trailing slash.
    """
    url = (raw or "").strip()
    if not url:
        raise ConfigError("PETABYTE_API_URL is required (e.g. https://your-petabyte-host)")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("PETABYTE_API_URL must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ConfigError("PETABYTE_API_URL must not embed credentials")
    if parts.query or parts.fragment:
        raise ConfigError("PETABYTE_API_URL must not carry a query string or fragment")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS and not allow_insecure_http:
        raise ConfigError(
            "PETABYTE_API_URL uses plaintext http to a non-loopback host; the API "
            "key would be sent unencrypted. Use https, or set "
            "PETABYTE_MCP_ALLOW_INSECURE_HTTP=true for a trusted private network."
        )
    return url.rstrip("/")


def validate_api_key(raw: str | None) -> str | None:
    """The key is an opaque token minted by the API; only its shape is checked (no whitespace,
    plausible length). An absent key is allowed — public marketplace tools still work."""
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if any(ch.isspace() for ch in key) or len(key) < 16:
        raise ConfigError("PETABYTE_API_KEY does not look like a Petabyte API key")
    return key


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration. Build with `Settings.from_env()`."""

    api_url: str
    api_key: str | None = None
    transport: Transport = "stdio"
    host: str = DEFAULT_HTTP_HOST
    port: int = DEFAULT_HTTP_PORT
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S
    write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    read_only: bool = False
    max_hours: int = DEFAULT_MAX_HOURS
    max_price_per_hour: float | None = None
    log_level: str = "INFO"
    log_format: LogFormat = "json"
    scope_cache_ttl_s: int = DEFAULT_SCOPE_CACHE_TTL_S
    scopes_module_path: str | None = None
    server_name: str = "petabyte"

    def __post_init__(self) -> None:
        if not 1 <= self.max_hours <= API_MAX_HOURS:
            raise ConfigError(f"max_hours must be between 1 and {API_MAX_HOURS}")
        if self.max_price_per_hour is not None and self.max_price_per_hour <= 0:
            raise ConfigError("max_price_per_hour must be positive")
        if self.transport not in ("stdio", "streamable-http"):
            raise ConfigError("transport must be 'stdio' or 'streamable-http'")
        if self.log_format not in ("json", "text"):
            raise ConfigError("log_format must be 'json' or 'text'")

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read configuration from environment variables (see `.env.example`)."""
        e: Mapping[str, str] = os.environ if env is None else env
        allow_http = _env_bool(e, "PETABYTE_MCP_ALLOW_INSECURE_HTTP", False)
        transport = (e.get("PETABYTE_MCP_TRANSPORT") or "stdio").strip().lower()
        if transport not in ("stdio", "streamable-http"):
            raise ConfigError("PETABYTE_MCP_TRANSPORT must be 'stdio' or 'streamable-http'")
        log_format = (e.get("PETABYTE_MCP_LOG_FORMAT") or "json").strip().lower()
        if log_format not in ("json", "text"):
            raise ConfigError("PETABYTE_MCP_LOG_FORMAT must be 'json' or 'text'")
        level = (e.get("PETABYTE_MCP_LOG_LEVEL") or "INFO").strip().upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError("PETABYTE_MCP_LOG_LEVEL must be DEBUG/INFO/WARNING/ERROR/CRITICAL")
        scopes_path = (e.get("PETABYTE_MCP_SCOPES_MODULE") or "").strip() or None
        return cls(
            api_url=validate_api_url(e.get("PETABYTE_API_URL", ""), allow_insecure_http=allow_http),
            api_key=validate_api_key(e.get("PETABYTE_API_KEY")),
            transport=transport,  # type: ignore[arg-type]
            host=(e.get("PETABYTE_MCP_HOST") or DEFAULT_HTTP_HOST).strip(),
            port=_env_int(e, "PETABYTE_MCP_PORT", DEFAULT_HTTP_PORT, 1, 65535),
            read_timeout_s=_env_float(
                e, "PETABYTE_MCP_TIMEOUT_S", DEFAULT_READ_TIMEOUT_S, 1.0, 600.0
            )
            or DEFAULT_READ_TIMEOUT_S,
            write_timeout_s=_env_float(
                e, "PETABYTE_MCP_WRITE_TIMEOUT_S", DEFAULT_WRITE_TIMEOUT_S, 1.0, 600.0
            )
            or DEFAULT_WRITE_TIMEOUT_S,
            connect_timeout_s=_env_float(
                e, "PETABYTE_MCP_CONNECT_TIMEOUT_S", DEFAULT_CONNECT_TIMEOUT_S, 1.0, 120.0
            )
            or DEFAULT_CONNECT_TIMEOUT_S,
            read_only=_env_bool(e, "PETABYTE_MCP_READ_ONLY", False),
            max_hours=_env_int(e, "PETABYTE_MCP_MAX_HOURS", DEFAULT_MAX_HOURS, 1, API_MAX_HOURS),
            max_price_per_hour=_env_float(
                e, "PETABYTE_MCP_MAX_PRICE_PER_HOUR", None, 0.000001, 1_000_000.0
            ),
            log_level=level,
            log_format=log_format,  # type: ignore[arg-type]
            scope_cache_ttl_s=_env_int(
                e, "PETABYTE_MCP_SCOPE_CACHE_TTL_S", DEFAULT_SCOPE_CACHE_TTL_S, 0, 3600
            ),
            scopes_module_path=scopes_path,
        )
