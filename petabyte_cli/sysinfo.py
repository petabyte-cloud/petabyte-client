"""sysinfo — cheap, cross-platform facts about THIS machine for the dashboard, doctor and wizard.

Everything is best-effort and bounded: subprocesses run without a shell and with short
timeouts, optional libraries (psutil) are used when present and skipped when not, and every
probe returns None / an empty value instead of raising. GPU detection reuses the stdlib
`modelhub.hardware` helper that already ships in the `petabyte-client` wheel.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

SUBPROCESS_TIMEOUT_S = 4


# ------------------------------------------------------------------ platform
def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_wsl() -> bool:
    if not is_linux():
        return False
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def os_summary() -> str:
    try:
        if is_windows():
            rel = platform.release()
            try:
                build = int(platform.version().split(".")[-1])
                if rel == "10" and build >= 22000:
                    rel = "11"
            except Exception:
                pass
            return f"Windows {rel} ({platform.machine() or 'unknown'})"
        if is_linux():
            name = None
            try:
                with open("/etc/os-release", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("PRETTY_NAME="):
                            name = line.split("=", 1)[1].strip().strip('"')
                            break
            except OSError:
                pass
            base = name or f"Linux {platform.release()}"
            return f"{base}{' on WSL2' if is_wsl() else ''} ({platform.machine() or 'unknown'})"
        if is_macos():
            return f"macOS {platform.mac_ver()[0] or platform.release()} ({platform.machine()})"
        return f"{platform.system()} {platform.release()} ({platform.machine()})"
    except Exception:
        return platform.platform()


def is_admin() -> bool:
    """root on POSIX, an elevated console on Windows."""
    try:
        if is_windows():
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def run(cmd: list[str], *, timeout: float = SUBPROCESS_TIMEOUT_S, env: dict | None = None) -> tuple[int, str, str]:
    """Run a command (no shell), never raise. Returns (rc, stdout, stderr); rc=-1 when it
    could not run at all (missing binary, timeout)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return -1, "", ""


def which(name: str) -> str | None:
    return shutil.which(name)


# ------------------------------------------------------------------ CPU / RAM / disk
def cpu_percent(interval: float = 0.25) -> float | None:
    try:
        import psutil

        return float(psutil.cpu_percent(interval=interval))
    except Exception:
        pass
    if is_linux():
        try:
            def _snap():
                with open("/proc/stat", encoding="utf-8") as f:
                    parts = f.readline().split()[1:]
                vals = [int(x) for x in parts]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                return idle, sum(vals)

            i1, t1 = _snap()
            time.sleep(interval)
            i2, t2 = _snap()
            if t2 > t1:
                return round(100.0 * (1 - (i2 - i1) / (t2 - t1)), 1)
        except Exception:
            pass
    return None


def cpu_count() -> int | None:
    try:
        return os.cpu_count()
    except Exception:
        return None


def memory_gb() -> tuple[float, float] | None:
    """(used_gb, total_gb)"""
    try:
        import psutil

        m = psutil.virtual_memory()
        return round((m.total - m.available) / 1e9, 1), round(m.total / 1e9, 1)
    except Exception:
        pass
    if is_linux():
        try:
            info = {}
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    info[k.strip()] = int(v.strip().split()[0]) * 1024
            total = info.get("MemTotal")
            avail = info.get("MemAvailable", info.get("MemFree"))
            if total:
                return round((total - (avail or 0)) / 1e9, 1), round(total / 1e9, 1)
        except Exception:
            pass
    if is_windows():
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MS()
            st.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return (round((st.ullTotalPhys - st.ullAvailPhys) / 1e9, 1), round(st.ullTotalPhys / 1e9, 1))
        except Exception:
            pass
    if is_macos():
        # sysctl gives the TOTAL only. Returning used=0.0 would render "RAM 0.0 / 16.0 GB", i.e.
        # "nothing in use" — a wrong number is worse than none, so say Unavailable instead
        # (installing the `system` extra gives psutil and the accurate figure above).
        return None
    return None


def disk_gb(path: str | None = None) -> tuple[float, float] | None:
    """(used_gb, total_gb) for the filesystem holding `path` (default: home)."""
    try:
        p = path or os.path.expanduser("~")
        u = shutil.disk_usage(p)
        return round(u.used / 1e9, 1), round(u.total / 1e9, 1)
    except Exception:
        return None


# ------------------------------------------------------------------ GPU
@dataclass
class Gpu:
    name: str
    vram_total_mb: int | None = None
    vram_used_mb: int | None = None
    util_pct: float | None = None
    temp_c: float | None = None

    @property
    def short_name(self) -> str:
        return self.name.replace("NVIDIA ", "").replace("GeForce ", "").strip()


def gpus(timeout: float = SUBPROCESS_TIMEOUT_S) -> list[Gpu]:
    """Live NVIDIA metrics via nvidia-smi (name, VRAM, utilisation, temperature); [] when no
    NVIDIA tooling. Falls back to the static detection in modelhub.hardware (AMD/rocminfo)."""
    smi = which("nvidia-smi")
    out_gpus: list[Gpu] = []
    if smi:
        rc, out, _ = run([smi, "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                          "--format=csv,noheader,nounits"], timeout=timeout)
        if rc == 0:
            for line in out.splitlines():
                cells = [c.strip() for c in line.split(",")]
                if not cells or not cells[0]:
                    continue

                def num(i):
                    try:
                        return float(cells[i])
                    except (IndexError, ValueError):
                        return None

                out_gpus.append(Gpu(name=cells[0],
                                    vram_total_mb=int(num(1)) if num(1) is not None else None,
                                    vram_used_mb=int(num(2)) if num(2) is not None else None,
                                    util_pct=num(3), temp_c=num(4)))
    if out_gpus:
        return out_gpus
    try:
        from modelhub import hardware as _hw

        for g in ((_hw.detect() or {}).get("gpus") or []):
            name = g.get("name") if isinstance(g, dict) else None
            if name:
                vram = g.get("vram_gb")
                out_gpus.append(Gpu(name=str(name),
                                    vram_total_mb=int(float(vram) * 1024) if vram else None))
    except Exception:
        pass
    return out_gpus


def nvidia_container_toolkit() -> bool | None:
    """True/False when we can tell, None when Docker is not around to ask."""
    if which("nvidia-ctk"):
        return True
    docker = which("docker")
    if not docker:
        return None
    rc, out, _ = run([docker, "info", "--format", "{{json .Runtimes}}"], timeout=6)
    if rc != 0:
        return None
    return "nvidia" in out.lower()


# ------------------------------------------------------------------ Docker
@dataclass
class DockerStatus:
    installed: bool
    running: bool | None
    version: str | None = None
    detail: str | None = None


def docker_status() -> DockerStatus:
    docker = which("docker")
    if not docker:
        return DockerStatus(installed=False, running=None, detail="docker not on PATH")
    rc, out, _ = run([docker, "--version"])
    version = out.strip().replace("Docker version ", "").split(",")[0] if rc == 0 and out else None
    rc, _, err = run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=6)
    if rc == 0:
        return DockerStatus(installed=True, running=True, version=version)
    # `err` can be whitespace only, which is truthy but has no last line — indexing that raised
    # IndexError out of a probe every caller treats as best-effort. Take the lines first.
    err_lines = (err or "").strip().splitlines()
    return DockerStatus(installed=True, running=False, version=version,
                        detail=err_lines[-1][:120] if err_lines else "daemon not reachable")


# ------------------------------------------------------------------ network
def network_ok(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Can we reach `url` over HTTPS/HTTP right now? (bool, short reason)"""
    try:
        import httpx

        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        return (r.status_code < 500), f"HTTP {r.status_code}"
    except Exception as e:
        return False, type(e).__name__


# ------------------------------------------------------------------ snapshot for the dashboard
@dataclass
class Snapshot:
    os: str = ""
    python: str = ""
    cpu_pct: float | None = None
    cpu_count: int | None = None
    ram: tuple[float, float] | None = None
    disk: tuple[float, float] | None = None
    gpus: list[Gpu] = field(default_factory=list)
    docker: DockerStatus | None = None
    network: tuple[bool, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"os": self.os, "python": self.python, "cpu_pct": self.cpu_pct, "cpu_count": self.cpu_count,
                "ram_used_gb": self.ram[0] if self.ram else None, "ram_total_gb": self.ram[1] if self.ram else None,
                "disk_used_gb": self.disk[0] if self.disk else None, "disk_total_gb": self.disk[1] if self.disk else None,
                "gpus": [{"name": g.name, "util_pct": g.util_pct, "vram_used_mb": g.vram_used_mb,
                          "vram_total_mb": g.vram_total_mb, "temp_c": g.temp_c} for g in self.gpus],
                "docker": {"installed": self.docker.installed, "running": self.docker.running,
                           "version": self.docker.version} if self.docker else None,
                "network": {"ok": self.network[0], "detail": self.network[1]} if self.network else None}


def snapshot(*, api_url: str | None = None, with_docker: bool = False, with_network: bool = True,
             cpu_interval: float = 0.25) -> Snapshot:
    v = sys.version_info
    s = Snapshot(os=os_summary(), python=f"{v.major}.{v.minor}.{v.micro}", cpu_pct=cpu_percent(cpu_interval),
                 cpu_count=cpu_count(), ram=memory_gb(), disk=disk_gb(), gpus=gpus())
    if with_docker:
        s.docker = docker_status()
    if with_network and api_url:
        s.network = network_ok(api_url.rstrip("/") + "/healthz")
    return s
