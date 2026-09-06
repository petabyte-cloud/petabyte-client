"""ssh_cmds — `petabyte ssh`, the guided setup that makes THIS computer able to reach your VMs.

    petabyte ssh                 the 5-step wizard (key → account → ssh config → verify)
    petabyte ssh <vm-id>         set up if needed, then connect
    petabyte ssh --status        what is configured on this machine
    petabyte ssh --print <vm>    just print the command to run

Same shape as `petabyte --install-agent`: check the machine, explain what will be written, ask,
do it, verify, then say exactly what to run next. It only touches files under ~/.ssh, writes its
config inside a block it owns, and never reads, prints or uploads a private key.
"""
from __future__ import annotations

import os
import subprocess
from typing import Callable

from . import api as apimod
from . import errors, sysinfo
from . import ssh_setup as S

TOTAL_STEPS = 5


def _step(ui, n: int, text: str) -> None:
    ui.blank()
    if ui.mode == "rich":
        ui.console.print(f"[bold]Step {n}/{TOTAL_STEPS}[/]  {ui.escape(text)}")
    else:
        ui.raw(f"Step {n}/{TOTAL_STEPS}  {text}")


# ------------------------------------------------------------------ status
def status_rows(st: S.SshState) -> list[tuple[str, object]]:
    rows: list[tuple[str, object]] = [("VM domain", f"*.{st.zone}")]
    rows.append(("SSH client", st.client or ("status", "missing")))
    if st.chosen:
        rows.append(("Key", st.chosen.label))
        prot = S.private_key_is_protected(st.chosen.priv_path)
        if prot is False:
            rows.append(("Key file", ("status", "world-readable — chmod 600 it")))
    else:
        rows.append(("Key", ("status", "none on this machine")))
    rows.append(("Registered with Petabyte", ("status", "yes") if st.registered else ("status", "no")))
    rows.append(("Keys on the account", str(len(st.account))))
    rows.append(("SSH config", ("status", "configured") if st.configured
                 else ("dim", f"no managed block in {S.config_path()}")))
    return rows


def cmd_status(ui, cfg, client_factory, *, json_mode: bool = False) -> int:
    st = S.detect(cfg)
    try:
        with client_factory(cfg) as c:
            st.zone = S.vm_zone(cfg, c)
            st.configured = S.config_matches(st.zone, st.chosen.priv_path if st.chosen else None)
            st.account = S.account_keys(c)
        st.registered = bool(st.chosen and any(S.same_key(st.chosen.line, k) for k in st.account))
    except errors.CliError as e:
        if json_mode:
            raise
        ui.warn(f"could not read the keys on your account: {e.title}")
    if json_mode:
        import json

        print(json.dumps(st.as_dict()))
        return 0
    ui.brand("SSH access")
    ui.kv(status_rows(st), title=None)
    if not (st.registered and st.configured):
        ui.command("petabyte ssh", caption="Finish the setup:")
    else:
        ui.command(f"ssh root@<vm-id>.{st.zone}", caption="Connect to a VM:")
    return 0


# ------------------------------------------------------------------ the wizard
def _choose_key(ui, st: S.SshState, *, yes: bool, new_key: bool, use_key: str | None,
                passphrase: bool) -> S.LocalKey:
    if use_key:
        path = os.path.expanduser(use_key)
        if not path.endswith(".pub") and os.path.isfile(path + ".pub"):
            path += ".pub"
        k = S._read_pub(path)
        if not k:
            raise errors.CliError(f"{path} is not an SSH public key.",
                                  fix="Point --key at the .pub file, e.g. ~/.ssh/id_ed25519.pub. "
                                      "Never the private key — Petabyte does not want it and "
                                      "would reject it.")
        if not os.path.isfile(k.priv_path):
            raise errors.CliError(f"the private half of {os.path.basename(path)} is missing.",
                                  reason=f"expected {k.priv_path}",
                                  fix="A public key on its own cannot log in.")
        return k
    if st.keys and not new_key:
        # --yes means "ask me nothing", however many keys are lying around: take the best
        # candidate (discover_keys puts ours, then the conventional names, first) instead of
        # blocking on a picker that a real terminal would sit at.
        if yes or not ui.interactive:
            return st.keys[0]
        options = [k.label for k in st.keys] + ["Create a new key just for Petabyte"]
        ui.line("Which SSH key should Petabyte use?")
        pick = ui.choose("Choice", options, default=1)
        if pick <= len(st.keys):
            return st.keys[pick - 1]
    pw = None
    if passphrase and ui.interactive:
        pw = ui.ask("Passphrase for the new key (leave empty for none)", secret=True) or ""
    ui.step(f"Creating {S.managed_key_path()}")
    k = S.generate_key(passphrase=pw)
    ui.ok(f"Created a new key  ({k.key_type}{', ' + k.fp if k.fp else ''})")
    if not pw:
        ui.note("  No passphrase — anyone with this file can log into your VMs. Keep the machine "
                "locked, or re-run with --passphrase.")
    return k


def cmd_setup(ui, cfg, client_factory, *, yes: bool = False, new_key: bool = False,
              use_key: str | None = None, passphrase: bool = False, dry_run: bool = False,
              user: str = "root", connect_vm: str | None = None,
              login: Callable[[], None] | None = None) -> int:
    ui.brand("SSH access")
    ui.line("Let's set this computer up to reach your VMs.")

    # ---- 1. this machine ---------------------------------------------------------------
    _step(ui, 1, "Checking this computer…")
    st = S.detect(cfg)
    if not st.client:
        ui.error("No SSH client found on this computer.",
                 reason="`ssh` is not on your PATH.", fix=S._install_openssh_hint())
        return 1
    ui.ok(f"SSH client: {st.client}")
    ui.ok(sysinfo.os_summary())
    if st.keys:
        ui.ok(f"{len(st.keys)} SSH key(s) already on this machine")
    else:
        ui.info("No SSH key on this machine yet — one will be created.")

    # ---- 2. account --------------------------------------------------------------------
    _step(ui, 2, "Authentication")
    try:
        with client_factory(cfg) as c:
            me = apimod.fetch_me(c, str(cfg.get("api_url") or "")).data or {}
            st.account = S.account_keys(c)
    except errors.NotSignedIn:
        if login is not None and ui.interactive:
            ui.info("You're not signed in yet — opening the browser sign-in.")
            login()
            with client_factory(cfg) as c:
                me = apimod.fetch_me(c, str(cfg.get("api_url") or "")).data or {}
                st.account = S.account_keys(c)
        else:
            raise
    ui.ok(f"Petabyte account detected: {me.get('username')}")
    with client_factory(cfg) as c:
        zone = S.vm_zone(cfg, c)
    ui.ok(f"VM domain: {zone}")
    ui.note(f"  {len(st.account)} SSH key(s) already registered on the account"
            if st.account else "  no SSH keys registered on the account yet")

    # ---- 3. key ------------------------------------------------------------------------
    _step(ui, 3, "SSH key")
    key = _choose_key(ui, st, yes=yes, new_key=new_key, use_key=use_key, passphrase=passphrase)
    st.chosen = key
    ui.ok(f"Using {key.label}")
    prot = S.private_key_is_protected(key.priv_path)
    if prot is False:
        ui.warn(f"{key.priv_path} is readable by other users — run: chmod 600 {key.priv_path}")
    already = any(S.same_key(key.line, k) for k in st.account)
    ui.note("  Only the PUBLIC half is sent. The private key never leaves this computer.")

    # ---- 4. register + configure --------------------------------------------------------
    _step(ui, 4, "Registering and configuring")
    identity = key.priv_path
    cfg_needed = not S.config_matches(zone, identity, user)
    ui.line("This will:")
    if already:
        ui.line(f"  {ui.icon('bullet')} leave your account's SSH keys unchanged (this one is already registered)")
    else:
        ui.line(f"  {ui.icon('bullet')} add this public key to your Petabyte account "
                f"(keeping the {len(st.account)} already there)")
    if cfg_needed:
        ui.line(f"  {ui.icon('bullet')} write a managed block in {S.config_path()} for *.{zone} "
                f"(user {user}, this key, its own known_hosts)")
    else:
        ui.line(f"  {ui.icon('bullet')} leave {S.config_path()} unchanged (already configured)")
    if dry_run:
        ui.blank()
        ui.info("Dry run — nothing was written.")
        return 0
    if not (already and not cfg_needed) and not (yes or ui.confirm("Continue?", default=True)):
        ui.info("Cancelled — nothing was written.")
        return 0

    if already:
        ui.ok("Public key already registered with Petabyte")
        st.registered = True
    else:
        with client_factory(cfg) as c:
            changed, after = S.register_key(c, key.line, st.account)
        st.account = after
        st.registered = True
        ui.ok(f"Public key registered  ({len(after)} key(s) on the account)")
    if cfg_needed:
        path = S.write_config(zone, identity, user)
        ui.ok(f"SSH config updated  ({path})")
    else:
        ui.ok("SSH config already in place")
    st.configured = True

    # ---- 5. verify ----------------------------------------------------------------------
    _step(ui, 5, "Verifying")
    with client_factory(cfg) as c:
        vms = S.running_vms(c)
    ui.blank()
    if not vms:
        ui.ok("Setup complete.")
        ui.note("You have no running VMs to test against right now.")
        ui.command("petabyte launch ollama --hours 1", caption="Rent one, then connect:")
        ui.command(f"ssh {user}@<vm-id>.{zone}")
        _injection_note(ui)
        return 0
    target = connect_vm or str(vms[0].get("vm_id"))
    host = S.host_for(target, zone)
    ui.step(f"Trying {user}@{host} …")
    good, kind, why = S.probe(f"{user}@{host}")
    if good:
        ui.ok(f"Connected to {host}")
    else:
        ui.warn(f"Could not open a session yet: {why}")
        _why_not(ui, vms, target, user, zone, kind)
    ui.blank()
    ui.command(f"ssh {user}@{host}", caption="Connect with:")
    _injection_note(ui)
    if connect_vm:
        return connect(ui, cfg, target, user=user, client_factory=client_factory)
    return 0


def _injection_note(ui) -> None:
    """What this computer is now ready for — and what still has to happen on the platform side.

    Being straight about this is the whole point: the local half (a key, registered, with the
    client configured) is real and done. Direct SSH to a VM additionally needs the platform to
    publish the VM domain and route the connection to the machine running your VM, and to write
    your key into that VM. Claiming otherwise would send people to debug their own laptop."""
    ui.blank()
    ui.note("This computer is now ready: it has a key, Petabyte has the public half, and your SSH "
            "client knows the VM domain.")
    ui.note("Your key is recorded against a VM when the VM is CREATED, so it applies to VMs you "
            "launch from now on — not to one already running. Writing it into the VM itself is "
            "done by the platform, not by this command.")
    ui.note("If a connection is refused, it is the platform side, not your setup: direct SSH to a "
            "VM needs the VM gateway AND the agent-side key injection, and that platform half is "
            "not serving on every deployment yet. Use the console for a VM that is running, and "
            "`petabyte ssh --status` to confirm this computer's half is in place.")


def _why_not(ui, vms: list[dict], target: str, user: str, zone: str, kind: str) -> None:
    vm = next((v for v in vms if str(v.get("vm_id")) == target), None)
    hints: list[str] = []
    if vm and str(vm.get("status", "")).lower() in ("starting", "created"):
        hints.append("The VM is still starting — give it a minute and try again.")
    if kind == S.PROBE_DNS:
        hints.append(f"{S.host_for(target, zone)} does not resolve. That name is published by "
                     "Petabyte, not by you — nothing on this computer can fix it. Your setup is "
                     "still correct and will work once the VM gateway is serving.")
    elif kind == S.PROBE_AUTH:
        hints.append("The server answered but refused the key. If this VM was launched BEFORE you "
                     "registered this key, it does not have it — launch a new one.")
    else:
        hints.append("The name resolves but nothing accepted the connection — the VM gateway is "
                     "not routing to this VM yet.")
        hints.append(f"Check it yourself:  nslookup {S.host_for(target, zone)}")
    for h in hints:
        ui.line(f"  {ui.icon('bullet')} {h}")


# ------------------------------------------------------------------ connect
def connect(ui, cfg, vm_id: str | None, *, user: str = "root", print_only: bool = False,
            client_factory=None) -> int:
    """Hand the terminal to ssh. Resolves a missing id to the only running VM."""
    zone = S.vm_zone(cfg)
    host_override = None
    vms: list[dict] = []
    if client_factory is not None:
        with client_factory(cfg) as c:
            vms = S.running_vms(c)
            zone = S.vm_zone(cfg, c)
        if not vm_id:
            if not vms:
                ui.error("You have no running VMs to connect to.",
                         fix="Rent one first:", run="petabyte launch ollama --hours 1")
                return 1
            if len(vms) > 1 and ui.interactive and not print_only:
                ui.line("Which VM?")
                labels = [f"{v.get('vm_id')}  ({v.get('template') or 'VM'}, {v.get('status')})" for v in vms]
                vm_id = str(vms[ui.choose("Choice", labels, default=1) - 1].get("vm_id"))
            else:
                vm_id = str(vms[0].get("vm_id"))
        # Prefer the address the API itself reports for this VM over anything we construct — and
        # do it AFTER the id is settled, or an auto-selected VM silently misses the override.
        for v in vms:
            url = v.get("url")
            if str(v.get("vm_id")) == vm_id and isinstance(url, dict) and url.get("hostname"):
                host_override = str(url["hostname"])
    if not vm_id:
        ui.error("Which VM? Pass its id.", run=f"petabyte ssh <vm-id>")
        return 1
    st = S.detect(cfg)
    st.zone = zone
    st.configured = S.config_matches(zone, st.chosen.priv_path if st.chosen else None)
    argv = S.ssh_command(vm_id, zone, user=user, configured=st.configured,
                         identity=st.chosen.priv_path if st.chosen else None)
    if host_override:
        argv[-1] = f"{user}@{host_override}"
    if print_only:
        ui.raw(" ".join(argv))
        return 0
    if not st.client:
        ui.error("No SSH client found on this computer.", fix=S._install_openssh_hint())
        return 1
    if not st.configured or not st.chosen:
        ui.warn("This computer is not set up for Petabyte SSH yet — run `petabyte ssh` first.")
    ui.note("$ " + " ".join(argv))
    try:
        return int(subprocess.call(argv))
    except KeyboardInterrupt:
        ui.blank()
        return 130
    except OSError as e:
        ui.error("Could not start ssh.", detail=str(e))
        return 1
