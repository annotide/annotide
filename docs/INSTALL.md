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
| Azure: Terraform, then the Helm chart | Production on Azure, private networking (SEC-1) | [Terraform README](../infra/terraform/azure/README.md) |
| AWS: Terraform, then the Helm chart | Production on AWS (EKS, RDS, ElastiCache, S3) | [Terraform README](../infra/terraform/aws/README.md) |
| Google Cloud: Terraform, then the Helm chart | Production on Google Cloud (GKE, Cloud SQL, Memorystore, Cloud Storage) | [Terraform README](../infra/terraform/gcp/README.md) |

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
  group mappings on each project.
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
