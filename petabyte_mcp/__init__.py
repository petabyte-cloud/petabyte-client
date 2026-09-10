"""petabyte_mcp — a thin Model Context Protocol (MCP) server over the Petabyte API.

The server exposes marketplace discovery and compute management as MCP tools. Every tool is
a small, validated wrapper around an EXISTING Petabyte HTTP endpoint, authenticated with the
user's own Petabyte API key. Authorization, ownership, rate limits, idempotency and every piece
of provisioning / payment / settlement logic stay on the API side — this package never touches
the database and never re-implements business rules.
"""

__version__ = "0.1.0"
