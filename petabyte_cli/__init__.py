"""petabyte_cli — the product layer of the `petabyte` command.

`petabyte.py` (the argparse entrypoint) stays the thin, dependency-light client it always was;
this package adds what makes it feel like a product for everyday buyers and sellers:

  ui.py             one presentation layer (rich when available + a TTY, plain text otherwise)
  version_check.py  installed / latest version, supported-Python check, cached update notice
  sysinfo.py        local machine facts: OS, CPU/RAM/disk, GPU (nvidia-smi), Docker, network
  api.py            the dashboard data fetch — real endpoints only, each piece degrading on its own
  dashboard.py      `petabyte --me`
  agent.py          `--install-agent` / `--run-agent` / `--kill-agent` on the existing seller agent
  doctor.py         `petabyte doctor`
  menu.py / help.py the guided menu and the grouped, example-rich help

Everything here is importable without `rich`/`tqdm` (they are optional at import time and the
plain renderer takes over), so the CLI keeps working in a frozen .exe, a pipe, or CI.
"""
from __future__ import annotations

from .version_check import installed_version

__version__ = installed_version()

__all__ = ["__version__", "installed_version"]
