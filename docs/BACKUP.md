# Backup and restore (OPS-7)

Runbook for operators. Drilled against the compose stack on 2026-09-28
(schema head `0017`): dump, restore into a scratch database, row counts
identical, `alembic upgrade head` a no-op, scratch database dropped.

## What holds state

| What | Where | Back up? |
| ---- | ----- | -------- |
| Everything the platform owns: users, projects, schemas, items, annotations, tasks, jobs, audit log, licence state, webhooks | PostgreSQL | **Yes** — the only database to back up |
| `APP_SECRET_KEY` | secret store / env | **Yes, separately.** It signs API keys and local storage URLs and seals TOTP seeds. A restore with a different key invalidates every API key and every user's MFA (admins must reset them) |
| `APP_LICENSE_KEY` | env or `license_state.key` | Kept in the DB when installed from the UI; keep the vendor e-mail as well |
| Job queue | Redis | No. Redis carries only job ids; the `job` row is the truth |
| Media | customer storage (source connector) | Not ours. The platform never copies raw media |
| Published annotations `annotations/…/v{n}.json`, snapshots, exports | customer storage (result connector) | The customer's own backup policy. They are also a second copy of the annotations |
| `cache/` (thumbnails, tiles) | result connector | No. Derived; rebuilt by the `thumbnail` / `tile_image` jobs |

## Backup

**Managed PostgreSQL (production).** Azure Database for PostgreSQL Flexible
Server, RDS or Cloud SQL: turn on automated backups with point-in-time
restore (7–35 days) and geo-redundant backup where data residency allows.
That is the backup. Add a weekly logical dump (below) to independent storage
if the provider's backups must not be the only copy.

**Logical dump (compose, or anywhere `pg_dump` reaches the database).**

```sh
docker compose exec -T postgres \
  pg_dump -U annotation -d annotation -Fc > annotation-$(date +%F).dump
```

`-Fc` is the compressed custom format `pg_restore` needs. It includes the
`pgcrypto` / `citext` extensions and the `alembic_version` row. The dump is
consistent without stopping the app (single snapshot transaction). It holds
personal data (SEC-6): store it encrypted, with the same access rules as the
database.

## Restore

1. **Stop writers:** `docker compose stop backend worker` (or scale both to
   zero). The worker must not run against the restored database before
   step 5.
2. **Restore into a new database.** Never restore over the live one:

   ```sh
   docker compose exec -T postgres createdb -U annotation annotation_restored
   docker compose exec -T postgres \
     pg_restore -U annotation -d annotation_restored --no-owner --exit-on-error \
     < annotation-2026-09-28.dump
   ```

   Managed PostgreSQL: point-in-time restore creates a new server; use that.
3. **Check it:**

   ```sh
   docker compose exec -T postgres psql -U annotation -d annotation_restored -Atc \
     'SELECT version_num FROM alembic_version; SELECT count(*) FROM annotation;'
   ```

4. **Point the app at it:** set `APP_DATABASE_URL` to the restored database
   and use the **same `APP_SECRET_KEY`** as before. Starting the backend runs
   `alembic upgrade head`, which brings an older dump forward.
5. **Before starting the worker after a point-in-time restore**, read the
   next section. Then `docker compose up -d backend worker`.
6. **Stranded jobs** recover by themselves. Jobs that were `queued` when the
   backup was taken have no Redis entry any more; within five minutes the
   worker's stranded-job cron enqueues them again. Jobs that were `running`
   are marked `failed` ("lost by the queue …") once the job timeout has
   passed (35 min after they started), because they may have run in part;
   retry each with `POST /jobs/{id}/retry`. To see what is pending:

   ```sql
   SELECT id, type, status, started_at, updated_at FROM job
   WHERE status IN ('queued', 'running') ORDER BY created_at;
   ```

7. Rename or drop the old database only once the restored one has served
   traffic.

## Restoring to an earlier point in time

Annotations saved after the restore point are gone from the database but
still exist as `annotations/{project}/{item}/v{n}.json` blobs in the result
connector. New saves reuse the lost version numbers, and the outbox
publisher overwrites those blobs. Before starting the worker:

- copy the result connector's `annotations/` prefix aside (e.g.
  `azcopy copy` / `aws s3 sync` to a `annotations-before-restore/` prefix);
- if the lost work matters, re-enter it from those blobs once the platform
  runs again: each is self-contained (item path, schema version, result;
  DATA-3), but there is no importer for them yet, so it takes a script
  against the API or SDK.

Deliveries still `pending` in the restored `webhook_delivery` table are
sent again, so receivers may see repeats; they de-duplicate on the
`X-Annotation-Delivery` header.

## Drill

Do it quarterly, and after any change to the schema or to how the database
is hosted. It is safe on a live system: it only reads the live database.

```sh
docker compose exec -T postgres pg_dump -U annotation -d annotation -Fc > drill.dump
docker compose exec -T postgres createdb -U annotation annotation_restore_drill
docker compose exec -T postgres \
  pg_restore -U annotation -d annotation_restore_drill --no-owner --exit-on-error < drill.dump
for db in annotation annotation_restore_drill; do
  docker compose exec -T postgres psql -U annotation -d $db -Atc \
    'SELECT (SELECT version_num FROM alembic_version), (SELECT count(*) FROM "user"),
            (SELECT count(*) FROM item), (SELECT count(*) FROM annotation),
            (SELECT count(*) FROM audit_event)'
done
docker compose exec -T postgres dropdb -U annotation annotation_restore_drill
rm drill.dump
```

The two lines must be identical. Record the date and the dump size.
