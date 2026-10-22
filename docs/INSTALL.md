# Installation guide (OPS-8)

For the administrator who installs the platform in their own environment.
The platform runs entirely on your infrastructure. It reads media from your
object storage and never copies it, and it calls only the model endpoints
you register.

## 1. Choose how to run it

| Option | For | Guide |
| ------ | --- | ----- |
| Quick install (Docker Compose, published images) | One person or a small team, your own machine or a single server | §2 below |
| Docker Compose from source | Evaluation, demos, development | [README → Quick start](../README.md#development-quick-start) |
| Kubernetes with the Helm chart | Production on any cloud or on premises | [Helm chart README](../infra/helm/annotide/README.md) |
| Kubernetes with the Helm chart and the in-cluster database (the Azure Marketplace default) | Evaluations, pilots and small teams that run their own backups. [Read the risks](../infra/helm/annotide/README.md#risks-of-running-the-database-in-the-cluster) first | [Helm chart README → In-cluster PostgreSQL and Valkey](../infra/helm/annotide/README.md#in-cluster-postgresql-and-valkey) |
| Azure Container Apps, scaled to zero when idle (Deploy to Azure button) | One person or a team on Azure, not used all day | [Container Apps README](../infra/azure-container-apps/README.md) |
| Azure: Terraform, then the Helm chart | Production on Azure, private networking (SEC-1) | [Terraform README](../infra/terraform/azure/README.md) |
| AWS: Terraform, then the Helm chart | Production on AWS (EKS, RDS, ElastiCache, S3) | [Terraform README](../infra/terraform/aws/README.md) |
| Google Cloud: Terraform, then the Helm chart | Production on Google Cloud (GKE, Cloud SQL, Memorystore, Cloud Storage) | [Terraform README](../infra/terraform/gcp/README.md) |

Running it for one person or one team on a budget: see
[§8, Keeping the cost down on Azure](#8-keeping-the-cost-down-on-azure).

The "Docker Compose from source" stack (`docker-compose.yml` at the repo
root) includes emulated storage, development passwords and a mail catcher.
Do not expose it to the internet. The quick install below is the
stripped-down, published-image equivalent meant for a real (if small)
install.

## 2. Quick install

```sh
curl -fsSL https://annotide.com/install.sh | sh
```

One command: downloads [`deploy/personal/compose.yaml`](../deploy/personal/compose.yaml),
generates a `.env` with a database password and token signing key, starts
Postgres, Redis, the API, the worker and the frontend, and walks you through
creating the first administrator (`python -m app.cli create-superuser`,
the same bootstrap command §4 below uses — there is no sign-up page by
design, AUTH-2). Runs on `http://localhost:8080` by default, bound to
`127.0.0.1`. See [`deploy/personal/README.md`](../deploy/personal/README.md)
for configuration (licence key, telemetry, the bundled reference model
service), upgrades, backup and uninstall.

This uses the local storage connector (a bind-mounted `./data` directory)
rather than your own cloud storage — register a connector of your choice
under **Settings → Connectors** after signing in; see §5 below either way.
For more than a handful of users, prefer the Helm chart.

### Published images

Every tagged release (`.github/workflows/release.yml`) builds, signs
(cosign, keyless/OIDC) and publishes four multi-arch (`linux/amd64`,
`linux/arm64`) images to GHCR:

| Image | Built from |
| ----- | ---------- |
| `ghcr.io/annotide/annotide-backend` | `backend/` — also the worker (same image, different command) |
| `ghcr.io/annotide/annotide-frontend` | `frontend/` |
| `ghcr.io/annotide/annotide-model` | `model-service/` (reference BYOM model service) |
| `ghcr.io/annotide/annotide-model-sam2` | `model-service/`, target `sam2`: the same service with SAM 2.1 tiny for the smart polygon tool (`model.sam2` in the chart) |

Each is tagged `X.Y.Z`, `X.Y`, `X` (once X ≥ 1) and `latest`. The Helm chart
is published the same way, as `oci://ghcr.io/annotide/charts/annotide`
(chart `version` and `appVersion` match the release tag). Build your own
instead of using these if you need a build argument the published images
don't set (e.g. the backend's `otel` extra) — see the Compose file at the
repo root and the [Helm chart README](../infra/helm/annotide/README.md)
for the build-your-own path.

## 3. What you need

- **PostgreSQL 16** with the `citext` and `pgcrypto` extensions allowed. The
  migrations create them.
- **Redis 7**, over TLS (`rediss://`) in production.
- **Object storage you own**: Azure Blob, S3 or S3-compatible, or Google Cloud
  Storage. Use one container or bucket for source media and one for results
  (they can be the same), plus an optional one for derived data such as tiles
  and thumbnails.
- **A DNS name and a TLS certificate** for the app, e.g.
  `https://annotate.example.com`.
- **Container images**: the published `ghcr.io/annotide/annotide-*` images
  (§2 → Published images) work as-is for the Helm chart's defaults. Build
  your own from `backend/`, `frontend/` and `model-service/` instead if you
  need a build argument the published ones don't set — for the frontend,
  `CSP_MEDIA_ORIGINS` to the storage origins browsers will load media from
  (see §5).
- Optional: an OIDC identity provider for single sign-on, an SMTP server for
  e-mail notifications, an OpenTelemetry collector, and a licence key.

## 4. Install

Follow the guide for your option above. Then:

1. **Create the first administrator.**

   ```sh
   kubectl -n annotate exec deploy/annotate-annotide-backend -- \
     python -m app.cli create-superuser --email you@example.com
   ```

2. **Check it works.** `helm test <release>`, then `GET /api/v1/health`
   (liveness) and `GET /api/v1/ready` (database and Redis) both answer 200.
3. **Licence.** Without a key the install runs as Community (up to three
   users, commercial use included); an admin can start a 30-day Business
   trial under Settings → Licence. Paste the key under Settings → Licence, or set
   `APP_LICENSE_KEY`. See [LICENSING.md](LICENSING.md).

## 5. Connect your storage

The browser fetches media straight from your storage on short-lived signed
URLs (ARC-3, `APP_SIGNED_URL_TTL`, default 15 min). The API never proxies it.
That puts three requirements on the storage:

1. **Reachable from annotators' browsers.** A storage account behind a
   private endpoint only works for people on that private network. The
   Terraform module uses none and limits the public endpoint to an IP
   allow-list instead (see its README).
2. **CORS** allows the app's origin for `GET`, `HEAD`, `PUT` and `OPTIONS`.
   PUT is for browser uploads. **Check** on the Connectors page tests this and
   says exactly what is missing (SRC-7).
3. **Content Security Policy.** The frontend image's `CSP_MEDIA_ORIGINS`
   lists the storage origin. Real cloud storage is `https:` and is allowed by
   default. Anything else, such as a MinIO host, must be added at build time.
   If an origin is missing, the browser blocks the image and the server logs
   nothing.

Register the connector under **Connectors**. Prefer a cloud identity
(`managed_identity` on Azure, `iam_role` on AWS, `managed_identity` on GCP)
over keys. When a key is unavoidable, store it in a secret store and give the
connector a reference (`azurekeyvault://…`, `awssecrets://…`,
`gcpsecrets://…`, `env:…`, `file:…`). Connectors never hold the key itself
(AUTH-7).

## 6. Sign-in, people and notifications

- **Single sign-on:** set `APP_OIDC_ISSUER`, `APP_OIDC_CLIENT_ID` and, for a
  confidential client, `APP_OIDC_CLIENT_SECRET`. For Entra ID, see
  [ENTRA.md](ENTRA.md). Group-based roles use `APP_OIDC_ADMIN_GROUPS`, plus
  group mappings on each project. SAML 2.0 instead or as well: set
  `APP_SAML_IDP_METADATA_URL` and `APP_SAML_ACS_URL` (chart: `saml.*`); see
  [SAML.md](SAML.md) for Entra ID, Okta and Google Workspace.
- **Provisioning:** SCIM 2.0 under Settings → Users (AUTH-3).
- **E-mail:** set `APP_SMTP_HOST` and its companions (chart: `smtp.*`).
  People receive mentions, replies and review verdicts by e-mail and can turn
  them off on their Security page.
- **Slack / Microsoft Teams:** add a webhook with format *Slack* or
  *Microsoft Teams* under Webhooks, using the channel's incoming-webhook
  URL.
- **AI agents:** the MCP server in the SDK (`annotide mcp`) runs on the
  agent's machine with a service account's API key. See
  [sdk/README.md](../sdk/README.md).

## 7. Security checklist

- TLS end to end: the ingress, `rediss://`, and PostgreSQL with
  `sslmode`/`ssl=require`.
  The chart's in-cluster PostgreSQL and Valkey have no TLS: turn on
  `networkPolicy.enabled`, or use managed services where policy requires
  encryption in transit.
- `APP_SECRET_KEY`: 32+ random bytes, kept in your secret store. Rotating it
  signs everyone out, invalidates API keys and MFA enrolments (see
  [OPERATIONS.md](OPERATIONS.md)).
- `APP_TRUSTED_PROXIES`: your ingress controller's address range, so the
  audit log records real client IPs.
- `APP_WEBHOOK_ALLOW_PRIVATE_URLS` stays `false`, so webhooks cannot reach
  internal addresses or cloud metadata (SEC-4).
- **Telemetry.** The licence heartbeat is on by default and sends only salted
  hashes (no names, e-mail addresses or media). Its exact payload is under
  Settings → Licence. Set `APP_TELEMETRY_ENABLED=false` to turn it off.
  Nothing is sent unless `APP_LICENSE_SERVER_URL` is set, and no licence
  check depends on it (LIC-28).
- Back up before going live: [BACKUP.md](BACKUP.md).

Every setting is an environment variable listed in
[CONTRACTS.md → Environment variables](CONTRACTS.md#environment-variables-ops-1).

## 8. Keeping the cost down on Azure

A person or a team can run their own install and pay only for the Azure
resources it uses; Annotide itself is free for up to three active users (Community).
Only the Container Apps setup scales to zero by itself: it wakes on a
request or on work for the worker and sleeps again after a few idle minutes.
The others keep running until you **stop** them, which is what makes them
cheap when nobody uses them.

Approximate pay-as-you-go prices, West Europe, without VAT, October 2026.
Check your region in the [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/);
a one-year savings plan takes roughly a third off anything that runs all
the time.

| Setup | For | Running | Stopped |
| ----- | --- | ------- | ------- |
| Azure Container Apps, scaled to zero | One person or a team, not all day | about €18 / month idle, plus about €0.17 per hour in use | (sleeps by itself) |
| Quick install on one VM (`Standard_B2ms`, 2 vCPU / 8 GB) | One person or a small team | about €60 / month; about €20 on working hours only | disk and IP, a few € |
| Azure Marketplace, 1 × `Standard_D2as_v5` node | A small team that wants AKS | about €100 / month | disks and networking, about €20 |
| Azure Marketplace, 1 × `Standard_D4s_v5` node (the default) | A team, large images | about €180 / month | disks and networking, about €20 |
| Terraform with the small settings below | A team that wants managed PostgreSQL | about €140 / month | storage and networking, about €25 |
| Terraform defaults | Production | €500+ / month | n/a |

### Azure Container Apps

[`infra/azure-container-apps`](../infra/azure-container-apps/README.md):
one Deploy to Azure button creates the app, a small PostgreSQL server and a
storage account. The app runs as one replica that scales to zero when idle;
the first request after a quiet spell takes a few seconds. Only PostgreSQL
bills while nobody works. One replica fits a team; for whole-slide or
satellite images all day, use AKS.

### One VM with the quick install

The cheapest way for a small team: §2 on a Linux VM.

1. Create an Ubuntu VM (`Standard_B2ms`). Give its public IP a DNS name
   label (Networking → the IP → Configuration): you get
   `<label>.<region>.cloudapp.azure.com` for free.
2. Network security group: allow 80 and 443 from the internet and 22 only
   from your own address.
3. Install Docker, then run the quick install (§2).
4. Put HTTPS in front of it (the app refuses sign-in over plain HTTP beyond
   localhost, see [`deploy/personal/README.md`](../deploy/personal/README.md#exposing-this-beyond-localhost)).
   Caddy gets the certificate by itself; its whole `/etc/caddy/Caddyfile`:

   ```
   <label>.<region>.cloudapp.azure.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```

   Then set `APP_FRONTEND_URL=https://<label>.<region>.cloudapp.azure.com`
   in the install's `.env` and run `docker compose up -d`, so links in
   e-mails and sign-in redirects use that address.

5. VM → Operations → **Auto-shutdown** stops it every evening for free.
   Start it from the portal (or `az vm start`) in the morning; the data
   stays on its disk. Back up as in the quick install README.

### Azure Marketplace

Pick a 2 vCPU / 8 GB node (`Standard_D2as_v5` or `Standard_D2s_v5`) in the
install form for about half the cost of the default. It fits a small team;
tiling very large images takes longer. Choose the bigger node when people
work on whole-slide or satellite images all day.

Stop the whole cluster when nobody needs it, and start it again later:

```sh
az aks stop  --resource-group <group> --name <cluster>
az aks start --resource-group <group> --name <cluster>
```

The in-cluster database keeps its data on a retained disk, so a stop and a
start lose nothing. To do it on a schedule (evenings and weekends), run the
same two commands from an Azure Automation runbook whose managed identity
may start and stop the cluster (Contributor on the cluster resource).

### Terraform: small settings

[`infra/terraform/azure`](../infra/terraform/azure/README.md) defaults to
production sizes. For a small team, in `terraform.tfvars`:

```hcl
node_vm_size        = "Standard_D2as_v5"
node_count          = { min = 1, max = 3 }
postgres_sku        = "B_Standard_B1ms"
postgres_storage_mb = 32768
redis_sku           = { name = "Basic", family = "C", capacity = 0 }
```

Burstable PostgreSQL and Basic Redis have no failover and Basic Redis no
SLA; fine for a team, not for production. Stop and start as above, plus
`az postgres flexible-server stop|start`. Azure starts a stopped PostgreSQL
server again after seven days.
