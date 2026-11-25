# forge-ci.com

Live utilization dashboard for the forge CI fleet: the **E640** self-hosted
runner fleet (`dell-jw22zc3` and `dell-fsx3mn2`) and the **admin droplet** (`signal.forge-ci.com`,
DigitalOcean nyc1).

```
host ──(/proc every 5 s)──> forge-metrics-agent ──POST /api/ingest──> FastAPI on Render
                                                                       │ in-memory, 2 h rolling window
browser <──── GET /api/history (backfill) + /api/stream (SSE, live) ───┘
```

- **backend/** FastAPI. `POST /api/ingest` (bearer token per host; the server
  stores only SHA-256 hashes and decides the host from the token),
  `GET /api/hosts`, `GET /api/history?seconds=`, `GET /api/stream` (SSE),
  `GET /healthz`. One worker by design: the store is process-local and refills
  from the agents within minutes of a restart.
- **frontend/** Vite + strict TypeScript + uPlot. One chart per metric (CPU,
  memory, load per core, network, disk), one line per host, crosshair tooltip,
  direct labels, a data table view, light and dark palettes.
- **agent/** `forge_metrics_agent.py`, standard library only. Reads `/proc`
  (CPU, load, memory, physical-interface network, whole-disk I/O, uptime).

## Checks

All of these also run inside `docker build`, so Render cannot ship a failing gate.

```sh
uv sync
uv run ruff format --check .   # Black-compatible formatting
uv run ruff check .            # every ruff rule, isort ("I") included
uv run basedpyright            # typeCheckingMode = "all"
uv run pytest

cd frontend && npm ci
npm run check                  # prettier --check, tsc --strict, eslint strictTypeChecked
npm run build
```

Local dev: `uv run uvicorn forge_dash.app:app_factory --factory --app-dir backend --reload`
and `cd frontend && npm run dev` (Vite proxies `/api` to port 8000).

## Deploy

Render web service `forge-ci-com` (Docker runtime, see `render.yaml`) builds
this public repo. Set `FORGE_INGEST_TOKEN_SHA256` to
`{"dell-jw22zc3": "<sha256 hex>", "dell-fsx3mn2": "<sha256 hex>", "admin-droplet": "<sha256 hex>"}`.

Install or update an agent (the token goes over stdin and lands in
`/var/lib/forge-metrics/token`, mode 0600):

```sh
deploy/install-agent.sh root@192.168.1.173 https://forge-ci-com.onrender.com/api/ingest < e640.token
deploy/install-agent.sh root@159.89.232.188 https://forge-ci-com.onrender.com/api/ingest -i ~/.ssh/id_rsa < droplet.token
```

The container (`forge-metrics-agent`) uses `--network host`, a read-only root,
no capabilities, 64 MB and 0.2 CPU. Remove it with `docker rm -f forge-metrics-agent`.

The signed-in dashboard shows live GitHub Actions job counts for the two E640s.
Enable the dispatcher snapshot at `/run/forge/host-load.json` before installing
the agent. The installer mounts the parent directory read-only when present,
and observations older than 30 seconds appear as unavailable. On NixOS
runner hosts, install the optional Docker-independent observer with
`deploy/install-runner-count.sh <ssh-target>`. The agent uses its fresh
worker count while the dispatcher observation is unavailable.

Adding a host: add a `HostInfo` to `backend/forge_dash/config.py` (order = colour
order), add its token hash to the env var, run the installer.

## Soldr pairing

Register a GitHub OAuth app with callback
`https://forge-ci.com/soldr/pair/github`. Set `FORGE_SOLDR_GITHUB_CLIENT_ID`
and `FORGE_SOLDR_GITHUB_CLIENT_SECRET` on the `forge-ci-com` Render service.
For an existing service, add those two values in the Render Dashboard.

The Blueprint sets the first customer's numeric GitHub ID and generates a
persistent device signing key. On a service deployed outside the Blueprint,
set `FORGE_SOLDR_GITHUB_CUSTOMER_ID` to the allowed account ID and
`FORGE_SOLDR_SIGNING_KEY` to a stable, randomly generated secret of at least
32 bytes. Rotating the signing key invalidates previously issued credentials.
GitHub access tokens are used to fetch the account identity during the callback.

Configure `FORGE_SOLDR_WORKER_ORIGIN` with the HTTPS origin of a worker that
accepts Forge CI grants. Set `FORGE_SOLDR_WORKER_KEY` to a separate, randomly
generated secret of at least 32 bytes and provision it on that worker.
The discovery API returns no worker until both settings are valid.

## Git Ops

The signed-in dashboard opens with fleet utilization charts and live Dell job counts. Git Ops follows
with repository activity, per-job workflow and step details, execution placement,
and a sampled 24-hour outcome and duration summary. Individual host cards
appear below Git Ops.

`deploy/install-git-ops.sh actions root@signal-host` uses the existing
`forge-pool-watch` GitHub App on the admin droplet. It requests Actions read
access for repositories in that watcher's configuration, then posts a snapshot
with the droplet's existing metrics credential. The collector polls live runs
every two minutes and includes up to 12 completed workflow runs per repository
from the past 24 hours. A repository with incomplete coverage is marked in the
table. The GitHub counts include hosted jobs and are scoped to watched
repositories. The Dell host counts cover all locally observed workers.

`deploy/install-git-ops.sh runner root@runner-host` relays start and finish
events from the runner listener journal, using the existing host metrics token.
Events contain timestamp, runner and job name, phase, and result. GitHub
supplies repository, workflow, branch, attempt, job status, and step metadata.
The job details endpoint is available to signed-in dashboard users.
