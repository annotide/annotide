# End-to-end tests (NFR-8)

Playwright drives the real product — backend, worker, Postgres, Redis and
Azurite — against the demo data `make seed` creates. Nothing is mocked.

```sh
# 1. A licensed, running stack, seeded (idempotent; safe to repeat)
make dev-licence    # once: a throwaway Business licence the stack trusts (.dev/)
make dev
make seed

# 2. Browsers, once
cd frontend && npx playwright install chromium

# 3. Run
make e2e            # == cd frontend && npx playwright test
```

Without `make dev-licence` the stack is Community (three users, no Business
features), and the quality, SSO and SCIM specs fail with 402. CI runs it too.

`playwright.config.ts` starts the Vite dev server from the working tree on
:5174 (`E2E_PORT`), proxying `/api` to `VITE_API_PROXY_TARGET` (default
`http://localhost:8000`; set it to `http://localhost:$BACKEND_HOST_PORT` if
you moved the backend port in `.env`). It deliberately does not reuse the
compose `frontend` container on :5173, whose image may be older than the
source. Set `E2E_BASE_URL` to target a built deployment instead; `E2E_EMAIL`
/ `E2E_PASSWORD` override the seeded superuser (`admin@example.com` /
`admin-dev-password`, the `make seed` defaults).

| Spec | Covers |
| ---- | ------ |
| `auth.setup.ts` | Signs in once, saves storage state for the rest |
| `auth.spec.ts` | Route guard, wrong password, login/logout, deep-link restore |
| `projects.spec.ts` | Demo project list → item grid; create → settings → delete; dashboard |
| `annotate.spec.ts` | Item + media load, box drawn on the canvas, undo/redo, draft saved |
| `workflow.spec.ts` | Task queue and review (WF-1…4): claim + lock, release, submit → review task, reject → back to the annotator, re-submit, approve |
| `pdf.spec.ts` | Uploads a generated two-page PDF, pdf.js renders it, a box on page 2 is saved in page points with the words under it |
| `sso.spec.ts` | Opt-in (`E2E_SSO=1`): SSO through a real Keycloak; IdP groups grant, change and revoke a project role; the admin group makes a superuser (AUTH-1, AUTH-3). See "Single sign-on" below |
| `upload.spec.ts` | Files and a whole folder picked in the Upload panel → PUT to Azurite on signed URLs → the scan the panel queues → items with the folder's relative paths (§12) |
| `import-export.spec.ts` | COCO import: dry run previews, the real run writes a submitted `car` box; a COCO export downloaded on its signed URL holds it; a failed import is re-run from Retry (EXP-5, EXP-6, ARC-4) |
| `admin.spec.ts` | Connectors (demo check OK; create → storage events on, an anonymous delivery accepted, off → 404 → edit → delete), models (check OK, versions), org webhooks (one-time secret, pause/resume, delete), user directory search, licence page |
| `storage-events.spec.ts` | A blob PUT to Azurite, then an Event Grid `BlobCreated` posted with only the connector's event token → the worker registers that one blob as an item, measured (SRC-3). Skips if the demo connector already has events on |
| `scim.spec.ts` | The admin turns SCIM on in the Users page; a request context holding only the SCIM token provisions a person, deactivates them (the Users page shows *inactive*), adds them to a group and deprovisions them; a missing token gets a SCIM 401 (AUTH-3). Turns SCIM off again if it was off |
| `a11y.spec.ts` | axe-core WCAG 2.1 AA scan of every page in the dark and light themes and the sign-in page (any violation fails); no sideways scroll at 320 px; titles; the skip link; a box drawn, selected and deleted with the keyboard only (UX-7) |
| `prelabel.spec.ts` | Pre-label panel → worker → compose `model` service → mapped `car` draft, item `prelabeled`, annotator opens on it; re-run of the same version is a no-op (ML-2, ML-10) |

Tests create only what they delete. `annotate.spec.ts` works on a scratch
project per test, because the annotator opens an item on its latest version
and drafts saved on a shared item would pile up across runs. `workflow.spec.ts` and
`prelabel.spec.ts` (and the upload and import/export specs) need state nobody else touches, so each creates a project of its own per worker through the
API (demo connector + schema, `source_glob` matching one sample), scans it
(the compose `worker` must be running) and hard-deletes it at the end; the
annotation blobs it published to the result connector stay in Azurite.

The suite logs in once (`auth.setup.ts`) and reuses that token for its API
calls: logins and requests are rate limited (API-5). The compose stack's
limits are generous; against a deployment with production limits, repeated
runs within a minute can get `429`.

Debugging: `npx playwright test --ui`, `--headed`, `--debug`; on failure the
trace and screenshot land in `frontend/test-results/`.

## Single sign-on (Keycloak)

`sso.spec.ts` needs the dev Keycloak and a backend configured for it; it is
skipped unless `E2E_SSO=1`:

```sh
docker compose -f docker-compose.yml -f docker-compose.sso.yml up -d --wait keycloak backend worker
cd frontend && E2E_SSO=1 npx playwright test sso
# back to the plain stack afterwards
docker compose up -d backend worker && docker compose -f docker-compose.yml -f docker-compose.sso.yml stop keycloak
```

The override points the backend's callback and `APP_FRONTEND_URL` at the
Playwright dev server (:5174), so the rest of the suite is best run on the
plain stack. The realm (`infra/keycloak/annotation-realm.json`) has users
`alice` (group `annotators`) and `carol` (`platform-admins`); the spec moves
alice between groups through Keycloak's admin API and puts her back after
each test. Keycloak's console is http://localhost:8180 (admin / admin).
