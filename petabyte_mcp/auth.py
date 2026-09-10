"""API-key introspection through the API's OWN endpoint.

The MCP server never decodes, stores or inspects the key itself — it asks `GET /verify_api_key`
(an existing Petabyte endpoint) which scopes the key carries and for whom. The answer is cached
briefly so a burst of tool calls does not re-verify on every call; revocation and expiry still
bite immediately because every real call is authenticated by the API anyway.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .api_client import PetabyteClient
from .config import Settings
from .errors import Codes, PetabyteAPIError, ToolFailure, to_tool_failure


@dataclass(frozen=True)
class KeyInfo:
    username: str
    scopes: tuple[str, ...]


class ApiKeyAuth:
    def __init__(
        self,
        client: PetabyteClient,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock
        self._cached: KeyInfo | None = None
        self._cached_at: float = 0.0
        self.introspections = 0

    def invalidate(self) -> None:
        self._cached = None

    async def introspect(self, *, force: bool = False) -> KeyInfo:
        if not self._settings.has_api_key:
            raise ToolFailure(
                Codes.NO_API_KEY, "this tool needs a Petabyte API key; set PETABYTE_API_KEY."
            )
        ttl = self._settings.scope_cache_ttl_s
        if not force and self._cached is not None and self._clock() - self._cached_at < ttl:
            return self._cached
        try:
            body = await self._client.get("/verify_api_key")
        except PetabyteAPIError as err:
            self.invalidate()
            if err.status == 401:
                raise ToolFailure(
                    Codes.INVALID_API_KEY,
                    f"the Petabyte API rejected the API key: {err.message}. "
                    "Mint a new key on the Petabyte website.",
                    request_id=err.request_id,
                ) from err
            raise to_tool_failure(err) from err
        if not isinstance(body, dict) or body.get("status") != "valid":
            raise ToolFailure(Codes.INVALID_API_KEY, "the Petabyte API did not confirm the key")
        scopes = body.get("scopes") or []
        if not isinstance(scopes, list):
            scopes = []
        info = KeyInfo(
            username=str(body.get("username") or ""), scopes=tuple(str(s) for s in scopes)
        )
        self._cached, self._cached_at = info, self._clock()
        self.introspections += 1
        return info
