"""ssh_setup — `petabyte ssh`: make THIS computer able to reach your Petabyte VMs.

A rented VM is addressed by a stable per-VM hostname — `<vm-id>.<VM_DNS_ZONE>`, which is server
config and therefore READ from the API rather than guessed — and is meant to accept the SSH public
key stored on your Petabyte account. So "set up SSH" means four small things, and people get at
least one of them wrong every time:

  1. have an SSH key on this machine (and NOT accidentally hand over the private half);
  2. register its PUBLIC half with Petabyte — *added to* the keys already on the account, never
     replacing them, so setting up a second laptop does not lock the first one out;
  3. teach the local SSH client about the VM domain, so `ssh <id>.vm.petabyte.market` works with
     no flags: the right identity, the right default user, and no host-key prompt churn as
     failover moves a VM between machines;
  4. check it actually works.

That is what this module does, in the same guided shape as `petabyte --install-agent`. It only
ever touches files under ~/.ssh, it writes its SSH config inside a marked block it owns (so
re-running is idempotent and a hand-edited config is never clobbered), and the private key never
leaves the machine — it is not read, printed, or uploaded.

Two things this cannot fix, and says so instead rather than sending someone to debug their laptop:

  * your key is RECORDED against a VM when the VM is CREATED, so registering it now covers VMs
    you launch from now on, not one already running — and writing it into the VM's authorized_keys
    is platform-side work that this command cannot do and does not claim to;
  * reaching a VM also needs the platform side — the VM domain published in DNS and a gateway
    routing the connection to the machine hosting your VM. That is not serving on every
    deployment yet, so the wizard probes, and when the name does not resolve it says the address
    is published by Petabyte and the local setup is still correct.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from typing import Any

from . import api as apimod
from . import errors, sysinfo

# The VM domain is server-side config (VM_DNS_ZONE, falling back to BASE_DOMAIN), and it is NOT
# guessable: production currently leaves VM_DNS_ZONE empty, so a VM answers at
# `<id>.petabyte.market`, not `<id>.vm.petabyte.market`. So we read it off a real VM's hostname
# whenever the account has one, and only fall back to the API's own domain.
DEFAULT_VM_ZONE = "petabyte.market"
VM_ID_LEN = 12                              # db._rand_vm_id: [a-z][a-z0-9]{11}
KEY_NAME = "petabyte_ed25519"
BLOCK_BEGIN = "# >>> petabyte ssh (managed) >>>"
BLOCK_END = "# <<< petabyte ssh (managed) <<<"
MAX_ACCOUNT_KEYS = 10                       # db.validate_ssh_pubkey caps at 10
PUBKEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com",
                "sk-ecdsa-sha2-nistp256@openssh.com")
_PUBKEY_RE = re.compile(r"^(" + "|".join(re.escape(t) for t in PUBKEY_TYPES) + r")\s+([A-Za-z0-9+/=]+)(\s+.*)?$")


# ------------------------------------------------------------------ local key material
def ssh_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".ssh")


def config_path() -> str:
    return os.path.join(ssh_dir(), "config")


def managed_key_path() -> str:
    return os.path.join(ssh_dir(), KEY_NAME)


def parse_pubkey(text: str) -> tuple[str, str, str] | None:
    """(type, body, comment) for one public-key line, or None. Also the guard that stops a
    private key being treated as public — the halves live next to each other and are one
    tab-completion apart."""
    line = (text or "").strip()
    if not line or line.startswith("#"):
        return None
    m = _PUBKEY_RE.match(line)
    if not m:
        return None
    return m.group(1), m.group(2), (m.group(3) or "").strip()


def fingerprint(pub_path: str) -> str | None:
    """The SHA256 fingerprint ssh-keygen prints, for showing the user WHICH key we mean."""
    kg = sysinfo.which("ssh-keygen")
    if not kg:
        return None
    rc, out, _ = sysinfo.run([kg, "-lf", pub_path], timeout=6)
    if rc != 0 or not out.strip():
        return None
    parts = out.split()
    return parts[1] if len(parts) > 1 else out.strip()[:60]


@dataclass
class LocalKey:
    pub_path: str
    priv_path: str
    line: str                                # the full public-key line
    comment: str = ""
    fp: str | None = None
    managed: bool = False                    # created by this command

    @property
    def key_type(self) -> str:
        p = parse_pubkey(self.line)
        return p[0] if p else "?"

    @property
    def body(self) -> str:
        p = parse_pubkey(self.line)
        return p[1] if p else ""

    @property
    def label(self) -> str:
        base = os.path.basename(self.pub_path)
        return f"{base}  ({self.key_type}{', ' + self.fp if self.fp else ''})"


def _read_pub(path: str) -> LocalKey | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                p = parse_pubkey(raw)
                if p:
                    priv = path[:-4] if path.endswith(".pub") else path
                    return LocalKey(pub_path=path, priv_path=priv, line=raw.strip(), comment=p[2],
                                    fp=fingerprint(path),
                                    managed=os.path.basename(priv) == KEY_NAME)
    except OSError:
        return None
    return None


def discover_keys() -> list[LocalKey]:
    """Public keys already on this machine, ours first, then the conventional names. Only keys
    whose PRIVATE half is present are offered — a public key alone cannot log in."""
    d = ssh_dir()
    preferred = [KEY_NAME + ".pub", "id_ed25519.pub", "id_ecdsa_sk.pub", "id_ed25519_sk.pub",
                 "id_ecdsa.pub", "id_rsa.pub"]
    seen: list[LocalKey] = []
    try:
        present = set(os.listdir(d))
    except OSError:
        return []
    for name in preferred + sorted(n for n in present if n.endswith(".pub")):
        if name not in present or any(k.pub_path.endswith(os.sep + name) for k in seen):
            continue
        k = _read_pub(os.path.join(d, name))
        if k and os.path.isfile(k.priv_path):
            seen.append(k)
    return seen


def generate_key(*, passphrase: str | None = None, comment: str | None = None) -> LocalKey:
    """Create a dedicated ed25519 key for Petabyte with ssh-keygen (which gets the file modes
    right on every platform, including the Windows ACLs OpenSSH insists on)."""
    kg = sysinfo.which("ssh-keygen")
    if not kg:
        raise errors.CliError("ssh-keygen was not found on this computer.",
                              reason="It ships with OpenSSH, which Petabyte needs to make a key.",
                              fix=_install_openssh_hint())
    os.makedirs(ssh_dir(), 0o700, exist_ok=True)     # created 0700, not widened-then-chmod'd
    try:
        os.chmod(ssh_dir(), 0o700)
    except OSError:
        pass
    priv = managed_key_path()
    if os.path.exists(priv) or os.path.exists(priv + ".pub"):
        raise errors.CliError(f"{priv} already exists.",
                              fix="Re-run and choose that key instead of making a new one, or move "
                                  "the old one aside first.")
    cmd = [kg, "-t", "ed25519", "-f", priv, "-C", comment or _default_comment(),
           "-N", passphrase or ""]
    rc, _, err = sysinfo.run(cmd, timeout=60)
    if rc != 0 or not os.path.isfile(priv + ".pub"):
        raise errors.CliError("ssh-keygen could not create the key.",
                              reason=(err or "").strip()[-200:] or None,
                              fix=f"Try it by hand: ssh-keygen -t ed25519 -f {priv}")
    k = _read_pub(priv + ".pub")
    if not k:
        raise errors.CliError("the generated key could not be read back.")
    return k


def _default_comment() -> str:
    import getpass
    import socket

    try:
        return f"{getpass.getuser()}@{socket.gethostname()}-petabyte"
    except Exception:
        return "petabyte"


def _install_openssh_hint() -> list[str]:
    if sysinfo.is_windows():
        return ["Windows 10/11: Settings → System → Optional features → Add → 'OpenSSH Client'.",
                "Or in an elevated PowerShell: Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"]
    if sysinfo.is_macos():
        return ["macOS ships OpenSSH; if it is missing, install the Xcode command line tools: xcode-select --install"]
    return ["Debian/Ubuntu: sudo apt install openssh-client",
            "Fedora/RHEL:   sudo dnf install openssh-clients"]


def private_key_is_protected(priv_path: str) -> bool | None:
    """True/False when we can tell whether the private key is 0600-ish. None on Windows, where
    OpenSSH uses ACLs rather than mode bits."""
    if sysinfo.is_windows():
        return None
    try:
        mode = stat.S_IMODE(os.stat(priv_path).st_mode)
    except OSError:
        return None
    return not (mode & 0o077)


# ------------------------------------------------------------------ the account side
def account_keys(client) -> list[str]:
    """The public-key lines currently on the Petabyte account."""
    p = apimod._get(client, "/account/ssh-key")
    if not p.ok:
        raise errors.CliError("Could not read the SSH keys on your account.",
                              reason=p.error, run=errors.LOGIN if p.status == 401 else None)
    raw = p.get("ssh_pubkey") or ""
    return [ln.strip() for ln in raw.split("\n") if ln.strip()]


def same_key(a: str, b: str) -> bool:
    """Two lines are the same key when the type and body match — the comment is free text and
    differs between machines."""
    pa, pb = parse_pubkey(a), parse_pubkey(b)
    return bool(pa and pb and pa[0] == pb[0] and pa[1] == pb[1])


def register_key(client, key_line: str, existing: list[str]) -> tuple[bool, list[str]]:
    """Add `key_line` to the account's keys. Returns (changed, keys_after).

    The endpoint REPLACES the whole set, so this merges: setting up a second computer must not
    silently lock the first one out of every VM it launches."""
    if any(same_key(key_line, k) for k in existing):
        return False, existing
    merged = existing + [key_line]
    if len(merged) > MAX_ACCOUNT_KEYS:
        raise errors.CliError(f"Your account already has {len(existing)} SSH keys "
                              f"(the limit is {MAX_ACCOUNT_KEYS}).",
                              fix="Remove one you no longer use on the web, under Account → "
                                  "Security, then run this again.")
    r = client.post("/account/ssh-key", json={"public_key": "\n".join(merged)})
    if r.status_code != 200:
        raise errors.from_response(r, "save your SSH key")
    after = [ln.strip() for ln in ((r.json() or {}).get("ssh_pubkey") or "").split("\n") if ln.strip()]
    return True, after


# ------------------------------------------------------------------ the ssh client side
def config_block(zone: str, identity: str | None, user: str = "root") -> str:
    """The managed ~/.ssh/config stanza.

    `StrictHostKeyChecking accept-new` + a per-zone known_hosts: a VM is ephemeral and failover
    legitimately moves it to another machine, so the host key CHANGES for the same name. With the
    default settings that reads as a man-in-the-middle warning and blocks the login. Keeping these
    hosts in their own file means a real warning about your other servers still means something."""
    kh = os.path.join(ssh_dir(), "known_hosts_petabyte")
    lines = [BLOCK_BEGIN,
             "# Written by `petabyte ssh`. Edit outside this block; re-running rewrites it.",
             f"# VM domain: {zone} (from the API — server config, see VM_DNS_ZONE).",
             f"Host {config_host_pattern(zone)}",
             f"    User {user}",
             "    StrictHostKeyChecking accept-new",
             f"    UserKnownHostsFile {kh}",
             "    ServerAliveInterval 30",
             "    ServerAliveCountMax 6"]
    if identity:
        lines += [f"    IdentityFile {identity}",
                  "    IdentitiesOnly yes"]
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


def read_config() -> str:
    try:
        with open(config_path(), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def strip_block(text: str) -> str:
    """Remove EVERY complete managed block; everything else survives byte for byte.

    Anchored on BLOCK_END rather than BLOCK_BEGIN, because a truncated or hand-edited config can
    leave a BEGIN marker with no END after it. Cutting from BEGIN to end-of-file there would
    silently delete the user's own `Host` entries below it — the one thing this module promises
    not to do. With no terminated block to remove we return the text untouched and append a fresh
    block instead; searching backwards from the END for its BEGIN also means a stale unterminated
    marker further up is skipped over rather than used as the cut point.

    Every complete block goes, not just the first: OpenSSH takes the FIRST value it obtains for a
    keyword, so one leftover older block above the new one silently keeps deciding User/IdentityFile
    while the config looks freshly written. Duplicates are exactly what this function is called to
    prevent, and removing one of two is how a config ends up with them. Unterminated fragments are
    still preserved, and a stray END with no BEGIN is stepped over rather than treated as the end of
    a block we own — otherwise it would hide every real block below it from this cleanup.
    """
    pos = 0
    while True:
        end = text.find(BLOCK_END, pos)
        if end < 0:
            return text                              # nothing terminated left to remove
        begin = text.rfind(BLOCK_BEGIN, pos, end)
        if begin < 0:                                # a stray END marker: not ours to interpret
            pos = end + len(BLOCK_END)
            continue
        head, tail = text[:begin], text[end + len(BLOCK_END):]
        head = head.rstrip("\n") + ("\n" if head.strip() else "")
        text, pos = head + tail.lstrip("\n"), len(head)


def config_matches(zone: str, identity: str | None, user: str = "root") -> bool:
    cur = read_config()
    return BLOCK_BEGIN in cur and config_block(zone, identity, user).strip() in cur


def write_config(zone: str, identity: str | None, user: str = "root") -> str:
    """Replace (or append) our managed block. Everything outside it is preserved byte for byte."""
    os.makedirs(ssh_dir(), 0o700, exist_ok=True)     # created 0700, not widened-then-chmod'd
    try:
        os.chmod(ssh_dir(), 0o700)
    except OSError:
        pass
    path = config_path()
    existing = read_config()
    body = strip_block(existing).rstrip("\n")
    new = ((body + "\n\n") if body else "") + config_block(zone, identity, user)
    tmp = path + ".petabyte.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    if existing and not os.path.exists(path + ".petabyte.bak"):
        try:
            shutil.copy2(path, path + ".petabyte.bak")     # one-time safety copy of a hand-written config
        except OSError:
            pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ------------------------------------------------------------------ state
@dataclass
class SshState:
    zone: str = DEFAULT_VM_ZONE
    client: str | None = None                # path to the ssh binary
    keys: list[LocalKey] = field(default_factory=list)
    chosen: LocalKey | None = None
    account: list[str] = field(default_factory=list)
    registered: bool = False
    configured: bool = False

    def as_dict(self) -> dict[str, Any]:
        # Public keys and paths only — never the private key, never its contents.
        return {"zone": self.zone, "ssh_client": self.client,
                "local_keys": [{"path": k.pub_path, "type": k.key_type, "fingerprint": k.fp}
                               for k in self.keys],
                "using": self.chosen.pub_path if self.chosen else None,
                "account_key_count": len(self.account),
                "registered": self.registered, "configured": self.configured}


def zone_from_hostname(hostname: str) -> str:
    """`q7bk2mrelpza.vm.petabyte.market` -> `vm.petabyte.market`. The VM id is the first label."""
    parts = (hostname or "").strip().strip(".").split(".")
    return ".".join(parts[1:]) if len(parts) > 2 else (hostname or "").strip()


def api_domain(cfg: dict) -> str:
    """The API's own domain, used only as a last resort for the VM zone."""
    try:
        from urllib.parse import urlparse

        host = (urlparse(str(cfg.get("api_url") or "")).hostname or "").lower()
    except Exception:
        host = ""
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return DEFAULT_VM_ZONE
    return host[4:] if host.startswith("api.") else host


def vm_zone(cfg: dict, client=None) -> str:
    """The domain your VMs answer on.

    Precedence: PETABYTE_VM_ZONE > the hostname the API reports for a real VM of yours > the
    API's own domain. The middle one matters — the zone is server config we cannot infer, and
    guessing it produces an ~/.ssh/config block for a domain that does not exist."""
    env = (os.getenv("PETABYTE_VM_ZONE") or "").strip()
    if env:
        return env.lstrip(".")
    if client is not None:
        try:
            for v in (running_vms(client) or []):
                url = v.get("url")
                host = url.get("hostname") if isinstance(url, dict) else None
                if host:
                    return zone_from_hostname(str(host))
        except Exception:
            pass
    return api_domain(cfg)


def config_host_pattern(zone: str) -> str:
    """The `Host` pattern for the managed block.

    A bare `*.petabyte.market` would also capture `api.` and `www.`, forcing our identity and
    User root on hosts that are not VMs. VM ids are exactly 12 characters, so match that shape
    instead. Only a zone whose first label SAYS it is VMs-only gets the readable wildcard: label
    count proves nothing, since a self-hosted `pb.example.test` has three labels and still has an
    `api.` and a `www.` of its own."""
    if zone.split(".")[0] in ("vm", "vms"):
        return f"*.{zone}"
    return "?" * VM_ID_LEN + f".{zone}"


def host_for(vm_id: str, zone: str) -> str:
    return f"{vm_id}.{zone}"


def ssh_command(vm_id: str, zone: str, user: str = "root", configured: bool = True,
                identity: str | None = None) -> list[str]:
    """The argv to reach a VM. With our config block in place this is just `ssh user@host`; without
    it, spell out the identity and host-key options so the command still works standalone."""
    host = host_for(vm_id, zone)
    if configured:
        return ["ssh", f"{user}@{host}"]
    argv = ["ssh"]
    if identity:
        argv += ["-i", identity, "-o", "IdentitiesOnly=yes"]
    argv += ["-o", "StrictHostKeyChecking=accept-new",
             "-o", f"UserKnownHostsFile={os.path.join(ssh_dir(), 'known_hosts_petabyte')}",
             f"{user}@{host}"]
    return argv


def detect(cfg: dict) -> SshState:
    st = SshState(zone=vm_zone(cfg), client=sysinfo.which("ssh"))
    st.keys = discover_keys()
    st.chosen = next((k for k in st.keys if k.managed), st.keys[0] if st.keys else None)
    st.configured = config_matches(st.zone, st.chosen.priv_path if st.chosen else None)
    return st


def resolves(hostname: str) -> bool:
    """Does the VM hostname resolve at all? Separating this from the connection is what turns
    'it didn't work' into 'the platform does not publish this name yet'."""
    import socket

    try:
        socket.getaddrinfo(hostname, None)
        return True
    except Exception:
        return False


# Why a probe failed, in the order we can distinguish it. `kind` drives the advice.
PROBE_DNS = "dns"                 # the name does not resolve — nothing is published for it
PROBE_NET = "unreachable"         # resolves, but nothing accepted a connection
PROBE_AUTH = "auth"               # SSH answered and refused the key
PROBE_OK = "ok"


def probe(host: str, *, timeout: int = 10) -> tuple[bool, str, str]:
    """Try to open a session. Returns (ok, kind, detail). BatchMode so a missing key fails fast
    instead of hanging on a password prompt."""
    ssh = sysinfo.which("ssh")
    if not ssh:
        return False, PROBE_NET, "no ssh client"
    hostname = host.split("@", 1)[-1]
    if not resolves(hostname):
        return False, PROBE_DNS, f"{hostname} does not resolve"
    rc, out, err = sysinfo.run(
        [ssh, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
         "-o", "StrictHostKeyChecking=accept-new",
         "-o", f"UserKnownHostsFile={os.path.join(ssh_dir(), 'known_hosts_petabyte')}",
         host, "true"], timeout=timeout + 10)
    if rc == 0:
        return True, PROBE_OK, "connected"
    blob = ((err or "") + (out or ""))
    text = blob.strip().splitlines()
    detail = text[-1][:160] if text else f"exit {rc}"
    low = blob.lower()
    if "permission denied" in low or "too many authentication failures" in low:
        return False, PROBE_AUTH, detail
    return False, PROBE_NET, detail


def running_vms(client) -> list[dict]:
    p = apimod._get(client, "/vms")
    vms = (p.get("vms") or []) if p.ok else []
    return [v for v in vms if str(v.get("status", "")).lower() in apimod.RUNNING_VM_STATES]
