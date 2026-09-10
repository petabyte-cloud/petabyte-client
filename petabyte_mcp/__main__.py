"""`python -m petabyte_mcp` / `petabyte-mcp` — run the Petabyte MCP server (stdio by default)."""

import sys

from .config import ConfigError
from .scopes_registry import ScopeRegistryError


def run() -> None:
    # Import here (not at module top) so the console script can give a friendly message instead of
    # an ImportError traceback when installed without the [mcp] extra (the `mcp` SDK is missing).
    try:
        from .server import main
    except ImportError as exc:
        print(
            "petabyte-mcp requires the MCP extra (Python 3.12+):\n"
            '    pip install "petabyte-client[mcp]"\n'
            f"(missing dependency: {exc.name})",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        main()
    except (ConfigError, ScopeRegistryError) as exc:
        print(f"petabyte-mcp: configuration error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    run()
