"""Marketplace discovery tools — public Petabyte endpoints, no API key or scope needed."""

from typing import Any

from mcp.server.fastmcp import FastMCP as MCPServer
from mcp.types import ToolAnnotations

from ..errors import Codes, ToolFailure, fail
from ..runtime import Runtime
from ..shaping import shape_estimate, shape_offer, shape_offers, shape_templates
from ..validation import (
    ComputeMode,
    EstimateHours,
    ListLimit,
    OfferId,
    Offset,
    Price,
    Provider,
    ShortText,
    SortKey,
    TemplateName,
    Vram,
)

_READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


_CLOUD_GPU_FIELDS = (
    "gpu_model",
    "vram_gb",
    "gpu_count",
    "provider",
    "region",
    "country",
    "country_iso",
    "price_per_hour",
    "spot",
)


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(name="list_offers", title="Search GPU offers", annotations=_READ)
    async def list_offers(
        gpu: ShortText | None = None,
        max_price_per_hour: Price | None = None,
        region: ShortText | None = None,
        min_vram_gb: Vram | None = None,
        confidential: bool | None = None,
        compute_mode: ComputeMode | None = None,
        sort: SortKey = "price",
        limit: ListLimit = 20,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Search the public Petabyte GPU marketplace: attested, online hosts with capacity.

        Filters: `gpu` is a case-insensitive substring of the GPU model (e.g. "4090", "H100",
        "L4"); `max_price_per_hour` in USD; `region`; `min_vram_gb`; `confidential` (TEE
        hosts); `compute_mode` STANDARD/VERIFIED/CONFIDENTIAL. Sorted cheapest-first unless
        `sort` is "vram" or "rep" (reputation). Paginate with `limit`/`offset`; `count` is the
        total number of matches. Each offer's `offer_id` can be passed to get_offer,
        estimate_cost and create_instance. Requires no API key or scope.
        """
        await rt.authz.authorize("list_offers")
        params = {
            "gpu": gpu,
            "max_price": max_price_per_hour,
            "region": region,
            "min_vram": min_vram_gb,
            "confidential": confidential,
            "compute_mode": compute_mode,
            "sort": sort,
            "limit": limit,
            "offset": offset,
        }
        raw = await rt.get("/marketplace/specs", params=params, auth=False)
        return shape_offers(raw)

    @server.tool(name="get_offer", title="Get one GPU offer", annotations=_READ)
    async def get_offer(offer_id: OfferId) -> dict[str, Any]:
        """Details of one marketplace offer (host) by its `offer_id`: hardware, price, cloud
        reference price and savings, reputation, verification and buyer protections. Requires
        no API key or scope."""
        await rt.authz.authorize("get_offer")
        try:
            raw = await rt.get(f"/marketplace/specs/{offer_id}", auth=False)
        except ToolFailure as exc:
            if exc.code != Codes.NOT_FOUND:
                raise
            fail(
                Codes.NOT_FOUND,
                f"offer {offer_id} is not listed (it may have gone offline or been withdrawn). "
                "Use list_offers to search the current inventory.",
                request_id=exc.request_id,
            )
        return shape_offer(raw)

    @server.tool(name="list_templates", title="List launch templates", annotations=_READ)
    async def list_templates() -> dict[str, Any]:
        """The one-click templates an instance can be created from (e.g. ollama, jupyter,
        vllm, comfyui). Use a template `name` with create_instance. Requires no API key."""
        await rt.authz.authorize("list_templates")
        raw = await rt.get("/templates", auth=False)
        templates = shape_templates(raw)
        return {"templates": templates, "count": len(templates)}

    @server.tool(name="list_cloud_gpus", title="List on-demand cloud GPUs", annotations=_READ)
    async def list_cloud_gpus(
        provider: Provider | None = None, region: ShortText | None = None
    ) -> dict[str, Any]:
        """On-demand cloud GPUs Petabyte can start in a few minutes (DigitalOcean, AWS, Alibaba
        Cloud), each at the cloud's own hourly price. `provider` is do / aws / alibaba (any or
        omitted: every cloud); `region` is a cloud region (tor1, us-east-1, me-central-1) or a
        country code (SA, US). Only GPUs that can start right now are listed. Book one with
        create_instance(provider=..., region=...). Requires no API key or scope."""
        await rt.authz.authorize("list_cloud_gpus")
        params = {"provider": provider, "region": region}
        raw = await rt.get("/marketplace/catalogue", params=params, auth=False)
        gpus = [
            {k: row.get(k) for k in _CLOUD_GPU_FIELDS}
            for row in (raw or {}).get("catalogue", [])
            if row.get("provisionable")
        ]
        gpus.sort(key=lambda g: g.get("price_per_hour") or 0)
        return {"gpus": gpus, "count": len(gpus)}

    @server.tool(name="list_gateways", title="List connection gateways", annotations=_READ)
    async def list_gateways() -> dict[str, Any]:
        """The connection gateways an instance can be served through (e.g. `us`, `sa` = Riyadh,
        Saudi Arabia) with the latency from this machine to each. create_instance takes
        `gateway` (auto picks the fastest of these) and `residency` (e.g. "SA": a host verified
        in that country, an in-country gateway, checkpoints kept in-country). Requires no API key."""
        await rt.authz.authorize("list_gateways")
        from .. import gateways as _gws

        rows = await _gws.probe(rt)
        return {"gateways": rows, "count": len(rows)}

    @server.tool(name="estimate_cost", title="Estimate rental cost", annotations=_READ)
    async def estimate_cost(
        hours: EstimateHours = 1,
        template: TemplateName | None = None,
        offer_id: OfferId | None = None,
    ) -> dict[str, Any]:
        """What a rental will cost BEFORE committing: total prepaid into escrow, hourly rate,
        platform fee (included, not added), what a stop-after-1h refund looks like and a
        like-for-like cloud comparison. Give `offer_id` to price a specific host, or `template`
        to price the host auto-placement would pick. Requires no API key."""
        await rt.authz.authorize("estimate_cost")
        body: dict[str, Any] = {"hours": hours}
        if template:
            body["template"] = template
        if offer_id:
            body["spec_id"] = offer_id
        raw = await rt.post("/estimate", json=body, auth=False)
        return shape_estimate(raw)
