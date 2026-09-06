"""agent — install, start, stop and inspect the Petabyte seller agent through its EXISTING runtime.

The seller agent is not a new thing this module invents; it is what `lumaris_agent/install.sh`
sets up today:

  Linux    a root-installed systemd service `petabyte-agent` (/opt/petabyte-agent, venv,
           EnvironmentFile /etc/petabyte/agent.env, Restart=always);
  Windows  the SAME service inside the Ubuntu-24.04 WSL2 distro that install.ps1 bootstraps,
           driven with `wsl.exe -d <distro> -u root -- systemctl …` (exactly what manage.ps1 does);
  dev      a source checkout run in the foreground (`python main.py`) — used only when there is
           no service manager at all (a laptop without systemd, macOS).

So `--run-agent` / `--kill-agent` are `systemctl start|stop` with the right preflight, the
right words and the right warnings — never a second process manager, never a PID file the
agent doesn't know about. Status comes from three real sources: the service manager, the
agent's own local status endpoint (http://127.0.0.1:5000/api/status, see lumaris_agent/ui.py),
and the Docker containers it names `pb-task-*` / `pb-<template>-*` / `petabyte-vm-*`.

Secrets: the node API key minted for the agent is handed to the installer through the
subprocess ENVIRONMENT (never argv, never printed, never logged) and lives only in the
root-only 0600 env file the installer writes.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import sysinfo

INSTALL_DIR = "/opt/petabyte-agent"
ENV_FILE = "/etc/petabyte/agent.env"
SERVICE = "petabyte-agent"
UNIT_FILE = f"/etc/systemd/system/{SERVICE}.service"
WSL_DISTRO = os.getenv("PETABYTE_WSL_DISTRO", "Ubuntu-24.04")
LOCAL_STATUS_URL = os.getenv("PETABYTE_AGENT_STATUS_URL", "http://127.0.0.1:5000/api/status")
# ANCHORED match on the exact container names the agent creates (lumaris_agent/task_fetcher.py
# and vm.py). A bare "pb-" PREFIX matched anything merely starting with it -- pb-postgres,
# pb-redis, whatever else a seller happens to run on the same box -- and those names flow into
# stale_containers and then `docker rm -f` under --kill-agent, which --yes runs with no prompt.
# Deleting a seller's database because it shares three characters with our naming scheme is not a
# risk worth carrying for a cleanup convenience. Every alternative below requires the trailing id
# segment the agent always appends, and the template form deliberately excludes "-" so it cannot
# stretch across an unrelated name. Under-matching here just leaves a stale container behind;
# over-matching destroys someone's data.
JOB_CONTAINER_RE = re.compile(
    r"^(?:"
    r"pb-task-[0-9a-f]{6,}"                # task_fetcher: "pb-task-" + uuid4().hex[:12]
    r"|pb-run-.+-[0-9a-f]{6,}"             # task_fetcher: f"pb-run-{tid}-{uuid4().hex[:6]}"
    r"|pb-[A-Za-z0-9._]+-[0-9a-f]{8,}"     # task_fetcher: f"pb-{template}-{uuid4().hex[:8]}"
    r"|petabyte-vm-.+"                     # vm.py: f"petabyte-vm-{vm_id}"
    r")$"
)
IDLE_MINER_CONTAINER = "petabyte-idle-miner"
STOP_TIMEOUT_S = 90
START_WAIT_S = 25
NODE_KEY_SCOPES = "node,jobs"
NODE_KEY_DAYS = 90

# Test seams: everything that touches the machine goes through these module attributes.
run_cmd = sysinfo.run


def _http_get_json(url: str, timeout: float = 1.5) -> dict | None:
    try:
        import httpx

        r = httpx.get(url, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def local_status(timeout: float = 1.5) -> dict | None:
    """The agent's own view of itself (lumaris_agent/ui.py agent_status), or None when no
    agent is listening locally."""
    d = _http_get_json(LOCAL_STATUS_URL, timeout=timeout)
    return d if isinstance(d, dict) else None


def job_containers() -> tuple[list[str], list[str]]:
    """(job containers, other petabyte containers) currently present in Docker."""
    docker = sysinfo.which("docker")
    if not docker:
        return [], []
    rc, out, _ = run_cmd([docker, "ps", "--format", "{{.Names}}"], timeout=6)
    if rc != 0:
        return [], []
    jobs, other = [], []
    for name in (n.strip() for n in out.splitlines() if n.strip()):
        if name == IDLE_MINER_CONTAINER:
            other.append(name)
        elif JOB_CONTAINER_RE.match(name):
            jobs.append(name)
    return jobs, other


# ------------------------------------------------------------------ state
@dataclass
class AgentState:
    backend: str = "none"                 # systemd | wsl | process | none
    installed: bool = False
    running: bool = False
    service_state: str | None = None      # active | inactive | failed | activating | unknown
    enabled: bool | None = None
    pids: list[int] = field(default_factory=list)
    version: str | None = None
    local: dict | None = None
    job_containers: list[str] = field(default_factory=list)
    other_containers: list[str] = field(default_factory=list)
    install_dir: str | None = None
    env_present: bool | None = None
    detail: str = ""

    @property
    def active_jobs(self) -> int:
        loc = self.local or {}
        n = 0
        task = loc.get("current_task")
        if task and str(loc.get("status", "")).lower() not in ("idle", "offline", "error", ""):
            n = 1
        return max(n, len(self.job_containers))

    @property
    def stale_containers(self) -> list[str]:
        """Job/miner containers left behind while no agent is running."""
        return [] if self.running else (self.job_containers + self.other_containers)

    @property
    def connected(self) -> bool | None:
        loc = self.local
        if not loc:
            return None
        v = loc.get("api_connected")
        return bool(v) if v is not None else None

    @property
    def status_word(self) -> str:
        if not self.installed:
            return "not installed"
        if not self.running:
            return "offline"
        if self.connected is False:
            return "connecting"
        if self.active_jobs:
            return "earning"
        return "online"

    def as_dict(self) -> dict[str, Any]:
        return {"backend": self.backend, "installed": self.installed, "running": self.running,
                "service_state": self.service_state, "enabled": self.enabled, "pids": self.pids,
                "version": self.version, "status": self.status_word, "active_jobs": self.active_jobs,
                "connected": self.connected, "job_containers": self.job_containers,
                "stale_containers": self.stale_containers, "install_dir": self.install_dir,
                "env_present": self.env_present, "local": self.local, "detail": self.detail}


def _read_version(install_dir: str | None) -> str | None:
    if not install_dir:
        return None
    for name, pat in (("version.py", r'VERSION\s*=\s*["\']([^"\']+)["\']'),
                      ("setup.py", r'version\s*=\s*["\']([^"\']+)["\']')):
        try:
            with open(os.path.join(install_dir, name), encoding="utf-8") as f:
                m = re.search(pat, f.read())
                if m:
                    return m.group(1)
        except OSError:
            continue
    return None


def _env_presence(path: str | None = None) -> bool | None:
    """True/False when we can tell; None when the directory is root-only (normal for a
    non-root user — the file exists but we may not list /etc/petabyte)."""
    path = path or ENV_FILE
    try:
        if os.path.isfile(path):
            return True
        d = os.path.dirname(path)
        if os.path.isdir(d) and os.access(d, os.R_OK | os.X_OK):
            return False
        return None if os.path.exists(d) else False
    except OSError:
        return None


# ------------------------------------------------------------------ privilege helper
def privileged(cmd: list[str], *, interactive: bool) -> list[str] | None:
    """Prefix `cmd` with sudo when needed. Returns None when we are not root and can't ask
    for a password (non-interactive shell without passwordless sudo)."""
    if sysinfo.is_admin():
        return cmd
    sudo = sysinfo.which("sudo")
    if not sudo:
        return None
    if interactive:
        return [sudo] + cmd
    rc, _, _ = run_cmd([sudo, "-n", "true"], timeout=5)
    return [sudo, "-n"] + cmd if rc == 0 else None


# ------------------------------------------------------------------ backends
class SystemdBackend:
    name = "systemd"

    @staticmethod
    def available() -> bool:
        return sysinfo.is_linux() and bool(sysinfo.which("systemctl")) and os.path.isdir("/run/systemd/system")

    def state(self) -> AgentState:
        st = AgentState(backend=self.name, install_dir=INSTALL_DIR)
        st.installed = os.path.isfile(os.path.join(INSTALL_DIR, "main.py")) or os.path.isfile(UNIT_FILE)
        st.version = _read_version(INSTALL_DIR)
        st.env_present = _env_presence()
        rc, out, _ = run_cmd(["systemctl", "is-active", SERVICE])
        st.service_state = (out.strip() or "unknown") if rc >= 0 else "unknown"
        st.running = st.service_state in ("active", "activating", "reloading")
        rc, out, _ = run_cmd(["systemctl", "is-enabled", SERVICE])
        st.enabled = (out.strip() == "enabled") if rc >= 0 and out.strip() else None
        rc, out, _ = run_cmd(["systemctl", "show", "-p", "MainPID", "--value", SERVICE])
        try:
            pid = int(out.strip())
            if pid > 0:
                st.pids = [pid]
        except ValueError:
            pass
        if st.service_state == "failed":
            st.detail = "service is in a failed state (journalctl -u petabyte-agent -n 50)"
        return st

    def control(self, verb: str, *, interactive: bool) -> tuple[bool, str]:
        cmd = privileged(["systemctl", verb, SERVICE], interactive=interactive)
        if cmd is None:
            return False, f"needs root: run  sudo systemctl {verb} {SERVICE}"
        try:
            p = subprocess.run(cmd, text=True, capture_output=not interactive, timeout=STOP_TIMEOUT_S + 30, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"{type(e).__name__}: {e}"
        if p.returncode != 0:
            return False, ((p.stderr or "") if not interactive else "").strip()[-300:] or f"systemctl {verb} failed ({p.returncode})"
        return True, ""

    def logs_command(self) -> list[str]:
        return ["journalctl", "-u", SERVICE, "-f", "-n", "30", "--no-pager"]


class WslBackend:
    """Windows: the agent lives inside the WSL2 distro install.ps1 created."""
    name = "wsl"

    @staticmethod
    def available() -> bool:
        return sysinfo.is_windows() and bool(sysinfo.which("wsl.exe"))

    @staticmethod
    def _wsl(cmd: list[str], timeout: float = 20) -> tuple[int, str, str]:
        return run_cmd(["wsl.exe", "-d", WSL_DISTRO, "-u", "root", "--"] + cmd, timeout=timeout)

    @staticmethod
    def distro_present() -> bool:
        rc, out, _ = run_cmd(["wsl.exe", "-l", "-q"], timeout=15)
        if rc != 0:
            return False
        names = {n.replace("\x00", "").strip() for n in out.splitlines()}
        return WSL_DISTRO in names

    def state(self) -> AgentState:
        st = AgentState(backend=self.name, install_dir=f"WSL:{WSL_DISTRO}:{INSTALL_DIR}")
        if not self.distro_present():
            st.detail = f"WSL distro {WSL_DISTRO} is not installed"
            return st
        rc, _, _ = self._wsl(["test", "-f", f"{INSTALL_DIR}/main.py"])
        st.installed = rc == 0
        rc, out, _ = self._wsl(["cat", f"{INSTALL_DIR}/setup.py"])
        if rc == 0:
            m = re.search(r'version\s*=\s*["\']([^"\']+)["\']', out)
            st.version = m.group(1) if m else None
        rc, _, _ = self._wsl(["test", "-f", ENV_FILE])
        st.env_present = (rc == 0) if rc >= 0 else None
        rc, out, _ = self._wsl(["systemctl", "is-active", SERVICE])
        st.service_state = (out.strip() or "unknown") if rc >= 0 else "unknown"
        st.running = st.service_state in ("active", "activating")
        rc, out, _ = self._wsl(["systemctl", "is-enabled", SERVICE])
        st.enabled = (out.strip() == "enabled") if rc >= 0 and out.strip() else None
        return st

    def control(self, verb: str, *, interactive: bool) -> tuple[bool, str]:
        rc, out, err = self._wsl(["systemctl", verb, SERVICE], timeout=STOP_TIMEOUT_S + 30)
        if rc != 0:
            return False, (err or out).strip()[-300:] or f"systemctl {verb} failed inside WSL ({rc})"
        return True, ""

    def logs_command(self) -> list[str]:
        return ["wsl.exe", "-d", WSL_DISTRO, "-u", "root", "--", "journalctl", "-u", SERVICE, "-f", "-n", "30",
                "--no-pager"]


class ProcessBackend:
    """No service manager (macOS / a Linux without systemd / a dev checkout): the agent is a
    plain foreground process. We find it by its command line; we never store a PID."""
    name = "process"

    @staticmethod
    def available() -> bool:
        return True

    @staticmethod
    def candidate_dirs() -> list[str]:
        here = os.path.dirname(os.path.abspath(__file__))
        repo_agent = os.path.abspath(os.path.join(here, "..", "..", "..", "lumaris_agent"))
        cands = [os.getenv("PETABYTE_AGENT_DIR"), INSTALL_DIR, repo_agent]
        return [c for c in cands if c]

    def install_dir(self) -> str | None:
        for d in self.candidate_dirs():
            if os.path.isfile(os.path.join(d, "main.py")) and os.path.isfile(os.path.join(d, "task_fetcher.py")):
                return d
        return None

    @staticmethod
    def find_pids() -> list[int]:
        pids: list[int] = []
        try:
            import psutil

            me = os.getpid()
            for p in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cl = " ".join(p.info.get("cmdline") or [])
                except Exception:
                    continue
                if p.info["pid"] != me and "main.py" in cl and ("petabyte-agent" in cl or "lumaris_agent" in cl):
                    pids.append(int(p.info["pid"]))
            return pids
        except Exception:
            pass
        if sysinfo.which("pgrep"):
            rc, out, _ = run_cmd(["pgrep", "-f", "(petabyte-agent|lumaris_agent).*main.py"])
            if rc == 0:
                for tok in out.split():
                    try:
                        if int(tok) != os.getpid():
                            pids.append(int(tok))
                    except ValueError:
                        pass
        return pids

    def state(self) -> AgentState:
        st = AgentState(backend=self.name)
        st.install_dir = self.install_dir()
        st.installed = st.install_dir is not None
        st.version = _read_version(st.install_dir)
        st.pids = self.find_pids()
        st.running = bool(st.pids)
        st.service_state = "active" if st.running else "inactive"
        env_candidates = [ENV_FILE, os.path.expanduser("~/.petabyte/agent.env")]
        if st.install_dir:
            env_candidates.append(os.path.join(st.install_dir, ".env"))
        st.env_present = any(os.path.isfile(p) for p in env_candidates)
        return st

    def stop(self, pids: list[int], *, timeout: float = 20.0) -> tuple[bool, str]:
        """SIGTERM (the agent handles it like Ctrl-C), then SIGKILL only if it ignores us.

        Windows has no graceful signal for a process we did not spawn: os.kill() there routes
        every value except CTRL_C_EVENT/CTRL_BREAK_EVENT to TerminateProcess, and the console
        events address a process GROUP (only usable on a child started with
        CREATE_NEW_PROCESS_GROUP). Sending SIGBREAK to a PID therefore killed the agent
        outright while the code claimed to be asking politely. psutil.terminate() is the
        documented way; without it we fall back to the same forceful stop, but say so."""
        if not pids:
            return True, ""
        forced_on_windows = False
        for pid in pids:
            try:
                if sysinfo.is_windows():
                    try:
                        import psutil

                        psutil.Process(pid).terminate()
                    except ImportError:
                        forced_on_windows = True
                        os.kill(pid, signal.SIGTERM)      # == TerminateProcess on Windows
                else:
                    os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            except Exception:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline and self.find_pids():
            time.sleep(0.5)
        left = self.find_pids()
        if not left and forced_on_windows:
            return True, ("stopped the agent process forcefully (Windows has no graceful signal "
                          "for a process it did not start; install the `system` extra for psutil)")
        if left:
            for pid in left:
                try:
                    os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            time.sleep(0.5)
            return (not self.find_pids()), "had to force-stop the agent process"
        return True, ""

    def foreground_command(self, install_dir: str) -> list[str]:
        venv_py = os.path.join(install_dir, ".venv", "Scripts" if sysinfo.is_windows() else "bin",
                               "python.exe" if sysinfo.is_windows() else "python")
        py = venv_py if os.path.isfile(venv_py) else sys.executable
        return [py, "main.py"]


def backend():
    if WslBackend.available():
        return WslBackend()
    if SystemdBackend.available():
        return SystemdBackend()
    return ProcessBackend()


def detect(*, with_local: bool = True, with_docker: bool = True) -> AgentState:
    """One consolidated view of the agent on this machine."""
    st = backend().state()
    if st.backend != "process":
        # a stray foreground instance next to the service is worth knowing about
        stray = ProcessBackend.find_pids()
        if stray and not set(stray) <= set(st.pids):
            st.pids = sorted(set(st.pids) | set(stray))
            st.detail = (st.detail + "; " if st.detail else "") + f"{len(stray)} foreground instance(s) outside the service"
            st.running = True
    if with_local and st.running:
        st.local = local_status()
    elif with_local:
        loc = local_status(timeout=0.8)
        if loc:                                       # something IS listening even though no service is
            st.local, st.running = loc, True
            st.detail = (st.detail + "; " if st.detail else "") + "an agent is listening locally outside the service manager"
    if with_docker:
        st.job_containers, st.other_containers = job_containers()
    return st


# ------------------------------------------------------------------ env file (never the key)
def parse_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def public_config(env: dict[str, str]) -> dict[str, Any]:
    """The parts of the agent config that are safe to show."""
    return {"api_url": env.get("PETABYTE_API_URL"), "spec_id": env.get("PETABYTE_SPEC_ID"),
            "has_api_key": bool(env.get("PETABYTE_API_KEY")), "provider": env.get("PROVIDER"),
            "price_per_hour": env.get("PRICE_PER_HOUR")}


# ------------------------------------------------------------------ install
@dataclass
class InstallPlan:
    api_url: str
    sell: str = "all"                     # gpu | cpu | all
    price_per_hour: float | None = None
    lockdown_egress: bool = True
    installer_name: str = "install.sh"    # install.sh (Linux) | install.ps1 (Windows)

    def env_overrides(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.sell == "cpu":
            env["GPU_COUNT"] = "0"
            env["GPU_MODEL"] = ""
        if self.price_per_hour is not None:
            env["PRICE_PER_HOUR"] = f"{self.price_per_hour:.2f}"
        if not self.lockdown_egress:
            env["PETABYTE_LOCKDOWN_EGRESS"] = "false"
        return env


def installer_summary(plan: InstallPlan) -> list[str]:
    """What the official installer will do, in plain words (it IS privileged)."""
    if plan.installer_name == "install.ps1":
        return ["Enable WSL2 and install the Ubuntu-24.04 distro if missing (Windows feature — may need a reboot)",
                "Inside it, run the Linux installer below and register a login task that keeps the node alive"]
    return ["Install python3, git, curl and rsync with apt (if missing)",
            "Install Docker (get.docker.com) if it is not installed — buyer jobs run in sandboxed containers",
            "Install the NVIDIA container toolkit when an NVIDIA GPU is present",
            "Restrict container egress to the internet only (iptables) — turn off with --no-egress-lockdown",
            f"Install the agent to {INSTALL_DIR} with its own venv, write {ENV_FILE} (root-only, 0600)",
            f"Register + attest this machine, then enable and start the `{SERVICE}` service"]


def fetch_installer(client, name: str) -> str:
    """Download the official installer script over TLS from YOUR API host into a private temp
    file (0600). It is a file on disk you can read before it runs — never `curl | bash`."""
    r = client.get(f"/{name}")
    if r.status_code != 200 or not (r.text or "").strip():
        raise RuntimeError(f"could not download /{name} from the API ({r.status_code})")
    body = r.text
    if name == "install.sh" and "petabyte-agent" not in body:
        raise RuntimeError("downloaded install.sh does not look like the Petabyte agent installer")
    fd, path = tempfile.mkstemp(prefix="petabyte-", suffix="-" + name)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def mint_node_key(client) -> str:
    """A fresh, narrow (node,jobs) 90-day key for this machine — the same thing the web /install
    page mints. Returned to the caller only to be placed in the installer's environment."""
    r = client.post("/create_api_key", params={"days": NODE_KEY_DAYS, "scopes": NODE_KEY_SCOPES})
    if r.status_code == 403:
        raise PermissionError("your sign-in cannot mint node keys — run `petabyte login` (browser) and try again")
    if r.status_code != 200:
        raise RuntimeError(f"could not create a node key ({r.status_code})")
    key = (r.json() or {}).get("api_key")
    if not key:
        raise RuntimeError("the API returned no key")
    return key


def run_installer(path: str, plan: InstallPlan, node_key: str, *, interactive: bool,
                  runner: Callable[..., Any] | None = None) -> int:
    """Execute the downloaded installer with the secrets in its ENVIRONMENT only."""
    env = dict(os.environ)
    env.update({"PETABYTE_API_URL": plan.api_url, "PETABYTE_API_KEY": node_key})
    env.update(plan.env_overrides())
    keep = ",".join(["PETABYTE_API_URL", "PETABYTE_API_KEY", *plan.env_overrides().keys()])
    if plan.installer_name == "install.ps1":
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
    else:
        cmd = ["bash", path]
        if not sysinfo.is_admin():
            sudo = sysinfo.which("sudo")
            if not sudo:
                raise PermissionError("the installer needs root and `sudo` is not available — run as root")
            # sudo honours --preserve-env only when the sudoers rule carries the SETENV tag,
            # and many distributions ship rules without it. Without this probe the failure lands
            # AFTER Step 4 has begun making privileged changes, as either a bare non-zero exit or
            # -- worse -- an installer that runs with PETABYTE_API_KEY absent and fails
            # registration half-way through. Probe first and say exactly what to fix. Only the
            # specific "not allowed to preserve the environment" refusal is fatal: any other
            # failure here (a password prompt, say) is not evidence the policy forbids it.
            # LC_ALL=C (and LANGUAGE cleared, which otherwise overrides it for gettext messages):
            # the check below matches ENGLISH substrings, so on a translated host sudo's refusal
            # would parse as "no problem found" and the user would get a bare failure instead of
            # the SETENV guidance. Pin the locale for the probe only.
            probe = (runner or subprocess.run)(
                [sudo, "-n", f"--preserve-env={keep}", "true"],
                capture_output=True, text=True,
                env={**os.environ, "LC_ALL": "C", "LANGUAGE": ""})
            perr = (getattr(probe, "stderr", "") or "")
            if "preserve the environment" in perr or "not allowed to set" in perr:
                raise PermissionError(
                    "sudo refuses to preserve the environment on this host, so the installer "
                    "would run without PETABYTE_API_KEY and fail registration.\n"
                    "  Fix: add the SETENV tag to your sudoers rule, e.g.\n"
                    "    %sudo ALL=(ALL) SETENV: ALL\n"
                    "  or re-run this command as root.")
            cmd = [sudo, f"--preserve-env={keep}"] + ([] if interactive else ["-n"]) + cmd
    r = (runner or subprocess.run)(cmd, env=env)
    return int(getattr(r, "returncode", r) or 0)
