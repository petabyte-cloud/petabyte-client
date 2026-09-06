"""doctor — `petabyte doctor`: the first thing to run when something is off.

    ⚡ Petabyte Doctor

    Python        ✔ 3.12.3
    Network       ✔ petabyte.market reachable
    API           ✔ healthy
    Account       ✔ signed in as bader
    Docker        ✔ 27.1.1
    GPU           ✔ RTX 4090
    Agent         ✔ installed (systemd) v1.0.0
    Agent         ● offline

    Result: 1 issue found

    The seller agent is installed but not running.
      petabyte --run-agent

Each check has a state (ok / warn / fail / info), a value, and — when it is not ok — the fix
and the exact command to run. Exit code 1 when anything FAILS, so scripts can gate on it.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from . import agent as agentmod
from . import api as apimod
from . import errors, sysinfo, version_check

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


@dataclass
class Check:
    name: str
    state: str
    value: str
    fix: str | None = None
    run: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "value": self.value, "fix": self.fix, "run": self.run}


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def issues(self) -> list[Check]:
        return [c for c in self.checks if c.state in (FAIL, WARN)]

    @property
    def healthy(self) -> bool:
        return not any(c.state == FAIL for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {"product": "petabyte-cli", "version": version_check.installed_version(),
                "healthy": self.healthy, "issues": len(self.issues), "checks": [c.as_dict() for c in self.checks]}


def _host(api_url: str) -> str:
    try:
        return urlparse(api_url).hostname or api_url
    except Exception:
        return api_url


def _config_perms_check(config_path: str) -> Check | None:
    if sysinfo.is_windows() or not os.path.isfile(config_path):
        return None
    try:
        mode = stat.S_IMODE(os.stat(config_path).st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return Check("Config", WARN, f"{config_path} is readable by other users (mode {mode:o})",
                     fix="It holds your sign-in token. Make it private:", run=f"chmod 600 {config_path}")
    return Check("Config", OK, f"{config_path} is private")


def run_checks(*, cfg: dict, client_factory, config_path: str, agent_state: agentmod.AgentState | None = None,
               snap: sysinfo.Snapshot | None = None, update: version_check.UpdateInfo | None = None,
               with_agent: bool = True) -> Report:
    rep = Report()
    api_url = str(cfg.get("api_url") or "")
    host = _host(api_url)

    # ---- runtime -----------------------------------------------------------------
    rep.checks.append(Check("Python", OK if version_check.python_supported() else FAIL, version_check.python_version(),
                            fix=None if version_check.python_supported() else version_check.python_support_message()))
    if update is not None and update.newer:
        rep.checks.append(Check("CLI", WARN, f"{update.installed} (latest {update.latest})",
                                fix="A newer CLI is available:", run=update.upgrade_command))
    else:
        rep.checks.append(Check("CLI", OK, version_check.installed_version()))

    # ---- network / api -----------------------------------------------------------------
    net_ok, why = sysinfo.network_ok(api_url.rstrip("/") + "/healthz") if api_url else (False, "no API URL")
    rep.checks.append(Check("Network", OK if net_ok else FAIL, f"{host} {'reachable' if net_ok else 'unreachable'} ({why})",
                            fix=None if net_ok else "Check your internet connection, VPN or proxy, and PETABYTE_API_URL."))
    me: dict | None = None
    if net_ok:
        try:
            with client_factory(cfg) as c:
                h = apimod.api_health(c)
                rep.checks.append(Check("API", OK if h.ok else FAIL, "healthy" if h.ok else f"unhealthy ({h.error})",
                                        fix=None if h.ok else "The service is having trouble — try again in a minute."))
                try:
                    me = apimod.fetch_me(c, api_url).data
                    if not isinstance(me, dict):   # a null/list body would raise on .get() below
                        me = None
                        rep.checks.append(Check("Account", FAIL, "the API returned an unrecognisable profile",
                                                fix="This is a server-side problem — try again, and quote the "
                                                    "request id to support if it persists."))
                    else:
                        rep.checks.append(Check("Account", OK, f"signed in as {me.get('username')} "
                                                              f"({'seller' if apimod.is_seller_payload(me) else 'buyer'})"))
                except errors.NotSignedIn:
                    rep.checks.append(Check("Account", FAIL, "not signed in", fix="Sign in with your browser:", run=errors.LOGIN))
                except errors.CliError as e:
                    rep.checks.append(Check("Account", FAIL, e.title, fix=e.reason, run=e.run if isinstance(e.run, str) else None))
        except Exception as e:
            rep.checks.append(Check("API", FAIL, f"error ({type(e).__name__})", fix="Run again with --verbose for details."))
    else:
        rep.checks.append(Check("API", INFO, "skipped (no network)"))
        rep.checks.append(Check("Account", INFO, "skipped (no network)"))
    if me is not None:
        if not me.get("email_verified"):
            rep.checks.append(Check("Email", WARN, "not verified", fix="Payouts need a verified email — confirm it under Settings on the web."))
        else:
            rep.checks.append(Check("Email", OK, "verified"))

    # ---- local machine / seller -----------------------------------------------------------
    is_seller = apimod.is_seller_payload(me)
    st = agent_state if agent_state is not None else (agentmod.detect() if with_agent else None)
    seller_ctx = is_seller or bool(st and st.installed)
    d = snap.docker if (snap and snap.docker) else sysinfo.docker_status()
    if d.installed and d.running:
        rep.checks.append(Check("Docker", OK, d.version or "running"))
    elif d.installed:
        rep.checks.append(Check("Docker", WARN if seller_ctx else INFO, "installed but the daemon is not running",
                                fix="Start Docker Desktop / the docker service; seller jobs run in containers." if seller_ctx else None))
    else:
        rep.checks.append(Check("Docker", WARN if seller_ctx else INFO, "not installed",
                                fix="Seller agents need Docker to sandbox buyer jobs. The installer sets it up on Linux:" if seller_ctx else None,
                                run="petabyte --install-agent" if seller_ctx and not (st and st.installed) else None))
    gpus = snap.gpus if snap is not None else sysinfo.gpus()
    if gpus:
        rep.checks.append(Check("GPU", OK, ", ".join(g.short_name for g in gpus[:2])))
        if d.installed and d.running:
            tk = sysinfo.nvidia_container_toolkit()
            if tk is False:
                rep.checks.append(Check("NVIDIA toolkit", WARN if seller_ctx else INFO, "Docker has no nvidia runtime",
                                        fix="Install the NVIDIA Container Toolkit so jobs can use the GPU (the Linux installer does this)."))
            elif tk:
                rep.checks.append(Check("NVIDIA toolkit", OK, "nvidia runtime available"))
    else:
        rep.checks.append(Check("GPU", INFO, "none detected (CPU-only listing is still possible)"))

    if st is not None:
        if not st.installed:
            rep.checks.append(Check("Agent", WARN if is_seller else INFO, "not installed on this machine",
                                    fix="Turn this machine into a seller node:" if is_seller else None,
                                    run="petabyte --install-agent" if is_seller else None))
        else:
            rep.checks.append(Check("Agent", OK, f"installed ({st.backend})" + (f" v{st.version}" if st.version else "")))
            if st.running:
                conn = st.connected
                if conn is False:
                    rep.checks.append(Check("Agent", WARN, "running but not connected to the API",
                                            fix="Check the agent log for the reason:", run="petabyte agent logs"))
                else:
                    rep.checks.append(Check("Agent", OK, f"online · {st.active_jobs} active job(s)"))
            else:
                rep.checks.append(Check("Agent", FAIL, "offline" + (f" ({st.service_state})" if st.service_state not in (None, "inactive", "unknown") else ""),
                                        fix="The seller agent is installed but not running.", run="petabyte --run-agent"))
            if st.stale_containers:
                rep.checks.append(Check("Containers", WARN, f"{len(st.stale_containers)} leftover job container(s)",
                                        fix="They belong to a stopped agent; clean them up with:", run="petabyte --kill-agent"))
    cp = _config_perms_check(config_path)
    if cp:
        rep.checks.append(cp)
    return rep


def render(ui, rep: Report) -> None:
    ui.brand("Doctor")
    width = max(len(c.name) for c in rep.checks) if rep.checks else 8
    for c in rep.checks:
        label = f"{c.name:<{width}}"
        if c.state == OK:
            ui.ok(f"{label}  {c.value}")
        elif c.state == WARN:
            ui.warn(f"{label}  {c.value}")
        elif c.state == FAIL:
            ui.fail(f"{label}  {c.value}")
        else:
            ui.info(f"{label}  {c.value}")
    ui.blank()
    n = len(rep.issues)
    if n == 0:
        ui.ok("Result: everything looks good")
        return
    ui.line(f"Result: {n} issue{'s' if n != 1 else ''} found")
    for c in rep.issues:
        ui.blank()
        ui.line(c.fix or f"{c.name}: {c.value}")
        if c.run:
            ui.command(c.run)
