"""help — `petabyte --help` a newcomer can read in ten seconds: grouped by who you are, with
the four commands that matter first and real examples. Every subcommand keeps its own
argparse `--help` for the full option list.

On a terminal it is a branded screen (header, quick start, one panel per group laid out in two
columns when there is room, an examples panel); on a pipe / in CI it is the same words as plain
aligned text, so scripts and screenshots agree.
"""
from __future__ import annotations

from .ui import AMBER, TEAL

TAGLINE = "Rent verified GPUs, or earn by selling yours."
QUICK: list[tuple[str, str]] = [
    ("petabyte", "guided menu — pick BUY / SELL / ACCOUNT"),
    ("petabyte --me", "your dashboard"),
    ("petabyte <command> --help", "every option of a command"),
]

GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("ACCOUNT", [
        ("--me", "your dashboard: account, wallet, what's running, agent, system"),
        ("login", "sign in with your browser (no password on the CLI)"),
        ("wallet", "balance and earnings"),
        ("deposit <usd>", "add funds"),
        ("activity", "recent notifications"),
        ("doctor", "diagnose account, network, Docker, GPU and agent problems"),
    ]),
    ("SELLER", [
        ("--install-agent", "guided setup: turn this machine into a seller node"),
        ("--run-agent", "start the seller agent and watch it come online"),
        ("--kill-agent", "stop the seller agent safely"),
        ("agent status | logs", "what the agent is doing / the live log"),
        ("earnings", "withdrawable earnings and recent payouts"),
        ("node status <id>", "one of your nodes in detail"),
    ]),
    ("BUYER", [
        ("specs", "GPUs you can rent right now, cheapest first"),
        ("launch <template>", "one-click app (ollama, jupyter, blender…) on the cheapest GPU"),
        ("run <file>", "run a notebook / .py on a rented GPU and print the result"),
        ("jobs", "your running instances and recent bookings"),
        ("ask \"<prompt>\"", "pay-per-token inference (OpenAI-compatible)"),
        ("render / transcode", "Blender frames / GPU video transcode"),
        ("vpn <booking>", "WireGuard config for a VPN booking"),
    ]),
    ("MODELS", [
        ("model search|pull|list", "discover, download and manage AI models locally"),
        ("pull <id>", "alias for model pull"),
        ("run <model-id>", "run a local model (a model id instead of a file)"),
    ]),
    ("SYSTEM", [
        ("--version", "CLI, Python and update status"),
        ("--json", "machine-readable output (me, doctor, wallet, specs, agent status)"),
        ("--verbose", "full technical detail on errors"),
        ("--api <url>", "talk to another Petabyte host (default: petabyte.market)"),
        ("-y / --yes", "assume yes for confirmations (install / stop)"),
    ]),
]

EXAMPLES: list[tuple[str, str]] = [
    ("View your Petabyte profile", "petabyte --me"),
    ("Install the seller agent", "petabyte --install-agent"),
    ("Start earning", "petabyte --run-agent"),
    ("Stop the seller agent", "petabyte --kill-agent"),
    ("Rent the cheapest GPU for a notebook", "petabyte run train.ipynb --gpu \"RTX 4090\" --hours 2"),
    ("Something's wrong?", "petabyte doctor"),
]

FOOTER = "Docs: https://petabyte.market/wiki   ·   Config: ~/.petabyte/cli.json   ·   NO_COLOR=1 for plain output"


def _render_rich(ui, version: str) -> None:
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table

    c = ui.console
    esc = ui.escape
    width = min(ui.width(), 124)
    two_col = width >= 116          # narrower than that, side-by-side panels wrap every description

    # ---- header -----------------------------------------------------------------
    head = Table.grid(expand=True)
    head.add_column()
    head.add_column(justify="right")
    head.add_row(f"[bold {AMBER}]{ui.icon('bolt')} Petabyte CLI[/]  [dim]v{esc(version)}[/]".strip(),
                 "[dim]petabyte.market[/]")
    c.print(Panel(Group(head, f"[bold]{esc(TAGLINE)}[/]",
                        "[dim]Sign in once with your browser, then run one of the commands below.[/]"),
                  border_style=AMBER, padding=(0, 1), width=width))

    # ---- quick start ---------------------------------------------------------------
    q = Table.grid(padding=(0, 3))
    q.add_column(style=f"bold {TEAL}", no_wrap=True)
    q.add_column(style="dim")
    for cmd, desc in QUICK:
        q.add_row(esc(cmd), esc(desc))
    c.print(Panel(q, title="[bold]Start here[/]", title_align="left", border_style="dim", padding=(0, 1),
                  width=width))

    # ---- command groups ------------------------------------------------------------------
    def group_panel(name: str, rows: list[tuple[str, str]]) -> Panel:
        t = Table.grid(padding=(0, 2))
        t.add_column(style=f"bold {TEAL}", no_wrap=True)
        t.add_column()
        for cmd, desc in rows:
            t.add_row(esc(cmd), esc(desc))
        return Panel(t, title=f"[bold {AMBER}]{esc(name)}[/]", title_align="left", border_style="dim",
                     padding=(0, 1), expand=True)

    def examples_panel() -> Panel:
        t = Table.grid(padding=(0, 0))
        t.add_column()
        for caption, cmd in EXAMPLES:
            t.add_row(f"[dim]# {esc(caption)}[/]")
            t.add_row(f"[bold {TEAL}]{esc(cmd)}[/]")
        return Panel(t, title=f"[bold {AMBER}]EXAMPLES[/]", title_align="left", border_style="dim",
                     padding=(0, 1), expand=True)

    panels = [group_panel(n, r) for n, r in GROUPS] + [examples_panel()]
    if two_col:
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        for i in range(0, len(panels), 2):
            pair = panels[i:i + 2]
            grid.add_row(*(pair + [""] * (2 - len(pair))))
        c.print(grid, width=width)
    else:
        for p in panels:
            c.print(p, width=width)
    c.print(f"[dim]{esc(FOOTER)}[/]", width=width)


def _render_plain(ui, version: str) -> None:
    ui.brand(f"v{version}")
    ui.line(TAGLINE + " Sign in once, then:")
    ui.blank()
    lw = max(len(c) for c, _ in QUICK)
    for cmd, desc in QUICK:
        ui.line(f"  {cmd:<{lw}}  {desc}")
    for name, rows in GROUPS:
        ui.section(name)
        ui.kv([(cmd, desc) for cmd, desc in rows])
    ui.section("Examples")
    for caption, cmd in EXAMPLES:
        ui.line(f"  # {caption}")
        ui.command(cmd)
        ui.blank()
    ui.note(FOOTER)


def render(ui, *, version: str) -> None:
    if ui.mode == "rich":
        _render_rich(ui, version)
    else:
        _render_plain(ui, version)


def text(version: str) -> str:
    """The plain rendering (for argparse's help / tests)."""
    import io

    from .ui import Ui

    buf = io.StringIO()
    render(Ui(buf, mode="plain", color=False, unicode=False), version=version)
    return buf.getvalue()
