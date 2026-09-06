"""agent_cmds — the seller-agent commands as a person experiences them:

    petabyte --install-agent   a guided, five-step wizard around the official installer
    petabyte --run-agent       preflight, start the service, show status, follow the log
    petabyte --kill-agent      warn about active jobs, stop gracefully, clean up, idempotent
    petabyte agent status      what the agent is doing right now
    petabyte agent logs        the live log

The mechanics live in agent.py (systemd / WSL / process backends); this module is the
conversation. Every privileged step is announced and confirmed; secrets never reach stdout.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from . import agent as A
from . import api as apimod
from . import errors, sysinfo, version_check
from .ui import money

Step = Callable[[str], None]


def _step(ui, n: int, total: int, text: str) -> None:
    ui.blank()
    ui.raw(f"Step {n}/{total}  {text}") if ui.mode != "rich" else ui.console.print(f"[bold]Step {n}/{total}[/]  {ui.escape(text)}")


def status_rows(st: A.AgentState, *, forecast: dict | None = None, snap: sysinfo.Snapshot | None = None
                ) -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    if st.version:
        rows.append(("Version", st.version))
    rows.append(("Status", ("status", st.status_word.upper() if st.running else st.status_word)))
    rows.append(("Manager", {"systemd": "systemd service", "wsl": f"systemd inside WSL ({A.WSL_DISTRO})",
                             "process": "foreground process"}.get(st.backend, st.backend)))
    if st.pids:
        rows.append(("PID", ", ".join(str(p) for p in st.pids)))
    if st.connected is not None:
        rows.append(("API", ("status", "connected") if st.connected else ("status", "disconnected")))
    loc = st.local or {}
    if loc.get("last_heartbeat"):
        rows.append(("Last heartbeat", str(loc.get("last_heartbeat"))))
    if loc.get("uptime"):
        rows.append(("Uptime", str(loc.get("uptime"))))
    rows.append(("Active jobs", str(st.active_jobs)))
    if loc.get("current_task"):
        rows.append(("Current job", str(loc.get("current_task"))))
    if loc.get("tasks_completed") is not None:
        rows.append(("Jobs done", f"{loc.get('tasks_completed')} completed · {loc.get('tasks_failed', 0)} failed"))
    if snap is not None:
        if snap.gpus:
            g = snap.gpus[0]
            rows.append(("GPU", g.short_name + (f" — {g.util_pct:.0f}%" if g.util_pct is not None else "")))
        if snap.cpu_pct is not None:
            rows.append(("CPU", f"{snap.cpu_pct:.0f}%"))
    if forecast and forecast.get("net_per_hour") is not None:
        rows.append(("Earnings", f"{money(forecast.get('net_per_hour'))}/hr net while rented (listed price)"))
    if st.stale_containers:
        rows.append(("Leftover containers", ", ".join(st.stale_containers[:4])))
    if st.detail:
        rows.append(("Note", ("dim", st.detail)))
    return rows


def render_status(ui, st: A.AgentState, **kw) -> None:
    ui.panel("Seller Agent", status_rows(st, **kw))


def _seller_forecast(cfg, client_factory) -> dict | None:
    """The backend's net $/hr for this seller's first online node (None when unavailable)."""
    try:
        with client_factory(cfg) as c:
            me = apimod.fetch_me(c)
            if not (me.get("role") == "seller" or int(me.get("nodes") or 0) > 0):
                return None
            dash = apimod._get(c, "/seller/dashboard")
            nodes = (dash.get("nodes") or []) if dash.ok else []
            pick = next((n for n in nodes if n.get("online")), nodes[0] if nodes else None)
            if not pick or pick.get("spec_id") is None:
                return None
            fc = apimod._get(c, f"/nodes/{int(pick['spec_id'])}/earnings_forecast")
            return fc.data if fc.ok else None
    except Exception:
        return None


# ------------------------------------------------------------------ status / logs
def cmd_status(ui, cfg, client_factory, *, json_mode: bool = False) -> int:
    st = A.detect()
    snap = sysinfo.snapshot(with_network=False, cpu_interval=0.15) if st.installed else None
    if json_mode:
        import json

        sys.stdout.write(json.dumps({"agent": st.as_dict(), "system": snap.as_dict() if snap else None}, default=str) + "\n")
        return 0
    ui.brand("Seller Agent")
    if not st.installed and not st.running:
        ui.info("No seller agent is installed on this machine.")
        ui.command("petabyte --install-agent", caption="Turn this machine into a seller node:")
        return 0
    render_status(ui, st, forecast=_seller_forecast(cfg, client_factory) if st.running else None, snap=snap)
    if not st.running:
        ui.command("petabyte --run-agent", caption="Start it:")
    return 0


def cmd_logs(ui, cfg, *, lines: int = 30) -> int:
    st = A.detect(with_local=False, with_docker=False)
    if not st.installed:
        ui.info("No seller agent is installed on this machine.")
        return 0
    be = A.backend()
    if isinstance(be, A.ProcessBackend):
        log = os.path.join(st.install_dir or "", "petabyte_agent.log")
        if os.path.isfile(log):
            ui.note(f"Showing {log} — Ctrl-C to stop")
            cmd = ["tail", "-n", str(lines), "-f", log] if sysinfo.which("tail") else None
            if cmd is None:
                with open(log, encoding="utf-8", errors="replace") as f:
                    for ln in f.readlines()[-lines:]:
                        ui.raw(ln.rstrip())
                return 0
        else:
            ui.info("No log file yet (the agent writes petabyte_agent.log in its folder once it runs).")
            return 0
    else:
        cmd = be.logs_command()
        if isinstance(be, A.SystemdBackend) and not sysinfo.is_admin():
            priv = A.privileged(cmd, interactive=ui.interactive)
            cmd = priv or cmd
    ui.note("Following the agent log — Ctrl-C to stop (the agent keeps running).")
    try:
        return int(subprocess.call(cmd))
    except KeyboardInterrupt:
        ui.blank()
        return 0
    except OSError as e:
        ui.error("Could not open the agent log.", detail=str(e))
        return 1


# ------------------------------------------------------------------ install
def _system_checks(ui, api_url: str) -> tuple[bool, list[sysinfo.Gpu]]:
    ok_all = True
    if version_check.python_supported():
        ui.ok(f"Python {version_check.python_version()}")
    else:
        ui.fail(version_check.python_support_message())
        ok_all = False
    ui.ok(sysinfo.os_summary())
    if sysinfo.is_macos():
        ui.fail("The seller agent runs on Linux, or on Windows through WSL2 — not on macOS yet.")
        ok_all = False
    d = sysinfo.docker_status()
    if d.installed and d.running:
        ui.ok(f"Docker {d.version or ''}".strip())
    elif d.installed:
        ui.warn("Docker is installed but not running — the installer will start it; jobs need it.")
    elif sysinfo.is_windows():
        ui.info("Docker will run inside WSL2 — the installer sets it up there.")
    else:
        ui.warn("Docker is not installed — the official installer will install it (get.docker.com).")
    gpus = sysinfo.gpus()
    if gpus:
        ui.ok("GPU detected: " + ", ".join(g.short_name for g in gpus[:2]))
        if sysinfo.is_linux() and d.installed and sysinfo.nvidia_container_toolkit() is False:
            ui.info("NVIDIA container toolkit missing — the installer adds it.")
    else:
        ui.warn("No NVIDIA GPU detected — you can still sell CPU time.")
    net, why = sysinfo.network_ok(api_url.rstrip("/") + "/healthz")
    if net:
        ui.ok(f"Network: {api_url} reachable")
    else:
        ui.fail(f"Cannot reach {api_url} ({why})")
        ok_all = False
    return ok_all, gpus


def cmd_install(ui, cfg, client_factory, *, login: Callable[[], None] | None = None, sell: str | None = None,
                price: float | None = None, yes: bool = False, no_egress_lockdown: bool = False,
                dry_run: bool = False, reinstall: bool = False) -> int:
    total = 5
    api_url = str(cfg.get("api_url") or "")
    ui.brand("Seller Agent")
    ui.line("Let's get your machine earning.")

    # ---- 1. system ---------------------------------------------------------------
    _step(ui, 1, total, "Checking system…")
    ok_sys, gpus = _system_checks(ui, api_url)
    if not ok_sys:
        ui.blank()
        ui.error("This machine isn't ready for the seller agent yet.", fix="Fix the items marked ✗ above, then run:",
                 run="petabyte --install-agent")
        return 1
    existing = A.detect(with_local=True, with_docker=False)
    if existing.installed and not reinstall:
        ui.blank()
        ui.ok("A seller agent is already installed on this machine.")
        render_status(ui, existing)
        if not (yes or ui.confirm("Reinstall / update it anyway?", default=False)):
            ui.command("petabyte --run-agent" if not existing.running else "petabyte --me",
                       caption="Nothing to do. " + ("Start it with:" if not existing.running else "See it in your dashboard:"))
            return 0

    # ---- 2. authentication -------------------------------------------------------
    _step(ui, 2, total, "Authentication")
    me: dict = {}
    try:
        with client_factory(cfg) as c:
            me = apimod.fetch_me(c, api_url).data or {}
    except errors.NotSignedIn:
        if login is not None and ui.interactive:
            ui.info("You're not signed in yet — opening the browser sign-in.")
            login()
            with client_factory(cfg) as c:
                me = apimod.fetch_me(c, api_url).data or {}
        else:
            raise
    ui.ok(f"Petabyte account detected: {me.get('username')}")
    if me.get("role") != "seller":
        ui.info("Your account is set up as a buyer.")
        if yes or ui.confirm("Switch it to a seller account so this machine can be listed?", default=True):
            with client_factory(cfg) as c:
                r = c.post("/change_role", json={"role": "seller"})
                if r.status_code != 200:
                    raise errors.from_response(r, "switch your account to seller", api_url)
            ui.ok("Account switched to seller")
        else:
            ui.error("A seller account is required to list hardware.", run="petabyte --install-agent")
            return 1

    # ---- 3. configuration --------------------------------------------------------
    _step(ui, 3, total, "Agent configuration")
    if sell is None:
        ui.line("What would you like to sell?")
        options = (["GPU  (recommended — rent this GPU by the hour)"] if gpus else []) + \
                  ["CPU only  (no GPU jobs)", "All available resources  (GPU + CPU; disk and idle mining can be switched on in the console)"]
        pick = ui.choose("Choice", options, default=1)
        chosen = options[pick - 1]
        sell = "gpu" if chosen.startswith("GPU") else "cpu" if chosen.startswith("CPU") else "all"
    if sell == "gpu" and not gpus:
        ui.warn("No GPU was detected; listing CPU only.")
        sell = "cpu"
    if price is None and ui.interactive and not yes:
        raw = ui.ask("Price per hour in USD (leave empty for automatic pricing)", default="")
        if raw:
            try:
                price = float(raw)
                if not (price > 0) or price == float("inf"):   # rejects -5, inf and nan
                    raise ValueError(raw)
            except ValueError:
                ui.warn("Not a positive number — using automatic pricing.")
                price = None
    plan = A.InstallPlan(api_url=api_url, sell=sell, price_per_hour=price, lockdown_egress=not no_egress_lockdown,
                         installer_name="install.ps1" if sysinfo.is_windows() else "install.sh")
    ui.kv([("Sell", {"gpu": "GPU", "cpu": "CPU only", "all": "all available resources"}[sell]),
           ("Price", f"{money(price)}/hr" if price is not None else "automatic (market-suggested)"),
           ("Egress lockdown", "on" if plan.lockdown_egress else "off"),
           ("API", api_url)], title="Configuration")
    ui.ok("Configuration valid")

    # ---- 4. install ----------------------------------------------------------------
    _step(ui, 4, total, "Installing agent…")
    ui.line("The official Petabyte installer will now run with administrator rights. It will:")
    for s in A.installer_summary(plan):
        ui.line(f"  {ui.icon('bullet')} {s}")
    if dry_run:
        ui.blank()
        ui.info("Dry run — nothing was installed.")
        return 0
    if sysinfo.is_windows() and not sysinfo.is_admin():
        ui.error("The Windows installer needs an Administrator PowerShell.",
                 fix="Open PowerShell as Administrator and run:", run="petabyte --install-agent")
        return 1
    if not ui.interactive and not yes:
        ui.error("Refusing to run a privileged installer from a non-interactive shell.",
                 fix="Re-run with --yes to confirm, or run it from a terminal:", run="petabyte --install-agent --yes")
        return 1
    if not (yes or ui.confirm("Run the installer now?", default=True)):
        ui.info("Cancelled — nothing was installed.")
        return 0
    with client_factory(cfg) as c:
        try:
            node_key = A.mint_node_key(c)
        except PermissionError as e:
            ui.error(str(e), run=errors.LOGIN)
            return 1
        path = A.fetch_installer(c, plan.installer_name)
    ui.ok("Node key created (kept private)")
    ui.ok(f"Installer downloaded from {api_url}")
    ui.note("The installer's own output follows; this can take a few minutes.")
    ui.blank()
    try:
        rc = A.run_installer(path, plan, node_key, interactive=ui.interactive)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        node_key = ""
    if rc != 0:
        ui.blank()
        ui.error(f"The installer exited with code {rc}.",
                 fix=["Read the output above for the failing step.", "Then run the diagnostics and try again:"],
                 run=["petabyte doctor", "petabyte --install-agent"])
        return 1

    # ---- 5. verify -----------------------------------------------------------------
    _step(ui, 5, total, "Verifying")
    st = None
    for _ in range(10):
        st = A.detect(with_docker=False)
        if st.installed and st.running:
            break
        time.sleep(1)
    if st is None or not st.installed:
        ui.error("The installer finished but the agent was not found.", run="petabyte doctor")
        return 1
    ui.ok("Seller agent installed")
    if st.running:
        ui.ok("Agent is running")
        ui.command("petabyte --me", caption="Watch it come online and start earning:")
    else:
        ui.command("petabyte --run-agent", caption="Start it with:")
    return 0


# ------------------------------------------------------------------ run
def _wait_for(pred: Callable[[], bool], timeout: float, interval: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def cmd_run(ui, cfg, client_factory, *, follow: bool | None = None, foreground: bool = False) -> int:
    api_url = str(cfg.get("api_url") or "")
    ui.brand("Seller Agent")
    st = A.detect()
    if not st.installed:
        ui.error("The seller agent isn't installed on this machine.", fix="Install it first (takes a few minutes):",
                 run="petabyte --install-agent")
        return 1
    ui.line("Starting Petabyte Seller Agent")
    ui.blank()
    ready = True
    if st.env_present is False:
        ui.fail(f"Configuration missing ({A.ENV_FILE})")
        ready = False
    else:
        ui.ok("Configuration valid" if st.env_present else "Configuration present (root-only file)")
    try:
        with client_factory(cfg) as c:
            me = apimod.fetch_me(c, api_url).data or {}
        ui.ok(f"Account authenticated ({me.get('username')})")
    except errors.NotSignedIn:
        ui.warn("You're not signed in on this CLI (the agent uses its own node key, so it can still start).")
    except errors.CliError as e:
        ui.fail(e.title)
        ready = False
    snap = sysinfo.snapshot(api_url=api_url, with_docker=True, cpu_interval=0.15)
    if snap.gpus:
        ui.ok("GPU detected: " + ", ".join(g.short_name for g in snap.gpus[:2]))
    else:
        ui.warn("No GPU detected (CPU-only node)")
    if snap.docker and snap.docker.installed and snap.docker.running:
        ui.ok("Docker running")
    elif st.backend != "wsl":
        ui.warn("Docker is not running — the agent will start, but jobs need Docker.")
    if snap.network and snap.network[0]:
        ui.ok("Network connected")
    else:
        ui.fail(f"Cannot reach {api_url}")
        ready = False
    if not ready:
        ui.blank()
        ui.error("Not starting the agent until the items above are fixed.", run="petabyte doctor")
        return 1
    if st.running:
        ui.blank()
        ui.info("The agent is already running.")
        render_status(ui, st, forecast=_seller_forecast(cfg, client_factory), snap=snap)
        if follow:
            return cmd_logs(ui, cfg)
        return 0
    if st.stale_containers:
        ui.warn(f"Leftover containers from a previous run: {', '.join(st.stale_containers[:4])}")

    ui.blank()
    be = A.backend()
    if isinstance(be, A.ProcessBackend) or foreground:
        # no service manager: run attached; Ctrl-C stops the agent (its own handler)
        install_dir = st.install_dir or ""
        if not os.path.isdir(install_dir):
            ui.error("Cannot run the agent in the foreground here.",
                     reason=f"no local agent directory ({install_dir or 'unknown'}).",
                     fix="Install the agent on this machine first, or start the service instead.",
                     run=["petabyte --install-agent", "petabyte doctor"])
            return 1
        env = dict(os.environ)
        for p in (A.ENV_FILE, os.path.expanduser("~/.petabyte/agent.env"), os.path.join(install_dir, ".env")):
            env.update(A.parse_env_file(p))
        cmd = A.ProcessBackend().foreground_command(install_dir)
        ui.info("No service manager here — running the agent in the foreground. Ctrl-C stops it.")
        ui.blank()
        try:
            return int(subprocess.call(cmd, cwd=install_dir, env=env))
        except KeyboardInterrupt:
            ui.blank()
            ui.ok("Agent stopped")
            return 0
        except OSError as e:
            ui.error("Could not launch the agent.", detail=str(e), run="petabyte doctor")
            return 1
    okk, msg = be.control("start", interactive=ui.interactive)
    if not okk:
        ui.error("Could not start the agent service.", reason=msg or None,
                 fix="Start it by hand and check the log:", run=[f"sudo systemctl start {A.SERVICE}", "petabyte agent logs"])
        return 1
    with ui.status("Agent is starting…"):
        up = _wait_for(lambda: A.detect(with_docker=False).running, A.START_WAIT_S)
    if not up:
        ui.error("The service was started but is not reporting as running.", run="petabyte agent logs")
        return 1
    st = A.detect()
    with ui.status("Waiting for the agent to connect…"):
        _wait_for(lambda: bool(A.local_status(timeout=0.8)), 10, interval=1.0)
        st = A.detect()
    ui.blank()
    ui.raw(f"{ui.icon('on')} {st.status_word.upper()}") if ui.mode != "rich" else ui.console.print(
        f"[bold color(42)]{ui.icon('on')} {st.status_word.upper()}[/]")
    ui.blank()
    render_status(ui, st, forecast=_seller_forecast(cfg, client_factory), snap=snap)
    if follow is None:
        follow = ui.interactive
    if follow:
        ui.blank()
        rc = cmd_logs(ui, cfg)
        ui.info("Detached — the agent keeps running. Stop it with: petabyte --kill-agent")
        return rc
    ui.command("petabyte agent logs", caption="Follow the log with:")
    return 0


# ------------------------------------------------------------------ kill
def _remove_containers(ui, names: list[str]) -> None:
    docker = sysinfo.which("docker")
    if not docker or not names:
        return
    rc, _, err = A.run_cmd([docker, "rm", "-f", *names], timeout=60)
    if rc == 0:
        ui.ok(f"Removed {len(names)} leftover container(s)")
    else:
        ui.warn(f"Could not remove containers: {err.strip()[-160:] or rc}")


def cmd_kill(ui, cfg, *, yes: bool = False, force: bool = False, timeout: float = A.STOP_TIMEOUT_S) -> int:
    ui.brand("Seller Agent")
    st = A.detect()
    if not st.installed and not st.running and not st.stale_containers:
        ui.info("No seller agent is installed on this machine — nothing to stop.")
        return 0
    ui.kv([("Status", ("status", st.status_word.upper() if st.running else st.status_word)),
           ("Active jobs", str(st.active_jobs))], title="Agent")
    if not st.running:
        ui.blank()
        ui.ok("Agent is not running — nothing to stop.")
        if st.stale_containers:
            ui.warn(f"Leftover containers from a previous run: {', '.join(st.stale_containers[:4])}")
            if yes or ui.confirm("Remove them now?", default=False):
                _remove_containers(ui, st.stale_containers)
        return 0
    if st.active_jobs and not force:
        ui.blank()
        ui.warn(f"The agent currently has {st.active_jobs} active job{'s' if st.active_jobs != 1 else ''}.")
        ui.line("Stopping it may interrupt the job (the buyer is refunded for unused time).")
        if not (yes or ui.confirm("Continue?", default=False)):
            ui.info("Left running.")
            return 0
    ui.blank()
    ui.step("Stopping agent…")
    be = A.backend()
    problems: list[str] = []
    if st.backend in ("systemd", "wsl"):
        okk, msg = be.control("stop", interactive=ui.interactive)
        if not okk:
            problems.append(msg or "systemctl stop failed")
    stray = [p for p in st.pids] if st.backend == "process" else [p for p in A.ProcessBackend.find_pids()]
    if stray:
        okk, msg = A.ProcessBackend().stop(stray, timeout=min(timeout, 30))
        if not okk:
            problems.append(msg or "could not stop the foreground agent process")
        elif msg:
            ui.warn(msg)
    with ui.status("Waiting for the agent to exit…"):
        gone = _wait_for(lambda: not A.detect(with_docker=False).running, timeout, interval=1.0)
    if problems or not gone:
        ui.error("The agent did not stop cleanly.", reason="; ".join(problems) or "still running after the timeout",
                 fix="Stop it by hand:", run=[f"sudo systemctl stop {A.SERVICE}", "petabyte agent status"])
        return 1
    ui.ok("Agent stopped")
    after = A.detect(with_local=False)
    if after.stale_containers:
        ui.warn(f"Job containers still present: {', '.join(after.stale_containers[:4])}")
        if force or yes or ui.confirm("Remove them now?", default=False):
            _remove_containers(ui, after.stale_containers)
    if st.backend in ("systemd", "wsl") and st.enabled:
        ui.note(f"The service stays enabled and returns after a reboot; disable with: sudo systemctl disable {A.SERVICE}")
    return 0
