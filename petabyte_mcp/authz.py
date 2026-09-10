"""Tool -> required-scope policy, checked with the API's canonical scope semantics.

This is NOT a second permission system. The API remains the authority on every request (it
authenticates the key, checks revocation/expiry/environment, enforces its own per-endpoint
scopes, ownership and rate limits). What this module adds is a *pre-check* using the scopes
the API itself reports for the key (`GET /verify_api_key`), evaluated with the API's own
`scopes.scope_grants` / `can_act_as_account` functions, so that:

  * an agent gets a clear, early INSUFFICIENT_SCOPE error naming the missing scope, instead of
    a generic 403 after a confirmation dance;
  * the documented tool scopes (e.g. `compute:write` for stop/delete) are enforced even where
    the API endpoint itself only requires generic account access — the MCP layer can only ever
    be STRICTER than the API, never looser.

`TOOL_POLICIES` is also the single source for the README's scope table (a test keeps them in
sync).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .auth import ApiKeyAuth, KeyInfo
from .config import Settings
from .errors import Codes, fail
from .scopes_registry import ScopeRegistry

Kind = Literal["read", "write"]


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    required_scopes: tuple[str, ...]  # granular scopes the key must grant; () = public
    kind: Kind  # "write" tools are hidden in read-only mode
    endpoints: tuple[str, ...]  # the Petabyte endpoints the tool calls
    needs_account_access: bool = True  # False only for public (unauthenticated) endpoints
    destructive: bool = False  # terminates / settles an instance
    moves_money: bool = False  # escrows or charges the wallet
    confirm: bool = False  # uses the two-step confirmation protocol

    @property
    def public(self) -> bool:
        return not self.required_scopes and not self.needs_account_access


def _public(name: str, *endpoints: str) -> ToolPolicy:
    return ToolPolicy(name, (), "read", endpoints, needs_account_access=False)


def _read(name: str, scope: str, *endpoints: str) -> ToolPolicy:
    return ToolPolicy(name, (scope,), "read", endpoints)


TOOL_POLICIES: dict[str, ToolPolicy] = {
    p.name: p
    for p in (
        # -- marketplace discovery (public endpoints: no key, no scope) --
        _public("list_offers", "GET /marketplace/specs"),
        _public("get_offer", "GET /marketplace/specs/{offer_id}"),
        _public("list_templates", "GET /templates"),
        _public("estimate_cost", "POST /estimate"),
        # -- compute reads --
        _read("list_instances", "compute:read", "GET /vm"),
        _read(
            "get_instance", "compute:read", "GET /vm/{instance_id}", "GET /vm/{instance_id}/events"
        ),
        _read("get_usage", "compute:read", "GET /buyer/spend"),
        # -- billing / account reads --
        _read("get_balance", "wallets:read", "GET /wallet"),
        _read("get_account", "users:read", "GET /me", "GET /verify_api_key"),
        _read("list_bookings", "escrow:read", "GET /account/bookings"),
        # -- compute actions (compute:write) --
        ToolPolicy(
            "create_instance",
            ("compute:write",),
            "write",
            ("POST /launch", "POST /estimate"),
            moves_money=True,
            confirm=True,
        ),
        ToolPolicy(
            "extend_instance",
            ("compute:write",),
            "write",
            ("GET /vm/{instance_id}", "POST /vm/{instance_id}/extend"),
            moves_money=True,
            confirm=True,
        ),
        ToolPolicy(
            "stop_instance",
            ("compute:write",),
            "write",
            ("GET /vm/{instance_id}", "POST /vm/{instance_id}/stop"),
            destructive=True,
            confirm=True,
        ),
        ToolPolicy(
            "restart_instance",
            ("compute:write",),
            "write",
            ("GET /vm/{instance_id}", "POST /vm/{instance_id}/stop", "POST /launch"),
            destructive=True,
            moves_money=True,
            confirm=True,
        ),
        ToolPolicy(
            "delete_instance",
            ("compute:write",),
            "write",
            ("GET /vm/{instance_id}", "POST /vm/{instance_id}/stop"),
            destructive=True,
            confirm=True,
        ),
    )
}


class Authorizer:
    def __init__(self, auth: ApiKeyAuth, registry: ScopeRegistry, settings: Settings) -> None:
        self._auth = auth
        self._registry = registry
        self._settings = settings

    def policy(self, tool: str) -> ToolPolicy:
        try:
            return TOOL_POLICIES[tool]
        except KeyError as exc:  # programming error: every registered tool has a policy
            raise KeyError(f"no scope policy for tool {tool!r}") from exc

    async def authorize(self, tool: str) -> KeyInfo | None:
        """Raise a `ToolFailure` unless the configured key may run `tool`. Returns the key's
        introspection (None for public tools)."""
        policy = self.policy(tool)
        if policy.kind == "write" and self._settings.read_only:
            fail(
                Codes.READ_ONLY_MODE,
                f"{tool} is disabled: this MCP server runs in read-only mode "
                "(PETABYTE_MCP_READ_ONLY=true).",
            )
        if policy.public:
            return None
        info = await self._auth.introspect()
        held = list(info.scopes)
        reg = self._registry
        if not held:
            fail(
                Codes.INSUFFICIENT_SCOPE,
                "this API key carries no scopes; Petabyte refuses unscoped keys by default. "
                f"Re-mint it with: {', '.join(policy.required_scopes)} (plus 'read' for "
                "account access).",
                details={
                    "required": list(policy.required_scopes),
                    "missing": list(policy.required_scopes),
                    "held": [],
                },
            )
        missing = [s for s in policy.required_scopes if not reg.scope_grants(held, s)]
        if missing:
            fail(
                Codes.INSUFFICIENT_SCOPE,
                f"this API key is missing the required scope(s): {', '.join(missing)}. "
                f"{tool} requires {', '.join(policy.required_scopes)}; the key holds "
                f"{', '.join(held)}. Mint a key with that scope on the Petabyte website.",
                details={
                    "required": list(policy.required_scopes),
                    "missing": missing,
                    "held": held,
                },
            )
        if policy.needs_account_access and not reg.can_act_as_account(held):
            fail(
                Codes.ACCOUNT_ACCESS_REQUIRED,
                f"this API key holds {', '.join(held)} but no account-access scope. The "
                "Petabyte API only lets a key act on the account when it carries an "
                "account-family scope (for example the 'read' scope, or users:read). Mint a "
                f"key with 'read' plus {', '.join(policy.required_scopes)}.",
                details={"held": held, "required": list(policy.required_scopes)},
            )
        return info
