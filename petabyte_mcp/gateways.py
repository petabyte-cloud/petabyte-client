"""Connection gateways: which public box a rental's traffic enters through.

`GET /gateways` lists them (e.g. `us`, and `sa` in Riyadh). This MCP server runs on the user's own
machine, so it can measure what matters for `gateway=auto`: the latency from HERE to each gateway.
It sends those numbers with the launch and the API picks the fastest. Only hosts on the API's own
site are ever probed (a TCP connect to :443, nothing sent), so a hostile listing cannot make this
process connect anywhere else.
"""

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit


def _site(host: str) -> str:
    return ".".join(host.lower().rstrip(".").split(".")[-2:])


def probe_host_allowed(host: str | None, api_url: str) -> bool:
    api_host = urlsplit(api_url).hostname or ""
    if not host or not api_host:
        return False
    return host.lower().endswith("." + _site(api_host)) or host.lower() == _site(api_host)


async def _tcp_ms(host: str, samples: int = 3, timeout: float = 2.0) -> float | None:
    best = None
    for _ in range(samples):
        try:
            t0 = time.monotonic()
            _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 443), timeout)
            ms = (time.monotonic() - t0) * 1000
            writer.close()
            best = ms if best is None else min(best, ms)
        except (TimeoutError, OSError):
            break
    return round(best, 1) if best is not None else None


async def probe(rt: Any) -> list[dict[str, Any]]:
    """[{id, label, country, zone, default, rtt_ms}] — never raises (an older API has no list)."""
    try:
        raw = await rt.get("/gateways", auth=False)
    except Exception:  # noqa: BLE001 — auto falls back to server-side placement
        return []
    rows = raw.get("gateways") if isinstance(raw, dict) else None
    out: list[dict[str, Any]] = []
    for g in (rows or [])[:8]:
        if not isinstance(g, dict) or not isinstance(g.get("id"), str):
            continue
        host = urlsplit(str(g.get("ping_url") or "")).hostname
        ms = await _tcp_ms(host) if host and probe_host_allowed(host, rt.settings.api_url) else None
        out.append(
            {k: g.get(k) for k in ("id", "label", "country", "zone", "default")} | {"rtt_ms": ms}
        )
    return out


def rtt_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {r["id"]: r["rtt_ms"] for r in rows if isinstance(r.get("rtt_ms"), (int, float))}
