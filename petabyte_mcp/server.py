"""Assemble the MCP server: settings -> API client -> key introspection -> authorizer -> tools.

`build_server()` is the composition root used by both the CLI entry point and the tests (tests
inject an `httpx` transport so the very same code path runs against a fake API or the real
FastAPI app in-process).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp.server.mcpserver import MCPServer

from . import __version__
from .api_client import PetabyteClient
from .auth import ApiKeyAuth
from .authz import TOOL_POLICIES, Authorizer
from .config import Settings
from .logging_setup import configure_logging, get_logger, register_secret
from .runtime import Runtime
from .scopes_registry import ScopeRegistry, load_registry
from .tools import register_all

INSTRUCTIONS = """\
Petabyte is a GPU compute marketplace. Use these tools as a thin interface over the user's
Petabyte account, authenticated with THEIR API key; the API enforces ownership, scopes and
rate limits on every call.

Workflow: discover with list_offers / get_offer / list_templates / estimate_cost (public, no
key needed) -> act with create_instance (compute:write) -> monitor with list_instances /
get_instance / get_usage / get_balance -> extend_instance, stop_instance, restart_instance or
delete_instance (compute:write).

Money-moving and destructive tools are two-step: call once WITHOUT confirm=true to get a
side-effect-free preview (cost estimate, what will be stopped), show it to the user, and only
after they agree call again with confirm=true (and confirm_instance_id=<instance_id> for
instance actions). Never set confirm=true on your own initiative. Pass the same
idempotency_key when retrying create_instance so the API cannot book twice.

Errors are `CODE: message`. INSUFFICIENT_SCOPE / ACCOUNT_ACCESS_REQUIRED mean the API key lacks
a scope: tell the user which one (the message names it) rather than retrying.
"""


def build_runtime(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    registry: ScopeRegistry | None = None,
    logger: logging.Logger | None = None,
) -> Runtime:
    log = logger or get_logger("petabyte_mcp")
    register_secret(settings.api_key)
    reg = registry or load_registry(settings.scopes_module_path)
    client = PetabyteClient(settings, transport=transport, logger=get_logger("petabyte_mcp.api"))
    auth = ApiKeyAuth(client, settings)
    authz = Authorizer(auth, reg, settings)
    return Runtime(settings=settings, client=client, auth=auth, authz=authz, registry=reg, log=log)


def build_server(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    registry: ScopeRegistry | None = None,
    runtime: Runtime | None = None,
) -> MCPServer:
    """Create a fully wired `MCPServer`. Write tools are not registered in read-only mode."""
    rt = runtime or build_runtime(settings, transport=transport, registry=registry)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[Runtime]:
        rt.log.info(
            "server.start",
            extra={
                "api_url": settings.api_url,
                "transport": settings.transport,
                "read_only": settings.read_only,
                "has_api_key": settings.has_api_key,
                "max_hours": settings.max_hours,
                "version": __version__,
                "scope_registry": str(rt.registry.source),
            },
        )
        try:
            yield rt
        finally:
            await rt.aclose()
            rt.log.info("server.stop")

    server = MCPServer(
        name=settings.server_name, version=__version__, instructions=INSTRUCTIONS, lifespan=lifespan
    )
    register_all(server, rt)
    server.petabyte_runtime = rt  # type: ignore[attr-defined]  # handy for tests/diagnostics
    return server


def registered_tool_names(read_only: bool) -> list[str]:
    """Which tools a server exposes for a given mode (mirrors register_all), sorted."""
    return sorted(
        name for name, p in TOOL_POLICIES.items() if not (read_only and p.kind == "write")
    )


def main() -> None:
    """Console entry point: `petabyte-mcp` / `python -m petabyte_mcp`."""
    settings = Settings.from_env()
    log = configure_logging(settings.log_level, settings.log_format)
    server = build_server(settings)
    if settings.transport == "streamable-http":
        log.info("transport.http", extra={"host": settings.host, "port": settings.port})
        server.run(transport="streamable-http", host=settings.host, port=settings.port)
    else:
        server.run(transport="stdio")
