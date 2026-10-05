# Load tests (NFR-1…4)

[k6](https://k6.io) scripts that put a team of annotators and reviewers on
the API at once and hold each request's p95 latency against a target. k6
runs from its container image (`grafana/k6`), so nothing is added to the
backend or the frontend.

```sh
make dev && make seed           # the stack and the demo project
make loadtest PROFILE=smoke     # ~1 minute, 2 annotators: do the scripts work?
make loadtest                   # default profile, ~7 minutes
make loadtest PROFILE=stress K6_ARGS="-e HOLD=20m"
```

The run exits non-zero when a threshold fails (k6 exit code 99). CI runs
the smoke profile after the Playwright suite, so the scripts keep up with
the API. A smoke run says nothing about capacity.

## What a run does

`annotate.js` creates a throwaway project, runs two kinds of virtual user
against it, and removes it again.

| Scenario | Who | One iteration |
| -------- | --- | ------------- |
| `annotate` | annotators | `POST /tasks/next` → `GET /items/{id}` → `GET /items/{id}/annotations` → `SAVES` draft saves with think time, a lock `extend` every second save → submit (`SUBMIT_RATIO`) or `release` |
| `browse` | people on the project pages | three pages of `GET /projects/{id}/items` (each row signs a media URL) → `GET /projects/{id}/stats` → `GET /projects/{id}/tasks` |

Every save, draft or submit, writes an `annotation.written` outbox event,
so the worker publishes to the result connector throughout the run. Request
latency cannot show whether the publisher keeps up, so teardown also waits
until every version of up to 20 submitted items has a `blob_path` and
reports the wait as `outbox_catch_up_seconds`. The publisher runs once a
minute, so anything up to about 60 s is normal.

**Setup** (`lib/fixture.js`) logs in as the seeded superuser (`EMAIL` /
`PASSWORD`) and then:

- creates a project named `Load test <timestamp>`, borrowing the demo
  project's connectors and label schema;
- registers `ITEMS` image items with synthetic paths (`loadtest/item-*.jpg`).
  Nothing is read from storage: media never passes through the API, the
  browser fetches it on a signed URL;
- opens an annotate task on each item;
- gives every virtual user a service account `loadtest-NNN` (created once,
  reused by later runs) with a fresh read/write API key and the annotator role.
  A key per virtual user keeps each under its own rate limit, as a person
  is.

**Teardown** revokes the keys and hard-deletes the project; items, tasks
and annotations cascade. `KEEP=1` leaves everything in place. k6 also runs
teardown after a Ctrl-C. A run killed harder leaves a `Load test …`
project behind; delete it from the Projects page.

What stays after a run:

- the `loadtest-NNN` service accounts, active and without keys (no seat,
  LIC-23);
- the annotation JSON the outbox published to the demo result connector
  under the deleted project's id;
- the audit rows, and a LIC-31 notice for the admin: service accounts doing
  human-volume annotation is exactly what the test does.

## Profiles and knobs

| | `smoke` | `default` | `stress` |
| --- | --- | --- | --- |
| `ANNOTATORS` | 2 | 50 | 200 |
| `READERS` | 1 | 5 | 20 |
| `ITEMS` | 20 | 1000 | 5000 |
| `RAMP_UP` / `HOLD` | 5s / 30s | 1m / 5m | 3m / 10m |
| `THINK` (mean seconds between actions, ±50 %) | 1 | 10 | 10 |

Also: `SAVES` (3), `SUBMIT_RATIO` (0.3), `BASE_URL`, `DEMO_PROJECT`,
`KEEP`. Pass them as `K6_ARGS="-e NAME=value"` to `make loadtest`, or as
`-e` flags to `k6 run`.

## Targets

p95 in milliseconds per request name. Override any of them with `-e`.

| Request | Target | Variable |
| ------- | ------ | -------- |
| every request of a scenario | 1000 | `P95_MS` |
| `claim` | 500 | `P95_CLAIM_MS` |
| `item`, `history`, `release`, `extend` | 300 | `P95_ITEM_MS`, … |
| `save` | 500 | `P95_SAVE_MS` |
| `submit` | 750 | `P95_SUBMIT_MS` |
| `list`, `stats`, `tasks` | 1000 | `P95_LIST_MS`, … |
| failed requests, failed checks | < 1 % | `MAX_ERROR_RATE` |
| submitted versions in storage after the load (seconds, not ms) | < 120 | `MAX_OUTBOX_CATCH_UP_S` |

These are placeholders until the NFR-1…4 figures are confirmed. The
overall 1 s matches the API latency alert in the Helm chart
(`alerts.thresholds.apiLatencyP95Seconds`); once NFR figures are measured,
set both from them.

## Against another deployment

```sh
k6 run -e BASE_URL=https://annotate.example.com \
       -e EMAIL=admin@example.com -e PASSWORD=… \
       -e PROFILE=default loadtest/annotate.js
# or without a local k6:
docker run --rm -i -v "$PWD/loadtest:/scripts:ro" -e BASE_URL=… grafana/k6:1.3.0 run /scripts/annotate.js
```

- The account must be a superuser without MFA, and the organisation needs a
  project with the `DEMO_PROJECT` name whose connectors the load-test
  project can use.
- Run k6 close to the ingress. Latency measured across the internet is the
  network's, not the platform's.
- On a Helm install, watch the API HPA and the worker ScaledObject (OPS-5)
  and the alert rules (OPS-4) while the run holds.
- Rate limits are per key per minute (`APP_RATE_LIMIT_API_KEY_PER_MINUTE`,
  1200 by default). An annotator at the default think time makes about 15
  requests a minute, so the default limits do not interfere.

## Not covered

- Media transfer: the browser reads from the customer's storage, not from
  the platform.
- Sign-in: logins are limited to 10 a minute per address (API-5) by design.
- Scans of large buckets, imports, exports, snapshots, pre-labelling: these
  are worker jobs, whose throughput is a different measurement (queue wait,
  job duration) from request latency.
- The annotator UI itself: canvas rendering and tile loading happen in the
  browser.
