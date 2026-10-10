<!-- Source of truth: the petabyte CLI is developed in the (private) petabyte monorepo and
     mirrored here by scripts/build_cli_package.py. Open issues/PRs against this repo for the
     client; the server is closed-source. -->

# Petabyte CLI

`petabyte` is the command line for the Petabyte GPU marketplace — for **buyers** who rent verified
GPUs, for **sellers** who earn with their own hardware, and for anyone who wants an AI model on their
machine. Install → sign in → choose buyer or seller → go.

```bash
pip install -U petabyte-client        # the command it installs is `petabyte`
petabyte login                        # sign in with your browser — no password ever touches the CLI
petabyte --me                         # your dashboard: account, wallet, what's running, agent, system
```

Run `petabyte` on its own for a guided menu, or `petabyte --help` for the grouped command list.

## 5-minute Seller Quickstart

Turn an idle GPU (or CPU) into income. On the machine you want to sell:

```bash
pip install -U petabyte-client
petabyte login                 # browser sign-in
petabyte --install-agent       # guided wizard: checks the system, mints a node key, runs the official installer
petabyte --run-agent           # starts the seller agent and follows it coming online
petabyte --me                  # watch the node, the earning rate and your wallet
```

The wizard checks Python, Docker, the GPU and the network first, tells you exactly what to fix if
something is missing, then downloads the official installer **to a file** from your Petabyte host
(never `curl | bash`), shows what it will do (it needs administrator rights: Docker, the NVIDIA
container toolkit, a systemd service) and asks before running it. The node key it creates goes to the
installer through the environment only — it is never printed or logged.

Later: `petabyte --kill-agent` stops the agent safely (it warns if a job is running),
`petabyte agent status` / `petabyte agent logs` show what it is doing, `petabyte earnings` and
`petabyte node status <id>` show the money and the node.

- Linux: the agent is the `petabyte-agent` systemd service under `/opt/petabyte-agent`.
- Windows: the same service inside the agent's own `Petabyte` WSL2 distro the installer sets up
  (run the install from an **Administrator** PowerShell).
- macOS: the seller agent is not supported yet; buying works everywhere.

## Buyer Quickstart

```bash
petabyte login
petabyte deposit 20                                   # add funds (test credit in the sandbox)
petabyte specs                                        # available GPUs; use the ID column with --spec
petabyte launch ollama --hours 2                      # choose a GPU from the interactive picker
petabyte run train.ipynb --gpu "RTX 4090" --hours 1   # run a notebook / .py on a rented GPU
petabyte instances                                         # what's running, recent bookings
petabyte extend <vm-id> --hours 2                     # add hours to a running VM (shows the charge, confirms)
petabyte snapshot create <vm-id> --name my-env        # save the running container as an image (0.5.2+)
petabyte launch jupyter --snapshot <snapshot-id>      # relaunch from it (petabyte snapshot list / delete <id>)
petabyte ssh                                          # one-time: make this computer able to reach your VMs
petabyte ssh <vm-id>                                  # then connect
petabyte ask "explain attention" --model llama3.2     # pay-per-token inference (OpenAI-compatible)
```

To select a specific host, copy its **ID** from `petabyte specs` into
`petabyte launch <template> --spec <ID> --hours 1`. For example, if the table shows ID `246`:

```bash
petabyte launch fedora-kde --spec 246 --hours 1 --max-price 0.13
```

From client version 0.4.9, `launch --spec` also accepts the marketplace's public host ID.

From 0.6.0, choose the provider and region:

```bash
petabyte specs --provider aws --region US                   # on-demand AWS GPUs in the US
petabyte launch jupyter --provider community --region SA    # seller GPUs in Saudi Arabia only
petabyte launch jupyter --provider alibaba --region me-central-1 --hours 2 --yes
```

* `--provider`:
  * `any` (default): a seller GPU, else the cheapest on-demand cloud GPU.
  * `community`: seller GPUs only.
  * `do` (DigitalOcean), `aws` or `alibaba`: an on-demand GPU from that cloud.
* `--region`: a country code (`SA`, `US`) or a cloud region (`tor1`, `us-east-1`, `me-central-1`).
* An on-demand GPU shows its price and asks before booking; `--yes` books it without asking.

From 0.5.0, spot (interruptible, billed per second) rentals:

```bash
petabyte launch jupyter --spot --max-price 0.50 --hours 1                    # now, or fail
petabyte launch jupyter --spot --gpu a100-80gb --max-price 1.20 --within 24h # wait for one
petabyte orders                 # waiting orders
petabyte orders cancel <id>
```

A waiting spot order starts (and emails you) as soon as a matching host is idle, and resumes from
its checkpoint after an interruption until it expires. `--spec ID` and `--gateway us|sa` pin a host
or gateway.
You do not need to translate a public ID into a number. Without `--spec`, an interactive terminal
shows a numbered GPU picker; scripts and `--json` let the server select a matching host.

## Commands

| Group | Command | What it does |
|---|---|---|
| **Account** | `--me` / `me` | dashboard: identity, wallet, current workflow, agent, expected return (estimates labelled), local CPU/RAM/GPU |
| | `login` | browser device-flow sign-in; saves a token in `~/.petabyte/cli.json` (0600) |
| | `wallet` · `deposit <usd>` · `activity` | balance & earnings · add funds · recent notifications |
| | `doctor` | diagnoses Python, network, API, account, Docker, GPU, agent — with the command to run next |
| **Seller** | `--install-agent` | guided seller setup (`--sell gpu\|cpu\|all`, `--price`, `--dry-run`, `--yes`) |
| | `--run-agent` | preflight → start the service → status panel → follow the log (`--no-follow`) |
| | `--kill-agent` | graceful stop; warns about active jobs (`--force` to stop anyway); idempotent |
| | `agent status\|logs\|install\|start\|stop` | the same as subcommands |
| | `earnings` · `node status <id>` · `node sync-models <id>` | payouts · one node in detail · report cached models |
| **Buyer** | `specs` · `launch <template>` · `run <file>` · `jobs` | rent and run |
| | `extend <vm-id> --hours N` | add hours to a running VM: shows hours × $/hr, confirms (`--yes` to skip); accepts a pasted address/URL (0.5.1+) |
| | `snapshot create <vm-id>\|list\|delete <id>` | save a running VM's container as an image in Petabyte object storage; warns + confirms when that leaves a residency region (`--yes` to accept); relaunch with `launch <template> --snapshot <id>` (0.5.2+) |
| | `ssh [vm-id]` | set this computer up to reach your VMs, then connect (`--status`, `--print`, `--key`, `--new-key`) |
| | `ask "<prompt>"` · `render` · `transcode` · `vpn <booking>` | inference · Blender · NVENC · WireGuard config |
| **Models** | `model search\|info\|pull\|list\|inspect\|remove` · `pull <id>` · `run <model-id>` | local model hub (no account needed) |
| **System** | `--version` · `--json` · `--verbose` · `--api <url>` · `-y/--yes` | version & update status · machine-readable output · full error detail · another host · assume yes |

Every subcommand keeps its full option list under `petabyte <command> --help`.

## Output, config & environment

- **Rich terminal UI** (panels, tables, spinners) on a real terminal; **plain, aligned text** on a pipe,
  in CI, under `NO_COLOR=1` / `PETABYTE_COLOR=never` / `TERM=dumb`, or when `rich` is missing. The
  words are the same in both. `PETABYTE_UI=plain|rich` forces a renderer, `PETABYTE_ASCII=1` avoids
  Unicode symbols, `PETABYTE_NONINTERACTIVE=1` makes every prompt take its default.
- `--json` (before the command) gives machine-readable output for `--me`, `doctor`, `wallet`,
  `specs`, `earnings`, `jobs`, `activity`, `agent status`, `--version` — never coloured, never chatty.
- **Update check:** once a day, on an interactive terminal only, the CLI asks PyPI (1.5 s timeout)
  whether a newer `petabyte-client` exists and prints a one-line hint. It never blocks, never fails a
  command, and is skipped in CI or with `PETABYTE_NO_UPDATE_CHECK=1`. Python < 3.9 gets a clear
  unsupported-version message.
- **API host:** `--api <url>` > `PETABYTE_API_URL` > the saved file > `https://petabyte.market`.
  `PETABYTE_CONFIG=/path/cli.json` isolates the saved token (CI, tests).
- **Auth:** the saved token from `petabyte login`, or `PETABYTE_TOKEN`, or an `account`-scoped API
  key in `PETABYTE_API_KEY` (sent as `X-API-KEY`). `petabyte ask` uses an `inference`-scoped key.
- Errors are written for humans — what happened, why, what to run next — with the technical detail
  dimmed last; `--verbose` adds the traceback. Secrets (tokens, keys, node keys) never appear.

## When something is off

```bash
petabyte doctor
```

## For developers

From a source checkout: `python cli/petabyte.py <cmd>` (the product layer lives in `cli/petabyte_cli/`:
`ui.py`, `version_check.py`, `sysinfo.py`, `api.py`, `dashboard.py`, `agent.py`, `agent_cmds.py`,
`doctor.py`, `help.py`, `menu.py`). Tests: `python cli/petabyte_cli_test.py` (no server),
`python cli/cli_ui_test.py`, `python cli/cli_petabyte_test.py` (boots a local API).
The package is built from the repo-root `pyproject.toml` (`name = "petabyte-client"`, dependencies
`httpx`, `rich`, `tqdm`; optional extra `system` = `psutil` for live CPU/RAM) and mirrored to the public
repo by `scripts/build_cli_package.py`.

## Dashboard (web)

Served by the API at `/` — live nodes/jobs stats, wallet + deposit, the GPU inventory with a live
$/hr-vs-AWS savings column, and one-click job runs. Both the web console and the CLI need an
attested, online seller node (run the agent) to actually execute jobs.
