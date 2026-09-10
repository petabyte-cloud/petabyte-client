"""scopes.py — the canonical API-key scope registry.

ONE source of truth for what a scope IS (validation on mint) and what a scope GRANTS (enforcement).
Scopes are `resource:action` (e.g. `jobs:read`, `payouts:write`). Separating read from write is the
whole point: `jobs:read` must never satisfy a `jobs:write` (or `payments:write`) requirement.

Backward compatibility (migration strategy = additive): the pre-existing COARSE scopes
(`node`, `jobs`, `data`, `inference`, `account`, `read`, `spend`, `manage`, `admin`, and `*`) remain
VALID and keep working — each maps to the granular capabilities it always implied (LEGACY_SCOPE_MAP).
So existing keys are never silently broadened or broken; only NEW mints are validated against this
registry, and enforcement understands both vocabularies via `scope_grants()`.

Wildcards are explicit, never implicit: `*` = every scope; `<resource>:*` = every action on one
resource. They are valid to request but a caller can only MINT a privilege it holds (see
deps.confine_minted_scopes), so `*` cannot be handed to an ordinary key by accident.
"""
import re

# resource -> the actions defined for it. Uses Petabyte's own resource names.
RESOURCE_ACTIONS: dict[str, tuple[str, ...]] = {
    "compute":  ("read", "write"),            # VMs / rented instances
    "jobs":     ("read", "write", "cancel"),  # compute jobs
    "nodes":    ("read", "write"),            # seller specs / node agents
    "clusters": ("read", "write"),            # distributed multi-node jobs
    "storage":  ("read", "write"),            # volumes / blobs
    "payments": ("read", "write"),            # buyer payments / charges
    "escrow":   ("read", "write"),            # booking escrow
    "ledger":   ("read",),                    # the internal double-entry ledger (read-only)
    "payouts":  ("read", "write"),            # seller withdrawals
    "wallets":  ("read", "write"),            # wallet balance / payout methods
    "api_keys": ("read", "write", "revoke"),  # this key registry
    "users":    ("read", "write"),            # account / profile
    "webhooks": ("read", "write"),            # webhook config
    "billing":  ("read", "write"),            # invoices / usage billing
    "data":     ("read",),                    # metered Data API
    "inference": ("read", "write"),           # metered Inference API
    "admin":    ("read", "write"),            # platform admin surface (NOTE: real admin is the
    #                                           ADMIN_USERS allowlist, not this scope; kept for
    #                                           completeness/least-privilege of admin tooling)
}

FULL_ACCESS = "*"

# The canonical granular scopes, e.g. "jobs:read".
GRANULAR_SCOPES: frozenset[str] = frozenset(
    f"{res}:{act}" for res, actions in RESOURCE_ACTIONS.items() for act in actions)

# Per-resource wildcards, e.g. "jobs:*".
RESOURCE_WILDCARDS: frozenset[str] = frozenset(f"{res}:*" for res in RESOURCE_ACTIONS)

# Legacy coarse scopes that predate the registry — still valid, mapped below.
LEGACY_SCOPES: frozenset[str] = frozenset(
    {"node", "jobs", "data", "inference", "account", "read", "spend", "manage", "admin"})

# Every string that is a VALID scope to request/mint.
VALID_SCOPES: frozenset[str] = (
    GRANULAR_SCOPES | RESOURCE_WILDCARDS | LEGACY_SCOPES | {FULL_ACCESS})

_ALL_READS = frozenset(f"{r}:read" for r in RESOURCE_ACTIONS)
_MONEY_WRITE = frozenset({"payments:write", "payouts:write", "wallets:write", "escrow:write"})

# What each LEGACY coarse scope grants, expressed in the granular vocabulary. Enforcement expands a
# held legacy scope through this map so a legacy key satisfies the new granular requirements too.
LEGACY_SCOPE_MAP: dict[str, frozenset[str]] = {
    "read":  _ALL_READS,
    # 'spend' historically = "can move money / book compute" (deposit, book, pay, refund, withdraw).
    "spend": _MONEY_WRITE | frozenset({"compute:write", "clusters:write", "jobs:write",
                                       "jobs:cancel", "storage:write"}),
    # 'manage' = account/key management.
    "manage": frozenset({"api_keys:read", "api_keys:write", "api_keys:revoke",
                         "users:read", "users:write"}),
    # 'node' = a seller node agent: manage its own spec + run jobs.
    "node":  frozenset({"nodes:read", "nodes:write", "jobs:read", "jobs:write"}),
    # legacy bare 'jobs' == all job actions.
    "jobs":  frozenset({"jobs:read", "jobs:write", "jobs:cancel"}),
    "data":  frozenset({"data:read"}),
    "inference": frozenset({"inference:read", "inference:write"}),
    "admin": frozenset({"admin:read", "admin:write"}),
}
# 'account' = full account access (the old _ACCOUNT ∪ _SPEND behaviour): read + spend + manage.
LEGACY_SCOPE_MAP["account"] = (
    LEGACY_SCOPE_MAP["read"] | LEGACY_SCOPE_MAP["spend"] | LEGACY_SCOPE_MAP["manage"])

# Account-family granular scopes: holding any of these means the key can act on the account (the
# get_current_user gate), as opposed to a pure compute/agent key (compute/jobs/nodes/clusters/storage).
ACCOUNT_RESOURCES = frozenset({"payments", "payouts", "wallets", "escrow", "ledger",
                               "api_keys", "users", "billing", "admin"})

# Plain-language descriptions for the scope picker in the web UI (GET /scopes). Kept HERE, next to
# RESOURCE_ACTIONS, so a new resource without a description fails the smoke test instead of
# appearing in the picker as a bare identifier.
RESOURCE_DESCRIPTIONS: dict[str, str] = {
    "compute":   "VMs and rented instances",
    "jobs":      "Compute jobs you run",
    "nodes":     "Your seller nodes and their specs",
    "clusters":  "Distributed multi-node jobs",
    "storage":   "Volumes and blobs",
    "payments":  "Buyer payments and charges",
    "escrow":    "Booking escrow",
    "ledger":    "The double-entry ledger (read-only)",
    "payouts":   "Seller withdrawals",
    "wallets":   "Wallet balance and payout methods",
    "api_keys":  "This key registry",
    "users":     "Account and profile",
    "webhooks":  "Webhook configuration",
    "billing":   "Invoices and usage billing",
    "data":      "Metered Data API",
    "inference": "Metered Inference API",
    "admin":     "Platform admin tooling",
}
ACTION_DESCRIPTIONS: dict[str, str] = {
    "read":   "list and retrieve",
    "write":  "create and change",
    "cancel": "cancel a running job",
    "revoke": "revoke keys",
}
# Resources whose write scopes move money — the picker flags them so a seller does not hand a CI
# runner a key that can withdraw.
MONEY_RESOURCES: frozenset[str] = frozenset({"payments", "payouts", "wallets", "escrow"})


def registry_payload() -> dict:
    """The registry as the web scope picker consumes it: one entry per resource with its actions,
    description and money flag, plus the two presets. Derived from RESOURCE_ACTIONS so the picker
    can never drift from what validate_scopes() accepts."""
    return {
        "resources": [{"name": r, "actions": list(acts),
                       "description": RESOURCE_DESCRIPTIONS.get(r, ""),
                       "money": r in MONEY_RESOURCES}
                      for r, acts in RESOURCE_ACTIONS.items()],
        "actions": dict(ACTION_DESCRIPTIONS),
        "presets": {"read_only": sorted(_ALL_READS), "full_access": [FULL_ACCESS]},
        "full_access": FULL_ACCESS,
    }


_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]*(:(\*|[a-z][a-z0-9_]*))?$|^\*$")


def is_valid_scope(scope: str) -> bool:
    """A scope is valid if it is a known granular scope, a known resource wildcard, a legacy scope,
    or '*'. Unknown resources/actions and malformed strings are rejected."""
    return isinstance(scope, str) and scope in VALID_SCOPES


def validate_scopes(scopes) -> list[str]:
    """Normalize + validate a requested scope list. De-duplicates (preserving order), rejects
    empty/whitespace/malformed and UNKNOWN scopes (an unknown resource or action, a typo, a made-up
    `jobs:destroy`). Raises ValueError listing the offenders. Returns the clean list."""
    seen: list[str] = []
    bad: list[str] = []
    for raw in scopes or []:
        s = (raw or "").strip()
        if not s:
            bad.append(repr(raw))
            continue
        if not _SCOPE_RE.match(s) or not is_valid_scope(s):
            bad.append(s)
            continue
        if s not in seen:
            seen.append(s)
    if bad:
        raise ValueError("unknown or malformed scope(s): " + ", ".join(bad))
    return seen


def scope_grants(held, required: str) -> bool:
    """Does a set of HELD scopes (which may mix granular, legacy and wildcard) satisfy a single
    REQUIRED granular scope? '*' grants everything; an exact match or a `<resource>:*` wildcard
    grants it; and a held legacy scope grants it if `required` is in that legacy scope's expansion.
    Read never satisfies write: `jobs:read` does not grant `jobs:write`."""
    held = set(held or [])
    if FULL_ACCESS in held:
        return True
    if required in held:
        return True
    resource = required.split(":", 1)[0]
    if f"{resource}:*" in held:
        return True
    return any(required in LEGACY_SCOPE_MAP.get(h, ()) for h in held)


def grants_any(held, required_scopes) -> bool:
    """True if the held scopes satisfy ANY of the required granular scopes."""
    return any(scope_grants(held, r) for r in required_scopes)


def can_act_as_account(held) -> bool:
    """Whether an API key may act on the ACCOUNT (vs. a pure compute/agent key). True for the legacy
    account-family scopes, for '*', or for any granular scope on an account-family resource."""
    held = set(held or [])
    if FULL_ACCESS in held or ({"account", "read", "spend", "manage", "admin"} & held):
        return True
    return any(s.split(":", 1)[0] in ACCOUNT_RESOURCES for s in held if ":" in s)


def can_spend(held) -> bool:
    """Whether an API key may MOVE MONEY. True for the legacy spend family, or any granular
    money-write scope (payments/payouts/wallets/escrow :write), or '*'."""
    held = set(held or [])
    if FULL_ACCESS in held or ({"account", "spend", "admin"} & held):
        return True
    return grants_any(held, _MONEY_WRITE)
