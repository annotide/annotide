# Annotide quick install

A single-machine Docker Compose install of Annotide: Postgres, Redis, the
API, the worker and the web frontend, all published images from GHCR. No
build step, no Kubernetes. For production-scale or multi-node use, see the
[Helm chart](../../infra/helm/annotide/README.md) instead.

## Quick start

```sh
curl -fsSL https://annotide.com/install.sh | sh
```

This downloads `compose.yaml` into `~/annotide` (override with
`ANNOTIDE_DIR`), writes a `.env` with a generated database password and
token signing key, starts the stack, and walks you through creating the
first administrator. See [`install.sh`](install.sh) for every environment
variable it reads (install directory, port, registry override, etc).

## Manual install

```sh
mkdir -p ~/annotide/data && cd ~/annotide
curl -fsSL https://annotide.com/install/compose.yaml -o compose.yaml
cat > .env <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 20)
APP_SECRET_KEY=$(openssl rand -hex 32)
APP_INSTALL_ID=$(openssl rand -hex 16)
EOF
chmod 600 .env
docker compose pull
docker compose up -d --wait
docker compose exec backend python -m app.cli create-superuser --email you@example.com
```

Open `http://localhost:8080`.

## Configuration

Everything lives in `.env` next to `compose.yaml`. The three generated
values (`POSTGRES_PASSWORD`, `APP_SECRET_KEY`, `APP_INSTALL_ID`) are
required; everything else is optional and documented inline in the
generated file. The full list of `APP_*` variables the backend reads is in
[`docs/CONTRACTS.md`](../../docs/CONTRACTS.md#environment-variables-ops-1);
anything in that list can be added to `.env` and picked up by
`docker compose up -d` (the compose file forwards a curated subset with
sane defaults — add others as plain `KEY=value` lines and wire them into
`compose.yaml`'s `x-app-env` block if you need one that isn't there yet).

Common ones:

- `ANNOTIDE_PORT` — host port for the web UI (default `8080`, bound to
  `127.0.0.1` only).
- `APP_LICENSE_KEY` — paste a licence key, or do it later under
  Settings -> Licence in the app. Unset runs the Community edition (up to
  three users, commercial use included — see [LICENSING.md](../../docs/LICENSING.md)).
- `APP_TELEMETRY_ENABLED=false` — turns off the licence heartbeat entirely.
  It is on by default and sends only salted hashes (no names, e-mails or
  media) to `APP_LICENSE_SERVER_URL`, and no licence check ever depends on
  it. `APP_LICENSE_SERVER_URL=` (set, but empty) turns off every call to
  the licence server: the heartbeat, the licence refresh and the trial.
- `COMPOSE_PROFILES=models` — also starts the bundled reference model
  service (an offline heuristic detector, no GPU or weights needed) for
  pre-labelling. Without it, register your own model endpoint in the UI.
- `MODEL_BACKEND` / `MODEL_PATH` — point the bundled model service at a
  real ONNX detector instead of the heuristic one (needs `COMPOSE_PROFILES=models`).

### Storage

Media is never proxied by the API in the general case — the browser reads
and writes it directly via signed URLs. This install ships the `local`
storage connector instead of a cloud account: `compose.yaml` bind-mounts
`./data` into the backend and worker containers at `/data`. After signing
in, go to **Settings -> Connectors**, add a connector of type `local` with
root `/data` (or a subdirectory of it), and point a project's source/result
connector at it. Back up `./data` along with the database (below) — it is
the only copy of anything written to it.

To use real cloud storage (Azure Blob, S3, GCS) instead, register that
connector in the UI with the appropriate credential reference; the bind
mount above can then be left empty or removed.

### Exposing this beyond localhost

By default the UI is bound to `127.0.0.1` and `APP_ENV=production`, which
requires HTTPS for secure cookies — browsers also treat `127.0.0.1` and
`localhost` as secure contexts, so this works out of the box. If you expose
the install beyond loopback (changing the published port's bind address,
or port-forwarding it), put a TLS-terminating reverse proxy in front of it
first; otherwise sign-in cookies will not be sent and login will silently
fail.

## Upgrade

```sh
cd ~/annotide
docker compose pull
docker compose up -d
```

Pin a version instead of always tracking `latest` by setting
`ANNOTIDE_VERSION=X.Y.Z` in `.env`. Database migrations run automatically
as part of the backend container's startup command.

## Backup

Back up the `postgres_data` named volume (`docker compose exec postgres
pg_dump` or a volume snapshot) and the `./data` directory if you are using
the local storage connector. See [BACKUP.md](../../docs/BACKUP.md) for the
full procedure and restore steps.

## Uninstall

```sh
cd ~/annotide
docker compose down -v   # also deletes the database — back up first!
cd .. && rm -rf annotide
```

`docker compose down` (without `-v`) stops everything but keeps the named
volumes, so `docker compose up -d` later picks up where you left off.
