"""ui — the one presentation layer for the `petabyte` command.

Two renderers behind one API:

  rich   when `rich` is importable AND we are talking to a colour-capable terminal
         (panels, tables, rules, spinners, prompts — the product experience);
  plain  everywhere else — a pipe, a file, CI, NO_COLOR, TERM=dumb, a frozen build without
         rich — deterministic aligned text with the same information and the same words.

Both share one vocabulary (✔ ✗ ⚠ ● ○ → …, with ASCII fallbacks when the stream can't encode
Unicode) so the CLI reads the same in a screenshot and in a log file. Progress bars come from
`tqdm` only for operations with measurable progress, and only on an interactive terminal.

Environment (all optional):
    NO_COLOR=1 / PETABYTE_COLOR=never    plain text, no escape codes
    PETABYTE_COLOR=always / FORCE_COLOR=1  colour even when stdout is not a TTY
    PETABYTE_UI=plain|rich               force a renderer (rich still needs the package)
    PETABYTE_ASCII=1                     never emit Unicode symbols
    PETABYTE_NONINTERACTIVE=1            never prompt; every question takes its default
"""
from __future__ import annotations

import contextlib
import getpass
import os
import re
import shutil
import sys
from collections.abc import Iterable, Sequence
from typing import Any

BRAND = "Petabyte"
AMBER = "color(214)"
TEAL = "color(44)"
MINT = "color(42)"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_UNICODE = {"ok": "✔", "fail": "✗", "warn": "⚠", "info": "ℹ", "bullet": "•", "arrow": "→",
            "on": "●", "off": "○", "bolt": "⚡", "rule": "─", "ellipsis": "…"}
_ASCII = {"ok": "OK", "fail": "x", "warn": "!", "info": "i", "bullet": "*", "arrow": "->",
          "on": "*", "off": "o", "bolt": "", "rule": "-", "ellipsis": "..."}


# ------------------------------------------------------------------ capability probing
def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def is_ci() -> bool:
    return any(os.environ.get(n) for n in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE",
                                            "TF_BUILD", "JENKINS_URL"))


def is_tty(stream) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:
        return False


def color_enabled(stream) -> bool:
    mode = os.environ.get("PETABYTE_COLOR", "auto").strip().lower()
    if mode == "never" or _env_flag("NO_COLOR"):
        return False
    if mode == "always" or _env_flag("FORCE_COLOR"):
        return True
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    return is_tty(stream)


def unicode_enabled(stream) -> bool:
    if _env_flag("PETABYTE_ASCII"):
        return False
    enc = (getattr(stream, "encoding", None) or "").lower()
    if not enc:
        return False
    if "utf" in enc:
        return True
    try:
        "✔✗●→⚡".encode(enc)
        return True
    except Exception:
        return False


def rich_available() -> bool:
    """Is `rich` importable? Probed by spec, not by importing it: this is called on the startup
    path of every command, and the plain renderer must cost nothing when rich is absent."""
    try:
        import importlib.util

        return importlib.util.find_spec("rich") is not None
    except Exception:
        return False


def choose_mode(stream, color: bool) -> str:
    forced = os.environ.get("PETABYTE_UI", "").strip().lower()
    if forced == "plain":
        return "plain"
    if forced == "rich":
        return "rich" if rich_available() else "plain"
    return "rich" if (color and rich_available()) else "plain"


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def money(v: Any, dash: str = "—") -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return dash


def pct(v: Any, dash: str = "—") -> str:
    try:
        return f"{float(v):.0f}%"
    except (TypeError, ValueError):
        return dash


class _NullProgress:
    """A progress bar that renders nothing (non-interactive streams / tqdm missing)."""

    def __init__(self, total=None, desc=""):
        self.total, self.n, self.desc = total, 0, desc

    def update(self, n=1):
        self.n += n

    def set_description(self, desc):
        self.desc = desc

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ------------------------------------------------------------------ the presenter
class Ui:
    """A presenter bound to one stream. `out` and `err` below are the module-level instances."""

    def __init__(self, stream=None, *, mode: str | None = None, color: bool | None = None,
                 unicode: bool | None = None, width: int | None = None,
                 interactive: bool | None = None):
        self.stream = stream if stream is not None else sys.stdout
        self.color = color_enabled(self.stream) if color is None else bool(color)
        self.unicode = unicode_enabled(self.stream) if unicode is None else bool(unicode)
        self.mode = choose_mode(self.stream, self.color) if mode is None else mode
        if self.mode == "rich" and not rich_available():
            self.mode = "plain"
        self.icons = _UNICODE if self.unicode else _ASCII
        self._width = width
        self._interactive = interactive
        self.console = None
        if self.mode == "rich":
            from rich.console import Console

            self.console = Console(file=self.stream, force_terminal=self.color,
                                   no_color=not self.color, width=width, emoji=False,
                                   highlight=False, soft_wrap=False,
                                   legacy_windows=None if os.name == "nt" else False)

    # ---- facts -----------------------------------------------------------
    @property
    def interactive(self) -> bool:
        if self._interactive is not None:
            return self._interactive
        if _env_flag("PETABYTE_NONINTERACTIVE") or is_ci():
            return False
        return is_tty(self.stream) and is_tty(sys.stdin)

    def width(self, default: int = 80) -> int:
        if self._width:
            return self._width
        try:
            w = shutil.get_terminal_size((default, 24)).columns
            return w if w and w > 0 else default
        except Exception:
            return default

    def icon(self, key: str) -> str:
        return self.icons.get(key, "")

    # ---- low level -------------------------------------------------------
    @staticmethod
    def escape(text: Any) -> str:
        """Escape rich markup so user/API strings render literally."""
        return re.sub(r"(\\*)(\[[a-z#/@][^[]*?])", lambda m: f"{m.group(1)}{m.group(1)}\\{m.group(2)}",
                      str(text))

    def _write(self, s: str = "") -> None:
        if self.stream is None:                      # windowed build without a console
            return
        try:
            self.stream.write(s + "\n")
        except UnicodeEncodeError:
            self.stream.write(strip_ansi(s).encode("ascii", "replace").decode() + "\n")
        except (OSError, ValueError):
            pass

    def _rich(self, markup: str) -> None:
        self.console.print(markup)

    def line(self, text: Any = "") -> None:
        """Plain text, rendered literally in both modes."""
        if self.mode == "rich":
            self._rich(self.escape(text))
        else:
            self._write(str(text))

    def blank(self) -> None:
        self._write("") if self.mode != "rich" else self.console.print()

    def raw(self, text: str) -> None:
        """Pre-styled plain text (no markup interpretation)."""
        if self.mode == "rich":
            self.console.print(text, markup=False, highlight=False)
        else:
            self._write(text)

    # ---- brand & structure ----------------------------------------------
    def brand(self, subtitle: str | None = None) -> None:
        bolt = self.icon("bolt")
        head = f"{bolt} {BRAND}".strip()
        if self.mode == "rich":
            self._rich(f"[bold {AMBER}]{head}[/]" + (f"  [dim]{self.escape(subtitle)}[/]" if subtitle else ""))
        else:
            self._write(head + (f"  {subtitle}" if subtitle else ""))
        self.blank()

    def section(self, title: str) -> None:
        if self.mode == "rich":
            from rich.rule import Rule

            self.console.print(Rule(f"[bold {TEAL}]{self.escape(title.upper())}[/]", align="left",
                                    style="dim"))
        else:
            self._write("")
            self._write(title.upper())
            self._write(self.icon("rule") * max(8, min(self.width(), len(title) + 20)))

    def rule(self) -> None:
        if self.mode == "rich":
            from rich.rule import Rule

            self.console.print(Rule(style="dim"))
        else:
            self._write(self.icon("rule") * min(self.width(), 60))

    # ---- messages ---------------------------------------------------------
    def _msg(self, key: str, style: str, msg: Any, detail: str | None = None) -> None:
        icon = self.icon(key)
        if self.mode == "rich":
            tail = f"  [dim]{self.escape(detail)}[/]" if detail else ""
            self._rich(f"[{style}]{icon}[/] {self.escape(msg)}{tail}")
        else:
            self._write(f"{icon} {msg}" + (f"  ({detail})" if detail else ""))

    def ok(self, msg: Any, detail: str | None = None) -> None:
        self._msg("ok", f"bold {MINT}", msg, detail)

    def fail(self, msg: Any, detail: str | None = None) -> None:
        self._msg("fail", "bold red", msg, detail)

    def warn(self, msg: Any, detail: str | None = None) -> None:
        self._msg("warn", "bold yellow", msg, detail)

    def info(self, msg: Any, detail: str | None = None) -> None:
        self._msg("info", TEAL, msg, detail)

    def step(self, msg: Any, detail: str | None = None) -> None:
        self._msg("arrow", "dim", msg, detail)

    def note(self, msg: Any) -> None:
        if self.mode == "rich":
            self._rich(f"[dim]{self.escape(msg)}[/]")
        else:
            self._write(str(msg))

    def command(self, cmd: str, caption: str | None = None) -> None:
        """A command the user can copy: indented, highlighted."""
        if caption:
            self.note(caption)
        if self.mode == "rich":
            self._rich(f"  [bold {TEAL}]{self.escape(cmd)}[/]")
        else:
            self._write(f"  {cmd}")

    def status_word(self, state: Any) -> str:
        """'online' -> a coloured ● online (rich) / '● online' (plain)."""
        s = str(state or "unknown")
        low = s.lower()
        if low in ("online", "running", "ready", "active", "healthy", "earning", "connected", "verified", "completed", "ok", "paid"):
            key, style = "on", MINT
        elif low in ("pending", "queued", "starting", "connecting", "idle", "held", "degraded", "warning", "clearing"):
            key, style = "on", "yellow"
        elif low in ("offline", "stopped", "failed", "error", "disconnected", "unverified", "missing", "not installed", "suspended"):
            key, style = "off", "red"
        else:
            key, style = "bullet", TEAL
        icon = self.icon(key)
        if self.mode == "rich":
            return f"[{style}]{icon} {self.escape(s)}[/]"
        return f"{icon} {s}"

    # ---- structured -------------------------------------------------------
    def kv(self, rows: Sequence[tuple[str, Any]], *, title: str | None = None,
           label_width: int | None = None) -> None:
        """Label / value rows. A value may be a str, or a tuple ('status', state)."""
        pairs = [(str(k), v) for k, v in rows]
        if self.mode == "rich":
            from rich.table import Table

            t = Table.grid(padding=(0, 2))
            t.add_column(style="dim", no_wrap=True)
            t.add_column()
            for k, v in pairs:
                t.add_row(self.escape(k), self._value_markup(v))
            if title:
                self._rich(f"[bold]{self.escape(title)}[/]")
            self.console.print(t)
        else:
            if title:
                self._write(title)
            lw = label_width or max((len(k) for k, _ in pairs), default=0)
            for k, v in pairs:
                self._write(f"  {k:<{lw}}  {self._value_plain(v)}")

    def _value_markup(self, v: Any) -> str:
        if isinstance(v, tuple) and len(v) == 2 and v[0] == "status":
            return self.status_word(v[1])
        if isinstance(v, tuple) and len(v) == 2 and v[0] == "dim":
            return f"[dim]{self.escape(v[1])}[/]"
        if isinstance(v, tuple) and len(v) == 2 and v[0] == "money":
            return f"[bold {AMBER}]{self.escape(v[1])}[/]"
        return self.escape("—" if v is None else v)

    def _value_plain(self, v: Any) -> str:
        if isinstance(v, tuple) and len(v) == 2 and v[0] == "status":
            return strip_ansi(self.status_word(v[1]))
        if isinstance(v, tuple) and len(v) == 2:
            return str(v[1])
        return "-" if v is None else str(v)

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[Any]], *, title: str | None = None,
              aligns: Sequence[str] | None = None) -> None:
        srows = [["" if c is None else str(c) for c in r] for r in rows]
        if self.mode == "rich":
            from rich import box
            from rich.table import Table

            t = Table(title=title, box=box.SIMPLE_HEAD, header_style=f"bold {TEAL}", show_edge=False,
                      pad_edge=False, title_justify="left", title_style="bold")
            for i, h in enumerate(headers):
                t.add_column(self.escape(h), justify=(aligns[i] if aligns and i < len(aligns) else "left"),
                             no_wrap=False)
            for r in srows:
                t.add_row(*[self.escape(c) for c in r])
            self.console.print(t)
            return
        if title:
            self._write(title)
        cols = len(headers)
        widths = [len(str(headers[i])) for i in range(cols)]
        for r in srows:
            for i in range(min(cols, len(r))):
                widths[i] = max(widths[i], len(r[i]))

        def fmt(cells):
            out = []
            for i in range(cols):
                c = cells[i] if i < len(cells) else ""
                a = aligns[i] if aligns and i < len(aligns) else "left"
                out.append(c.rjust(widths[i]) if a == "right" else c.ljust(widths[i]))
            return "  ".join(out).rstrip()

        self._write(fmt([str(h) for h in headers]))
        self._write(fmt([self.icon("rule") * w for w in widths]))
        for r in srows:
            self._write(fmt(r))

    def panel(self, title: str, rows: Sequence[tuple[str, Any]] | None = None,
              text: str | None = None, *, style: str = TEAL) -> None:
        if self.mode == "rich":
            from rich.panel import Panel
            from rich.table import Table

            if rows:
                body = Table.grid(padding=(0, 2))
                body.add_column(style="dim", no_wrap=True)
                body.add_column()
                for k, v in rows:
                    body.add_row(self.escape(k), self._value_markup(v))
                self.console.print(Panel(body, title=f"[bold]{self.escape(title)}[/]", title_align="left",
                                         border_style=style, padding=(0, 1)))
            else:
                self.console.print(Panel(self.escape(text or ""), title=f"[bold]{self.escape(title)}[/]",
                                         title_align="left", border_style=style, padding=(0, 1)))
            return
        self.section(title)
        if rows:
            self.kv(rows)
        if text:
            self._write(text)

    def dashboard(self, title: str, subtitle: str | None,
                  sections: Sequence[tuple[str, Sequence[tuple[str, Any]] | str]]) -> None:
        """One framed dashboard: a title, then sections of key/value rows (or a sentence)."""
        if self.mode == "rich":
            from rich.console import Group
            from rich.panel import Panel
            from rich.rule import Rule
            from rich.table import Table

            parts: list[Any] = []
            for name, rows in sections:
                parts.append(Rule(f"[bold {TEAL}]{self.escape(name.upper())}[/]", align="left", style="dim"))
                if isinstance(rows, str):
                    parts.append(f"[dim]{self.escape(rows)}[/]")
                else:
                    g = Table.grid(padding=(0, 2))
                    g.add_column(style="dim", no_wrap=True, min_width=14)
                    g.add_column()
                    for k, v in rows:
                        g.add_row(self.escape(k), self._value_markup(v))
                    parts.append(g)
            head = f"[bold {AMBER}]{self.icon('bolt')} {self.escape(title)}[/]".strip()
            if subtitle:
                head += f"\n[dim]{self.escape(subtitle)}[/]"
            self.console.print(Panel(Group(*parts), title=head, title_align="left", border_style=AMBER,
                                     padding=(0, 1), width=min(self.width(), 88)))
            return
        self._write(f"{self.icon('bolt')} {title}".strip() + (f"  {subtitle}" if subtitle else ""))
        for name, rows in sections:
            self.section(name)
            if isinstance(rows, str):
                self._write(rows)
            else:
                self.kv(rows)

    def tree(self, label: str, children: Sequence[tuple[str, Sequence[str]]]) -> None:
        if self.mode == "rich":
            from rich.tree import Tree

            t = Tree(f"[bold]{self.escape(label)}[/]")
            for name, leaves in children:
                b = t.add(f"[{TEAL}]{self.escape(name)}[/]")
                for leaf in leaves:
                    b.add(self.escape(leaf))
            self.console.print(t)
            return
        self._write(label)
        for name, leaves in children:
            self._write(f"  {name}")
            for leaf in leaves:
                self._write(f"    {self.icon('bullet')} {leaf}")

    # ---- errors ------------------------------------------------------------
    def error(self, title: str, *, reason: str | None = None, fix: str | Sequence[str] | None = None,
              run: str | Sequence[str] | None = None, detail: str | None = None) -> None:
        """A human-first error: what happened, why, what to do, what to run next.
        Technical detail (status codes, exception class) comes last, dimmed."""
        self.fail(title)
        if reason:
            self.line(f"  {reason}")
        if fix:
            for f in ([fix] if isinstance(fix, str) else fix):
                self.line(f"  {f}")
        if run:
            self.blank()
            for r in ([run] if isinstance(run, str) else run):
                self.command(r)
        if detail:
            self.note(f"  ({detail})")

    # ---- interaction -------------------------------------------------------
    quiet = False   # set for --json: transient status lines must never reach stdout

    @contextlib.contextmanager
    def status(self, msg: str):
        """A spinner while something runs (rich + TTY); a single line on a plain TTY; nothing
        on a pipe or in --json mode (transient chatter must not end up in captured output)."""
        if self.quiet or not is_tty(self.stream):
            yield
            return
        if self.mode == "rich":
            with self.console.status(f"[dim]{self.escape(msg)}[/]", spinner="dots"):
                yield
            return
        self.step(msg)
        yield

    def progress(self, total: int | None = None, desc: str = "", unit: str = "B", *, force: bool = False):
        """A tqdm bar for MEASURABLE work; silent when not interactive or tqdm is missing."""
        if not (force or self.interactive):
            return _NullProgress(total, desc)
        try:
            from tqdm import tqdm

            return tqdm(total=total, desc=desc, unit=unit, unit_scale=(unit == "B"), unit_divisor=1024,
                        file=self.stream, dynamic_ncols=True, leave=False, ascii=not self.unicode,
                        disable=False)
        except Exception:
            return _NullProgress(total, desc)

    def confirm(self, question: str, *, default: bool = False) -> bool:
        """Yes/no. Non-interactive shells take the default (and say so)."""
        yn = "[Y/n]" if default else "[y/N]"
        if not self.interactive:
            self.note(f"{question} {yn}  (non-interactive: {'yes' if default else 'no'})")
            return default
        try:
            if self.mode == "rich":
                from rich.prompt import Confirm

                return Confirm.ask(self.escape(question), default=default, console=self.console)
            ans = input(f"{question} {yn} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            self.blank()
            return False
        if not ans:
            return default
        return ans in ("y", "yes")

    def choose(self, prompt: str, options: Sequence[str], *, default: int = 1) -> int:
        """Numbered pick; returns the 1-based index. Non-interactive -> default."""
        for i, o in enumerate(options, 1):
            if self.mode == "rich":
                self._rich(f"  [bold {TEAL}]{i}[/]  {self.escape(o)}")
            else:
                self._write(f"  {i}  {o}")
        if not self.interactive:
            self.note(f"{prompt}  (non-interactive: {default})")
            return default
        while True:
            try:
                raw = input(f"{prompt} [{default}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                self.blank()
                return default
            if not raw:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw)
            self.warn(f"Enter a number between 1 and {len(options)}.")

    def ask(self, prompt: str, *, default: str | None = None, secret: bool = False) -> str | None:
        if not self.interactive:
            return default
        try:
            if secret:
                return getpass.getpass(f"{prompt}: ") or default
            raw = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
            return raw or default
        except (EOFError, KeyboardInterrupt):
            self.blank()
            return default


# Module-level presenters bound to the standard streams.
out = Ui(sys.stdout)
err = Ui(sys.stderr)


def refresh() -> None:
    """Re-probe the standard streams (tests swap sys.stdout; env changes at runtime)."""
    global out, err
    out = Ui(sys.stdout)
    err = Ui(sys.stderr)
