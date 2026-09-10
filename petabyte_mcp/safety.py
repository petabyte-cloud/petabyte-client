"""Two-step confirmation for money-moving and destructive operations.

Every tool that spends, settles or terminates follows the same protocol:

  1. Called WITHOUT `confirm=true` it performs no side effect. It returns a PREVIEW of exactly
     what would happen (target instance, current status, hours left, cost) with
     `requires_confirmation: true` and the literal arguments needed to proceed.
  2. Called WITH `confirm=true` it additionally requires the caller to ECHO the target
     (`confirm_instance_id` must equal `instance_id` byte-for-byte). A confirmation that names a
     different instance — the classic hallucinated/malformed argument — is refused before any
     request is made.

`confirm` is a strict JSON boolean (`StrictBool`), so "yes", "true" or 1 are schema errors.
"""

from __future__ import annotations

from typing import Any

from .errors import Codes, fail

HOW_TO_CONFIRM = (
    "Nothing was changed. To proceed, call this tool again with confirm=true and "
    "confirm_instance_id set to exactly the instance_id shown here."
)
HOW_TO_CONFIRM_CREATE = (
    "Nothing was booked or charged. To proceed, call this tool again with "
    "confirm=true (optionally with the same idempotency_key so a retry can "
    "never double-book)."
)


def preview(action: str, *, how: str = HOW_TO_CONFIRM, **facts: Any) -> dict[str, Any]:
    """A side-effect-free description of what a confirmed call would do."""
    out: dict[str, Any] = {"requires_confirmation": True, "action": action}
    out.update(facts)
    out["how_to_confirm"] = how
    return out


def require_echo(action: str, instance_id: str, confirm_instance_id: str | None) -> None:
    """Refuse a confirmed destructive call unless the target was echoed back exactly."""
    if confirm_instance_id is None or confirm_instance_id == "":
        fail(
            Codes.CONFIRMATION_REQUIRED,
            f"{action} requires confirm_instance_id='{instance_id}' alongside confirm=true.",
            details={"instance_id": instance_id},
        )
    if confirm_instance_id != instance_id:
        fail(
            Codes.CONFIRMATION_MISMATCH,
            f"confirm_instance_id ({confirm_instance_id!r}) does not match instance_id "
            f"({instance_id!r}); refusing to {action}. Nothing was changed.",
            details={"instance_id": instance_id},
        )
