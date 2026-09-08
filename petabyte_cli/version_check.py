"""version_check — which petabyte CLI is installed, which Python, and is there a newer release.

Design rules (they are what keep this from ever annoying anyone):
  * never block the command: one short-timeout request, cached for a day, skipped entirely in
    CI / non-interactive shells / when the user opts out (PETABYTE_NO_UPDATE_CHECK=1);
  * never fail the command: every network / disk / parse problem degrades to "no notice";
  * never lie: a dev build (0.3.0.dev12) is not "older" than 0.3.0, and the notice is shown only
    when the published version is strictly newer than the installed one.

The installed version comes from package metadata (`petabyte-client`) so a pip install reports
the real number; a source checkout falls back to FALLBACK_VERSION, which a test pins to the
root pyproject so the two never drift.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

PACKAGE = "petabyte-client"
FALLBACK_VERSION = "0.3.4"
MIN_PYTHON = (3, 9)
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CACHE_TTL_S = 24 * 3600
REQUEST_TIMEOUT_S = 1.5
UPGRADE_COMMAND = f"pip install -U {PACKAGE}"

_VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)(.*)$")


def installed_version() -> str:
    try:
        from importlib.metadata import version

        return version(PACKAGE)
    except Exception:
        return FALLBACK_VERSION


def python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def python_supported() -> bool:
    return sys.version_info[:2] >= MIN_PYTHON


def python_support_message() -> str:
    return (f"Petabyte CLI needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; "
            f"you are running {python_version()}. Install a current Python from "
            f"https://www.python.org/downloads/ and reinstall with: {UPGRADE_COMMAND}")


def parse_version(s: str) -> tuple:
    """A PEP 440-lite key: numeric release parts, then a pre-release rank so that
    1.4.0.dev1 < 1.4.0rc1 < 1.4.0 and 1.4.0 < 1.4.1. Unparseable -> (0,)."""
    m = _VERSION_RE.match(str(s or ""))
    if not m:
        return (0,)
    nums = tuple(int(p) for p in m.group(1).split("."))
    rest = m.group(2).lower()
    # rank orders the RELEASE KIND, serial orders within it. Both are needed: without a serial
    # 1.4.0rc1 and 1.4.0rc2 compared EQUAL, and a post-release was indistinguishable from the
    # final it follows (0.3.0.post1 == 0.3.0), so is_newer() suppressed a genuinely newer PyPI
    # release. post ranks ABOVE final, per PEP 440: 1.0 < 1.0.post1 < 1.0.1 (the numeric parts
    # still compare first, so the last of those holds).
    def _serial(marker: str) -> int:
        m2 = re.search(marker + r"[._-]?(\d+)", rest)
        return int(m2.group(1)) if m2 else 0

    rank, serial = 3, 0                                 # final release
    if "dev" in rest:
        rank, serial = 0, _serial(r"dev")
    elif re.search(r"(a|alpha)\d*", rest):
        rank, serial = 1, _serial(r"(?:alpha|a)")
    elif re.search(r"(b|beta|rc|c)\d*", rest):
        rank, serial = 2, _serial(r"(?:beta|rc|b|c)")
    elif re.search(r"post\d*", rest):
        rank, serial = 4, _serial(r"post")
    # pad to 4 numeric parts so (1, 4) == (1, 4, 0)
    nums = nums + (0,) * (4 - len(nums)) if len(nums) < 4 else nums
    return nums + (rank, serial)


def is_newer(candidate: str, installed: str) -> bool:
    return parse_version(candidate) > parse_version(installed)


@dataclass
class UpdateInfo:
    installed: str
    latest: str | None
    newer: bool
    source: str = "pypi"                                # pypi | cache | none

    @property
    def upgrade_command(self) -> str:
        return UPGRADE_COMMAND


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def is_ci() -> bool:
    return any(_env_flag(n) or os.environ.get(n) for n in
               ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "TF_BUILD", "JENKINS_URL"))


def should_check(stream=None) -> bool:
    """Only in an interactive shell where a human will read the notice."""
    if _env_flag("PETABYTE_NO_UPDATE_CHECK") or is_ci():
        return False
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def default_cache_path() -> str:
    cfg = os.getenv("PETABYTE_CONFIG")
    base = os.path.dirname(cfg) if cfg and os.path.dirname(cfg) else os.path.expanduser("~/.petabyte")
    return os.path.join(base, "update-check.json")


def fetch_latest(timeout: float = REQUEST_TIMEOUT_S) -> str | None:
    """One small GET to PyPI. Returns the latest version string or None. Never raises."""
    try:
        import httpx

        r = httpx.get(PYPI_URL, timeout=timeout, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None
        v = (r.json().get("info") or {}).get("version")
        return str(v) if v else None
    except Exception:
        return None


def _read_cache(path: str, now: float, ttl: int) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if now - float(d.get("checked_at", 0)) <= ttl and d.get("latest"):
            return str(d["latest"])
    except Exception:
        pass
    return None


def _write_cache(path: str, latest: str, now: float) -> None:
    try:
        # dirname("update-check.json") is "", and makedirs("") raises — which the outer except
        # swallowed, so the result was silently never cached and every run re-checked PyPI.
        _parent = os.path.dirname(path)
        if _parent:
            os.makedirs(_parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"latest": latest, "checked_at": now, "package": PACKAGE}, f)
        os.replace(tmp, path)
    except Exception:
        pass


def check_for_update(*, installed: str | None = None, cache_path: str | None = None,
                     ttl: int = CACHE_TTL_S, now: float | None = None,
                     fetch: Callable[[], str | None] | None = None,
                     force: bool = False) -> UpdateInfo:
    """Compare the installed version with the latest published one (cached for `ttl`).
    Never raises; `latest` is None when nothing could be learned."""
    installed = installed or installed_version()
    now = time.time() if now is None else now
    path = cache_path or default_cache_path()
    latest = None if force else _read_cache(path, now, ttl)
    source = "cache"
    if latest is None:
        latest = (fetch or fetch_latest)()
        source = "pypi"
        if latest:
            _write_cache(path, latest, now)
    if not latest:
        return UpdateInfo(installed=installed, latest=None, newer=False, source="none")
    return UpdateInfo(installed=installed, latest=latest, newer=is_newer(latest, installed),
                      source=source)


def notice_lines(info: UpdateInfo) -> list[str]:
    """The friendly, non-blocking update hint (empty when nothing to say)."""
    if not info.newer or not info.latest:
        return []
    return ["A newer version of the Petabyte CLI is available:",
            f"  installed:  {info.installed}",
            f"  latest:     {info.latest}",
            "",
            "Update with:",
            f"  {info.upgrade_command}"]
