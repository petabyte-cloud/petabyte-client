"""Shared runtime handed to every tool: settings, API client, key introspection, authorizer.

`get`/`post` are the ONLY ways tools talk to the API. They translate `PetabyteAPIError` into the
agent-facing `ToolFailure` vocabulary so every tool surfaces API errors identically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .api_client import PetabyteClient
from .auth import ApiKeyAuth
from .authz import Authorizer
from .config import Settings
from .errors import PetabyteAPIError, to_tool_failure
from .scopes_registry import ScopeRegistry


@dataclass
class Runtime:
    settings: Settings
    client: PetabyteClient
    auth: ApiKeyAuth
    authz: Authorizer
    registry: ScopeRegistry
    log: logging.Logger

    async def get(
        self, path: str, *, params: dict[str, Any] | None = None, auth: bool = True
    ) -> Any:
        try:
            return await self.client.get(path, params=params, auth=auth)
        except PetabyteAPIError as err:
            raise to_tool_failure(err) from err

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        auth: bool = True,
        idempotency_key: str | None = None,
        long: bool = False,
    ) -> Any:
        try:
            return await self.client.post(
                path, json=json, auth=auth, idempotency_key=idempotency_key, long=long
            )
        except PetabyteAPIError as err:
            raise to_tool_failure(err) from err

    async def aclose(self) -> None:
        await self.client.aclose()
