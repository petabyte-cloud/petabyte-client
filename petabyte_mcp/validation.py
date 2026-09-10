"""Input validation — the SAME constraints the Petabyte API enforces, applied before any call.

The MCP SDK validates every tool argument against the JSON schema derived from these annotated
types, so a malformed AI-generated argument is rejected client-side (no request is made). The
numeric bounds mirror the API's pydantic models (see `config.API_*`); the API remains the final
authority and its own 422/400 responses are surfaced verbatim if anything slips through.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, StringConstraints

from .config import (
    API_MAX_BOOKINGS_LIMIT,
    API_MAX_ESTIMATE_HOURS,
    API_MAX_EXTEND_HOURS,
    API_MAX_HOURS,
    API_MAX_LIST_LIMIT,
)
from .errors import Codes, fail

# Opaque handles: VM ids are random alphanumerics (db._rand_vm_id), spec public ids likewise.
_HANDLE = r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$"

InstanceId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=_HANDLE)]
OfferId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=_HANDLE)]
TemplateName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$"),
]
Hours = Annotated[int, Field(ge=1, le=API_MAX_HOURS, description="hours to prepay into escrow")]
ExtendHours = Annotated[int, Field(ge=1, le=API_MAX_EXTEND_HOURS)]
EstimateHours = Annotated[int, Field(ge=1, le=API_MAX_ESTIMATE_HOURS)]
ListLimit = Annotated[int, Field(ge=1, le=API_MAX_LIST_LIMIT)]
BookingsLimit = Annotated[int, Field(ge=1, le=API_MAX_BOOKINGS_LIMIT)]
Offset = Annotated[int, Field(ge=0, le=1_000_000)]
Price = Annotated[float, Field(gt=0, le=1_000_000, description="USD per hour")]
Vram = Annotated[int, Field(ge=0, le=100_000, description="GB")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
IdempotencyKey = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
]
Confirm = StrictBool  # a JSON boolean only — "yes", 1, "true" are rejected

SortKey = Literal["price", "vram", "rep"]
ComputeMode = Literal["STANDARD", "VERIFIED", "CONFIDENTIAL"]
CpuTee = Literal["sev_snp", "tdx", "any"]
InstanceStatus = Literal["starting", "running", "migrating", "stopped", "failed"]

ACTIVE_STATUSES = frozenset({"starting", "running", "migrating"})
TERMINAL_STATUSES = frozenset({"stopped", "failed"})

_MAX_TEMPLATE_PARAMS_BYTES = 4096
_MAX_TEMPLATE_PARAMS_DEPTH = 3


def _depth(value: Any, level: int = 0) -> int:
    if isinstance(value, dict):
        return max([level + 1] + [_depth(v, level + 1) for v in value.values()])
    if isinstance(value, list):
        return max([level + 1] + [_depth(v, level + 1) for v in value])
    return level


def validate_template_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Template parameters are forwarded to the API verbatim (it validates their meaning, e.g.
    `notebook_url`). Here we only bound their SHAPE so an agent cannot push megabytes or deeply
    nested junk through the tool: string keys, JSON-serialisable, <= 4 KiB, depth <= 3."""
    if params is None:
        return None
    if not isinstance(params, dict):
        fail(Codes.INVALID_ARGUMENT, "template_params must be an object")
    for key in params:
        if not isinstance(key, str) or not key or len(key) > 64:
            fail(Codes.INVALID_ARGUMENT, "template_params keys must be short non-empty strings")
    try:
        encoded = json.dumps(params)
    except (TypeError, ValueError):
        fail(Codes.INVALID_ARGUMENT, "template_params must be JSON-serialisable")
    if len(encoded.encode()) > _MAX_TEMPLATE_PARAMS_BYTES:
        fail(Codes.INVALID_ARGUMENT, f"template_params exceeds {_MAX_TEMPLATE_PARAMS_BYTES} bytes")
    if _depth(params) > _MAX_TEMPLATE_PARAMS_DEPTH:
        fail(Codes.INVALID_ARGUMENT, "template_params is nested too deeply")
    return params


def enforce_hours_cap(hours: int, max_hours: int) -> int:
    """MCP-side guard rail: an agent may not escrow more than `max_hours` in one call, even
    though the API itself allows up to a year. Raise LIMIT_EXCEEDED with the cap named."""
    if hours > max_hours:
        fail(
            Codes.LIMIT_EXCEEDED,
            f"hours={hours} exceeds this MCP server's cap of {max_hours} hours per call "
            "(PETABYTE_MCP_MAX_HOURS). Ask for a shorter window or extend later.",
            details={"max_hours": max_hours},
        )
    return hours


def enforce_price_cap(max_price_per_hour: float | None, cap: float | None) -> float | None:
    """If the operator configured a $/hour ceiling, every launch is bounded by it: an unset
    price becomes the cap, a higher price is refused."""
    if cap is None:
        return max_price_per_hour
    if max_price_per_hour is None:
        return cap
    if max_price_per_hour > cap:
        fail(
            Codes.LIMIT_EXCEEDED,
            f"max_price_per_hour={max_price_per_hour} exceeds this MCP server's cap of "
            f"${cap}/hour (PETABYTE_MCP_MAX_PRICE_PER_HOUR).",
            details={"max_price_per_hour": cap},
        )
    return max_price_per_hour
