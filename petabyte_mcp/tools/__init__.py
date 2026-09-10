"""Tool registration. Each module exposes `register(server, runtime)`."""

from mcp.server.mcpserver import MCPServer

from ..runtime import Runtime
from . import account, compute, marketplace


def register_all(server: MCPServer, rt: Runtime) -> None:
    marketplace.register(server, rt)
    compute.register(server, rt)
    account.register(server, rt)
