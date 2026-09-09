"""dashboard — `petabyte --me`: one screen that says who you are and what is happening.

Every row has one authoritative source; nothing is computed that the backend does not
compute itself:

  ACCOUNT          GET /me (username, email, role, email_verified, reputation, counts),
                   GET /account/2fa
  WALLET           GET /wallet (balance, earnings, withdrawable, clearing),
                   GET /buyer/spend (in_escrow, spent_lifetime), GET /wallet/methods (redacted)
  WORKFLOW         GET /vms + /clusters (buyer), GET /seller/earnings + /seller/dashboard (seller),
                   the local agent status endpoint (current task)
  AGENT            local service state (systemd / WSL / process), 127.0.0.1:5000/api/status,
                   GET /seller/dashboard nodes (last_seen, units, price, earned_total)
  EXPECTED RETURN  GET /nodes/{id}/earnings_forecast — the backend's own estimate, shown AS an
                   estimate; buyers get their real burn rate from /buyer/spend
  SYSTEM           this machine (sysinfo)
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from . import agent as agentmod
from . import api as apimod
from . import sysinfo
from .ui import money, pct

_DASH = "—"


def _parse_ts(ts: Any) -> _dt.datetime | None:
    """An API timestamp as an aware datetime, or None. Used to ORDER timestamps: comparing the
    raw strings is only correct when every node uses one format, and `Z` vs `+00:00` or a
    stray fractional part silently picks the wrong heartbeat."""
    if not ts:
        return None
    try:
        s = str(ts).replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        t = _dt.datetime.fromisoformat(s)
        return t.replace(tzinfo=_dt.timezone.utc) if t.tzinfo is None else t   # noqa: UP017
    except Exception:
        return None


def _rel(ts: Any, now: _dt.datetime | None = None) -> str:
    """'12s ago' / '3m ago' from an ISO timestamp (naive = UTC)."""
    if not ts:
        return _DASH
    try:
        s = str(ts).replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        t = _dt.datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)  # noqa: UP017 — the CLI supports Python 3.9
        now = now or _dt.datetime.now(_dt.timezone.utc)  # noqa: UP017
        sec = max(0, int((now - t).total_seconds()))
    except Exception:
        return str(ts)[:19]
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


def _title(s: Any) -> str:
    return str(s or "").replace("_", " ").strip().capitalize() or _DASH


def account_rows(d: apimod.Dashboard) -> list[tuple[str, Any]]:
    me = d.me.data or {}
    name = me.get("username") or _DASH
    email = me.get("email")
    who = f"{name}  ({email})" if email and email != name else name
    role = "Seller" if d.is_seller else "Buyer"
    if me.get("is_admin"):
        role += " · admin"
    rows: list[tuple[str, Any]] = [
        ("Name", who),
        ("Role", role),
        ("Status", ("status", "connected")),
        ("Email", ("status", "verified") if me.get("email_verified") else ("status", "unverified")),
    ]
    if d.twofa.ok:
        rows.append(("2FA", ("status", "on") if d.twofa.get("enabled") else ("dim", "off — petabyte.market/console#access")))
    if me.get("reputation") is not None:
        rows.append(("Reputation", f"{me.get('reputation')}/100"))
    rows.append(("Activity", f"{int(me.get('bookings') or 0)} bookings · {int(me.get('nodes') or 0)} nodes"))
    return rows


def wallet_rows(d: apimod.Dashboard) -> list[tuple[str, Any]]:
    if not d.wallet.ok:
        return [("Wallet", ("dim", f"unavailable ({d.wallet.error or 'no data'})"))]
    w = d.wallet.data or {}
    rows: list[tuple[str, Any]] = [("Available balance", ("money", money(w.get("balance"))))]
    earn = money(w.get("earnings"))
    detail = []
    if w.get("withdrawable") is not None:
        detail.append(f"withdrawable {money(w.get('withdrawable'))}")
    if w.get("clearing"):
        detail.append(f"clearing {money(w.get('clearing'))} · held {w.get('hold_hours', '?')}h")
    rows.append(("Earnings", earn + (f"   {' · '.join(detail)}" if detail else "")))
    if d.spend.ok:
        sp = d.spend.data or {}
        if float(sp.get("in_escrow") or 0) > 0:
            rows.append(("In escrow", money(sp.get("in_escrow"))))
        rows.append(("Spent to date", money(sp.get("spent_lifetime"))))
    m = d.usdc_method
    if m:
        dest = str(m.get("destination") or "")
        tail = dest[-4:] if dest else "????"
        rows.append(("USDC payout", f"…{tail}  ({'verified' if m.get('verified') else 'unverified'})"))
    elif d.payout_methods.ok:
        ms = d.payout_methods.get("methods") or []
        if ms:
            first = ms[0]
            rows.append(("Payout method", f"{str(first.get('kind', '?')).upper()} …{str(first.get('destination') or '')[-4:]}"
                                          f"  ({'verified' if first.get('verified') else 'unverified'})"))
        else:
            rows.append(("Payout method", ("dim", "none yet — add one under Earnings on the web")))
    rows.append(("Currency", "USD"))
    return rows


def _node_label(n: dict) -> str:
    gpu = n.get("gpu_model") or "CPU"
    units = n.get("units_total")
    price = n.get("price_per_hour")
    s = f"{gpu}"
    if units:
        s += f" × {units}"
    if price is not None:
        s += f" @ {money(price)}/hr"
    return s


def workflow_rows(d: apimod.Dashboard, st: agentmod.AgentState | None) -> list[tuple[str, Any]] | str:
    rows: list[tuple[str, Any]] = []
    busy = False
    if d.is_seller:
        online = d.nodes_online
        rows.append(("Role", "Seller"))
        if st is not None and st.installed:
            rows.append(("Agent", ("status", st.status_word)))
        elif d.seller.ok:
            rows.append(("Nodes online", f"{len(online)} of {len(d.nodes)}"))
        rentals = d.active_rentals
        local_task = (st.local or {}).get("current_task") if st else None
        if rentals:
            rows.append(("Status", ("status", "earning")))
            rows.append(("Active rentals", str(rentals)))
            busy = True
        elif online:
            rows.append(("Status", ("status", "listed") ))
            rows.append(("Waiting for", "buyers — your GPU is bookable"))
        elif d.nodes:
            rows.append(("Status", ("status", "offline")))
        if online:
            rows.append(("Current resource", ", ".join(_node_label(n) for n in online[:2])))
        if local_task:
            rows.append(("Current job", str(local_task)))
            rows.append(("Job status", ("status", str((st.local or {}).get("status") or "running"))))
            busy = True
        if d.seller_earnings.ok and d.seller_earnings.get("utilization") is not None:
            rows.append(("Utilization", pct(d.seller_earnings.get("utilization"))))
    vms = d.running_vms
    clusters = d.running_clusters
    if vms or clusters or not d.is_seller:
        if not d.is_seller:
            rows.append(("Role", "Buyer"))
        rows.append(("Rented by you" if d.is_seller else "Active rentals", str(len(vms))))
        if vms:
            v = vms[0]
            rows.append(("Current job", f"{v.get('template') or 'VM'}  ({v.get('vm_id')})"))
            rows.append(("Job status", ("status", str(v.get("status")))))
            if v.get("hours_left") is not None:
                rows.append(("Time left", f"{v.get('hours_left')} h"))
            busy = True
        if clusters:
            rows.append(("Clusters", f"{len(clusters)} running"))
            busy = True
        if d.spend.ok and float((d.spend.data or {}).get("burn_rate_per_hour") or 0) > 0:
            rows.append(("Spending", f"{money(d.spend.get('burn_rate_per_hour'))}/hr"))
    if not busy and not (d.is_seller and d.nodes_online):
        return "Nothing currently running."
    return rows


def agent_rows(d: apimod.Dashboard, st: agentmod.AgentState | None) -> list[tuple[str, Any]] | str | None:
    if st is None or (not st.installed and not d.is_seller):
        return None
    rows: list[tuple[str, Any]] = []
    if not st.installed:
        rows.append(("On this machine", ("dim", "no seller agent installed — petabyte --install-agent")))
    else:
        rows.append(("Installed", f"yes ({st.backend})" + (f" · v{st.version}" if st.version else "")))
        rows.append(("Running", ("status", "running") if st.running else ("status", "stopped")))
        conn = st.connected
        if conn is not None:
            rows.append(("Connected", ("status", "connected") if conn else ("status", "disconnected")))
        hb = (st.local or {}).get("last_heartbeat")
        if hb:
            rows.append(("Last heartbeat", _rel(hb) if str(hb)[:2].isdigit() else str(hb)))
        rows.append(("Active jobs", str(st.active_jobs)))
    nodes = d.nodes
    if nodes:
        online = [n for n in nodes if n.get("online")]
        if not st.installed or not (st.local or {}).get("last_heartbeat"):
            seen = [n.get("last_seen") for n in nodes if n.get("last_seen")]
            newest = max(seen, key=lambda s: (_parse_ts(s) or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)),  # noqa: UP017
                         default=None)
            if newest:
                rows.append(("Last heartbeat", _rel(newest)))
        rows.append(("Nodes", f"{len(online)} online of {len(nodes)}"))
        for n in nodes[:3]:
            rows.append(("Advertising", _node_label(n) + ("" if n.get("online") else "  (offline)")))
        tot = d.seller.get("totals") or {}
        if tot.get("earned_lifetime") is not None:
            rows.append(("Agent earnings", money(tot.get("earned_lifetime")) + "  (lifetime, realized)"))
        for b in (d.seller.get("blockers") or [])[:2]:
            rows.append(("Blocker", f"{b.get('issue')} — {b.get('fix')}"))
    elif d.is_seller and d.seller.ok:
        rows.append(("Nodes", ("dim", "none registered yet")))
    return rows or None


def expected_return_rows(d: apimod.Dashboard) -> list[tuple[str, Any]] | str | None:
    if d.is_seller:
        fc = next((p for p in d.forecasts.values() if p.ok), None)
        if fc is None:
            if d.nodes:
                return "Unavailable right now (the forecast endpoint did not answer)."
            return None
        f = fc.data or {}
        rows: list[tuple[str, Any]] = []
        if f.get("net_per_hour") is not None:
            rows.append(("Listed rate", f"{money(f.get('net_per_hour'))}/hr net  (your price minus the platform fee)"))
        ests = f.get("estimates") or []
        head = f.get("headline") or {}
        lo = head.get("low_daily_usd")
        hi = head.get("high_daily_usd")
        if lo is not None and hi is not None:
            rows.append(("Estimated / day", f"{money(lo)} – {money(hi)}"))
        months = [e.get("monthly_usd") for e in ests if e.get("monthly_usd") is not None]
        if months:
            rows.append(("Estimated / month", f"{money(min(months))} – {money(max(months))}"))
        rows.append(("Basis", ("dim", f.get("note") or "Estimate from your listed price at typical utilization — actual earnings depend on demand.")))
        return rows
    if d.spend.ok:
        sp = d.spend.data or {}
        burn = float(sp.get("burn_rate_per_hour") or 0)
        if burn <= 0:
            return None
        rows = [("Current spend", f"{money(burn)}/hr"),
                ("Projected / 24h", money(sp.get("projected_24h")))]
        rw = sp.get("hours_of_runway")
        rows.append(("Runway", f"{rw} h at this rate" if rw is not None else "unlimited (no spend)"))
        return rows
    return None


def system_rows(snap: sysinfo.Snapshot | None) -> list[tuple[str, Any]]:
    if snap is None:
        return [("System", ("dim", "unavailable"))]
    rows: list[tuple[str, Any]] = []
    cpu = pct(snap.cpu_pct) if snap.cpu_pct is not None else "Unavailable"
    if snap.cpu_count:
        cpu += f"  ({snap.cpu_count} cores)"
    rows.append(("CPU", cpu))
    rows.append(("RAM", f"{snap.ram[0]} / {snap.ram[1]} GB" if snap.ram else "Unavailable"))
    rows.append(("Disk", f"{snap.disk[0]} / {snap.disk[1]} GB used" if snap.disk else "Unavailable"))
    if snap.gpus:
        for g in snap.gpus[:2]:
            bits = [g.short_name]
            if g.util_pct is not None:
                bits.append(pct(g.util_pct))
            if g.vram_total_mb:
                used = f"{(g.vram_used_mb or 0) / 1024:.1f}" if g.vram_used_mb is not None else "?"
                bits.append(f"{used}/{g.vram_total_mb / 1024:.0f} GB")
            if g.temp_c is not None:
                bits.append(f"{g.temp_c:.0f}°C")
            rows.append(("GPU", " — ".join(bits[:2]) + ("  ·  " + " · ".join(bits[2:]) if len(bits) > 2 else "")))
    else:
        rows.append(("GPU", "Unavailable"))
    if snap.network:
        ok, why = snap.network
        rows.append(("Network", ("status", "connected") if ok else ("status", "disconnected")))
        if not ok:
            rows.append(("Network detail", ("dim", why)))
    rows.append(("OS", f"{snap.os} · Python {snap.python}"))
    return rows


def build_sections(d: apimod.Dashboard, st: agentmod.AgentState | None, snap: sysinfo.Snapshot | None
                   ) -> list[tuple[str, Any]]:
    sections: list[tuple[str, Any]] = [("Account", account_rows(d)), ("Wallet", wallet_rows(d)),
                                        ("Workflow", workflow_rows(d, st))]
    ar = agent_rows(d, st)
    if ar:
        sections.append(("Agent", ar))
    er = expected_return_rows(d)
    if er:
        sections.append(("Expected return", er))
    sections.append(("System", system_rows(snap)))
    return sections


def next_steps(d: apimod.Dashboard, st: agentmod.AgentState | None) -> list[tuple[str, str]]:
    """(caption, command) hints that fit the current state."""
    tips: list[tuple[str, str]] = []
    me = d.me.data or {}
    if d.is_seller:
        if st is not None and not st.installed and not d.nodes:
            tips.append(("Start earning on this machine:", "petabyte --install-agent"))
        elif st is not None and st.installed and not st.running:
            tips.append(("Your seller agent is installed but not running:", "petabyte --run-agent"))
        if d.payout_methods.ok and not (d.payout_methods.get("methods") or []):
            tips.append(("Add a payout method before you withdraw:", "https://petabyte.market/seller/payouts"))
    else:
        if float((d.wallet.data or {}).get("balance") or 0) <= 0:
            tips.append(("Add funds, then rent a GPU:", "petabyte deposit 20"))
        if not d.running_vms:
            tips.append(("See what's bookable right now:", "petabyte specs"))
    if not me.get("email_verified"):
        tips.append(("Verify your email to unlock payouts:", "https://petabyte.market/console#access"))
    return tips[:3]


def render(ui, d: apimod.Dashboard, st: agentmod.AgentState | None, snap: sysinfo.Snapshot | None,
           *, update=None) -> None:
    me = d.me.data or {}
    ui.dashboard("PETABYTE", f"Your dashboard · {me.get('username', '')}", build_sections(d, st, snap))
    for caption, cmd in next_steps(d, st):
        ui.command(cmd, caption=caption)
    if update is not None and getattr(update, "newer", False):
        ui.blank()
        ui.note(f"CLI update available: {update.installed} → {update.latest}   ({update.upgrade_command})")


def as_json(d: apimod.Dashboard, st: agentmod.AgentState | None, snap: sysinfo.Snapshot | None) -> dict[str, Any]:
    return {"me": d.me.data, "sections": {name: (rows if isinstance(rows, str) else
                                                 [[k, (v[1] if isinstance(v, tuple) else v)] for k, v in rows])
                                          for name, rows in build_sections(d, st, snap)},
            "dashboard": d.as_dict(), "agent": st.as_dict() if st else None,
            "system": snap.as_dict() if snap else None}
