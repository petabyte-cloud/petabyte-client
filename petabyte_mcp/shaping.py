"""Response shaping: explicit ALLOWLISTS of the fields each tool returns.

The API's responses are shaped for its web UI and CLI; an agent needs a compact, stable subset.
Allowlisting (rather than passing bodies through) also guarantees that a field added to the API
tomorrow — an internal id, a node IP, a payout reference — can never leak through the MCP layer
without a deliberate change here. Values are passed through untouched (no rounding of money).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def _pick(src: Mapping[str, Any] | None, fields: Iterable[str]) -> dict[str, Any]:
    if not isinstance(src, Mapping):
        return {}
    return {f: src[f] for f in fields if f in src}


_OFFER_FIELDS = (
    "id",
    "gpu_model",
    "gpu_count",
    "vram_gb",
    "cpu",
    "ram_gb",
    "price_per_hour",
    "cloud_reference",
    "region",
    "region_verified",
    "confidential",
    "compute_mode",
    "reputation_score",
    "available_units",
    "total_units",
    "trust",
    "attested",
    "jobs_completed",
    "jobs_failed",
    "success_rate",
    "online",
    "savings_pct",
)
_OFFER_VERIFICATION_FIELDS = (
    "agent_attested",
    "benchmark_verified",
    "region_verified",
    "hardware_attested",
)
_INSTANCE_FIELDS = (
    "template",
    "status",
    "port",
    "booking_id",
    "hourly_rate",
    "hours_left",
    "migrations",
    "created_at",
)
_INSTANCE_URL_FIELDS = ("hostname", "http", "ssh", "default_user")
_EVENT_FIELDS = ("event", "detail", "at")
_USAGE_FIELDS = (
    "balance",
    "in_escrow",
    "spent_lifetime",
    "active_instances",
    "burn_rate_per_hour",
    "projected_24h",
    "burn_by_template",
    "hours_of_runway",
)
_BALANCE_FIELDS = (
    "balance",
    "earnings",
    "withdrawable",
    "clearing",
    "instant_eligible",
    "hold_hours",
)
_ACCOUNT_FIELDS = (
    "username",
    "email",
    "role",
    "email_verified",
    "reputation",
    "balance",
    "earnings",
    "can_accept_paid_jobs",
    "nodes",
    "bookings",
)
_BOOKING_FIELDS = ("id", "role", "gpu_model", "hours", "gross_amount", "status", "created_at")
_ESTIMATE_FIELDS = (
    "gpu_model",
    "region",
    "price_per_hour",
    "hours",
    "total",
    "min_charge",
    "platform_fee_included",
    "you_pay_now",
    "if_you_stop_after_1h",
    "cloud_comparison",
    "notes",
)
_TEMPLATE_FIELDS = ("name", "desc", "port", "gpu", "min_vram", "kind", "stateful")
_LAUNCH_FIELDS = (
    "booking_id",
    "task_id",
    "template",
    "port",
    "gpu_model",
    "region",
    "price_per_hour",
    "hours",
    "gross_amount",
    "status",
    "routing_explanation",
    "connect",
)


def shape_offer(raw: Mapping[str, Any]) -> dict[str, Any]:
    out = _pick(raw, _OFFER_FIELDS)
    if "id" in out:
        out["offer_id"] = out.pop("id")
    if isinstance(raw.get("verification"), Mapping):
        out["verification"] = _pick(raw["verification"], _OFFER_VERIFICATION_FIELDS)
    return out


def shape_offers(raw: Mapping[str, Any]) -> dict[str, Any]:
    specs = raw.get("specs") if isinstance(raw, Mapping) else None
    offers = [shape_offer(s) for s in specs] if isinstance(specs, list) else []
    out: dict[str, Any] = {"offers": offers, "returned": len(offers)}
    for f in ("count", "limit", "offset"):
        if f in raw:
            out[f] = raw[f]
    return out


def shape_instance(raw: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "vm_id" in raw:
        out["instance_id"] = raw["vm_id"]
    out.update(_pick(raw, _INSTANCE_FIELDS))
    if isinstance(raw.get("url"), Mapping):
        out["url"] = _pick(raw["url"], _INSTANCE_URL_FIELDS)
    if raw.get("note"):
        out["note"] = raw["note"]
    return out


def shape_instances(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    vms = raw.get("vms") if isinstance(raw, Mapping) else None
    return [shape_instance(v) for v in vms] if isinstance(vms, list) else []


def shape_events(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    events = raw.get("events") if isinstance(raw, Mapping) else None
    return [_pick(e, _EVENT_FIELDS) for e in events] if isinstance(events, list) else []


def shape_usage(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _pick(raw, _USAGE_FIELDS)


def shape_balance(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _pick(raw, _BALANCE_FIELDS)


def shape_account(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _pick(raw, _ACCOUNT_FIELDS)


def shape_bookings(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = raw.get("bookings") if isinstance(raw, Mapping) else None
    out = []
    if isinstance(rows, list):
        for b in rows:
            row = _pick(b, _BOOKING_FIELDS)
            if "id" in row:
                row["booking_id"] = row.pop("id")
            out.append(row)
    return out


def shape_estimate(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _pick(raw, _ESTIMATE_FIELDS)


def shape_templates(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = raw.get("templates") if isinstance(raw, Mapping) else None
    return [_pick(t, _TEMPLATE_FIELDS) for t in rows] if isinstance(rows, list) else []


def shape_launch(raw: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "vm_id" in raw:
        out["instance_id"] = raw["vm_id"]
    out.update(_pick(raw, _LAUNCH_FIELDS))
    if isinstance(raw.get("url"), Mapping):
        out["url"] = _pick(raw["url"], _INSTANCE_URL_FIELDS)
    return out
