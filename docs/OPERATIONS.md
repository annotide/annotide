# Operations guide (OPS-8)

For the administrator who keeps an installation running. Install it first
with [INSTALL.md](INSTALL.md).

## Upgrades

1. Back up the database ([BACKUP.md](BACKUP.md)).
2. Read the release notes for new settings. New environment variables are
   listed in [CONTRACTS.md](CONTRACTS.md#environment-variables-ops-1).
3. `helm upgrade` with the new image tags. The chart's migration hook runs
   `alembic upgrade head` before the new pods start. Migrations only move
   forward, so to roll back you restore the backup taken in step 1 and deploy
   the previous tag.
4. `helm test <release>`, and check `GET /api/v1/ready`.

Running jobs survive a rolling upgrade. The worker waits 45 s for them, and
anything it had to stop goes back to the queue.

## Monitoring

| What | Where |
| ---- | ----- |
| Liveness / readiness | `GET /api/v1/health`, `GET /api/v1/ready` (database + Redis) |
| Logs | JSON on stdout (`APP_LOG_FORMAT=json`), one line per request and job, with the trace id when tracing is on |
| Traces and metrics | OpenTelemetry to your collector: `APP_OTEL_ENABLED=true`, image built with the `otel` extra (OPS-3) |
| Alerts | The chart's PrometheusRule (OPS-4): error rate, queue backlog, failed jobs, outbox lag, licence expiry, storage errors. See the [chart README](../infra/helm/annotide/README.md#alerts-ops-4) |
| Audit | `GET /api/v1/audit` (superuser): sign-ins, permission, connector and annotation changes, exports (SEC-3). Feed it to your SIEM by polling with `since` |

## Scaling

- The API scales on CPU (HPA) and the workers on the job backlog (KEDA).
  Both are in the chart (OPS-5).
- The load test (`make loadtest`, [loadtest/README.md](../loadtest/README.md))
  ran 200 annotators on a laptop with p95 under 25 ms. Size PostgreSQL first:
  it carries the queue rows, the outbox and the statistics.

## Jobs

Scans, exports, snapshots, imports, pre-labelling and thumbnails are jobs.
The `job` row in PostgreSQL is the source of truth; Redis carries only its
id.

- **Failed:** the job list shows the error. **Retry** queues it again.
  Transient errors (storage briefly down, a model timeout) are retried
  automatically with backoff before a job is marked failed.
- **Stuck:** if Redis was flushed or restored, the `requeue_stranded_jobs`
  cron re-queues `queued` jobs every five minutes and fails overdue
  `running` ones, so you can retry them.
- **Derived data** (tiles, thumbnails) lives in the cache container and can
  be rebuilt at any time: Project settings → *Rebuild cache*
  (`POST /projects/{id}/cache/rebuild`, SRC-6).

## Secrets and keys

| Secret | Rotating it |
| ------ | ----------- |
| `APP_SECRET_KEY` | Signs everyone out, invalidates every API key, and makes MFA enrolments unreadable (people re-enrol). Plan it; do not rotate casually. |
| Connector and model credentials | Update the value in your secret store. The reference stays the same; the new value is used within `APP_SECRET_CACHE_TTL` (5 min). |
| Database / Redis credentials (Terraform on Azure) | Nothing to rotate: both accept only Microsoft Entra tokens (`APP_DATABASE_AUTH` / `APP_REDIS_AUTH` = `entra`). The app renews its token before it expires; Redis connections re-authenticate in place and database connections are recycled every 45 minutes. To cut access, remove the identity's PostgreSQL administrator or Redis access policy. |
| Database / Redis password (other deployments, `password` auth) | Change it on the server, update `APP_DATABASE_URL` / `APP_REDIS_URL`, restart the API and the worker. |
| Webhook signing secret | Webhooks → *Rotate secret*. The new secret is shown once; update the receiver. |
| API keys | Settings → API keys: revoke and mint. Service accounts' keys are managed there too. |
| SCIM token | Settings → Users → SCIM: minting a new token replaces the old one. |

## People and personal data (SEC-6)

- **Leavers:** deactivate under Settings → Users, or let SCIM do it. Seats
  free up as the 30-day activity window passes.
- **Access request:** the person downloads their own data on the Security
  page, or a superuser calls `GET /users/{id}/personal-data`.
- **Erasure:** Settings → Users → *Erase* (typed e-mail confirmation). This
  pseudonymises the account, removes memberships, notifications and keys,
  and strips IP and e-mail addresses from the person's audit rows.
  Annotations stay, attributed to the pseudonym.

## Licence

- **Renewal** is automatic for keyed installs. The daily licence refresh
  fetches the renewed key (LIC-27). Offline installs paste the new key under
  Settings → Licence.
- **Expiry:** banners, then 30 days' grace, then restricted mode. Annotation
  and new tasks stop; reading and export always work (LIC-5). No data is ever
  locked.
- **Seats:** Settings → Licence shows active users over the 30-day window
  and the seat report for the true-up (LIC-30).

## Reaching PostgreSQL on Azure

The Terraform deployment turns password sign-in off. Sign in as yourself
(listed in `postgres_admins`, from an address in `admin_ip_ranges`) with an
Entra token as the password; a token lasts about an hour:

```sh
export PGHOST=$(terraform -chdir=infra/terraform/azure output -raw postgres_fqdn)
export PGUSER=you@example.com PGDATABASE=annotation PGSSLMODE=require
export PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)
pg_dump -Fc > annotation-$(date +%F).dump    # or psql
```

## Backup and restore

Follow [BACKUP.md](BACKUP.md): what holds state, how to back up, the restore
procedure and a drill. Media and published annotation files are in your own
storage. Protect them with that storage's versioning and soft delete; the
Terraform module enables both.
