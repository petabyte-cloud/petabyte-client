"""Load the CANONICAL Petabyte scope registry (`lumaris_api/scopes.py`) — never a copy of it.

The MCP server pre-checks a tool's documented scopes against the scopes the API says the key
holds. That check must use exactly the semantics the API enforces (wildcards, `resource:*`,
legacy coarse scopes such as `read`/`spend`, `*`), so the registry module is loaded from the
repository by file path with `importlib` — it is pure standard library (only `re`) and has no
side effects. Loading by path (instead of `sys.path` insertion) avoids shadowing generic module
names the API package uses (`db`, `main`, `auth`, …).

Resolution order:
  1. `PETABYTE_MCP_SCOPES_MODULE` (explicit path to a `scopes.py`), if set;
  2. the sibling checkout: `<repo>/lumaris_api/scopes.py`;
  3. a build-time snapshot bundled into the wheel (`petabyte_mcp/_scopes_snapshot.py`).

Missing everywhere is a hard configuration error: the server refuses to start rather than
guessing what a scope grants.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

_MODULE_NAME = "petabyte_mcp._canonical_scopes"
_REQUIRED_ATTRS = (
    "scope_grants",
    "can_act_as_account",
    "is_valid_scope",
    "VALID_SCOPES",
    "FULL_ACCESS",
    "GRANULAR_SCOPES",
)


class ScopeRegistryError(RuntimeError):
    """The canonical scope registry could not be located or is not the module we expect."""


def candidate_paths(explicit: str | None = None) -> list[Path]:
    here = Path(__file__).resolve()
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    cands.append(here.parents[2] / "lumaris_api" / "scopes.py")  # <repo>/lumaris_api/scopes.py
    cands.append(here.parent / "_scopes_snapshot.py")  # wheel-bundled snapshot
    return cands


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ScopeRegistryError(f"cannot load scope registry from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    missing = [a for a in _REQUIRED_ATTRS if not hasattr(module, a)]
    if missing:
        del sys.modules[_MODULE_NAME]
        raise ScopeRegistryError(
            f"{path} does not look like Petabyte's scopes.py (missing {', '.join(missing)})"
        )
    return module


class ScopeRegistry:
    """A thin, typed facade over the canonical module (the module IS the source of truth)."""

    def __init__(self, module: ModuleType, source: Path) -> None:
        self._m = module
        self.source = source

    @property
    def full_access(self) -> str:
        return str(self._m.FULL_ACCESS)

    @property
    def valid_scopes(self) -> frozenset[str]:
        return frozenset(self._m.VALID_SCOPES)

    @property
    def granular_scopes(self) -> frozenset[str]:
        return frozenset(self._m.GRANULAR_SCOPES)

    def is_valid_scope(self, scope: str) -> bool:
        return bool(self._m.is_valid_scope(scope))

    def scope_grants(self, held: Iterable[str], required: str) -> bool:
        return bool(self._m.scope_grants(list(held), required))

    def can_act_as_account(self, held: Iterable[str]) -> bool:
        return bool(self._m.can_act_as_account(list(held)))


def load_registry(explicit_path: str | None = None) -> ScopeRegistry:
    tried: list[str] = []
    for path in candidate_paths(explicit_path):
        if path.is_file():
            return ScopeRegistry(_load_module(path), path)
        tried.append(str(path))
    raise ScopeRegistryError(
        "Petabyte scope registry (lumaris_api/scopes.py) not found. Run the MCP server from the "
        "repository checkout, install the built wheel, or set PETABYTE_MCP_SCOPES_MODULE to the "
        "path of scopes.py. Tried: " + ", ".join(tried)
    )
