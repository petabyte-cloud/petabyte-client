#!/usr/bin/env python3
"""Petabyte CLI — rent verified GPUs, or earn by selling yours.

  petabyte login              sign in with your browser (no password on the CLI)
  petabyte --me               your dashboard: account, wallet, what's running, agent, system
  petabyte --install-agent    guided seller setup       petabyte --run-agent / --kill-agent
  petabyte specs              rentable GPUs             petabyte launch ollama --hours 2
  petabyte run notebook.ipynb --gpu H100 --hours 1      petabyte doctor

Authentication: `petabyte login` (browser device flow) saves a session token in
~/.petabyte/cli.json (0600); or export an 'account'-scoped API key as PETABYTE_API_KEY.
The product layer (rich UI, dashboard, agent wizard, doctor, version check) lives in the
sibling `petabyte_cli` package; the classic commands below work without it.
"""
import argparse
import json
import os
import os as _os
import sys
import time

import httpx

# The model hub (discover/pull/manage models) lives in the sibling `modelhub` package. Make it
# importable whether the CLI is run as `python cli/petabyte.py` or installed as `petabyte`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from modelhub import cli as mh_cli
except Exception:
    mh_cli = None

# The product layer (dashboard, agent wizard, doctor, rich UI). Importable both from a source
# checkout (lumaris_api/cli/petabyte_cli) and from the wheel (top-level package).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import petabyte_cli
    from petabyte_cli import errors as _errors
    from petabyte_cli import ui as _ui
    from petabyte_cli import version_check as _vc
except Exception:
    petabyte_cli = None
    _errors = _ui = _vc = None

JSON = False            # --json: machine-readable output where a command supports it
VERBOSE = False         # --verbose: full technical detail on errors
_UPDATE = None          # UpdateInfo from the startup check (or None)

_TTY = hasattr(__import__("sys").stdout, "isatty") and __import__("sys").stdout.isatty() and not _os.getenv("NO_COLOR")
def _c(txt, code):
    return f"\033[{code}m{txt}\033[0m" if _TTY else txt
def _amber(t): return _c(t, "38;5;214")
def _cyan(t): return _c(t, "38;5;44")
def _green(t): return _c(t, "38;5;42")
def _dim(t): return _c(t, "2")
def _bold(t): return _c(t, "1")

CONFIG = os.getenv("PETABYTE_CONFIG") or os.path.expanduser("~/.petabyte/cli.json")
# Default to the production service so a fresh `pip install petabyte-client` just works
# (`petabyte login` hits the real API). Point at test/local with $PETABYTE_API_URL or --api,
# e.g. PETABYTE_API_URL=http://localhost:8000 for a dev server.
DEFAULT_API = os.getenv("PETABYTE_API_URL", "https://petabyte.market")


def _cfg():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    # Precedence: --api > $PETABYTE_API_URL > the saved file > production. An explicit environment
    # variable must win over a file saved months ago, or "point at my dev server" silently fails.
    if os.getenv("PETABYTE_API_URL"):
        cfg["api_url"] = os.environ["PETABYTE_API_URL"]
    cfg.setdefault("api_url", DEFAULT_API)   # a hand-edited file without api_url must not crash
    return cfg


def _save(cfg):
    d = os.path.dirname(CONFIG)
    if d:                                   # a bare filename (e.g. PETABYTE_CONFIG=cli.json) has no dir
        os.makedirs(d, exist_ok=True)
    # The file holds your sign-in token: create it private (0600) and keep it that way.
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


def _api_key(cfg):
    """The account API key used to authenticate. Export PETABYTE_API_KEY (an 'account'-scoped
    key created while signed in on the web), or save it as `api_key` in your config file."""
    return os.getenv("PETABYTE_API_KEY") or cfg.get("api_key")


def _client(cfg, auth=True):
    headers = {}
    if auth:
        # Prefer a session token from browser `login` (or $PETABYTE_TOKEN for headless/CI) as a
        # Bearer; otherwise fall back to an 'account'-scoped API key as X-API-KEY. The API accepts
        # either for every buyer endpoint (deps.get_current_user).
        tok = cfg.get("token") or os.getenv("PETABYTE_TOKEN")
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        else:
            key = _api_key(cfg)
            if key:
                headers["X-API-KEY"] = key
    return httpx.Client(base_url=cfg["api_url"], headers=headers, timeout=30)


def _humanize_error(r):
    """Pull a clean, human message out of a server error response instead of dumping raw
    JSON at the user. Handles our {"error":{"message":…}} / {"detail":…} envelopes and
    FastAPI's 422 validation list; falls back to the raw body. Keeps the request_id when
    present so a report is still traceable."""
    try:
        j = r.json()
    except Exception:
        return (r.text or "").strip()[:200]
    err = j.get("error") if isinstance(j, dict) else None
    msg = (err or {}).get("message") if isinstance(err, dict) else None
    detail = j.get("detail") if isinstance(j, dict) else None
    if isinstance(detail, list):            # FastAPI/pydantic 422 → "field: message; …"
        parts = []
        for e in detail:
            loc = ".".join(str(p) for p in (e.get("loc") or [])[1:]) or "input"
            parts.append(f"{loc}: {e.get('msg')}")
        detail = "; ".join(parts)
    out = msg or (detail if isinstance(detail, str) else None) or (r.text or "").strip()[:200]
    rid = (err or {}).get("request_id") if isinstance(err, dict) else None
    return f"{out}  [req {rid}]" if rid else out


def _die(msg, r=None):
    """Exit 1 with a human-first error (what happened, the server's own words, what to run)."""
    if _ui is None:
        if r is not None:
            msg += f" ({r.status_code}: {_humanize_error(r)})"
        print(f"error: {msg}", file=sys.stderr)
        sys.exit(1)
    if JSON:
        _ui.err.raw(json.dumps({"error": msg, "status": getattr(r, "status_code", None),
                                "detail": _humanize_error(r) if r is not None else None}))
        sys.exit(1)
    if r is None:
        _ui.err.error(msg)
    elif r.status_code == 401:
        _ui.err.error(msg, reason="You're not signed in (or the key/token is no longer valid).",
                      fix="Sign in with your browser, or export a valid PETABYTE_API_KEY.",
                      run="petabyte login", detail=f"401: {_humanize_error(r)}")
    elif r.status_code == 402:
        _ui.err.error(msg, reason=_humanize_error(r), fix="Add funds first:", run="petabyte deposit 20",
                      detail="402")
    elif r.status_code >= 500:
        _ui.err.error(msg, reason="Petabyte had a problem on its side.", fix="Try again in a moment.",
                      run="petabyte doctor", detail=f"{r.status_code}: {_humanize_error(r)}")
    else:
        _ui.err.error(msg, reason=_humanize_error(r), detail=f"HTTP {r.status_code}")
    sys.exit(1)


# Password login + /register_user were removed server-side (OAuth + API keys only). The CLI
# authenticates with an exported PETABYTE_API_KEY (see the module docstring), or `login` runs the
# browser device flow below to mint a session token — no password ever touches the CLI.
def cmd_login(a, cfg):
    return _login_web(cfg)


def _login_web(cfg):
    """OAuth-device-style browser login: start a request, open the approval page, poll for a token.
    No password ever touches the CLI."""
    import time as _t
    import webbrowser
    with _client(cfg, auth=False) as c:
        r = c.post("/auth/cli/start")
    if r.status_code != 200:
        _die("could not start browser login", r)
    d = r.json()
    url = d.get("verification_uri_complete") or d.get("verification_uri")
    if _ui is not None:
        _ui.out.brand("Sign in")
        _ui.out.line("Open this link in your browser to authorize the CLI:")
        _ui.out.command(url)
        _ui.out.line("and confirm this code:  " + str(d.get("user_code", "?")))
    else:
        print("Open this URL to authorize the CLI:\n  " + _cyan(url) +
              "\nand confirm the code: " + _bold(d.get("user_code", "?")))
    try:
        webbrowser.open(url)
    except Exception:
        pass
    interval = max(2, int(d.get("interval", 3)))
    deadline = _t.time() + int(d.get("expires_in", 600))
    print(_dim("waiting for approval in your browser… (Ctrl-C to cancel)"))
    while _t.time() < deadline:
        _t.sleep(interval)
        try:
            with _client(cfg, auth=False) as c:
                pr = c.post("/auth/cli/poll", json={"device_code": d["device_code"]})
        except Exception:
            continue                              # transient network hiccup — keep polling
        if pr.status_code != 200:
            continue
        st = pr.json().get("status")
        if st == "approved":
            cfg["token"] = pr.json()["access_token"]
            _save(cfg)
            if _ui is not None:
                _ui.out.ok("Logged in")
                _ui.out.command("petabyte --me", caption="Next: see your dashboard")
            else:
                print(_green("✓ logged in"))
            return
        if st in ("denied", "expired"):
            _die(f"browser login {st} — run `petabyte login --web` again")
    _die("browser login timed out — run it again")


def cmd_deposit(a, cfg):
    with _client(cfg) as c:
        r = c.post("/deposit", json={"amount": a.amount})
    print(f"balance: ${r.json()['balance']}" if r.status_code == 200 else _die("deposit failed", r))


def cmd_wallet(a, cfg):
    with _client(cfg) as c:
        r = c.get("/wallet")
    if r.status_code != 200:
        _die("Could not load your wallet", r)
    w = r.json()
    if JSON:
        print(json.dumps(w))
        return
    if _ui is None:
        print(f"balance:  ${w['balance']}\nearnings: ${w['earnings']}")
        return
    rows = [("balance", ("money", f"${float(w.get('balance', 0)):,.2f}")),
            ("earnings", f"${float(w.get('earnings', 0)):,.2f}")]
    if w.get("withdrawable") is not None:
        rows.append(("withdrawable", f"${float(w['withdrawable']):,.2f}"))
    if w.get("clearing"):
        rows.append(("clearing", f"${float(w['clearing']):,.2f}  (held {w.get('hold_hours', '?')}h)"))
    _ui.out.kv(rows, title="Wallet (USD)")


def cmd_specs(a, cfg):
    with _client(cfg) as c:
        r = c.get("/specs")
    if r.status_code != 200:
        _die("specs failed", r)
    specs = r.json()["specs"]
    if JSON:
        print(json.dumps({"specs": specs}))
        return
    if not specs:
        if _ui is not None:
            _ui.out.info("no bookable GPUs available right now — try again in a few minutes")
        else:
            print("no bookable GPUs available right now")
        return
    if _ui is not None:
        rows = []
        for sp in specs:
            rep_ = sp.get("reputation_score", sp.get("reputation"))
            tags = " ".join(t for t, on in (("confidential", sp.get("confidential")),
                                            ("region\u2713", sp.get("region_verified"))) if on)
            rows.append([sp["spec_id"], sp["gpu_model"] or "CPU", f"{sp['price_per_hour']:.2f}",
                         sp["available_units"], rep_, sp["provider"], tags])
        _ui.out.table(["ID", "GPU", "$/HR", "UNITS", "REP", "PROVIDER", ""], rows,
                      aligns=["right", "left", "right", "right", "right", "left", "left"],
                      title="GPUs you can rent right now (cheapest first)")
        _ui.out.command("petabyte launch ollama --hours 1", caption="Rent one:")
        return
    print(_dim(f"  {'ID':>3}  {'GPU':<10} {'$/HR':>7}  {'UNITS':>5}  {'REP':>3}  PROVIDER"))
    for sp in specs:
        rep = sp.get("reputation_score", sp.get("reputation"))
        tags = []
        if sp.get("confidential"): tags.append(_amber("confidential"))
        if sp.get("region_verified"): tags.append(_cyan("region\u2713"))
        line = (f"  {sp['spec_id']:>3}  {_bold(str(sp['gpu_model'] or 'CPU')):<10} "
                f"{_amber('$'+format(sp['price_per_hour'],'.2f')):>7}  "
                f"{sp['available_units']:>5}  {rep:>3}  {sp['provider']}")
        print(line + ("  " + " ".join(tags) if tags else ""))


def _read_code(path):
    if not os.path.isfile(path):
        _die(f"file not found: {path}")
    if path.endswith(".ipynb"):
        nb = json.load(open(path))
        cells = [c for c in nb.get("cells", []) if c.get("cell_type") == "code"]
        return "\n\n".join("".join(c.get("source", [])) for c in cells)
    return open(path).read()


# Files that must NEVER be shipped to a seller node even if they sit next to the entry
# script. Hidden files (.env, .netrc, …) are already skipped; this catches the non-dotfile
# credential names/suffixes that a plain `os.walk` would otherwise sweep up.
import re as _re_secret

_SECRET_SUFFIXES = (".pem", ".key", ".ppk", ".pfx", ".p12", ".jks", ".keystore",
                    ".kdbx", ".ovpn", ".asc", ".gpg")
_SECRET_SUBSTRINGS = ("secret", "credential", "password", "_key", "apikey", "api_key",
                      "id_rsa", "id_ed25519", "id_ecdsa")
# "token" as a BARE substring matched tokenizer.py, detokenizer.py, tokenize_utils.py -- about the
# most likely filenames in a real ML job. Those were silently dropped from the bundle, so the job
# booked (money taken), uploaded, then died remotely with an ImportError the buyer could not
# explain. Bound it to a whole path segment instead: token.py / auth_token.json / api-token still
# match; tokenizer.py does not.
_SECRET_WORD_RE = _re_secret.compile(r"(?:^|[^a-z0-9])tokens?(?:[^a-z0-9]|$)")
_SECRET_EXACT = {"credentials", "credentials.json", ".env", ".netrc", ".git-credentials",
                 "cli.json", ".pgpass", ".dockercfg"}


def _looks_secret(filename):
    low = filename.lower()
    return (low in _SECRET_EXACT
            or low.endswith(_SECRET_SUFFIXES)
            or any(s in low for s in _SECRET_SUBSTRINGS)
            or bool(_SECRET_WORD_RE.search(low)))


def _bundle_project(entry, max_bytes=25 * 1024 * 1024):
    """tar.gz the entry's project folder (siblings + subpackages), skipping junk, secrets
    and files >8MB, and return (base64, entry_relpath, included_names) — or None if it's a
    lone file or the bundled code exceeds max_bytes (mount/download big data separately)."""
    import base64
    import io
    import tarfile
    entry = os.path.abspath(entry)
    root = os.path.dirname(entry) or "."
    IGNORE = {".git", "__pycache__", ".venv", "venv", "env", "node_modules", ".mypy_cache",
              ".ipynb_checkpoints", ".pytest_cache", "dist", "build", ".idea", ".vscode"}
    buf = io.BytesIO(); total = 0; included = []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in IGNORE and not d.startswith(".")]
            for fn in files:
                if fn.endswith((".pyc", ".pyo")) or fn.startswith("."):
                    continue
                if _looks_secret(fn):              # never ship credentials to the seller
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                if sz > 8 * 1024 * 1024:            # skip big data — not source
                    continue
                total += sz
                if total > max_bytes:
                    return None
                rel = os.path.relpath(fp, root)
                tar.add(fp, arcname=rel)
                included.append(rel)
    return base64.b64encode(buf.getvalue()).decode(), os.path.relpath(entry, root), included


def _warn_unhashed_requirements(root):
    """Your job runs on a SELLER-operated host that controls its network. If your code pip-installs
    over that network from an UNHASHED requirements.txt, the seller can MITM the install (swap a
    package for a malicious one). Warn the buyer to pin hashes so pip verifies every download."""
    req = os.path.join(root, "requirements.txt")
    if not os.path.exists(req):
        return
    try:
        body = open(req, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    has_pkgs = any(ln.strip() and not ln.strip().startswith("#") for ln in body.splitlines())
    if has_pkgs and "--hash=" not in body:
        print(_amber(
            "! requirements.txt has no pinned hashes. This job runs on a seller-operated host that "
            "controls the network, so an unhashed `pip install` can be MITM'd. Pin hashes:\n"
            "    pip-compile --generate-hashes -o requirements.txt requirements.in\n"
            "  then in your entry script: pip install --require-hashes -r requirements.txt\n"
            "  (see docs/SECURITY_AUDIT_PEER_TO_PEER.md)."))


def _run_payload(a):
    """The notebook `code` payload for `run`: a JSON project bundle when the entry has
    local dependencies, else the plain single-file source (unchanged behaviour)."""
    code = _read_code(a.file)
    if getattr(a, "deps", None) is False:          # --no-deps: force single file
        return code
    root = os.path.dirname(os.path.abspath(a.file)) or "."
    try:
        siblings = [x for x in os.listdir(root)
                    if x.endswith(".py") and os.path.join(root, x) != os.path.abspath(a.file)]
    except OSError:
        siblings = []
    auto = bool(siblings) or os.path.exists(os.path.join(root, "requirements.txt"))
    if getattr(a, "deps", None) is True or auto:
        _warn_unhashed_requirements(root)
        b = _bundle_project(a.file)
        if b is None:
            print(_amber("! project code exceeds 25MB — running the single file only "
                         "(mount or download large data from your script)"))
            return code
        b64, entry, included = b
        shown = ", ".join(included[:8]) + (f" +{len(included) - 8} more" if len(included) > 8 else "")
        print(_dim(f"→ bundling {len(included)} file(s) to the seller "
                   f"({len(b64) // 1024} KB, entry {entry}): {shown}"))
        return json.dumps({"bundle_b64": b64, "entry": entry, "gpu": bool(getattr(a, "gpu", None))})
    return code


def cmd_run(a, cfg):
    # Read/bundle the code BEFORE booking. If the file is missing or the bundle fails, we exit
    # here — never after a booking, which would escrow money for a job we can't even submit.
    payload = _run_payload(a)
    with _client(cfg) as c:
        # pick a spec
        spec_id = a.spec
        if not spec_id:
            sr = c.get("/specs")
            if sr.status_code != 200:       # e.g. 401/5xx — don't .json() a non-OK body
                _die("could not list GPUs", sr)
            specs = sr.json().get("specs", [])
            if a.gpu:
                specs = [s for s in specs if (s["gpu_model"] or "").lower() == a.gpu.lower()]
            if not specs:
                _die("no matching GPU available")
            spec_id = specs[0]["spec_id"]   # cheapest (list is price-sorted)
            print(_dim(f"→ selected spec {spec_id} ({specs[0]['gpu_model']} @ ${specs[0]['price_per_hour']}/hr)"))
        # book (optionally on a private WireGuard VPN — the buyer chooses)
        want_vpn = bool(getattr(a, "vpn", False))
        r = c.post("/request_vm", json={"spec_id": spec_id, "hours": a.hours, "vpn": want_vpn})
        if r.status_code != 200:
            _die("booking failed", r)
        bk = r.json()
        print(f"booked #{bk['booking_id']}  escrow ${bk['gross_amount']} "
              f"(fee ${bk['platform_fee']}, seller ${bk['seller_payout']})")
        if want_vpn and bk.get("vpn_config_url"):
            cr = c.get(bk["vpn_config_url"])
            if cr.status_code == 200:
                path = f"petabyte-{bk['booking_id']}.conf"
                with open(path, "w") as f:
                    f.write(cr.text)
                print(_green(f"✓ VPN config written to {path}") +
                      _dim(f"  → connect with:  sudo wg-quick up ./{path}"))
            else:
                print(_amber("! could not fetch VPN config (booking still active)"))
        # create task (payload was read/bundled up-front, before we spent anything)
        r = c.post("/create_task", json={"booking_id": bk["booking_id"],
                                         "task_type": "notebook", "code": payload})
        if r.status_code != 200:
            _die("task creation failed", r)
        tid = r.json()["task_id"]
        print(f"dispatched task #{tid} — waiting for a node to execute...")
        # poll
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            t = c.get(f"/tasks/{tid}").json()
            if t["status"] in ("completed", "failed"):
                hdr = _green("\u2713 COMPLETED") if t["status"]=="completed" else _amber("\u2717 FAILED")
                print(f"\n{hdr}")
                print(t.get("result") or "(no output)")
                return
            time.sleep(2)
        print("timed out waiting for result", file=sys.stderr)


def cmd_launch(a, cfg):
    """Launch a ready-made template (ollama, jupyter, blender, minecraft, …) on the cheapest
    verified GPU that fits — the CLI twin of the web one-click launcher (`POST /launch`)."""
    body = {"template": a.template, "hours": a.hours}
    if getattr(a, "max_price", None) is not None:
        body["max_price_per_hour"] = a.max_price
    if getattr(a, "region", None):
        body["region"] = a.region
    if getattr(a, "spec", None):
        body["spec_id"] = str(a.spec)
    with _client(cfg) as c:
        r = c.post("/launch", json=body)
        if r.status_code != 200:
            _die("launch failed", r)
        d = r.json()
        print(_green("✓ launched ") + _bold(a.template) +
              _dim(f"  · {d.get('gpu_model', '?')} @ ${d.get('price_per_hour', '?')}/hr · {a.hours}h"))
        print(f"  booking #{d.get('booking_id')}   escrow ${d.get('gross_amount')}")
        if d.get("routing_explanation"):
            print("  " + _dim(d["routing_explanation"]))
        url = d.get("url")
        addr = url.get("http") if isinstance(url, dict) else url
        if addr:
            print("  address  " + _cyan(addr))
        if isinstance(url, dict) and url.get("ssh"):
            print("  ssh      " + _dim(url["ssh"]))
        if d.get("connect"):
            print("  " + _dim(d["connect"]))


def cmd_ask(a, cfg):
    """Send a prompt to the pay-per-token Inference API (OpenAI-compatible) and print the answer.

    Uses an `inference`-scoped API key (not the login token): --key, else $PETABYTE_API_KEY,
    else `api_key` in your saved config. Answer goes to stdout (pipeable); the token/cost line
    goes to stderr."""
    if getattr(a, "key_stdin", False):
        key = sys.stdin.readline().strip()      # key never touches argv / shell history / ps
    else:
        key = a.key or os.getenv("PETABYTE_API_KEY") or cfg.get("api_key")
    if not key:
        _die("no inference API key — create one with scope 'inference' at /keys, then pipe it "
             "in with --key-stdin, set PETABYTE_API_KEY, or save it as 'api_key' in your config")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    with httpx.Client(base_url=cfg["api_url"], headers=headers, timeout=120) as c:
        body = {"messages": [{"role": "user", "content": a.prompt}]}
        if getattr(a, "model", None):
            body["model"] = a.model
        try:
            r = c.post("/v1/chat/completions", json=body)
        except httpx.RequestError as e:
            _die(f"cannot reach the inference API at {cfg['api_url']} ({e.__class__.__name__})")
        if r.status_code != 200:
            _die("inference failed", r)
        try:
            j = r.json()
        except ValueError:
            _die("unexpected non-JSON response from the inference API", r)
        try:
            print(j["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            _die("unexpected response from the inference API", r)
        b = j.get("x_petabyte") or {}
        if b.get("tokens") is not None:
            note = f"{b.get('tokens')} tokens"
            if b.get("charged") is not None:
                note += f" · ${float(b.get('charged')):.6f}" + (" billed" if b.get("billed") else " (free)")
            print(_dim("  " + note), file=sys.stderr)


def cmd_vpn(a, cfg):
    """Download (or re-download) the WireGuard client config for a VPN-enabled booking."""
    with _client(cfg) as c:
        r = c.get(f"/vpn_config/{a.booking_id}")
        if r.status_code != 200:
            _die("no VPN config for that booking (was it booked with --vpn?)", r)
        path = a.out or f"petabyte-{a.booking_id}.conf"
        with open(path, "w") as f:
            f.write(r.text)
        print(_green(f"✓ VPN config written to {path}"))
        print(_dim(f"  connect:  sudo wg-quick up ./{path}      disconnect:  sudo wg-quick down ./{path}"))


def cmd_earnings(a, cfg):
    """Seller payout state: balance, withdrawable earnings, what's still clearing, recent payouts."""
    with _client(cfg) as c:
        w = c.get("/wallet")
        if w.status_code != 200:
            _die("wallet failed", w)
        w = w.json()
        pays = c.get("/wallet/payouts")
    if JSON:
        print(json.dumps({"wallet": w, "payouts": pays.json().get("payouts", []) if pays.status_code == 200 else []}))
        return
    if _ui is not None:
        _ui.out.kv([("balance", ("money", f"${w['balance']:,.2f}")), ("earnings", f"${w['earnings']:,.2f}"),
                    ("withdrawable", f"${w['withdrawable']:,.2f}"),
                    ("clearing", f"${w['clearing']:,.2f}  (held {w['hold_hours']}h)"),
                    ("instant payout", ("status", "eligible") if w.get("instant_eligible") else ("dim", "not yet"))],
                   title="Earnings (USD)")
        ps = pays.json().get("payouts", []) if pays.status_code == 200 else []
        if ps:
            _ui.out.blank()
            _ui.out.table(["AMOUNT", "KIND", "STATUS", "WHEN"],
                          [[f"${p['amount_usd']:,.2f}", str(p["kind"])[:12], str(p["status"])[:10],
                            str(p["created_at"])[:10]] for p in ps[:8]],
                          aligns=["right", "left", "left", "left"], title="Recent payouts")
        else:
            _ui.out.note("no payouts yet — add a payout method on the earnings page, then withdraw.")
        return
    print(_bold("Earnings"))
    print(f"  balance:      {_amber('$' + format(w['balance'], '.2f'))}")
    print(f"  earnings:     ${format(w['earnings'], '.2f')}")
    print(f"  withdrawable: {_green('$' + format(w['withdrawable'], '.2f'))}")
    print(f"  clearing:     ${format(w['clearing'], '.2f')}  " + _dim(f"(held {w['hold_hours']}h)"))
    print("  instant payout: " + (_green('eligible') if w.get('instant_eligible') else _dim('not yet')))
    if pays.status_code == 200 and pays.json().get("payouts"):
        print(_dim("\n  recent payouts:"))
        print(_dim(f"    {'AMOUNT':>10}  {'KIND':<12} {'STATUS':<10} WHEN"))
        for p in pays.json()["payouts"][:8]:
            print(f"    {'$' + format(p['amount_usd'], '.2f'):>10}  {str(p['kind'])[:12]:<12} "
                  f"{str(p['status'])[:10]:<10} {str(p['created_at'])[:10]}")
    else:
        print(_dim("\n  no payouts yet — add a payout method on the earnings page, then `withdraw`."))


def _node_status(a, cfg):
    sid = a.spec_id
    with _client(cfg) as c:
        dash = c.get("/seller/dashboard")
        if dash.status_code != 200:
            _die("dashboard failed", dash)
        dash = dash.json()
        models = c.get(f"/nodes/{sid}/models")
        disk = c.get(f"/nodes/{sid}/disk")
    node = next((n for n in dash.get("nodes", []) if n.get("spec_id") == sid), None)
    if not node:
        _die(f"node {sid} not found (is it one of yours?)")
    on = _green("online") if node["online"] else _amber("offline")
    att = _green("attested") if node["attested"] else _amber("unverified")
    print(_bold(f"Node {sid}") + f"  {node.get('gpu_model') or 'CPU'}  [{on} · {att}]")
    sug = _dim(f"  (suggested ${format(node['suggested_price'], '.2f')})") if node.get("suggested_price") else ""
    print(f"  price:        ${format(node['price_per_hour'], '.2f')}/hr" + sug)
    print(f"  units:        {node['units_busy']}/{node['units_total']} busy  ({node['utilization_pct']}% util)")
    succ = f"  ({node['success_rate']}% success)" if node.get("success_rate") is not None else ""
    print(f"  jobs:         {node['jobs_completed']} done · {node['jobs_failed']} failed" + succ)
    print(f"  reputation:   {node['reputation']}")
    print(f"  earned:       {_green('$' + format(node['earned_total'], '.2f'))}")
    print(f"  last seen:    {node.get('last_seen') or 'never'}")
    if models.status_code == 200:
        ms = models.json().get("models", [])
        if ms:
            print(f"  models cached: {len(ms)}" + _dim("  " + ", ".join(ms[:6]) + (" …" if len(ms) > 6 else "")))
        else:
            print("  models cached: 0" + _dim(f"  (run: petabyte node sync-models {sid})"))
    if disk.status_code == 200 and disk.json().get("enabled"):
        d = disk.json()
        print(f"  disk rental:  {d['provider']} up to {d['alloc_gb']} GB")
    for b in [x for x in dash.get("blockers", []) if x.get("node") == node.get("id")]:
        print(_amber("  ! " + b["issue"]) + _dim("  fix: " + b.get("fix", "")))


def _node_sync_models(a, cfg):
    """Scan the local ~/.petabyte model cache and report the ids to the marketplace, so the
    scheduler prefers THIS node for jobs that need a model it already holds."""
    try:
        from modelhub import ModelManager
    except Exception:
        _die("model hub not available on this machine (cannot scan the local cache)")
    ids = ModelManager().cached_model_ids()
    with _client(cfg) as c:
        r = c.post("/nodes/models", json={"spec_id": a.spec_id, "models": ids})
    if r.status_code != 200:
        _die("sync failed", r)
    n = r.json().get("cached_models", 0)
    print(_green(f"✓ reported {n} cached model(s)") + _dim(f" for node {a.spec_id}"))
    for m in ids[:12]:
        print(_dim("    " + m))
    if not ids:
        print(_dim("    (local cache is empty — pull a model first: petabyte model pull <id>)"))


def cmd_node(a, cfg):
    {"status": _node_status, "sync-models": _node_sync_models}[a.node_cmd](a, cfg)


_JOB_DONE = {"complete", "done", "ok", "stitched", "succeeded"}
_JOB_FAILED = {"failed", "error", "cancelled"}


def _parse_frames(spec):
    spec = (spec or "1-1").strip()
    if "-" in spec:
        a, b = spec.split("-", 1)
        return int(a), int(b)
    n = int(spec)
    return n, n


def _upload_input(c, path):
    if not os.path.exists(path):
        _die(f"file not found: {path}")
    print(_dim(f"uploading {os.path.basename(path)} …"))   # only after we know the file is real
    r = c.post("/uploads/url", json={"filename": os.path.basename(path)})
    if r.status_code >= 300:
        _die("could not get an upload URL", r)
    up = r.json()
    with open(path, "rb") as fh:
        pr = httpx.put(up["upload_url"], content=fh.read(), timeout=1800)
    if pr.status_code >= 300:
        _die(f"upload failed ({pr.status_code})")
    return up["ref"]


def _poll_job(c, job_id, label):
    seen = None
    while True:
        r = c.get(f"/jobs/manifest/{job_id}")
        if r.status_code >= 300:
            _die("could not read job status", r)
        m = r.json()
        segs = m.get("segments", [])
        done = sum(1 for x in segs if str(x.get("status", "")).lower() in _JOB_DONE)
        tot = m.get("total_segments") or len(segs) or 1
        key = (done, m.get("status"))
        if key != seen:
            print(f"  {label}: {done}/{tot} segment(s) — {m.get('status')}")
            seen = key
        st = str(m.get("status", "")).lower()
        if st in _JOB_DONE or st in _JOB_FAILED or (tot and done >= tot):
            return m
        time.sleep(4)


def _download_outputs(c, job_id, outdir):
    r = c.post("/jobs/output_url", json={"job_id": job_id})
    if r.status_code >= 300:
        _die("could not list outputs", r)
    outs = r.json().get("outputs", [])
    os.makedirs(outdir, exist_ok=True)
    saved = []
    for o in outs:
        name = (o.get("output_ref", "").rstrip("/").split("/")[-1]) or f"seg{o.get('idx')}"
        d = httpx.get(o["download_url"], timeout=1800)
        if d.status_code < 300:
            dst = os.path.join(outdir, name)
            with open(dst, "wb") as fh:
                fh.write(d.content)
            saved.append(dst)
    return saved


def cmd_render(a, cfg):
    """Drop a .blend, render on the farm, pay only for render time, download the frames."""
    fs, fe = _parse_frames(a.frames)
    with _client(cfg) as c:
        ref = _upload_input(c, a.file)
        r = c.post("/render", json={"blend_ref": ref, "frame_start": fs, "frame_end": fe,
                                    "samples": a.samples, "nodes": a.nodes, "hours": a.hours})
        if r.status_code >= 300:
            _die("render request failed", r)
        d = r.json()
        print(_green("✓ render started  ")
              + f"job #{d['job_id']} · {d['nodes']} node(s) · frames {fs}-{fe}")
        print("  " + _bold(f"~${d.get('estimated_cost')}")
              + _dim(" max — you're billed only for actual render time"))
        m = _poll_job(c, d["job_id"], "render")
        if str(m.get("status", "")).lower() in _JOB_FAILED:
            _die("render job failed")
        saved = _download_outputs(c, d["job_id"], a.out)
        print(_green(f"✓ {len(saved)} frame(s) → {a.out}") if saved
              else _amber("job finished but no outputs were ready — re-run to fetch later"))


def cmd_transcode(a, cfg):
    """Drop a video, GPU-transcode it (NVENC), download the result."""
    with _client(cfg) as c:
        ref = _upload_input(c, a.file)
        body = {"input_ref": ref, "codec": a.codec, "container": a.container, "use_gpu": True}
        if a.resolution:
            body["resolution"] = a.resolution
        if a.crf is not None:
            body["crf"] = a.crf
        r = c.post("/transcode", json=body)
        if r.status_code >= 300:
            _die("transcode request failed", r)
        d = r.json()
        print(_green("✓ transcode started  ") + f"job #{d.get('job_id')}")
        if d.get("estimated_cost") is not None:
            print("  " + _bold(f"~${d.get('estimated_cost')}") + _dim(" max — billed for actual time"))
        m = _poll_job(c, d["job_id"], "transcode")
        if str(m.get("status", "")).lower() in _JOB_FAILED:
            _die("transcode job failed")
        saved = _download_outputs(c, d["job_id"], a.out)
        print(_green(f"✓ output → {a.out}") if saved else _amber("finished; no output ready yet"))


def _require_product():
    if petabyte_cli is None:
        _die("this build of the CLI is missing its product layer (petabyte_cli) — reinstall: "
             "pip install -U petabyte-client")


def cmd_me(a, cfg):
    """`petabyte --me` / `petabyte me`: the dashboard."""
    _require_product()
    from concurrent.futures import ThreadPoolExecutor

    from petabyte_cli import agent as _agent
    from petabyte_cli import api as _api
    from petabyte_cli import dashboard as _dash
    from petabyte_cli import sysinfo as _si
    api_url = cfg["api_url"]
    with _client(cfg) as c, _ui.out.status("Loading your dashboard…"):
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_dash = ex.submit(_api.fetch_dashboard, c, api_url=api_url)
            f_agent = ex.submit(_agent.detect)
            f_snap = ex.submit(_si.snapshot, api_url=api_url, with_docker=False, cpu_interval=0.3)
            st, snap = f_agent.result(), f_snap.result()
            d = f_dash.result()
    if JSON:
        print(json.dumps(_dash.as_json(d, st, snap), default=str))
        return
    _dash.render(_ui.out, d, st, snap, update=_UPDATE)


def cmd_doctor(a, cfg):
    _require_product()
    from petabyte_cli import agent as _agent
    from petabyte_cli import doctor as _doc
    from petabyte_cli import sysinfo as _si
    with _ui.out.status("Checking…"):
        st = _agent.detect()
        snap = _si.snapshot(api_url=cfg["api_url"], with_docker=True, with_network=False, cpu_interval=0.1)
        rep = _doc.run_checks(cfg=cfg, client_factory=_client, config_path=CONFIG, agent_state=st, snap=snap,
                              update=_UPDATE)
    if JSON:
        print(json.dumps(rep.as_dict(), default=str))
    else:
        _doc.render(_ui.out, rep)
    sys.exit(0 if rep.healthy else 1)


def cmd_jobs(a, cfg):
    """Your running instances and recent bookings (buyer view)."""
    _require_product()
    from petabyte_cli import api as _api
    with _client(cfg) as c:
        _api.fetch_me(c, cfg["api_url"])
        vms = _api._get(c, "/vms")
        bk = _api._get(c, "/account/bookings", {"limit": int(getattr(a, "limit", 10) or 10)})
    if JSON:
        print(json.dumps({"vms": vms.data, "bookings": bk.data}, default=str))
        return
    live = [v for v in (vms.get("vms") or []) if str(v.get("status", "")).lower() in _api.RUNNING_VM_STATES]
    if live:
        _ui.out.table(["VM", "TEMPLATE", "STATUS", "HOURS LEFT", "$/HR", "ADDRESS"],
                      [[v.get("vm_id"), v.get("template"), v.get("status"), v.get("hours_left"),
                        f"{float(v.get('hourly_rate') or 0):.2f}",
                        (v.get("url") or {}).get("hostname") if isinstance(v.get("url"), dict) else v.get("url")]
                       for v in live], title="Running now")
    else:
        _ui.out.info("Nothing running right now.")
    rows = bk.get("bookings") or []
    if rows:
        _ui.out.blank()
        _ui.out.table(["ID", "ROLE", "GPU", "HOURS", "AMOUNT", "STATUS", "WHEN"],
                      [[b.get("id"), b.get("role"), b.get("gpu_model"), b.get("hours"),
                        f"${float(b.get('gross_amount') or 0):,.2f}", b.get("status"), str(b.get("created_at"))[:16]]
                       for b in rows], title="Recent bookings")
    if not live:
        _ui.out.command("petabyte specs", caption="Rent a GPU:")


def cmd_activity(a, cfg):
    _require_product()
    from petabyte_cli import api as _api
    with _client(cfg) as c:
        _api.fetch_me(c, cfg["api_url"])
        n = _api._get(c, "/notifications")
    items = n.get("notifications") or []
    if JSON:
        print(json.dumps({"notifications": items}, default=str))
        return
    if not items:
        _ui.out.info("No activity yet.")
        return
    _ui.out.table(["WHEN", "EVENT", "SUBJECT", "STATUS"],
                  [[str(i.get("created_at"))[:16], i.get("event_type"), i.get("subject") or "", i.get("status")]
                   for i in items[:20]], title="Recent activity")


def cmd_version(a, cfg):
    _require_product()
    info = _UPDATE or (_vc.check_for_update() if _vc.should_check(sys.stdout) or getattr(a, "check", False) else None)
    if JSON:
        print(json.dumps({"cli": _vc.installed_version(), "python": _vc.python_version(),
                          "python_supported": _vc.python_supported(),
                          "latest": info.latest if info else None, "update_available": bool(info and info.newer)}))
        return
    _ui.out.brand(f"CLI v{_vc.installed_version()}")
    rows = [("Python", f"{_vc.python_version()}  ({'supported' if _vc.python_supported() else 'UNSUPPORTED'})")]
    if info and info.latest:
        rows.append(("Latest on PyPI", info.latest + ("  (update available)" if info.newer else "  (up to date)")))
    else:
        rows.append(("Latest on PyPI", ("dim", "not checked (offline, CI, or PETABYTE_NO_UPDATE_CHECK=1)")))
    _ui.out.kv(rows)
    if info and info.newer:
        _ui.out.command(info.upgrade_command, caption="Update with:")
    if not _vc.python_supported():
        _ui.out.warn(_vc.python_support_message())


def cmd_agent(a, cfg):
    _require_product()
    from petabyte_cli import agent_cmds as _ac
    what = getattr(a, "agent_cmd", None) or "status"
    yes = bool(getattr(a, "yes", False))
    if what == "install":
        return _ac.cmd_install(_ui.out, cfg, _client, login=lambda: _login_web(cfg),
                               sell=getattr(a, "sell", None), price=getattr(a, "price", None), yes=yes,
                               no_egress_lockdown=bool(getattr(a, "no_egress_lockdown", False)),
                               dry_run=bool(getattr(a, "dry_run", False)), reinstall=bool(getattr(a, "reinstall", False)))
    if what in ("start", "run"):
        return _ac.cmd_run(_ui.out, cfg, _client, follow=getattr(a, "follow", None),
                           foreground=bool(getattr(a, "foreground", False)))
    if what in ("stop", "kill"):
        return _ac.cmd_kill(_ui.out, cfg, yes=yes, force=bool(getattr(a, "force", False)))
    if what == "logs":
        return _ac.cmd_logs(_ui.out, cfg, lines=int(getattr(a, "lines", 30) or 30))
    return _ac.cmd_status(_ui.out, cfg, _client, json_mode=JSON)


def cmd_ssh(a, cfg):
    """`petabyte ssh` — set this computer up to reach your VMs, and connect to one."""
    _require_product()
    from petabyte_cli import ssh_cmds as _ssh
    if getattr(a, "status", False):
        return _ssh.cmd_status(_ui.out, cfg, _client, json_mode=JSON)
    vm = getattr(a, "vm", None)
    user = a.user
    # People paste the whole target they were shown (`root@<id>.<zone>`). Honor an embedded login
    # user instead of prepending root to it, and let host construction strip the id down (see
    # ssh_setup.bare_vm_id) — otherwise this became `ssh root@root@<id>.<zone>.<zone>`.
    if vm and "@" in vm:
        head, vm = vm.split("@", 1)
        user = head or user
    if getattr(a, "print_only", False):
        return _ssh.connect(_ui.out, cfg, vm, user=user, print_only=True, client_factory=_client)
    # An already-set-up machine connecting to a named VM should just connect — the wizard is for
    # the first run (or when something is missing).
    if vm and not getattr(a, "setup", False):
        st = _ssh.S.detect(cfg)
        if st.configured and st.chosen:
            return _ssh.connect(_ui.out, cfg, vm, user=user, client_factory=_client)
    return _ssh.cmd_setup(_ui.out, cfg, _client, login=lambda: _login_web(cfg),
                          yes=bool(getattr(a, "yes", False)),
                          new_key=bool(getattr(a, "new_key", False)),
                          use_key=getattr(a, "key", None),
                          passphrase=bool(getattr(a, "passphrase", False)),
                          dry_run=bool(getattr(a, "dry_run", False)),
                          user=user, connect_vm=vm)


def _startup(a):
    """Python-version and update checks: quiet, cached, never blocking, never fatal."""
    global _UPDATE
    if petabyte_cli is None or JSON:
        return
    if not _vc.python_supported():
        _ui.err.warn(_vc.python_support_message())
    if getattr(a, "version", False) or a.cmd == "login" or not _vc.should_check(sys.stdout):
        return
    try:
        _UPDATE = _vc.check_for_update()
    except Exception:
        _UPDATE = None
    if _UPDATE and _UPDATE.newer and not getattr(a, "me", False):
        for ln in _vc.notice_lines(_UPDATE):
            _ui.out.note(ln)
        _ui.out.blank()


def _build_parser():
    p = argparse.ArgumentParser(prog="petabyte", add_help=False,
                                description="Rent verified GPUs, or earn by selling yours.")
    p.add_argument("-h", "--help", action="store_true", help="show this help")
    p.add_argument("--api", help="API base URL (overrides saved config)")
    p.add_argument("--json", action="store_true", help="machine-readable output (where supported)")
    p.add_argument("-v", "--verbose", action="store_true", help="full technical detail on errors")
    p.add_argument("-V", "--version", action="store_true", help="CLI / Python / update status")
    p.add_argument("--me", action="store_true", help="your dashboard")
    p.add_argument("--install-agent", dest="install_agent", action="store_true", help="guided seller-agent setup")
    p.add_argument("--run-agent", dest="run_agent", action="store_true", help="start the seller agent")
    p.add_argument("--kill-agent", dest="kill_agent", action="store_true", help="stop the seller agent safely")
    p.add_argument("-y", "--yes", action="store_true", help="assume yes for confirmations (install / stop)")
    p.add_argument("--force", action="store_true", help="--kill-agent: stop even with active jobs")
    p.add_argument("--no-follow", dest="follow", action="store_false", default=None,
                   help="--run-agent: don't follow the log after starting")
    p.add_argument("--foreground", action="store_true", help="--run-agent: run attached (no service manager)")
    p.add_argument("--sell", choices=["gpu", "cpu", "all"], help="--install-agent: what to sell (skips the question)")
    p.add_argument("--price", type=float, help="--install-agent: price per hour in USD (default: automatic)")
    p.add_argument("--no-egress-lockdown", dest="no_egress_lockdown", action="store_true",
                   help="--install-agent: don't restrict container egress")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="--install-agent: show the plan only")
    p.add_argument("--reinstall", action="store_true", help="--install-agent: reinstall even if present")
    sub = p.add_subparsers(dest="cmd", required=False)

    s = sub.add_parser("login", help="authorize in the browser (device flow) — no password on "
                                     "the CLI; token also via $PETABYTE_TOKEN")
    s.add_argument("--web", action="store_true", help="(default) browser device-login")
    s = sub.add_parser("deposit");  s.add_argument("amount", type=float)
    sub.add_parser("wallet")
    sub.add_parser("specs")
    s = sub.add_parser("run", help="run a notebook/.py on a rented GPU, OR start a model runtime")
    s.add_argument("--deps", dest="deps", action="store_true", default=None,
                   help="bundle the whole project folder (auto when siblings/requirements.txt exist)")
    s.add_argument("--no-deps", dest="deps", action="store_false",
                   help="ship only the single file")
    s.add_argument("file", help="a .ipynb/.py file (compute job) OR a model id like Qwen/Qwen3-8B")
    s.add_argument("--spec", type=int, help="pin to a specific host spec id (default: cheapest match)")
    s.add_argument("--gpu", help="require this GPU model, e.g. 'NVIDIA H100' (also runs the job in a "
                                 "CUDA container); default: cheapest available")
    s.add_argument("--hours", type=int, default=1, help="max hours to escrow (unused is refunded)")
    s.add_argument("--timeout", type=int, default=120,
                   help="seconds to wait for the result before giving up (default: 120)")
    s.add_argument("--vpn", action="store_true",
                   help="rent on a private WireGuard VPN and save the client config")
    s.add_argument("--revision", help="model runtime: git revision/branch to serve (model-id runs only)")
    s.add_argument("--format", help="model runtime: weight format, e.g. safetensors/gguf (model-id runs only)")
    s.add_argument("--quantization", help="model runtime: quantization, e.g. q4_k_m/awq (model-id runs only)")
    s.add_argument("--force", action="store_true", help="model runtime: re-pull even if already cached")
    s = sub.add_parser("launch",
                       help="launch a ready-made template (ollama, jupyter, blender, minecraft…) on the cheapest verified GPU")
    s.add_argument("template", help="template name, e.g. ollama, jupyter, blender, minecraft")
    s.add_argument("--hours", type=int, default=2)
    s.add_argument("--region")
    s.add_argument("--max-price", type=float, dest="max_price", help="cap the $/hour you'll pay")
    s.add_argument("--spec", type=int, help="pin to a specific host spec id")
    s = sub.add_parser("vpn", help="download the WireGuard config for a VPN booking")
    s.add_argument("booking_id", type=int); s.add_argument("-o", "--out")
    s = sub.add_parser("ask", help="send a prompt to the pay-per-token Inference API and print the answer")
    s.add_argument("prompt", help="the prompt to send")
    s.add_argument("--model", help="model id (default: the server's default)")
    s.add_argument("--key", help="inference API key — lands in shell history and `ps`, so prefer "
                                 "--key-stdin / $PETABYTE_API_KEY / saved config; use only with "
                                 "short-lived keys (e.g. CI)")
    s.add_argument("--key-stdin", action="store_true", dest="key_stdin",
                   help="read the API key from stdin, keeping it out of argv and shell history")

    # seller: read node/payout state, and feed the model cache-locality signal
    sub.add_parser("earnings", help="your balance, withdrawable earnings and recent payouts")
    n = sub.add_parser("node", help="inspect a node you host")
    ns = n.add_subparsers(dest="node_cmd", required=True)
    st = ns.add_parser("status", help="node status: online/attested, utilization, jobs, earnings")
    st.add_argument("spec_id", type=int)
    sm = ns.add_parser("sync-models",
                       help="scan the local ~/.petabyte cache and report it to the marketplace")
    sm.add_argument("spec_id", type=int)

    s = sub.add_parser("render", help="render a .blend on the GPU farm — pay only for render time")
    s.add_argument("file", help="path to a .blend scene")
    s.add_argument("--frames", default="1-1", help="frame range, e.g. 1-120 or 5")
    s.add_argument("--samples", type=int, default=128)
    s.add_argument("--nodes", type=int, default=1, help="split the frame range across N nodes")
    s.add_argument("--hours", type=int, default=1, help="max hours to escrow (unused is refunded)")
    s.add_argument("--out", default="./renders", help="download frames here")

    s = sub.add_parser("transcode", help="GPU-transcode a video (NVENC) — drop a file, get it back")
    s.add_argument("file", help="path to a source video")
    s.add_argument("--codec", default="h264", help="h264|h265|av1|vp9")
    s.add_argument("--resolution", help="e.g. 1920x1080")
    s.add_argument("--crf", type=int, help="quality 0-51 (lower = better)")
    s.add_argument("--container", default="mp4", help="mp4|mkv|webm|mov|…")
    s.add_argument("--out", default="./transcoded", help="download output here")

    # product layer: dashboard, doctor, jobs, activity, agent, menu
    sub.add_parser("me", help="your dashboard (same as --me)")
    sub.add_parser("doctor", help="diagnose account, network, Docker, GPU and agent problems")
    j = sub.add_parser("jobs", help="your running instances and recent bookings")
    j.add_argument("--limit", type=int, default=10)
    sub.add_parser("activity", help="recent notifications")
    sub.add_parser("menu", help="the guided menu")
    sub.add_parser("version", help="CLI / Python / update status")
    ag = sub.add_parser("agent", help="seller agent: install | start | stop | status | logs")
    ags = ag.add_subparsers(dest="agent_cmd", required=False)
    # SUPPRESS, not a default: these same options exist on the ROOT parser, and argparse writes
    # a subparser's default over whatever the root already parsed. `petabyte --yes agent stop`
    # would therefore arrive at cmd_agent with yes=False. With SUPPRESS the attribute is set only
    # when the option is actually present on the sub-command, so the root value survives.
    _S = argparse.SUPPRESS
    ai = ags.add_parser("install", help="guided setup (same as --install-agent)")
    ai.add_argument("--sell", choices=["gpu", "cpu", "all"], default=_S)
    ai.add_argument("--price", type=float, default=_S)
    ai.add_argument("--no-egress-lockdown", dest="no_egress_lockdown", action="store_true", default=_S)
    ai.add_argument("--dry-run", dest="dry_run", action="store_true", default=_S)
    ai.add_argument("--reinstall", action="store_true", default=_S)
    ai.add_argument("-y", "--yes", action="store_true", default=_S)
    ar = ags.add_parser("start", help="start the agent (same as --run-agent)")
    ar.add_argument("--no-follow", dest="follow", action="store_false", default=_S)
    ar.add_argument("--foreground", action="store_true", default=_S)
    ak = ags.add_parser("stop", help="stop the agent (same as --kill-agent)")
    ak.add_argument("-y", "--yes", action="store_true", default=_S)
    ak.add_argument("--force", action="store_true", default=_S)
    ags.add_parser("status", help="what the agent is doing right now")
    al = ags.add_parser("logs", help="follow the agent log")
    al.add_argument("-n", "--lines", type=int, default=30)

    sh = sub.add_parser("ssh", help="set this computer up to reach your VMs (and connect to one)")
    sh.add_argument("vm", nargs="?", help="VM id to connect to (default: set up only)")
    sh.add_argument("--status", action="store_true", help="show what is configured on this machine")
    sh.add_argument("--setup", action="store_true", help="run the setup wizard even if already configured")
    sh.add_argument("--key", help="use this SSH public key (path to a .pub file)")
    sh.add_argument("--new-key", dest="new_key", action="store_true",
                    help="create a new key just for Petabyte instead of reusing one")
    sh.add_argument("--passphrase", action="store_true", help="ask for a passphrase when creating a key")
    sh.add_argument("--user", default="root", help="login user on the VM (default: root)")
    sh.add_argument("--print", dest="print_only", action="store_true", help="print the ssh command, don't run it")
    sh.add_argument("--dry-run", dest="dry_run", action="store_true", default=_S)
    sh.add_argument("-y", "--yes", action="store_true", default=_S)

    # model hub: discover/pull/manage AI models (Hugging Face-grade UX). Owns `model`, `pull`, `auth`;
    # `run` is shared with the compute flow above and dispatched smartly below.
    if mh_cli is not None:
        mh_cli.register(sub, include=("model", "pull", "auth"))
    return p


COMMANDS = {"deposit": cmd_deposit, "login": cmd_login, "wallet": cmd_wallet, "specs": cmd_specs,
            "run": cmd_run, "launch": cmd_launch, "vpn": cmd_vpn, "earnings": cmd_earnings,
            "node": cmd_node, "ask": cmd_ask, "render": cmd_render, "transcode": cmd_transcode,
            "me": cmd_me, "doctor": cmd_doctor, "jobs": cmd_jobs, "activity": cmd_activity,
            "version": cmd_version, "agent": cmd_agent, "ssh": cmd_ssh}


def _dispatch(a, cfg, p):
    if a.help:
        if petabyte_cli is not None:
            from petabyte_cli import help as _help
            _help.render(_ui.out, version=_vc.installed_version())
        else:
            p.print_help()
        return 0
    if a.version or a.cmd == "version":
        return cmd_version(a, cfg)
    if a.me:
        return cmd_me(a, cfg)
    if a.install_agent:
        a.agent_cmd = "install"; return cmd_agent(a, cfg)
    if a.run_agent:
        a.agent_cmd = "start"; return cmd_agent(a, cfg)
    if a.kill_agent:
        a.agent_cmd = "stop"; return cmd_agent(a, cfg)
    if a.cmd is None or a.cmd == "menu":
        if petabyte_cli is not None and _ui.out.interactive:
            from petabyte_cli import menu as _menu
            return _menu.run(_ui.out, lambda argv: _run_menu_choice(argv, cfg, p))
        if petabyte_cli is not None:
            from petabyte_cli import help as _help
            _help.render(_ui.out, version=_vc.installed_version())
        else:
            p.print_help()
        return 0
    if mh_cli is not None and a.cmd in ("model", "pull", "auth"):
        return mh_cli.handle(a) or 0
    if a.cmd == "run" and _is_model_ref(a.file):
        if mh_cli is None:
            _die("model runtime unavailable (modelhub not importable)")
        ns = __import__("argparse").Namespace(
            id=a.file, format=a.format, quantization=a.quantization, revision=a.revision,
            force=a.force, home=None)
        return mh_cli.cmd_run(ns) or 0
    return COMMANDS[a.cmd](a, cfg) or 0


def _run_menu_choice(argv, cfg, p):
    """Run one menu selection as if it had been typed. The globals are re-applied from the
    chosen argv, so a menu entry carrying --json/--verbose is honoured like a real invocation."""
    global JSON, VERBOSE
    a = _build_parser().parse_args(argv)
    JSON, VERBOSE = bool(a.json), bool(a.verbose)
    if _ui is not None:
        _ui.out.quiet = JSON
    return _dispatch(a, cfg, p)


def main(argv=None):
    global JSON, VERBOSE
    p = _build_parser()
    a = p.parse_args(argv)
    JSON, VERBOSE = bool(a.json), bool(a.verbose)
    if _ui is not None:
        _ui.refresh()
        _ui.out.quiet = JSON
    cfg = _cfg()
    if a.api:
        cfg["api_url"] = a.api
    _startup(a)
    try:
        rc = _dispatch(a, cfg, p)
    except KeyboardInterrupt:
        if _ui is not None:
            _ui.err.blank(); _ui.err.info("Cancelled.")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        if petabyte_cli is None:
            raise
        err = e if isinstance(e, _errors.CliError) else _errors.from_exception(e, cfg.get("api_url", ""))
        if VERBOSE:
            import traceback
            traceback.print_exc()
        if JSON:
            _ui.err.raw(json.dumps(err.as_dict(), default=str))
        else:
            err.render(_ui.err)
        sys.exit(getattr(err, "exit_code", 1))
    sys.exit(int(rc or 0))


def _is_model_ref(arg):
    """`run` overloads a file path and a model id. A model id has a source/slug shape and is NOT an
    existing local file or a notebook/script."""
    if os.path.exists(arg) or arg.endswith((".ipynb", ".py")):
        return False
    return ("/" in arg) or (":" in arg) or arg.startswith(("hf:", "pt:", "http://", "https://"))


if __name__ == "__main__":
    main()
