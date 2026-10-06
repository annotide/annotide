# annotide Helm chart

Deploys the API, the arq worker, the web frontend and (optionally) the
reference model service. **PostgreSQL 16 and Redis 7 are external**: use your
cloud's managed services or your own operators. Media never passes through
the cluster: browsers read and write customer storage through signed URLs.

## What it creates

| Component | Kind | Notes |
| --------- | ---- | ----- |
| `backend` | Deployment + Service | uvicorn on 8000; startup/liveness `/api/v1/health`, readiness `/api/v1/ready` (DB + Redis) |
| `worker` | Deployment | `arq app.worker.main.WorkerSettings`; liveness `python -m app.worker.check` (the heartbeat key `arq --check` reads, signed in like the worker); `/tmp` scratch for tiling (`worker.scratch.sizeLimit`) |
| `frontend` | Deployment + Service + ConfigMap | nginx on 8080 with the chart's config, proxying `/api/` to this release's backend |
| `model` | Deployment + Service | only with `model.enabled` (reference BYOM service on 9000) |
| `migrate` | Job (hook) | `alembic upgrade head` before every install and upgrade |
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
- `networkPolicy.enabled` isolates every pod: the frontend takes traffic on
  8080 from `networkPolicy.ingressFrom` (your ingress controller; anywhere
  when empty), the API only from the frontend (plus
  `networkPolicy.backendExtraFrom`, e.g. a metrics scraper), the model
  service from the API and worker, and the worker and migration Job from
  nothing. Egress is left open: storage and model endpoints are the
  customer's and can be anywhere. Needs a CNI that enforces policies.

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
