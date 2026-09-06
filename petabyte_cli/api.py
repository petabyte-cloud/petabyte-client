"""api — the data behind `petabyte --me` and `petabyte doctor`, fetched from the REAL endpoints.

There is no combined dashboard endpoint on the backend, and every number the dashboard
shows has one authoritative source (see the table in dashboard.py). So this module fans the
handful of small reads out in parallel over one connection pool (one round-trip of latency,
not six), and keeps each piece independent: a 403 on `/wallet/methods` from a narrowly-scoped
key, or a 404 forecast for a node that just went offline, degrades that one row to "—" and
never takes the whole dashboard down. Only `/me` is required — it is how we know who you are.

Nothing here computes money the backend does not compute. Estimates come from the backend's
own forecast (`/nodes/{id}/earnings_forecast`, lumaris_api/earnings.py) and are labelled as
estimates by the renderer.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import errors

RUNNING_VM_STATES = ("running", "starting", "migrating", "created")
ACTIVE_BOOKING_STATES = ("escrowed", "active")


def is_seller_payload(me: Any) -> bool:
    """Does this `/me` payload describe a seller? The single source of that rule (doctor.py
    used to re-derive it), tolerant of anything the API might actually send: `nodes` is only
    as well-typed as the response, and this decides which SECTIONS to show — never worth
    raising over."""
    if not isinstance(me, dict):
        return False
    if me.get("role") == "seller":
        return True
    try:
        return int(me.get("nodes") or 0) > 0
    except (TypeError, ValueError):
        return False


@dataclass
class Piece:
    """One endpoint's outcome."""
    data: Any = None
    status: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.data is not None

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default) if isinstance(self.data, dict) else default


@dataclass
class Dashboard:
    me: Piece = field(default_factory=Piece)
    wallet: Piece = field(default_factory=Piece)
    spend: Piece = field(default_factory=Piece)
    seller: Piece = field(default_factory=Piece)            # /seller/dashboard
    seller_earnings: Piece = field(default_factory=Piece)   # /seller/earnings
    vms: Piece = field(default_factory=Piece)
    clusters: Piece = field(default_factory=Piece)
    bookings: Piece = field(default_factory=Piece)
    notifications: Piece = field(default_factory=Piece)
    twofa: Piece = field(default_factory=Piece)
    payout_methods: Piece = field(default_factory=Piece)
    forecasts: dict[int, Piece] = field(default_factory=dict)  # spec_id -> /nodes/{id}/earnings_forecast

    # ---- derived facts (no math the backend didn't do) --------------------------------
    @property
    def is_seller(self) -> bool:
        return is_seller_payload(self.me.data)

    @property
    def nodes(self) -> list[dict]:
        return list(self.seller.get("nodes") or []) if self.seller.ok else []

    @property
    def nodes_online(self) -> list[dict]:
        return [n for n in self.nodes if n.get("online")]

    @property
    def running_vms(self) -> list[dict]:
        vms = self.vms.get("vms") or [] if self.vms.ok else []
        return [v for v in vms if str(v.get("status", "")).lower() in RUNNING_VM_STATES]

    @property
    def running_clusters(self) -> list[dict]:
        cl = self.clusters.get("clusters") or [] if self.clusters.ok else []
        return [c for c in cl if str(c.get("status", "")).lower() in ("running", "pending", "assigned")]

    @property
    def active_rentals(self) -> int | None:
        if self.seller_earnings.ok:
            try:
                return int(self.seller_earnings.get("active_rentals") or 0)
            except (TypeError, ValueError):
                return None
        return None

    @property
    def usdc_method(self) -> dict | None:
        ms = self.payout_methods.get("methods") or [] if self.payout_methods.ok else []
        for m in ms:
            if str(m.get("kind", "")).lower() == "usdc":
                return m
        return None

    def as_dict(self) -> dict[str, Any]:
        def p(x: Piece):
            return {"ok": x.ok, "status": x.status, "error": x.error, "data": x.data}

        return {"me": p(self.me), "wallet": p(self.wallet), "spend": p(self.spend), "seller": p(self.seller),
                "seller_earnings": p(self.seller_earnings), "vms": p(self.vms), "clusters": p(self.clusters),
                "bookings": p(self.bookings), "notifications": p(self.notifications), "twofa": p(self.twofa),
                "payout_methods": p(self.payout_methods),
                "forecasts": {k: p(v) for k, v in self.forecasts.items()}}


def _get(client, path: str, params: dict | None = None) -> Piece:
    try:
        r = client.get(path, params=params)
    except Exception as e:
        return Piece(error=f"{type(e).__name__}: {e}"[:200])
    if r.status_code != 200:
        return Piece(status=r.status_code, error=errors.humanize_response(r) or f"HTTP {r.status_code}")
    try:
        return Piece(data=r.json(), status=200)
    except ValueError:
        return Piece(status=200, error="non-JSON response")


def fetch_me(client, api_url: str = "") -> Piece:
    """`/me` is the one required call: 401 -> NotSignedIn, network -> Unreachable (raised)."""
    try:
        r = client.get("/me")
    except Exception as e:
        raise errors.from_exception(e, api_url) from e
    if r.status_code != 200:
        raise errors.from_response(r, "load your profile", api_url)
    try:
        return Piece(data=r.json(), status=200)
    except ValueError as e:
        raise errors.CliError("Petabyte answered with something that isn't a profile.",
                              detail="non-JSON /me") from e


def fetch_dashboard(client, *, api_url: str = "", max_forecasts: int = 3,
                    workers: int = 8) -> Dashboard:
    """Everything `--me` shows, in parallel. Raises only for the two conditions that make a
    dashboard impossible (not signed in, API unreachable)."""
    d = Dashboard()
    d.me = fetch_me(client, api_url)
    jobs: dict[str, Callable[[], Piece]] = {
        "wallet": lambda: _get(client, "/wallet"),
        "spend": lambda: _get(client, "/buyer/spend"),
        "vms": lambda: _get(client, "/vms"),
        "clusters": lambda: _get(client, "/clusters"),
        "bookings": lambda: _get(client, "/account/bookings", {"limit": 5}),
        "notifications": lambda: _get(client, "/notifications"),
        "twofa": lambda: _get(client, "/account/2fa"),
        "payout_methods": lambda: _get(client, "/wallet/methods"),
    }
    if d.is_seller:
        jobs["seller"] = lambda: _get(client, "/seller/dashboard")
        jobs["seller_earnings"] = lambda: _get(client, "/seller/earnings")
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as ex:
        futs = {name: ex.submit(fn) for name, fn in jobs.items()}
        for name, fut in futs.items():
            setattr(d, name, fut.result())
    if d.is_seller and d.nodes:
        # the backend's own forecast per node (net $/hr from the listed price, daily/monthly ranges)
        wanted = [n for n in d.nodes if n.get("online")] or d.nodes
        ids: list[int] = []
        for n in wanted[:max_forecasts]:
            try:                                   # one unparsable spec_id must not sink --me
                ids.append(int(n["spec_id"]))
            except (KeyError, TypeError, ValueError):
                continue
        if ids:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(ids)))) as ex:
                futs2 = {sid: ex.submit(_get, client, f"/nodes/{sid}/earnings_forecast") for sid in ids}
                for sid, fut in futs2.items():
                    d.forecasts[sid] = fut.result()
    return d


def api_health(client) -> Piece:
    return _get(client, "/healthz")
