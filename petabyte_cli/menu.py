"""menu — what `petabyte` alone does in a terminal: ask what you want to do, then do it.

    ⚡ Petabyte

    What do you want to do?

    BUY
      1  Rent compute
      2  Find a GPU
      3  Run inference
      4  Manage my jobs
    SELL
      5  Install the seller agent
      ...

Each entry maps to a normal command line, so the menu never has behaviour of its own.
"""
from __future__ import annotations

from collections.abc import Callable

ITEMS: list[tuple[str, list[tuple[str, list[str]]]]] = [
    ("BUY", [
        # Every entry must DO something on its own — entries that duplicated each other, or
        # that only printed a command's --help, made the menu feel broken.
        ("Find a GPU to rent", ["specs"]),
        ("Manage my jobs", ["jobs"]),
        ("My wallet", ["wallet"]),
    ]),
    ("SELL", [
        ("Install the seller agent", ["--install-agent"]),
        ("Start the seller agent", ["--run-agent"]),
        ("Monitor my agent", ["agent", "status"]),
        ("View earnings", ["earnings"]),
    ]),
    ("ACCOUNT", [
        ("My profile / dashboard", ["--me"]),
        ("Wallet", ["wallet"]),
        ("Activity", ["activity"]),
        ("Doctor (diagnose problems)", ["doctor"]),
    ]),
]


def run(ui, dispatch: Callable[[list[str]], int]) -> int:
    ui.brand()
    ui.line("What do you want to do?")
    flat: list[list[str]] = []
    n = 0
    for group, entries in ITEMS:
        ui.section(group)
        for label, argv in entries:
            n += 1
            flat.append(argv)
            if ui.mode == "rich":
                ui.console.print(f"  [bold color(44)]{n:>2}[/]  {ui.escape(label)}")
            else:
                ui.line(f"  {n:>2}  {label}")
    ui.blank()
    ui.note("  q  quit          (tip: petabyte --help lists every command)")
    ui.blank()
    if not ui.interactive:
        ui.info("Run `petabyte --help` for the command list, or `petabyte --me` for your dashboard.")
        return 0
    while True:
        try:
            raw = input("Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ui.blank()
            return 0
        if raw in ("q", "quit", "exit", ""):
            return 0
        if raw.isdigit() and 1 <= int(raw) <= len(flat):
            argv = flat[int(raw) - 1]
            ui.blank()
            ui.note("$ petabyte " + " ".join(argv))
            ui.blank()
            return dispatch(argv)
        ui.warn(f"Enter a number between 1 and {len(flat)}, or q.")
