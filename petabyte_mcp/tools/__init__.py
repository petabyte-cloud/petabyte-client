"""Tool registration. Each module exposes `register(server, runtime)`."""

from mcp.server.fastmcp import FastMCP as MCPServer

from ..runtime import Runtime
from . import account, compute, marketplace


def register_all(server: MCPServer, rt: Runtime) -> None:
    marketplace.register(server, rt)
    compute.register(server, rt)
    account.register(server, rt)
