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
- `COMPOSE_PROFILES=sam2` — starts SAM2 for the Smart polygon tool (below).
  With the reference model too: `COMPOSE_PROFILES=models,sam2`.

### Smart polygon with SAM2

SAM2 outlines the object you click, or the one inside a box you drag, in the
annotator's Smart polygon tool (`S`). It runs on the CPU in its own
container (`annotide-model-sam2`, about 1 GB) and keeps your images on this
machine.

1. Add `COMPOSE_PROFILES=sam2` to `.env`, then `docker compose up -d`.
2. In the app, open **Models** → **Register model**: any name, **Task**
   *Segment*, **Endpoint URL** `http://model-sam2:9000`.
3. In the annotator, press `S`, pick the model under **Smart polygon**, and
   click an object.

The first click on an image takes about a second while SAM2 reads the image;
further clicks on it answer in tens of milliseconds. The last
`SAM2_CACHE_ITEMS` images (default 16, about 17 MB of memory each) stay
ready. The class you draw in must allow polygons; the canvas says so if it
does not.

#### SAM2 on another machine

SAM2 is a plain HTTP service, so it can run anywhere this install can
reach, for example a machine with more CPU:

```sh
docker run -d --restart unless-stopped -p 9000:9000 \
  -e MODEL_API_KEY=<a long random key> -e OCR_ENGINE=none \
  ghcr.io/annotide/annotide-model-sam2:latest
```

The annotator never talks to it directly: on a click, this install sends
SAM2 the click and a short-lived link to the image, SAM2 fetches the image
from that link and answers with the outline. So:

- **This install must reach SAM2.** Put HTTPS in front of it when the
  traffic leaves your network, and set the key: `MODEL_API_KEY` in this
  install's `.env` as well, then register the model with **Endpoint URL**
  `https://sam2.example.com`, **Identity** `bearer`, **Secret reference**
  `env:MODEL_API_KEY`.
- **SAM2 must reach the images.** With cloud storage (Azure Blob, S3, GCS)
  the link points at the storage itself. With local disk it points back at
  this install, so set `APP_INTERNAL_API_URL` to this install's address as
  SAM2 sees it, e.g. `https://annotate.example.com`.
- A click waits at most 8 seconds for SAM2, including fetching the image.

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

### Single sign-on (Business)

Sign-in through your identity provider (Entra ID, Okta, Google Workspace,
Keycloak, …) over OIDC or SAML 2.0 needs a licence with the `sso` feature
(Business) and the install on a public **https** address: the provider
sends people back to it, and browsers drop the sign-in cookie otherwise.
Put a TLS-terminating reverse proxy in front first (above), then add to
`.env`, with `https://annotate.example.com` as your address:

```sh
APP_FRONTEND_URL=https://annotate.example.com
APP_CORS_ORIGINS=https://annotate.example.com
APP_TRUSTED_PROXIES=<your proxy's address>

# OIDC (Entra ID: docs/ENTRA.md)
APP_OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
APP_OIDC_CLIENT_ID=<client id>
APP_OIDC_CLIENT_SECRET=<client secret>
APP_OIDC_REDIRECT_URI=https://annotate.example.com/api/v1/auth/oidc/callback

# or SAML 2.0 (docs/SAML.md); register Entity ID
# https://annotate.example.com/api/v1/auth/saml/metadata at the IdP
APP_SAML_IDP_METADATA_URL=<the IdP's metadata URL>
APP_SAML_ACS_URL=https://annotate.example.com/api/v1/auth/saml/acs
```

then `docker compose up -d`. The login page shows a *Sign in with …*
button beside the local form; the local admin account keeps working. The
compose file forwards the common `APP_OIDC_*` and `APP_SAML_*` settings
(admin groups, provisioning, e-mail linking); the full list is in
[`docs/CONTRACTS.md`](../../docs/CONTRACTS.md#environment-variables-ops-1).

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

### Reinstalling

The database lives in the Docker volume `annotide_postgres_data`, not in
`~/annotide`, and every install directory shares that name. It only accepts
the database password in the `.env` that created it, so `install.sh` stops
before starting anything when it finds the volume but no `.env` (the
directory was removed without `docker compose down -v`, or you are
installing into a new directory). Either copy the earlier install's `.env`
into the install directory to keep the old data, or delete the old database
with the command the installer prints (`docker volume rm
annotide_postgres_data`, after removing the old containers if they still
exist). Deleting the volume cannot be undone; the installer never does it
for you.

`.env` holds the database password and the token signing key, so keep the
copy readable by you only, as the installer created it:

```sh
cp -p /path/to/earlier/.env ~/annotide/.env && chmod 600 ~/annotide/.env
```
