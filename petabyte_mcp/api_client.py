"""The single HTTP path to the Petabyte API.

Design rules (each one is tested):
  * The user's API key is sent as `X-API-KEY` — exactly how the CLI authenticates — and only
    on calls that need it. Public marketplace endpoints are called without credentials.
  * Timeouts are explicit: connect / read for GETs, a longer read budget for provisioning POSTs.
  * NO automatic retry of any POST. A launch that timed out may still have booked; retrying it
    blindly could double-charge. Callers pass an `Idempotency-Key` where the API supports one
    (`/launch`), which is the API's own dedup mechanism. A GET is retried once on a transport
    error because it is side-effect free.
  * Errors become `PetabyteAPIError` (parsed envelope) — never a raw httpx exception.
  * Logging records method, path, status and latency. Never the key, never a header dump,
    never a response body.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from . import __version__
from .config import Settings
from .errors import Codes, PetabyteAPIError, ToolFailure, parse_error_response
from .logging_setup import get_logger

_USER_AGENT = f"petabyte-mcp/{__version__}"


class PetabyteClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._log = logger or get_logger("petabyte_mcp.api")
        timeout = httpx.Timeout(
            connect=settings.connect_timeout_s,
            read=settings.read_timeout_s,
            write=settings.read_timeout_s,
            pool=settings.connect_timeout_s,
        )
        self._client = httpx.AsyncClient(
            base_url=settings.api_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,  # a redirect could re-send the key to another host
        )
        self.calls_made = 0

    @property
    def base_url(self) -> str:
        return self._settings.api_url

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ public helpers
    async def get(
        self, path: str, *, params: dict[str, Any] | None = None, auth: bool = True
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        return await self._request("GET", path, params=clean, auth=auth, retry_once=True)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        auth: bool = True,
        idempotency_key: str | None = None,
        long: bool = False,
    ) -> Any:
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        timeout = None
        if long:
            timeout = httpx.Timeout(
                connect=self._settings.connect_timeout_s,
                read=self._settings.write_timeout_s,
                write=self._settings.write_timeout_s,
                pool=self._settings.connect_timeout_s,
            )
        return await self._request(
            "POST", path, json=json, auth=auth, headers=headers, retry_once=False, timeout=timeout
        )

    # ------------------------------------------------------------------ core
    def _auth_headers(self, auth: bool) -> dict[str, str]:
        if not auth:
            return {}
        key = self._settings.api_key
        if not key:
            raise ToolFailure(
                Codes.NO_API_KEY,
                "this tool needs a Petabyte API key; set PETABYTE_API_KEY "
                "(mint one on the Petabyte website under API keys).",
            )
        return {"X-API-KEY": key}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool,
        retry_once: bool,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> Any:
        hdrs = dict(headers or {})
        hdrs.update(self._auth_headers(auth))
        attempts = 2 if retry_once else 1
        last_exc: httpx.TransportError | None = None
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                kwargs: dict[str, Any] = {"params": params, "json": json, "headers": hdrs}
                if timeout is not None:
                    kwargs["timeout"] = timeout
                resp = await self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last_exc = exc
                self._log.warning(
                    "api.timeout", extra={"method": method, "path": path, "attempt": attempt}
                )
                if attempt < attempts:
                    continue
                raise ToolFailure(
                    Codes.API_TIMEOUT,
                    f"the Petabyte API at {self.base_url} did not answer "
                    f"{method} {path} in time; try again shortly",
                ) from exc
            except httpx.TransportError as exc:
                last_exc = exc
                self._log.warning(
                    "api.unavailable",
                    extra={
                        "method": method,
                        "path": path,
                        "attempt": attempt,
                        "error": type(exc).__name__,
                    },
                )
                if attempt < attempts:
                    continue
                raise ToolFailure(
                    Codes.API_UNAVAILABLE,
                    f"cannot reach the Petabyte API at {self.base_url} "
                    f"({type(exc).__name__}); check PETABYTE_API_URL and the "
                    "service status",
                ) from exc
            self.calls_made += 1
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._log.info(
                "api.request",
                extra={
                    "method": method,
                    "path": path,
                    "status": resp.status_code,
                    "ms": elapsed_ms,
                    "authenticated": bool(auth),
                },
            )
            if resp.status_code >= 400:
                err = parse_error_response(resp.status_code, dict(resp.headers), resp.text)
                raise err
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise PetabyteAPIError(
                    resp.status_code,
                    Codes.API_ERROR,
                    "the Petabyte API returned a non-JSON response",
                ) from exc
        raise ToolFailure(Codes.API_UNAVAILABLE, "request failed") from last_exc  # unreachable
