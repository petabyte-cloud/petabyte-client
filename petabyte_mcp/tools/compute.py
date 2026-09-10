"""Compute management tools: list/get instances, create, extend, stop, restart, delete.

An "instance" is a Petabyte VM (`/vm/*`). Reads need `compute:read`; every action needs
`compute:write` and follows the two-step confirmation protocol in `safety.py`. Every action
is a single existing API endpoint; the only composition is `restart_instance`, because the
Petabyte API has no in-place reboot (see its docstring).
"""

import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import Codes, ToolFailure, fail
from ..runtime import Runtime
from ..safety import HOW_TO_CONFIRM_CREATE, preview, require_echo
from ..shaping import shape_estimate, shape_events, shape_instance, shape_instances, shape_launch
from ..validation import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    ComputeMode,
    Confirm,
    ExtendHours,
    Hours,
    IdempotencyKey,
    InstanceId,
    InstanceStatus,
    OfferId,
    Price,
    ShortText,
    TemplateName,
    enforce_hours_cap,
    enforce_price_cap,
    validate_template_params,
)

_READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
_MONEY = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)
_DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)

_STOP_EFFECT = (
    "The instance is terminated (Petabyte instances are ephemeral: stop is final, "
    "there is no resume). You are billed for the hours actually held and the unused "
    "prepaid hours are refunded to your wallet."
)


def _new_key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _estimated_cost(hourly_rate: Any, hours: int) -> float | None:
    try:
        return round(float(hourly_rate) * hours, 6)
    except (TypeError, ValueError):
        return None


async def _fetch_instance(rt: Runtime, instance_id: str) -> dict[str, Any]:
    """GET /vm/{id}, shaped. The API answers 404 both for an unknown id and for another
    user's instance (deliberately indistinguishable), and its generic 404 handler words it as
    a missing endpoint; say what it actually means for the agent instead."""
    try:
        return shape_instance(await rt.get(f"/vm/{instance_id}"))
    except ToolFailure as exc:
        if exc.code != Codes.NOT_FOUND:
            raise
        fail(
            Codes.NOT_FOUND,
            f"instance {instance_id} was not found for this API key (it may belong to another "
            "user, or never existed). Use list_instances to see your instances.",
            request_id=exc.request_id,
        )


def register(server: MCPServer, rt: Runtime) -> None:
    write_enabled = not rt.settings.read_only

    # ------------------------------------------------------------------ reads
    @server.tool(name="list_instances", title="List my instances", annotations=_READ)
    async def list_instances(status: InstanceStatus | None = None) -> dict[str, Any]:
        """List the caller's own GPU instances (VMs) with status, stable address, hourly rate
        and hours left in the prepaid window. Optionally filter by `status`
        (starting/running/migrating/stopped/failed). Requires scope compute:read."""
        await rt.authz.authorize("list_instances")
        instances = shape_instances(await rt.get("/vm"))
        if status:
            instances = [i for i in instances if i.get("status") == status]
        return {"instances": instances, "count": len(instances)}

    @server.tool(name="get_instance", title="Get instance", annotations=_READ)
    async def get_instance(instance_id: InstanceId, include_events: bool = False) -> dict[str, Any]:
        """One of the caller's instances by `instance_id`: status, address (hostname/http/ssh),
        template, booking, hourly rate, hours left. `include_events=true` adds its timeline
        (start, migrations, stop). Another user's instance is reported as NOT_FOUND by the API.
        Requires scope compute:read."""
        await rt.authz.authorize("get_instance")
        out = await _fetch_instance(rt, instance_id)
        if include_events:
            out["events"] = shape_events(await rt.get(f"/vm/{instance_id}/events"))
        return out

    if not write_enabled:
        return

    # ------------------------------------------------------------------ create
    @server.tool(name="create_instance", title="Create (launch) instance", annotations=_MONEY)
    async def create_instance(
        template: TemplateName,
        hours: Hours = 1,
        max_price_per_hour: Price | None = None,
        region: ShortText | None = None,
        offer_id: OfferId | None = None,
        template_params: dict[str, Any] | None = None,
        compute_mode: ComputeMode | None = None,
        confirm: Confirm = False,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Launch a GPU instance from a template (see list_templates) on the cheapest verified
        host that fits, or on a specific host via `offer_id` (from list_offers). Prepays
        `hours` x rate into escrow from the wallet; unused hours are refunded on stop.

        Two-step: without `confirm=true` this only returns a cost estimate and what would be
        booked (no side effect). With `confirm=true` it books and launches. Pass the same
        `idempotency_key` on a retry so the API returns the original launch instead of booking
        twice. Requires scope compute:write (a read-only key is refused before any request).
        """
        await rt.authz.authorize("create_instance")
        enforce_hours_cap(hours, rt.settings.max_hours)
        price = enforce_price_cap(max_price_per_hour, rt.settings.max_price_per_hour)
        params = validate_template_params(template_params)
        intent: dict[str, Any] = {"template": template, "hours": hours}
        if price is not None:
            intent["max_price_per_hour"] = price
        if region:
            intent["region"] = region
        if offer_id:
            intent["spec_id"] = offer_id
        if params is not None:
            intent["template_params"] = params
        if compute_mode:
            intent["compute_mode"] = compute_mode
        if not confirm:
            facts: dict[str, Any] = {k: v for k, v in intent.items() if k != "template_params"}
            if offer_id:
                facts["offer_id"] = facts.pop("spec_id")
            est_body: dict[str, Any] = {"template": template, "hours": min(hours, 720)}
            if offer_id:
                est_body["spec_id"] = offer_id
            try:
                facts["estimate"] = shape_estimate(
                    await rt.post("/estimate", json=est_body, auth=False)
                )
            except ToolFailure as exc:
                facts["estimate_unavailable"] = exc.message
            facts["suggested_idempotency_key"] = idempotency_key or _new_key("mcp-launch")
            return preview("create_instance", how=HOW_TO_CONFIRM_CREATE, **facts)
        key = idempotency_key or _new_key("mcp-launch")
        raw = await rt.post("/launch", json=intent, idempotency_key=key, long=True)
        out = shape_launch(raw)
        out["idempotency_key"] = key
        out["escrowed"] = True
        return out

    # ------------------------------------------------------------------ extend
    @server.tool(name="extend_instance", title="Extend instance", annotations=_MONEY)
    async def extend_instance(
        instance_id: InstanceId,
        hours: ExtendHours,
        confirm: Confirm = False,
        confirm_instance_id: InstanceId | None = None,
    ) -> dict[str, Any]:
        """Add `hours` to a running instance's prepaid window (charged from the wallet at the
        instance's hourly rate). Two-step: preview without `confirm=true`; then call again with
        confirm=true and confirm_instance_id=<instance_id>. Requires scope compute:write."""
        await rt.authz.authorize("extend_instance")
        enforce_hours_cap(hours, rt.settings.max_hours)
        inst = await _fetch_instance(rt, instance_id)
        if inst.get("status") in TERMINAL_STATUSES:
            fail(
                Codes.ALREADY_STOPPED,
                f"instance {instance_id} is {inst.get('status')} and cannot be extended.",
            )
        if not confirm:
            return preview(
                "extend_instance",
                instance=inst,
                hours_to_add=hours,
                estimated_cost=_estimated_cost(inst.get("hourly_rate"), hours),
                effect="the prepaid window grows by the hours above; the cost is "
                "debited from your wallet now",
            )
        require_echo("extend_instance", instance_id, confirm_instance_id)
        raw = await rt.post(f"/vm/{instance_id}/extend", json={"hours": hours})
        return {
            "instance_id": instance_id,
            "status": "extended",
            "hours_added": hours,
            "hours_left": (raw or {}).get("hours_left"),
        }

    # ------------------------------------------------------------------ stop / delete
    async def _terminate(
        tool: str,
        instance_id: str,
        confirm: bool,
        confirm_instance_id: str | None,
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        inst = await _fetch_instance(rt, instance_id)
        status = inst.get("status")
        if status in TERMINAL_STATUSES:
            if idempotent:
                return {
                    "instance_id": instance_id,
                    "status": status,
                    "changed": False,
                    "note": "already terminated; nothing to do",
                }
            fail(Codes.ALREADY_STOPPED, f"instance {instance_id} is already {status}.")
        if not confirm:
            return preview(
                tool,
                instance=inst,
                effect=_STOP_EFFECT,
                hours_left_to_refund=inst.get("hours_left"),
            )
        require_echo(tool, instance_id, confirm_instance_id)
        raw = await rt.post(f"/vm/{instance_id}/stop")
        return {
            "instance_id": instance_id,
            "status": (raw or {}).get("status", "stopped"),
            "changed": True,
            "note": _STOP_EFFECT,
        }

    @server.tool(name="stop_instance", title="Stop instance", annotations=_DESTRUCTIVE)
    async def stop_instance(
        instance_id: InstanceId,
        confirm: Confirm = False,
        confirm_instance_id: InstanceId | None = None,
    ) -> dict[str, Any]:
        """Stop (terminate) one of the caller's instances. Stop is FINAL on Petabyte: the VM is
        released, actual hours are billed, unused prepaid hours are refunded. Refuses an
        instance that is already stopped. Two-step: preview without confirm; then confirm=true
        plus confirm_instance_id=<instance_id>. Requires scope compute:write."""
        await rt.authz.authorize("stop_instance")
        return await _terminate(
            "stop_instance", instance_id, confirm, confirm_instance_id, idempotent=False
        )

    @server.tool(
        name="delete_instance",
        title="Delete (terminate) instance",
        annotations=_DESTRUCTIVE_IDEMPOTENT,
    )
    async def delete_instance(
        instance_id: InstanceId,
        confirm: Confirm = False,
        confirm_instance_id: InstanceId | None = None,
    ) -> dict[str, Any]:
        """Terminate an instance and release its host. Petabyte has no separate delete: this is
        the terminal stop (billed for hours held, unused prepay refunded) and the record stays
        visible in list_instances as `stopped` for billing history. Idempotent: an already
        terminated instance is reported as unchanged. Two-step: preview without confirm; then
        confirm=true plus confirm_instance_id=<instance_id>. Requires scope compute:write."""
        await rt.authz.authorize("delete_instance")
        return await _terminate(
            "delete_instance", instance_id, confirm, confirm_instance_id, idempotent=True
        )

    # ------------------------------------------------------------------ restart
    @server.tool(
        name="restart_instance", title="Restart (re-provision) instance", annotations=_DESTRUCTIVE
    )
    async def restart_instance(
        instance_id: InstanceId,
        hours: Hours = 1,
        max_price_per_hour: Price | None = None,
        region: ShortText | None = None,
        template_params: dict[str, Any] | None = None,
        confirm: Confirm = False,
        confirm_instance_id: InstanceId | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Re-provision an instance. The Petabyte API has NO in-place reboot, so this stops the
        instance (final; hours held are billed, unused prepay refunded) and then launches a NEW
        instance of the same template with a NEW booking that escrows `hours` x rate. The new
        instance gets a new instance_id and address; nothing on the old VM's disk is preserved.
        Pass `template_params` if the original launch used any. Two-step: preview without
        confirm; then confirm=true plus confirm_instance_id=<instance_id>. Requires scope
        compute:write."""
        await rt.authz.authorize("restart_instance")
        enforce_hours_cap(hours, rt.settings.max_hours)
        price = enforce_price_cap(max_price_per_hour, rt.settings.max_price_per_hour)
        params = validate_template_params(template_params)
        inst = await _fetch_instance(rt, instance_id)
        template = inst.get("template")
        if not template:
            fail(
                Codes.INVALID_ARGUMENT,
                f"instance {instance_id} has no template recorded, so it cannot be relaunched.",
            )
        launch: dict[str, Any] = {"template": template, "hours": hours}
        if price is not None:
            launch["max_price_per_hour"] = price
        if region:
            launch["region"] = region
        if params is not None:
            launch["template_params"] = params
        will_stop = inst.get("status") in ACTIVE_STATUSES
        if not confirm:
            return preview(
                "restart_instance",
                instance=inst,
                steps=[
                    (
                        "stop the current instance (final; unused prepaid hours refunded)"
                        if will_stop
                        else "current instance is already terminated; skip stop"
                    ),
                    (
                        f"launch a NEW '{template}' instance for {hours}h (new booking, new "
                        "escrow charge, new instance_id and address)"
                    ),
                ],
                relaunch=launch,
                warning="Petabyte has no in-place reboot: data on the old VM is not preserved.",
                suggested_idempotency_key=idempotency_key or _new_key("mcp-restart"),
            )
        require_echo("restart_instance", instance_id, confirm_instance_id)
        key = idempotency_key or _new_key("mcp-restart")
        stopped = False
        if will_stop:
            await rt.post(f"/vm/{instance_id}/stop")
            stopped = True
        try:
            raw = await rt.post("/launch", json=launch, idempotency_key=key, long=True)
        except ToolFailure as exc:
            fail(
                Codes.RESTART_RELAUNCH_FAILED,
                f"instance {instance_id} was {'stopped' if stopped else 'already terminated'}"
                f" but the relaunch failed: {exc.code}: {exc.message}. Your wallet was "
                "refunded per Petabyte's metering; launch again with create_instance.",
                details={
                    "previous_instance_id": instance_id,
                    "previous_instance_stopped": stopped,
                    "relaunch_error": exc.as_dict(),
                    "idempotency_key": key,
                },
            )
        return {
            "previous_instance_id": instance_id,
            "previous_instance_stopped": stopped,
            "new_instance": shape_launch(raw),
            "idempotency_key": key,
            "note": "the new instance has a new address; the old one is terminated",
        }
