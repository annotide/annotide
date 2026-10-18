# annotide Helm chart

Deploys the API, the arq worker, the web frontend and (optionally) the
reference model service. **PostgreSQL 16 and Redis 7 are external** in
production: use your cloud's managed services or your own operators. For
small and evaluation installs the chart can run both in the cluster instead
(see [In-cluster PostgreSQL and Valkey](#in-cluster-postgresql-and-valkey)).
Media never passes through the cluster: browsers read and write customer
storage through signed URLs.

## What it creates

| Component | Kind | Notes |
| --------- | ---- | ----- |
| `backend` | Deployment + Service | uvicorn on 8000; startup/liveness `/api/v1/health`, readiness `/api/v1/ready` (DB + Redis) |
| `worker` | Deployment | `arq app.worker.main.WorkerSettings`; liveness `python -m app.worker.check` (the heartbeat key `arq --check` reads, signed in like the worker); `/tmp` scratch for tiling (`worker.scratch.sizeLimit`) |
| `frontend` | Deployment + Service + ConfigMap | nginx on 8080 with the chart's config, proxying `/api/` to this release's backend |
| `model` | Deployment + Service | only with `model.enabled` (reference BYOM service on 9000) |
| `migrate` | Job (hook) | `alembic upgrade head` before every install and upgrade (after the first install with the in-cluster PostgreSQL) |
| `postgresql` | StatefulSet + Service + Secret | only with `postgresql.enabled`; one pod on a PVC |
| `valkey` | Deployment + Service + Secret | only with `valkey.enabled`; no persistence |
| Ingress, PDBs, ServiceAccount | | optional / defaults on where it makes sense |
| NetworkPolicies | | only with `networkPolicy.enabled`; ingress rules per component |
| HPA (API), KEDA ScaledObject (worker) | | only with `backend.autoscaling.enabled` / `worker.autoscaling.enabled` (OPS-5) |
| PrometheusRule | | only with `alerts.enabled` (OPS-4) |

Every pod runs as the image's numeric user with a read-only root filesystem,
no capabilities and the RuntimeDefault seccomp profile; writable paths are
`emptyDir` volumes.

## Install

Published images (`ghcr.io/annotide/annotide-{backend,frontend,model}`,
multi-arch `linux/amd64`/`linux/arm64`) and the chart itself
(`oci://ghcr.io/annotide/charts/annotide`) are built and pushed by
`.github/workflows/release.yml` on every `vX.Y.Z` tag — nothing to build
yourself for a stock install. To build your own (a fork, or to add
`--build-arg EXTRAS=otel` to the backend for OpenTelemetry), build
`backend/`, `frontend/` and `model-service/` and set `image.registry` to
where you pushed them.

```sh
helm upgrade --install annotate oci://ghcr.io/annotide/charts/annotide \
  --namespace annotate --create-namespace \
  --set publicUrl=https://annotate.example.com \
  --set ingress.enabled=true --set 'ingress.hosts[0].host=annotate.example.com' \
  --set database.url='postgresql+asyncpg://annotation:…@db:5432/annotation' \
  --set redis.url='rediss://:…@cache:6380/0' \
  --set secrets.secretKey="$(openssl rand -hex 32)"
```

Prefer `secrets.existingSecret` in production: a Secret you manage with
`APP_DATABASE_URL`, `APP_REDIS_URL`, `APP_SECRET_KEY` and any optional keys
(`APP_OIDC_CLIENT_SECRET`, `APP_LICENSE_KEY`, …). The chart then creates no
Secret, and the migration hook reads yours.

First administrator (the notes printed by `helm install` repeat this):

```sh
kubectl -n annotate exec deploy/annotate-annotide-backend -- \
  python -m app.cli create-superuser --email you@example.com
helm test annotate -n annotate   # frontend → API → database + Redis
```

## Configuration

- `publicUrl` (or the first ingress host) sets `APP_FRONTEND_URL`,
  `APP_CORS_ORIGINS`, `APP_PUBLIC_HOSTNAME` (licence host binding, LIC-29)
  and the OIDC redirect URI.
- `oidc.*` configures single sign-on; `secrets.oidcClientSecret` holds the
  client secret. Register `<publicUrl>/api/v1/auth/oidc/callback` at the IdP.
- `config` takes any other non-secret `APP_*` / `OTEL_*` variable
  (docs/CONTRACTS.md, "Environment variables") and wins over derived values.
  Set `APP_TRUSTED_PROXIES` to the ingress controller's pod CIDR so audit
  rows record real client IPs.
- `otel.enabled` needs the `otel` backend image; the API and worker refuse
  to start without it.
- The CSP's allowed media origins are baked into the frontend image at build
  time (`CSP_MEDIA_ORIGINS`, default `https:`).
- `ingress.host` is a single hostname as a plain string. When set, it
  replaces `ingress.hosts`, for tools that can't set list entries (Azure
  cluster extension settings). With `ingress.clusterIssuer` the derived
  public URL is `https://`.
- `secrets.generateSecretKey` generates `APP_SECRET_KEY` on the first
  install and keeps it in the `<release>-secret-key` Secret, which outlives
  `helm uninstall`. It's for installs where nobody supplies a key (the Azure
  Marketplace). It doesn't work with `helm template`.
- `global.azure.images` is for the Azure Marketplace bundle only
  ([infra/marketplace](../../marketplace/README.md)): every image as
  `{registry, image, tag | digest}` by key, which wins over `image.*`.
- `networkPolicy.enabled` isolates every pod: the frontend takes traffic on
  8080 from `networkPolicy.ingressFrom` (your ingress controller; anywhere
  when empty), the API only from the frontend (plus
  `networkPolicy.backendExtraFrom`, e.g. a metrics scraper), the model
  service from the API and worker, and the worker and migration Job from
  nothing. Egress is left open: storage and model endpoints are the
  customer's and can be anywhere. Needs a CNI that enforces policies.

## In-cluster PostgreSQL and Valkey

For installs that cannot or need not use managed services (the Azure
Marketplace offer turns both on by default: its ARM template may create only
the AKS cluster and the extension), leave `database.url` / `redis.url` empty
and set:

```sh
  --set postgresql.enabled=true --set valkey.enabled=true
```

The two are independent: an external PostgreSQL with an in-cluster Valkey
works too. Setting both a URL and the matching `enabled` fails the render.

- **PostgreSQL 16** (`postgres` image): one StatefulSet pod with its data on
  a PVC (`postgresql.persistence.size`, default 32 Gi, on the cluster's
  default StorageClass). The PVC and the `<release>-postgresql` Secret
  (the password the data directory was initialised with) are not part of
  the release, so `helm uninstall` leaves both and a reinstall under the
  same name finds them. On AKS, `postgresql.persistence.azureDisk.enabled`
  creates a StorageClass with `reclaimPolicy: Retain` (`skuName`
  `StandardSSD_ZRS` / `Premium_ZRS` for zone-redundant disks).
- **Valkey** (BSD-licensed, Redis-compatible): one Deployment pod with a
  generated password and no persistence.

### Risks of running the database in the cluster

Fine for evaluations, pilots and small teams that can accept minutes of
downtime and run their own backups. For production with an availability
or recovery target, use a managed database (Azure Database for PostgreSQL,
Amazon RDS, Cloud SQL) through `database.url`.

| Risk | What happens | Mitigation |
| ---- | ------------ | ---------- |
| **No automatic backups** | No point-in-time restore. A bad upgrade, an accidental delete or a corrupted disk can be recovered only from a backup you made | Scheduled `pg_dump` to storage outside the cluster, or volume snapshots (Azure Backup for AKS, Velero): [BACKUP.md](../../../docs/BACKUP.md). Test a restore |
| **Deleting the cluster deletes the data** | On AKS the disk lives in the cluster's node resource group (`MC_…`); deleting the cluster, or that group, deletes it, whatever the reclaim policy | Backups outside the cluster. Lock the node resource group if your policy allows |
| **Deleting the namespace or the PVC** | With AKS's built-in StorageClasses (`reclaimPolicy: Delete`) the disk is deleted with the PVC | `postgresql.persistence.azureDisk.enabled` (Retain; the Marketplace default). The disk then stays, and a person rebinds it |
| **One instance, no failover** | Node failures, node image and Kubernetes upgrades, and evictions stop the database until the pod runs again elsewhere: typically one to a few minutes, longer after an unclean node failure (Azure Disk detach). The app answers 503 meanwhile | Accept the window, schedule AKS maintenance windows, or use a managed database with high availability |
| **Zone outage** | A locally redundant (LRS) disk is tied to one availability zone; the pod cannot start in another until the zone recovers | ZRS `skuName` in a region with zones |
| **Lost password Secret** | PostgreSQL keeps the password it was initialised with; without the `<release>-postgresql` Secret the app cannot sign in until an operator resets it | Back up the Secret together with the database and `APP_SECRET_KEY` |
| **Disk full** | The database stops accepting writes; annotators' saves fail | Monitor the PVC; raise `postgresql.persistence.size` (Azure Disk expands online) |
| **Upgrades are yours** | Chart releases bring PostgreSQL minor versions; a major version (16 → 17) needs a dump and restore, which the chart does not do | Follow the release notes; [BACKUP.md](../../../docs/BACKUP.md) for dump/restore |
| **Shared resources** | The database competes with the API and worker for the node's CPU and memory | Raise `postgresql.resources`, or pin it with `postgresql.nodeSelector` / `affinity` |
| **No TLS inside the cluster** | App ↔ database and app ↔ Valkey traffic is unencrypted on the pod network (the disk itself is encrypted at rest by Azure) | `networkPolicy.enabled`, so only the API, worker and migration Job reach them; a managed database with TLS where policy requires it |

Valkey holds only job ids. A restart delays queued jobs by up to five
minutes (the worker queues them again from their PostgreSQL rows) and
resets rate-limit windows; no data is lost.

**Moving to a managed database later** is a `pg_dump` / `pg_restore`
([BACKUP.md](../../../docs/BACKUP.md)), then `postgresql.enabled=false` and
`database.url` in one upgrade. Switching the setting alone is not a
migration: the app would start on an empty database.

### Operating notes

- Passwords are generated on the first install and read back from the live
  Secrets on upgrades. `helm template` (GitOps) cannot read them: set
  `postgresql.password` and `valkey.password`. With `secrets.existingSecret`
  they are required, and your Secret's `APP_DATABASE_URL` and
  `APP_REDIS_URL` must use them
  (`postgresql+asyncpg://annotation:<pw>@<release>-annotide-postgresql:5432/annotation`,
  `redis://:<pw>@<release>-annotide-valkey:6379/0`).
- On the first install the migration runs after the release (post-install)
  instead of before it, once the database exists; the API and worker start
  without the schema meanwhile (readiness checks only that the database
  answers). Upgrades migrate first, as with an external database.
- With `networkPolicy.enabled`, PostgreSQL accepts the API, worker and
  migration Job, plus `networkPolicy.postgresqlExtraFrom` (KEDA's operator
  for `worker.autoscaling`, a backup job); Valkey accepts the API and worker.
  The Azure Marketplace bundle turns it on.
- Keep the `<release>-annotide-postgresql` Secret as long as the volume.
  PostgreSQL sets its password only when it first initialises the data
  directory, so a Secret deleted by hand (while the PVC stays) makes the
  next install or upgrade generate a new password the database rejects:
  the API and worker fail with "password authentication failed". Restore
  the Secret from a backup, or set `postgresql.password` to the original.

## Autoscaling (OPS-5)

- `backend.autoscaling` adds a CPU HorizontalPodAutoscaler for the API.
- `worker.autoscaling` needs [KEDA](https://keda.sh). Its PostgreSQL scaler
  counts `queued` + `running` rows in the `job` table (the queue's source of
  truth) and aims for `jobsPerReplica` per worker, never below one replica
  (the worker runs the cron jobs too). The scaler connects with a libpq
  string: `worker.autoscaling.postgresConnection`, or `database.url` with the
  `+asyncpg` suffix removed, kept in its own `<release>-keda` Secret. With
  `secrets.existingSecret`, add the key `KEDA_POSTGRES_CONNECTION` to it. A
  read-only role that can `SELECT` from `job` is enough.
- Scaling in is one pod a minute after `cooldownPeriod`. A stopped worker
  lets running jobs finish for 45 s, then puts them back in the queue; the
  pod's 60 s grace period covers that.

## Alerts (OPS-4)

`alerts.enabled` renders a PrometheusRule for the prometheus-operator (set
`alerts.labels` to match its `ruleSelector`). It needs `otel.enabled` and a
collector exporting metrics to Prometheus with the default name translation.

| Alert | Severity | Fires when |
| ----- | -------- | ---------- |
| `AnnotationApiErrorRate` | critical | 5xx share above `apiErrorRatio` (5 %) for 10 min |
| `AnnotationApiSlow` | warning | p95 above `apiLatencyP95Seconds` (1 s) for 15 min |
| `AnnotationNoWorker` | critical | no worker has reported the backlog gauges for 10 min |
| `AnnotationQueueStalled` | critical | the oldest queued job has waited over `queueWaitSeconds` (10 min) |
| `AnnotationJobFailureRate` | warning | over `jobFailureRatio` (20 %) of job runs fail, cron ticks excluded |
| `AnnotationCronFailing` | warning | a worker cron failed more than twice in 15 min |
| `AnnotationOutboxLagging` | warning | the oldest unpublished annotation is over `outboxAgeSeconds` (15 min) old |
| `AnnotationOutboxDead` | warning | outbox events reached `APP_OUTBOX_MAX_ATTEMPTS` |

Series used: `http_server_request_duration_seconds_*` (stable HTTP
semantic conventions, which the app opts into),
`annotation_worker_job_duration_seconds_*` and the backlog gauges
`annotation_jobs_queued`, `annotation_jobs_running`,
`annotation_jobs_queued_oldest_age_seconds`, `annotation_outbox_pending`,
`annotation_outbox_pending_oldest_age_seconds`, `annotation_outbox_dead`.
`../tests/alerts.test.yaml` tests the rules with `promtool test rules`.

## Validate

```sh
helm lint infra/helm/annotide -f infra/helm/annotide/ci/full-values.yaml
helm template t infra/helm/annotide -f infra/helm/annotide/ci/full-values.yaml \
  | kubeconform -strict -summary
```

CI runs both for every file in `ci/` (kubeconform with the CRD schemas from
datreeio/CRDs-catalog), plus the alert rule tests:

```sh
helm template t infra/helm/annotide -f infra/helm/annotide/ci/full-values.yaml \
  --show-only templates/prometheusrule.yaml | yq '.spec' > infra/helm/tests/rules.yaml
promtool test rules infra/helm/tests/alerts.test.yaml
```

A real install on a throwaway cluster (needs only Docker; runs k3s as a
privileged container and deletes it afterwards):

```sh
docker compose build backend frontend model
infra/helm/smoke/k3s.sh     # KEEP=1 to leave the cluster running
```

It checks the migration hook, `helm test`, Ingress → nginx → API →
database/Redis, an admin login, an upgrade that rolls the pods and cleans up
the hook Secret, and the worker's liveness probe. Last run: k3s v1.30.6,
2026-09-28, plus the browser sign-in E2E through a port-forward. The smoke
test does not install KEDA or the prometheus-operator: the ScaledObject and
PrometheusRule are validated against their CRD schemas and the rules with
promtool, not yet on a live cluster.
