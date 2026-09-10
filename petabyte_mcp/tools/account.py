"""Billing / account read tools — each is an existing Petabyte read endpoint."""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..runtime import Runtime
from ..shaping import shape_account, shape_balance, shape_bookings, shape_usage
from ..validation import BookingsLimit

_READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(name="get_balance", title="Wallet balance", annotations=_READ)
    async def get_balance() -> dict[str, Any]:
        """The caller's wallet: spendable balance, seller earnings, what is withdrawable vs
        still clearing. Requires scope wallets:read (the legacy 'read' scope grants it)."""
        await rt.authz.authorize("get_balance")
        return shape_balance(await rt.get("/wallet"))

    @server.tool(name="get_usage", title="Current spend / burn rate", annotations=_READ)
    async def get_usage() -> dict[str, Any]:
        """What the caller is spending right now: active instances, hourly burn rate, projected
        24h spend, funds held in escrow, lifetime spend and hours of runway at the current
        balance. Requires scope compute:read."""
        await rt.authz.authorize("get_usage")
        return shape_usage(await rt.get("/buyer/spend"))

    @server.tool(name="get_account", title="Account profile", annotations=_READ)
    async def get_account() -> dict[str, Any]:
        """The caller's account profile (username, role, verification, reputation, balance,
        counts) plus the scopes this MCP server's API key holds — useful to know up front
        which tools will be permitted. Requires scope users:read."""
        info = await rt.authz.authorize("get_account")
        out = {"account": shape_account(await rt.get("/me"))}
        if info is not None:
            out["api_key"] = {"scopes": list(info.scopes)}
        return out

    @server.tool(name="list_bookings", title="List bookings", annotations=_READ)
    async def list_bookings(limit: BookingsLimit = 20) -> dict[str, Any]:
        """Recent bookings (escrow records) the caller bought or sold: GPU, hours, amount,
        status. Requires scope escrow:read (the legacy 'read' scope grants it)."""
        await rt.authz.authorize("list_bookings")
        rows = shape_bookings(await rt.get("/account/bookings", params={"limit": limit}))
        return {"bookings": rows, "count": len(rows)}
