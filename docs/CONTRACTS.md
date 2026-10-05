# Shared contracts

Single source of truth for the Phase-1 MVP skeleton. Every module codes against
this file. Change it here first, then change the code.

Requirement IDs (`ARC-1`, `SRC-2`, …) refer to *Annotointialusta – tekniset
vaatimukset*. All code, comments and identifiers are English.

## Stack

| Layer    | Choice |
| -------- | ------ |
| Backend  | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async), Alembic |
| Database | PostgreSQL 16 (JSONB) |
| Queue    | Redis + arq |
| Frontend | React 18, TypeScript 5, Vite 5, TanStack Query 5, Zustand, Tailwind 3 |
| Canvas   | Konva / react-konva |
| Tests    | pytest + pytest-asyncio, Vitest, Playwright |

## Layout

```
backend/app/
  core/        config, security, logging      — no imports from api/ or models/
  db/          engine, session, Base          — imports core/ only
  models/      SQLAlchemy ORM                 — imports db/ only
  schemas/     Pydantic DTOs                  — imports core/ only, never models/
  connectors/  storage plugin interface       — imports core/ + schemas/ only
  exporters/   COCO / YOLO / native           — imports schemas/ only
  importers/   COCO / YOLO / VOC / CVAT / Label Studio — imports schemas/ only
  services/    business logic                 — imports models/ + schemas/ + connectors/
  api/v1/      FastAPI routers                — imports services/ + schemas/
  worker/      arq entrypoint + job bodies    — imports services/ + models/ + connectors/
```

The worker is the same package as the API, started as a second process
(`arq app.worker.main.WorkerSettings`) from the same image. It sits beside
`api/v1/` in the dependency order: both are entrypoints over `services/`, and
neither imports the other.

Dependency direction is one-way, top to bottom. A module never imports from a
layer below it in that list.

## Identifiers and conventions

- Every table has `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`.
- Every table has `created_at` / `updated_at` as `TIMESTAMP WITH TIME ZONE`,
  server-side `now()`, `updated_at` maintained by SQLAlchemy `onupdate`.
- Soft delete where it exists: nullable `deleted_at`, never a boolean.
- Enums live in Python as `enum.StrEnum` and in PostgreSQL as native enum types
  named `<entity>_<field>` (e.g. `item_status`).
- Table names are singular (`item`, `task`, `annotation`) per §9 of the spec.
- JSON columns are `JSONB`, never `JSON`.
- Money and durations: never floats. Durations are integer milliseconds.

## Data model (§9)

Only the columns the MVP needs. Fields marked `— later` are declared in the
model but unused in Phase 1.

### organization

`id`, `name`, `slug` (unique), `scim_token_hash` (nullable, unique — SHA-256
hex of the SCIM bearer token, AUTH-3; SCIM is off while null), `created_at`,
`updated_at`

### user

`id`, `organization_id` → organization, `email` (unique, citext), `display_name`,
`idp_subject` (nullable, unique), `password_hash` (nullable, argon2, AUTH-2),
`is_active`, `is_superuser`, `is_service`, `last_seen_at`, `totp_secret`
(text, nullable — the TOTP seed, AES-GCM sealed with a key derived from
`APP_SECRET_KEY`), `totp_enabled_at` (timestamptz, nullable — MFA is on when
set), `totp_last_step` (bigint, nullable — last accepted time step, so a code
works once), `mfa_recovery_codes` (JSONB, nullable — SHA-256 hex of the
unused recovery codes), `erased_at` (timestamptz, nullable — SEC-6 erasure),
`scim_external_id` (nullable — the IdP's SCIM `externalId`, AUTH-3),
`scim_deleted_at` (timestamptz, nullable — a SCIM `DELETE` hid the account from
SCIM), `email_notifications` (bool, default true — e-mail copies of in-app
notifications, API-7), `created_at`, `updated_at`

MFA (AUTH-2, `services/mfa.py`) is TOTP per RFC 6238 — SHA-1, 6 digits, 30 s
steps, one step of clock drift either way — for local accounts only; SSO
accounts get MFA from their provider. Ten single-use recovery codes
(`xxxxx-xxxxx`, lowercase base32) are shown once when MFA is turned on.
Rotating `APP_SECRET_KEY` makes the sealed seeds unreadable: every user then
has to be reset by an administrator, as with API keys.

`is_service` marks a **service account** (AUTH-4): a user row that cannot sign
in (no password, no `idp_subject`, synthetic `svc-<id>@service.invalid`
e-mail) and acts only through API keys. It holds project memberships like a
person, so a CI job or an integration gets exactly the roles it is given.

`idp_subject` is the stable ID from OIDC. `email` and `display_name` are
separately erasable for pseudonymisation (DATA-4); `id` is the durable
reference used everywhere else.

**Personal data (SEC-6, `services/personal_data.py`).** *Access*: `GET
/users/{id}/personal-data` returns everything the platform holds about one
person as JSON — the profile (no password hash, TOTP seed or recovery codes;
`idp_linked` / `mfa_enabled` flags instead), memberships with project names,
API key metadata, comments with their bodies, the metadata (not the result)
of annotations they authored, tasks assigned to them,
notifications, and audit rows where they are the actor or the target (with
`ip`). *Erasure*: `POST /users/{id}/erase` pseudonymises in one transaction.
The row stays, because annotations, audit rows and snapshots reference its
`id`, and annotation blobs in customer storage carry only that UUID:

- `email` → `erased-<id>@erased.invalid`, `display_name` → `Erased user`;
  `idp_subject`, `password_hash`, the TOTP columns, recovery codes and
  `last_seen_at` cleared; `is_active` and `is_superuser` false; `erased_at`
  set. A later SSO sign-in by the same person provisions a new account.
- Memberships and notifications are deleted; API keys are
  revoked; `open` / `in_progress` tasks they hold go back to `open` without
  assignee or lock (finished tasks keep the pseudonymous id).
- Audit rows where they are the actor or the `user` target lose `ip` and any
  `email` key in `before` / `after` — the one sanctioned exception to the
  append-only rule (SEC-3). The erasure itself is audited as `user.erase`
  with no personal data.
- Comment bodies are kept (they are project content) unless the request
  says `redact_comments: true`, which replaces each body with `[erased]`.

Access tokens already issued stay valid until they expire
(`APP_ACCESS_TOKEN_TTL`), as with any deactivation.

### membership

`id`, `user_id`, `project_id`, `role` (`project_role` enum), `source`
(`manual` | `idp`, default `manual`), `path_prefixes` (JSONB list, nullable),
`created_at`

`project_role`: `owner` | `annotator` | `reviewer` | `viewer`

**Folder-level access (§4, C).** `path_prefixes` limits an annotator,
reviewer or viewer to the items whose `path` starts with one of them
(1–20 prefixes, each 1–512 characters, no `..` segment); `null` means the
whole project. Owners and superusers are never limited (an owner with
prefixes is 422). A limited member gets 404 for any item outside their
folders (and its annotations, comments, views, tiles, consensus and OCR):
the item is not theirs to know about. `GET /projects/{id}/items` and the
task lists show only their items, `POST /tasks/next` claims only tasks on
them, and bulk actions and region splits refuse item ids outside them
(404). Companion views outside their folders are left out of
`GET /items/{id}/views`, and `GET /jobs/{id}/download` is 403 for them: an
export covers the whole project. Project-wide aggregates (dashboard counts, agreement) are not
limited. IdP group sync (below) never sets prefixes; a manual edit does.

System administrator is a flag on `user` (`is_superuser`), not a project role.

**IdP group sync (AUTH-3, `services/group_sync.py`).** On every SSO sign-in,
after the account is resolved, the groups in the ID token claim
`APP_OIDC_GROUPS_CLAIM` (default `groups`; a list of strings — names or,
for Entra ID, object ids) drive memberships and the admin flag. A token
without that claim means *no groups* — Keycloak and Entra leave it out for
a user in none, so removing someone from every group revokes what sync
granted. Only Entra's group overage (`_claim_names` names the claim, or
`hasgroups: true`) and a claim of an unexpected type change nothing.

- Projects of the user's organisation whose `settings.idp_groups` maps one
  of the user's groups get a membership with the highest mapped role
  (`owner` > `reviewer` > `annotator` > `viewer`), `source: idp`. An
  existing `idp` membership is re-roled or, when no group maps any more,
  removed. A `manual` membership (added in the Members panel or the API) is
  never touched, so an owner can always override the IdP.
- `APP_OIDC_ADMIN_GROUPS` (comma-separated, unset = not managed): when set,
  `is_superuser` becomes "member of one of these groups" — except that the
  last active superuser of the organisation is never demoted.
- The last owner of a project is never removed or demoted by sync.
- Changes are audited as `membership.create` / `.update` / `.delete` with
  `after.source = "idp"`, and `user.superuser_sync` with `{is_superuser}`;
  the actor is the signing-in user. Changing a role in the Members panel
  turns an `idp` membership into `manual`.
- Once the organisation has SCIM groups (below), SCIM is the source of
  groups and sign-in sync is skipped: two sources would undo each other.

### SCIM provisioning (AUTH-3)

SCIM 2.0 (RFC 7643/7644) under `/api/v1/scim/v2`, for Entra ID, Okta and
other IdPs to create, update and deactivate accounts and to push groups
before anyone signs in. `services/scim.py`, router `api/v1/scim.py`.

- **Authentication.** One bearer token per organisation, `scim_<random>`,
  minted by a superuser (`POST /scim/token`, shown once) and stored as its
  SHA-256 in `organization.scim_token_hash`; the token selects the
  organisation. Minting again replaces it, `DELETE /scim/token` turns SCIM
  off. The token can create, rename and deactivate any person in the
  organisation (not the last active superuser) and, through groups and
  `APP_OIDC_ADMIN_GROUPS`, grant roles: keep it like an administrator
  credential. A missing or unknown token is 401. Requests are rate-limited per
  organisation at `APP_RATE_LIMIT_API_KEY_PER_MINUTE`. SCIM changes are
  audited with no actor and `after.via = "scim"`. A restricted licence does
  not block SCIM: deprovisioning must always work (LIC-5).
- **Wire format.** `application/scim+json` in and out; errors are SCIM
  error bodies (`schemas: [urn:ietf:params:scim:api:messages:2.0:Error]`,
  `status` as a string, `scimType` where RFC 7644 defines one), not
  problem details. Bodies are capped at 1 MiB (413). `filter` supports one
  `<attr> eq "<value>"` comparison — `userName`, `externalId`, `id`,
  `emails.value` for users; `displayName`, `externalId`, `id` for groups;
  anything else is 400 `invalidFilter`. `startIndex` (1-based) and `count`
  (default 100, max 200) page a list. No bulk, sort, ETags or password
  changes. `/ServiceProviderConfig`, `/ResourceTypes` and `/Schemas`
  describe this.
- **Users.** A SCIM user is a `user` row of the token's organisation.
  `userName` *is* the e-mail (400 `invalidValue` unless it contains `@`);
  `emails` is echoed but never read. `displayName`, else `name.formatted`,
  else `name.givenName name.familyName`, else the e-mail's local part is
  the display name. `externalId` → `scim_external_id`; `active` →
  `is_active`. Lists and lookups cover the organisation's people (service
  accounts, erased and SCIM-deleted accounts excluded), so an IdP can match
  and adopt an account that SSO or an administrator created. `POST` for an
  e-mail already in use is 409 `uniqueness` — except a SCIM-deleted account
  of the same organisation, which is revived (cleared `scim_deleted_at`,
  reactivated). The new account has no password and no `idp_subject`: the
  person signs in with SSO, which links it by e-mail (AUTH-1). Provisioning
  takes no licence seat; a seat is taken at sign-in (LIC-24).
  `PUT` replaces the mapped attributes; `PATCH` takes `add` / `replace` /
  `remove` (case-insensitive) on `active`, `userName`, `displayName`,
  `name.formatted` and `externalId`, with a path or in the path-less form
  with a value object (Entra's). Other attributes — `name.givenName`,
  `emails`, enterprise extension fields — are accepted and ignored, so a
  PATCH renames only through `displayName` or `name.formatted`. `active`
  also accepts the strings `"True"` / `"False"`. Map `userName` to the
  attribute the ID token's e-mail claim carries, or SSO will not find the
  provisioned account and creates a second one.
  Deactivation keeps memberships; a deactivated user cannot sign in and
  their API keys stop working at once. The last active superuser of the
  organisation is never deactivated or deleted (409 `mutability`).
  `DELETE` deactivates, sets `scim_deleted_at`, drops the account from
  every SCIM group (which re-syncs its `idp` memberships away) and hides it
  from SCIM (404 afterwards). It does not erase: that stays the explicit
  SEC-6 action.
- **Groups.** `scim_group` holds the IdP's groups (`display_name`,
  `external_id`) and `scim_group_member` who is in them. A user's groups
  for AUTH-3 are the `display_name` *and* the `external_id` of every group
  they are in, so a `settings.idp_groups` map written for the ID-token
  claim (names, or Entra object ids) works unchanged. After any change to a
  group's name, id or members, `group_sync.sync_groups` runs for each
  affected user (active or not), `APP_OIDC_ADMIN_GROUPS` included, with no
  actor and `after.via = "scim"` on the membership rows. `PATCH` on a group takes `displayName`, `externalId` and `members`
  (`add` with `[{value}]`, `remove` with `members[value eq "<id>"]` or a
  value list, `replace`); an unknown member id is 400 `invalidValue`.
  `DELETE` removes the group and re-syncs its members. `members` in a
  `GET` lists every member (no paging); `?excludedAttributes=members` omits
  it.

### scim_group / scim_group_member (AUTH-3)

`scim_group`: `id`, `organization_id` → organization, `display_name`,
`external_id` (nullable), `created_at`, `updated_at`; `display_name` unique
per organisation (409 `uniqueness`).

`scim_group_member`: `id`, `group_id` → scim_group (cascade), `user_id` →
user (cascade), `created_at`; unique (`group_id`, `user_id`).

### api_key (AUTH-4)

`id`, `organization_id`, `user_id` → user (the identity the key acts as: a
person or a service account), `name`, `scopes` (JSONB list), `token_prefix`
(nullable, unique, 8 hex characters), `token_hash` (nullable, sha256 hex of
the whole token), `expires_at` (nullable), `last_used_at` (nullable),
`revoked_at` (nullable), `created_by` → user (nullable), `created_at`

The key itself is an opaque token `ant_<token_prefix>_<secret>` (`secret`:
32 random bytes, url-safe base64), shown once at creation. The row keeps the
prefix to find it and the sha256 to check it (constant-time); the token
cannot be read back from the database and does not depend on
`APP_SECRET_KEY`, so a key survives a rotation. The `ant_` prefix lets secret
scanners recognise a leaked key. Keys issued before migration 0032 are JWTs
signed with `APP_SECRET_KEY` carrying `kid` (this row's id), `sub`, `org`,
`service: true` and `exp` when `expires_at` is set; they have neither column
and keep working (verified with `APP_SECRET_KEY` or
`APP_SECRET_KEY_PREVIOUS`) until revoked or expired. Every request with a key
resolves the row and its user, so revocation (`revoked_at`), expiry, user
deactivation and role changes all take effect immediately. `last_used_at` is
refreshed at most once a minute.

`scopes` ⊆ {`read`, `write`, `admin`}, each implying the previous: `read`
allows `GET`/`HEAD` only, `write` any method, `admin` additionally the
superuser-only endpoints when the key's user is a superuser. A key can never
manage API keys or service accounts, whatever its scopes.

### connector

`id`, `organization_id`, `name`, `type` (`connector_type` enum),
`identity_type` (`connector_identity` enum), `secret_ref` (nullable text —
a Key Vault reference, never a secret, SEC/AUTH-7), `config` (JSONB: container,
prefix, endpoint, …), `event_token_hash` (nullable text — SHA-256 hex of the
storage-event token, SRC-3; never returned, the API shows `events_enabled`),
`created_at`, `updated_at`

`connector_type`: `azure_blob` | `s3` | `gcs` | `local` | `http` | `sharepoint` |
`databricks_volume`
`connector_identity`: `managed_identity` | `service_principal` | `account_key` |
`sas_token` | `iam_role` | `access_key` | `none`

### label_schema / label_schema_version

`label_schema`: `id`, `project_id`, `name`, `created_at`, `updated_at`
`label_schema_version`: `id`, `label_schema_id`, `version` (int, unique per
schema), `definition` (JSONB, see *Label schema JSON* below), `created_at`

Annotations reference `label_schema_version_id`, never the schema itself
(TOOL-4).

### project

`id`, `organization_id`, `name`, `description`, `label_schema_id` (nullable),
`source_connector_id` (nullable), `result_connector_id` (nullable),
`cache_connector_id` (nullable, SRC-6), `source_prefix`, `source_glob`,
`workflow` (JSONB, see *Project workflow JSON*, WF-1), `settings` (JSONB),
`created_at`, `updated_at`

`cache_connector_id` names where derived data (`cache/`: thumbnails, tile
pyramids) is written and signed from; `null` means the result connector.
The *effective* cache connector is `cache_connector_id ?? result_connector_id`.
A PATCH that changes the effective cache connector queues a `rebuild_cache`
job (the old `thumbnail_path` / `meta.tiles` point into the old container)
and reports its id in the response header `X-Rebuild-Job-Id`. The update
stands when the queue is unreachable; the header is then absent.

`settings.companion_extensions` (§5 multimodal, optional): a list of 1–10
extensions (`.txt`, `.json`, `.md`, `.jpg` …, lower case with the dot).
A scan then treats a file whose name is another file's name with one of
these extensions instead (`photos/1.txt` beside `photos/1.jpg`) as a
companion view of that item, adding it to the item's `meta.views`, rather
than as an item of its own. A file with no primary beside it stays an item.
Off by default: turning it on does not remove items that earlier scans
created.

`settings.pdf_mode` (TOOL, optional): `layout` (default) | `text` — how a
scan takes in `.pdf` files. `layout`: a `pdf` item, annotated on the
rendered page (*PDF items*). `text`: a `text` item whose text the worker
extracts once (*PDF text mode*, below). It applies to items that scans
create from then on; existing items keep their media type. `text` needs a
result connector: 422 on create / `PATCH /projects/{id}` without one, and
on a PATCH that removes the result connector of a `text` project; `null`
removes the setting (`layout`), any other value is 422.

`settings.calibration` (TOOL-8, optional): `{units_per_pixel, unit}` — the
physical size of one image pixel for the project's measurements, e.g.
`{"units_per_pixel": 0.05, "unit": "mm"}`. `units_per_pixel` is a positive
number, `unit` 1–16 characters; anything else is 422 on `PATCH
/projects/{id}`. `null` removes it.

`settings.idp_groups` (AUTH-3, optional): `{"<IdP group>": "<project_role>"}`
— which IdP groups grant which role on this project at SSO sign-in (see
"### membership"). Keys 1–255 characters, values a `project_role`; anything
else is 422. `null` or `{}` stops granting; existing `idp` memberships are
removed at each member's next sign-in.

### item

`id`, `project_id`, `connector_id`, `path`, `media_type` (`media_type` enum),
`etag` (nullable), `size_bytes`, `width`, `height` (nullable),
`meta` (JSONB; `meta.tags` is a sorted list of free-form tags set by the bulk
`tag` action, WF-8; `meta.skip_reason` by skip, TOOL-6; `meta.pixel_spacing`
`{x, y, unit}` is the item's own pixel size, e.g. from DICOM or GeoTIFF, and
takes precedence over the project's `settings.calibration`, TOOL-8), `status` (`item_status` enum), `thumbnail_path` (nullable —
set by the `thumbnail` job, path on the *result* connector, IMG-8),
`created_at`, `updated_at`

Unique on `(project_id, connector_id, path)`.

`media_type`: `image` | `video` | `audio` | `text` | `pdf` | `llm` | `timeseries`

`meta.views` (§5 multimodal, optional): companion views shown beside the
annotator in the same task, e.g. an image and its caption. A list of up to
10 `{path, label?}` on the item's connector, inside the project's
`source_prefix`. Set by a scan with `settings.companion_extensions`, or
through `POST /projects/{id}/items` / `PATCH /items/{id}`. Annotation stays
on the item's own media; judgements across views are `classification`.

A scan maps a file to its media type by extension; `*.llm.json` is `llm`
(LLM evaluation data, below), other `.json` files are `text`;
`*.timeseries.csv` is `timeseries`, other `.csv` files are `text`; `.wav`,
`.mp3`, `.flac`, `.ogg` and `.m4a` are `audio`; `.pdf` is `pdf`, or `text`
in a project with `settings.pdf_mode: text` (*PDF text mode*).
`item_status`: `new` | `prelabeled` | `annotating` | `submitted` |
`in_review` | `approved` | `rejected` | `skipped`

Matches the state machine in §7. Transitions are enforced in
`services/workflow.py`, never in the router.

### task

`id`, `item_id`, `project_id`, `type` (`task_type` enum), `assignee_id`
(nullable), `status` (`task_status` enum), `locked_by_id` (nullable),
`locked_until` (nullable, WF-3, default lock 30 min), `priority` (int,
default 0), `deadline` (nullable), `slot` (int, nullable — consensus replica
index, QA-1), `region` (JSONB, nullable — `[x_min, y_min, x_max, y_max]` in
original-image pixels, IMG-6), `gold` (bool, default false — QA-4),
`created_at`, `updated_at`

`task_type`: `annotate` | `review`
`task_status`: `open` | `in_progress` | `done` | `cancelled`

Lifecycle (WF-2, WF-3, WF-4). Tasks are the unit of work the queue hands
out; they are opened and closed by the system, not by hand, except for
explicit assignment through `POST /projects/{id}/tasks`:

- A source scan opens one `annotate` task per *new* item. Re-scanning an
  item that already has an open or in-progress task opens nothing.
- Submitting an annotation marks the item's open `annotate` task `done` and
  opens a `review` task — unless the project's workflow says `review:
  "none"`, in which case the item goes straight to `approved` and no review
  task exists (WF-1).
- Approving marks the `review` task `done`. Rejecting marks it `done` and
  opens a new `annotate` task with `assignee_id` = the original annotator,
  so the item goes back to the same person (WF-1 default,
  `rejection_returns_to: "same_annotator"`); with `"queue"` the task is
  opened unassigned.
- The item-status side of these steps and the task side are applied together
  by `services/item_flow.py` (`submit_item`, `review_item`, `skip_item`),
  which reads the project's workflow config. Routers call those; they never
  sequence status changes and task rows themselves.
- Claiming (`POST /tasks/next`) takes the lock and sets `assignee_id` to the
  claimant; releasing clears the lock and returns the task to `open`. If the
  caller already holds an `in_progress` task (lock expired or not), claiming
  returns that task first and renews its lock — a reload resumes the task in
  hand rather than reporting an empty queue.
- Lock expiry: the worker cron `reap_expired_task_locks` (once a minute)
  returns every `in_progress` task whose `locked_until` has passed to `open`,
  clearing the lock and `assignee_id` — same effect as a release.
- Queue order (WF-6): `POST /tasks/next` hands out gold tasks first (QA-4:
  they are always assigned to the caller), then `priority DESC, deadline
  ASC NULLS LAST, created_at ASC` — the most urgent, then the most overdue,
  then the oldest-waiting task. `priority` and `deadline` are set on
  `POST /projects/{id}/tasks` or changed later with `PATCH /tasks/{id}`
  (owner / reviewer; `open` or `in_progress` only — a `done` / `cancelled`
  task is history, 409; reassigning an `in_progress` task is 409, release it
  first). Only keys present in the PATCH body change; `null` clears
  `deadline` / `assignee_id`. The review task a submit opens, and the
  annotate task a rejection reopens, inherit the closed task's `priority`
  and `deadline`, so an item keeps its place in the queue across the cycle.
- `POST /projects/{id}/tasks` opens through the same helper as the system
  does: an item with a live task of that type is a 409 (one live task per
  item and type), PATCH the existing task instead.
- Helpers live in `services/tasks.py`; routers and jobs never write task
  rows directly.

Parallel annotate tasks (QA-1, QA-4, IMG-6). An ordinary task has `slot`
null, `region` null, `gold` false, and the "one live task per item and type"
rule above holds for it. Three kinds of annotate task may be live on one
item at the same time; liveness is keyed on `(item, type, slot, region,
gold)` (gold also on `assignee_id`), so `open_task` stays idempotent:

- **Consensus** (`slot` 0…N−1, QA-1). With `workflow.consensus_annotators`
  = N > 1, wherever the system opens the *first* annotate task of an item
  (source scan, upload scan, import, prelabel, `POST /projects/{id}/tasks`
  without an explicit `slot`, bulk `assign`) it opens N slot tasks instead.
  `POST /tasks/next` never hands a caller a slot task on an item where they
  already hold or finished another slot task, or authored a `consensus`
  version — N distinct people annotate it. Versions saved under a slot task
  are `kind = consensus`. Submitting one closes that task only; the item
  stays `annotating` until its last live annotate task closes, then moves
  `annotating → submitted` and one `review` task opens (priority / deadline
  of the last closed task). A rejection opens one *ordinary* annotate task —
  rework is single-annotator.
- **Region** (`region` set, IMG-6), created by `POST /items/{id}/split`.
  Saving a version under a region task merges on the server, under a row
  lock on the item: start from the item's latest `primary` version (empty
  when none), drop its shapes whose *anchor* lies inside the region, add the
  submitted shapes, and overlay the submitted `classification` keys. A
  submitted shape whose anchor lies outside the region is 422. The merged
  whole-item result is stored as a `primary` version (drafts too), so two
  region annotators never overwrite each other. Anchor: centre of the
  axis-aligned envelope (bbox, rbox, polygon, polyline, mask), the point
  itself (point); a region's box is half-open `[x_min, x_max) × [y_min,
  y_max)`. The item moves to `submitted` when its last live annotate task
  closes, as for consensus; review and rejection are whole-item.
- **Gold** (`gold` true, QA-4), opened by `POST /projects/{id}/gold/tasks`
  on an item that has a gold reference, always with an `assignee_id`.
  Versions saved under it are `kind = gold`. Submitting closes the task and
  nothing else: the item's status, its other tasks and its `primary` line
  are untouched, no review task opens and no webhook fires.

Which task a save belongs to: the `task_id` in the create body when given
(must be the caller's live annotate task on that item, else 409), otherwise
the caller's own `in_progress` annotate task on the item, otherwise none
(an ordinary `primary` save, as before). Consensus and region cannot be
combined: `POST /items/{id}/split` is 409 on a project with
`consensus_annotators` > 1.

### annotation

`id`, `item_id`, `task_id` (nullable), `version` (int, unique per item),
`author_user_id` (nullable), `author_model_version_id` (nullable),
`source` (`annotation_source` enum), `label_schema_version_id`,
`result` (JSONB, see *Annotation result JSON*), `status`
(`annotation_status` enum), `duration_ms` (int, nullable),
`blob_path` (nullable — set once the outbox writer has persisted it),
`kind` (`annotation_kind` enum, default `primary`, QA-1 / QA-4),
`created_at`

Never updated in place: a change is a new row with `version + 1` (DATA-1).
Exactly one of `author_user_id` / `author_model_version_id` is set.

`annotation_kind`: `primary` | `consensus` | `gold`. Only `primary` versions
are the item's annotation: "latest version" everywhere (annotate / review
pages, exports, snapshots, stats, correction metrics, the outbox publish to
the result connector) means the latest `primary` version. `consensus` and
`gold` versions share the item's `version` sequence but are per-annotator
attempts, kept for agreement and accuracy metrics and never published.

Blind annotation: `GET /items/{id}/annotations` returns every kind to project
owners, reviewers and superusers. Anyone else gets `primary` versions plus
their *own* `consensus` / `gold` versions — and while they hold a live
consensus or gold task on the item, only their own versions of that kind
(no prelabel, no other annotator's work, no gold reference).

`annotation_source`: `human` | `model`
`annotation_status`: `draft` | `submitted` | `approved` | `rejected`

### comment

`id`, `project_id`, `item_id` (nullable), `annotation_id` (nullable),
`parent_id` (nullable, self-FK — thread root), `author_id`, `body`,
`anchor` (JSONB, nullable — region the comment points at), `resolved_at`
(nullable), `created_at`, `updated_at`

Mentions (WF-5): a `@` immediately followed by the e-mail address of a user
in the same organisation (`@anna@example.com`) creates a `mention`
notification for that user. Unknown addresses are left as plain text. A
reply (`parent_id` set) notifies the thread root's author with `reply`
unless they are the replier. Reviewing an annotation (`POST
/annotations/{id}/review`) notifies its author with `review`.

### model / model_version (ML-1, BYOM-2, BYOM-3)

`model`: `id`, `organization_id`, `name`, `task` (`detect` | `segment` |
`classify` | `ner` | `llm` | `ocr`), `endpoint_url` (nullable), `identity_type`,
`secret_ref`, `identity_config` (JSONB, default `{}`: the non-secret half of an
Entra identity, below), `created_at`, `deleted_at` (nullable: soft delete)

**Deleting a model** (`APP_MODEL_DELETE_MODE`). `soft` (default) sets
`deleted_at`: the model and its versions are gone from the API (404, absent
from lists, the derivation graph and snapshot lineage; pre-labelling,
`/interactive` and `/ocr` answer 404, a queued pre-label job fails), but the
rows stay, so every annotation a version wrote keeps its author. `hard`
removes the model and its versions, and answers 409 while any annotation was
written by one of them: an annotation needs an author
(`ck_annotation_author_xor`) and its history is never rewritten (DATA-1).
The audit row's `after` is `{"mode": "soft" | "hard"}`. A soft-deleted model
can't be restored from the API.

A model with no `endpoint_url` is an **external producer** (API-8): an AI
agent or an offline pipeline that posts its own pre-labels with
`POST /items/{id}/prelabels` under one of the model's versions. The
platform never calls it: `POST /projects/{id}/prelabel` answers 409, `POST
/models/{id}/check` answers `ok: false`, and `/items/{id}/interactive` and
`/items/{id}/ocr` answer 503, each saying the model has no endpoint.
`model_version`: `id`, `model_id`, `version` (int, unique per model),
`class_mapping` (JSONB), `metrics` (JSONB), `snapshot_id` (nullable, FK
`snapshot`, set null on delete), `snapshot_digest` (nullable, sha256 hex),
`training_run` (JSONB, nullable), `parent_version_id` (nullable, FK
`model_version`, set null on delete), `derivation` (nullable: `trained` |
`distilled` | `quantized`), `created_at`

**Derivation (EXP-8).** A version can name the version it came from:
`quantized` — the parent's weights at lower precision (same architecture,
usually the same model); `distilled` — a student trained on data the parent
(the teacher) pre-labelled and people corrected, usually a smaller model;
`trained` — trained or fine-tuned, with the parent optional (the starting
weights). The parent may belong to any model of the organisation (a teacher
in one model, its student in another); outside it is a 404. `distilled` and
`quantized` without a parent are a 422. A parent always exists before its
child, so the graph has no cycles. `GET /models/{id}/family` returns every
version connected to one of the model's versions through parent links, in
any model, so the UI can draw the family graph.

**Well-known metric keys.** `metrics` stays free-form, but the training
pipeline writes and the UI reads these when present: `precision`, `recall`,
`f1` (0–1, on the held-out split), `split`, `per_class` (`{class: {precision,
recall, f1, tp, fp, fn}}`), `size_bytes` (int, the artifact), `latency_ms_p50`
(int ms per item on the pipeline's machine, `latency_device` names it), and
`dtype` (`fp32` | `fp16` | `int8`, or another numeric type such as the
baseline's `fp64`).

**Lineage (EXP-8).** A version records what it was trained on. The
customer's pipeline (triggered by `retrain.requested`, ML-9) registers the
version with the `snapshot_id` and `snapshot_digest` it received; the
platform checks the digest against the snapshot row and rejects a mismatch
with 409, so a version can never claim a snapshot whose content it did not
see. `snapshot_id` alone fills the digest from the row; `snapshot_digest`
alone links the organisation's snapshot with that digest when one exists
(a re-created snapshot with the same content has the same digest) and is
otherwise stored as-is. `training_run` is free-form JSON for the run itself
(`id`, `url`, `started_at`, `finished_at`, `params`, …) — the platform
stores and shows it, never interprets it. Downstream lineage is implicit:
annotations carry `author_model_version_id`, so snapshot → version →
predicted items is one join.

`endpoint_url` is the base URL of a service implementing the §8 model
interface (`model-service/` is the reference): the platform calls
`{endpoint_url}/info` and `{endpoint_url}/predict`, and — for `segment` models
driving the annotator's smart-polygon tool (ML-7) — `{endpoint_url}/interactive`
with `{item_id, url, width, height, point? | box?}` (see
`model-service/app/schemas.py::InteractiveRequest`).
The reference service takes an optional `MODEL_API_KEY` (unset = open):
when set, every route but `/health` and `/ready` needs `Authorization:
Bearer <key>`, which the platform sends for a model registered with the
`bearer` identity and a `secret_ref` holding the key.
For `ocr` models (reading scanned PDFs, below) the platform calls
`{endpoint_url}/ocr` with `{item_id, url, media_type: "pdf", pages: [n]}`
and expects `{engine, pages: [{page, width, height, words: [{text, bbox}]}]}`
— `bbox` `[x_min, y_min, x_max, y_max]` in the page's points (the space of
pdf shapes), `width` / `height` the page size in points (see
`model-service/app/schemas.py::OcrRequest`). How the model reads the page
is its own business: the reference service runs Tesseract locally or sends
the rendered page to an external vision LLM (Claude, or any
OpenAI-compatible endpoint), chosen by its `OCR_ENGINE`.

`identity_type` (BYOM-3): `none` | `api_key` | `bearer` |
`service_principal` | `managed_identity`. For `api_key` the secret resolved
from `secret_ref` is sent as `X-API-Key`; for `bearer` as
`Authorization: Bearer …`. `secret_ref` is a secret-store reference, never
the credential, and is never returned by the API (only `has_secret`).

The two Entra identities send `Authorization: Bearer <Microsoft Entra token>`
for `identity_config.scope` — `https://ml.azure.com/.default` for an Azure ML
online endpoint, `https://cognitiveservices.azure.com/.default` for Azure
OpenAI / AI Foundry, `api://<app id>/.default` for a customer's own API; it
must end in `/.default`. The token is fetched for every request, not once per
client: `azure-identity` caches it and renews it five minutes before expiry,
so a prelabel job that keeps one client for hours never sends an expired one.

| `identity_type` | `secret_ref` | `identity_config` |
| --------------- | ------------ | ----------------- |
| `service_principal` | required: the client secret | `scope`, `tenant_id`, `client_id` required |
| `managed_identity` | refused | `scope` required; `client_id` picks a user-assigned identity (omitted: system-assigned, or the AKS workload identity) |

`identity_config` is returned by the API (nothing in it is secret) and
replaced whole on `PATCH`; `null` clears it. A failed Entra sign-in (wrong
secret, no identity on the host, no role) is permanent — the job fails; an
unreachable token endpoint is transient — the worker retries. The identity
needs a role on the endpoint (Azure ML: *AzureML Data Scientist* or a custom
role with `…/onlineEndpoints/score/action`; Azure OpenAI: *Cognitive Services
OpenAI User*).

`class_mapping` (BYOM-2) maps the model's class names to the project's
schema: `{"<model class>": "<schema class>" | null}`. Pre-labelling posts
the model a schema built from the *model's* class names (so the model can
emit them), then rewrites each returned shape's `class` through the mapping.
`null` drops that model class. A model class absent from the mapping is
kept only if the schema has a class of the same name, otherwise dropped.
An empty mapping is the identity: the model must emit schema class names.

### ml_platform (API-6)

A customer's MLflow tracking server, Databricks workspace or Azure ML
workspace. `id`, `organization_id`, `name` (unique per organisation),
`kind` (`ml_platform_kind`: `mlflow` | `databricks` | `azureml`),
`tracking_uri`, `identity_type` (text), `secret_ref` (nullable, a secret-store
reference, never returned — only `has_secret`), `config` (JSONB, the
non-secret parts of the identity and the retrain job), `created_at`,
`updated_at`.

All three speak MLflow's REST API — Databricks hosts managed MLflow, an Azure
ML workspace exposes an MLflow-compatible endpoint — so one client serves
them: `mlflow-skinny` (backend extra `mlflow`, imported lazily; it brings
`databricks-sdk`). Credentials are per row, handed to the client for each
call, never put in the process environment. Without the extra, every
platform call is a 503 naming it and `check` reports it.

| `kind` | `tracking_uri` | `identity_type` | `secret_ref` holds | `config` |
| ------ | -------------- | --------------- | ------------------ | -------- |
| `mlflow` | `http(s)://host[:port][/prefix]` | `none`, `bearer`, `basic` | token / `user:password` | `ui_url` (optional): where people open the UI when it differs from the address the backend calls; run links use it |
| `databricks` | workspace URL, `https://adb-….azuredatabricks.net` | `bearer`, `service_principal` | PAT / OAuth client secret | `client_id` (SP), `job_id` (retrain, optional) |
| `azureml` | the workspace's MLflow tracking URI, `azureml://<region>.api.azureml.ms/mlflow/v1.0/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.MachineLearningServices/workspaces/<ws>` (the `https://` form is accepted too) | `service_principal`, `managed_identity` | client secret (SP) | `tenant_id` + `client_id` (SP); `client_id` for a user-assigned identity (MI, optional) |

Databricks service principals use OAuth M2M through `databricks-sdk`; Azure
ML gets an Entra token for `https://management.azure.com/.default` through
`azure-identity`. A call gives up after `_CALL_TIMEOUT_SECONDS` (30 s, check:
10 s); MLflow's own retries are capped at 2.

What the platform does with one — it trains nothing itself (ML-9):

- **Publish a snapshot** (`POST /projects/{id}/snapshots/{sid}/mlflow`):
  a FINISHED run named `snapshot <name>` in the experiment
  (default `annotation/<project name>`, on Databricks
  `/Shared/annotation/<project name>`), with the snapshot as a dataset input
  (`source_type` `annotation-snapshot`; `source` JSON `{snapshot_id,
  project_id, digest, blob_path, result_connector_id}`; `digest` the first
  16 hex characters, since MLflow stores at most 36; `profile` JSON
  `{item_count, split}`), params `annotation.project_id`,
  `annotation.snapshot_id`, `annotation.label_schema_version_id`,
  `item_count`, and tags `annotation.snapshot_id`,
  `annotation.snapshot_digest` (full sha256). Nothing is copied: the run
  points at the snapshot in the customer's result storage. Publishing the
  same snapshot to the same experiment again returns the existing run.
- **Import a version** (`POST /models/{id}/versions/import`): reads an MLflow
  run — named directly, or through a registered model version — and adds a
  `model_version` from it: `metrics` = the run's latest metrics,
  `training_run` = `{id, url, name, experiment_id, status, started_at,
  finished_at, params, source: {kind, ml_platform_id, registered_model?,
  model_version?, model_uri}}`. Lineage (EXP-8) comes from the run's
  `annotation.snapshot_id` / `annotation.snapshot_digest` tags, else from an
  `annotation-snapshot` dataset input, and is checked exactly like a version
  added by hand (409 on a digest mismatch). A training run that logs the
  published snapshot as its input — `training/` does — therefore needs no
  extra step. Unity Catalog model names (`catalog.schema.model`) are not
  read; import those by `run_id`.
- **Retrain on Databricks** (`POST /projects/{id}/retrain` with
  `ml_platform_id`): calls Jobs `run-now` on `config.job_id` with
  `job_parameters` `{project_id, snapshot_id, snapshot_digest,
  snapshot_blob_path, model_id, note}` (empty string when absent) before
  the webhook event is emitted; the run is added to the event payload and
  the response as `ml_run: {ml_platform_id, run_id, run_url}`. A failed
  call is a 503 and emits nothing. `azureml` and `mlflow` platforms are a
  422 — use the `retrain.requested` webhook.

### job

`id`, `project_id` (nullable), `type` (`job_type` enum), `status`
(`job_status` enum), `progress` (int 0–100), `payload` (JSONB),
`result` (JSONB, nullable), `error` (text, nullable), `attempts` (int),
`started_at`, `finished_at`, `created_at`, `updated_at`

`job_type`: `scan_source` | `tile_image` | `prelabel` | `export` | `snapshot` |
`import` | `thumbnail` | `rebuild_cache` | `extract_text`

The arq entry carries the row id as its only argument. With OpenTelemetry on
(`APP_OTEL_ENABLED`) it also carries the enqueuer's W3C trace context as the
keyword `otel_context` (`{traceparent, tracestate?}`), so the job's span
continues the API request's trace; the worker strips it before the job body
runs. It is never state: a job requeued by the stranded-job cron starts a
trace of its own. `progress` is written at most once a second while the row
is `running`, in its own short transaction, and never to a row in any other
status.

**Worker shutdown** (deploy, scale-down, drain): running jobs get 45 s to
finish (`SHUTDOWN_JOB_WAIT_SECONDS`), then arq cancels them and queues them
again. The row goes back to `queued` (progress 0, `attempts` kept) — unless
`POST /jobs/{id}/cancel` already marked it `cancelled`, which stays final.
The pod's grace period (60 s) must exceed the wait.

**Per-project limit** (`APP_JOB_MAX_RUNNING_PER_PROJECT`, default 3): a job
whose project already has that many `running` jobs is not started — it stays
`queued` and is offered again every 15 s, without spending an attempt. The
check locks the project row, so two workers never both take the last slot; a
`running` row older than the job timeout (a dead worker's) does not count.

**Stranded jobs** (`worker/stranded.py`, arq cron every 5 minutes). A row
the queue has lost — Redis flushed, failed over or restored from an older
snapshot — is recovered from the row, the source of truth:
- `queued` for over 5 minutes with no arq job key → enqueued again under
  its own id (arq refuses a duplicate while the entry exists, so a job that
  is merely waiting is never doubled); logged `job.requeued_stranded`.
- `running` since longer than the worker's job timeout plus 5 minutes
  (35 min) with no arq job key → `failed` with `error` "lost by the queue
  (worker or Redis restarted); retry it". It is not re-run automatically
  because it may have partly run; `POST /jobs/{id}/retry` runs it again.

`prelabel` (ML-2, ML-4, ML-10): runs a model version over the selected items
in batches, storing each non-empty prediction as a `draft` annotation version
with `source = model`, `author_model_version_id` set and per-shape
`confidence`, and moves `new` items to `prelabeled`. `filter.item_status`
may only name `new` and/or `prelabeled` (422 otherwise), and items that
already have a human version are never touched, whichever filter is given
(ML-10). Idempotent per model version: an item that already has a draft by
this `model_version_id` is skipped, so a retry after a partial run and a
plain re-run add nothing, while a newer version adds its own drafts.
With `prioritize_uncertain` (ML-6) every scored item gets
`meta.uncertainty` = `1 - min(shape confidence)` (1.0 for an empty
prediction, 0.5 for shapes without a confidence) and its open / in-progress
`annotate` task's `priority` = `round(uncertainty × 100)`, so the WF-6
queue serves the model's least confident items first; `result.prioritized`
counts the tasks touched.
Image and text items are selected; an item whose media type the model does
not list in `GET /info` `media_types` (absent = `["image"]`) is counted in
`skipped_media` and never sent.
`result`: `{"model_version_id", "selected", "predicted", "empty",
"skipped_human", "skipped_done", "skipped_media", "errors",
"dropped_shapes"}` —
`dropped_shapes` counts shapes the class mapping discarded, including shapes
whose mapped class does not allow their tool.
`thumbnail` (IMG-8, UX-6): downscales every `image` item that has no
`thumbnail_path` yet (all of them with `{"force": true}`) to
`APP_THUMBNAIL_SIZE` px on the longest side, writes the JPEG to
`cache/thumbnails/{item_id}.jpg` on the project's effective cache connector and sets
`item.thumbnail_path`. Items over `APP_THUMBNAIL_MAX_SOURCE_BYTES` and
files Pillow cannot decode are counted, not failed. A `scan_source` job
queues one automatically when it created image items and the project has an
effective cache connector. Payload: `{"force"?: bool, "item_ids"?: [uuid]}`.
`result`: `{"selected", "written", "skipped_too_large", "errors"}`.
`tile_image` (IMG-1, IMG-2): turns large image items into a Deep Zoom
(DZI) tile pyramid so the browser never loads the whole image. Payload
`{"item_ids"?: [uuid], "force"?: bool}`. Selects `image` items without
`meta.tiles` (all of them with `force`). Each source is streamed to a
temporary file under `APP_TILE_WORK_DIR` in 8 MiB ranged reads (sources over
`APP_TILE_MAX_SOURCE_BYTES` are skipped and counted), opened with libvips
(pyvips, sequential access) and measured; one smaller than
`APP_TILE_MIN_PIXELS` (width × height) is skipped and counted. Otherwise
libvips writes a DZI pyramid — 256 px JPEG tiles (quality 85), overlap 0,
level 0 = 1 × 1 px, `max_level` = ⌈log₂ max(width, height)⌉, level L sized
⌈W / 2^(max_level − L)⌉ × ⌈H / 2^(max_level − L)⌉ — uploaded to the effective
cache connector under `cache/tiles/{item_id}/` (`image.dzi`,
`image_files/{level}/{col}_{row}.jpeg`, 16 uploads in flight). Then
`item.meta.tiles` = `{"format": "dzi", "path": "cache/tiles/{item_id}/",
"tile_size": 256, "overlap": 0, "suffix": "jpeg", "max_level", "width",
"height"}`, `item.width` / `item.height` are filled when null, and a
thumbnail is written from the same file (IMG-8, `thumbnail_path`), which
the `thumbnail` job cannot do for such sources. Temporary files are always
removed. `result`: `{"selected", "tiled", "skipped_small",
"skipped_too_large", "errors"}`. A `scan_source` job that created items
queues one when the project has an untiled image item over
`APP_THUMBNAIL_MAX_SOURCE_BYTES`, with width × height ≥
`APP_TILE_MIN_PIXELS`, or with no measured size (TIFF and other formats the
scan does not measure; the job measures and skips the small ones). Whole-slide formats that need OpenSlide
(`.svs`, `.ndpi`, `.mrxs`) are not supported by the bundled libvips build
and fail per item (counted in `errors`).
`rebuild_cache` (SRC-6): regenerates the project's derived data on its
effective cache connector. Payload `{"purge"?: bool, "reason"?: str}`. Every
image and PDF item of the project loses `thumbnail_path` and `meta.tiles` (in
one transaction); with `purge`, each item's `cache/thumbnails/{item_id}.jpg`
and everything under `cache/tiles/{item_id}/` is deleted from the cache
connector first — per item, never the whole `cache/` prefix, which other
projects sharing the container also use. Then a `thumbnail` job is queued
for the project and, when any item had tiles, a `tile_image` job for exactly
those items. `result`: `{"cleared_thumbnails", "cleared_tiles",
"purged_blobs", "purge_errors", "thumbnail_job_id", "tile_job_id"}` (job
ids `null` when not queued). Fails when the project has no effective cache
connector.
`import` (EXP-6): reads an annotation file or archive from a connector,
parses it with the format's importer (`app.importers`, formats `coco`,
`yolo`, `voc`, `cvat`, `label_studio`), matches every record to a project
item and writes one new annotation version per matched item. Payload:
`{"format", "connector_id", "path", "class_mapping"?, "attribute_mapping"?,
"status"?, "dry_run"?, "label_schema_version_id"?, "created_by_id"}`.

- `path` is a single file, a `.zip` archive (expanded in memory, entries are
  the importer's input files) or a prefix ending in `/` (every object under
  it). Imported files are annotation data, never media: the platform still
  never copies raw media (ARC-3).
- Items are matched by the path the source names, in order: exact `item.path`,
  then unique basename, then unique stem (YOLO label files carry only the
  stem). A record with no match counts as `unmatched` and is skipped.
- Class names go through `class_mapping` (`{source_name: schema_class}`,
  identity by default); a shape whose class is not in the label schema is
  dropped and counted in `dropped_shapes`.
- Shape attribute names then go through `attribute_mapping`
  (`{schema_class | "*": {source_attribute: schema_attribute | null}}`,
  identity by default), looked up by the shape's *mapped* class; a class
  entry wins over `"*"` key by key, and `null` discards the attribute. An
  attribute the target class does not declare after mapping is dropped and
  counted in `dropped_attributes` rather than failing the item.
  Classification keys are not mapped. Normalised coordinates (YOLO,
  Label Studio percentages) are converted to pixels with the item's stored
  width/height, or the source's own dimensions; an item with neither is an
  `error`.
- Each written version is authored by `created_by_id` (`source = human`) with
  `status` `submitted` (default) or `draft`. A `submitted` import moves items
  in `new` / `prelabeled` / `annotating` / `skipped` / `rejected` to
  `submitted`; `in_review` / `approved` items keep their status; a `draft`
  import never changes item status. Re-running the same import adds another
  version per item — imports are not idempotent, `dry_run` is the preview.
- `dry_run: true` parses and matches but writes nothing.
- `result`: `{"format", "dry_run", "parsed", "matched", "unmatched",
  "imported", "errors", "dropped_shapes", "dropped_attributes",
  "classes": {source_name: count},
  "attributes": {source_class: [source_attribute, …]} (sorted, as seen
  before mapping — what the UI offers to map after a dry run),
  "unmatched_sample": [path, …≤20], "problems": [message, …≤20]}`.
- Masks: COCO `segmentation` RLE is accepted uncompressed (`counts` a list)
  and compressed (`counts` the pycocotools string); Label Studio
  `brushlabels` results (`format: "rle"`, its own bit-packed RLE) are
  decoded too. Both become a `mask` shape whose `rle` carries uncompressed
  column-major COCO counts (`sum(counts) == height × width`, else skipped
  with a warning). CVAT video `<track>` elements are skipped with a warning
  until video items exist.
- Keypoints: a COCO category that names its `keypoints` imports each of its
  annotations as one `keypoints` shape (the annotation's segmentation and
  bbox are left out); a count that differs from the category's names is
  skipped with a warning. Other categories' keypoints become one `point`
  shape per labelled keypoint, as before. A `keypoints` shape whose mapped
  class has no skeleton, or one of another size, counts in `dropped_shapes`.
- A malformed file (`ImportFormatError`) fails the job permanently.

`job_status`: `queued` | `running` | `succeeded` | `failed` | `cancelled`

### snapshot

`id`, `project_id`, `name`, `filter` (JSONB), `split` (JSONB, nullable —
the *Split JSON* below, EXP-3), `label_schema_version_id`, `item_count`,
`blob_path`, `digest` (sha256 hex), `created_by_id`, `created_at`

Immutable once written (EXP-1).

**Split JSON** (`POST /projects/{id}/snapshots` body `split`, EXP-3):
`{train, val, test, seed, group_by}`. The three ratios are floats in [0, 1]
summing to 1 (default 0.8 / 0.1 / 0.1); `seed` is an int (default 0);
`group_by` is `null`, `"folder"` (the item path's parent directory) or
`"meta.<key>"` (a value in `item.meta`, items without it fall back to their
own id). An item's split is `sha256("{seed}:{group key}")` bucketed by the
cumulative ratios — deterministic, and every item sharing a group key lands
in the same split (group-aware). The manifest gets `split: {config, counts}`
and each entry and JSONL document a `split` of `train` \| `val` \| `test`.
An export of a split snapshot writes the exporter's files under `train/`,
`val/`, `test/`; `POST /projects/{id}/exports` with `split` (requires
`snapshot_id`) exports that one partition only.

`filter` (also the `filter` of `POST /projects/{id}/exports`) selects items
that have at least one annotation version and narrows on each item's
**latest** version (EXP-2). Every field is optional and they combine with
AND:

| field | matches when |
| ----- | ------------ |
| `item_status` | list of `item_status` — the item is in one of them |
| `annotation_status` | list of `annotation_status` — the latest version is |
| `path_prefix` | `item.path` starts with it |
| `classes` | the latest version has at least one shape with `class` in the list |
| `annotator_ids` | the latest version's `author_user_id` is in the list |
| `source` | list of `human` / `model` — who produced the latest version |
| `annotated_after` | the latest version's `created_at` ≥ this instant (naive = UTC) |
| `annotated_before` | the latest version's `created_at` < this instant |

Not yet: filtering on classification values or attributes, on *any* version
rather than the latest, or a random / stratified sample.

### audit_event

`id`, `organization_id`, `actor_id` (nullable), `action` (text),
`target_type` (text), `target_id` (UUID, nullable), `ip` (inet, nullable),
`before` (JSONB, nullable), `after` (JSONB, nullable), `created_at`

Append-only. No updates, no deletes (SEC-3) — except that a SEC-6 erasure
clears `ip` and `email` keys on the erased person's rows. Written by
`services/audit.py::record` in the same transaction as the change; a
rolled-back change leaves no audit row. `ip` is the direct peer address
(`X-Forwarded-For` is not trusted until a proxy allow-list exists).
`before` / `after` are small snapshots chosen by the caller and never carry
a credential.

`action` is `<target>.<verb>`. The set today:

| action | target_type | when |
| ------ | ----------- | ---- |
| `auth.login`, `auth.login_failed` | `user` | local or SSO sign-in (`after.method = "oidc"` for SSO); a failed attempt is recorded only for a known account (an unknown email has no organisation to file it under) |
| `user.superuser_sync` | `user` | AUTH-3: SSO group sync changed `is_superuser`; `after` `{is_superuser}` |
| `user.provision`, `user.link_idp` | `user` | SSO created the account / attached `idp_subject` to an existing account matched by e-mail (AUTH-1) |
| `user.update`, `user.deactivate`, `user.activate`, `user.scim_delete` | `user` | SCIM (AUTH-3) changed the e-mail, display name or `externalId` (`before` / `after` carry the changed keys) / `is_active` / hid the account; `user.provision` with `after.method = "scim"` for a SCIM-created account |
| `scim_group.create` / `.update` / `.delete` | `scim_group` | SCIM (AUTH-3); `after` `{display_name, external_id, members}` (member count) |
| `organization.scim_token` | `organization` | a superuser minted (`after.enabled = true`) or removed the SCIM token (AUTH-3) |
| `connector.events_token` | `connector` | a superuser minted or removed the storage-event token (SRC-3) |
| `user.export_personal_data`, `user.erase` | `user` | SEC-6 access and erasure; `after` carries only `{redact_comments}` for an erasure |
| `service_account.create` / `.delete` | `user` | a service account made / deactivated by a superuser (AUTH-4) |
| `api_key.create` / `.revoke` | `api_key` | `after` carries `{user_id, name, scopes, expires_at}`; never the token |
| `project.create` / `.update` / `.delete` | `project` | |
| `label_schema.create` | `label_schema_version` | a new schema version |
| `membership.create` / `.update` / `.delete` | `membership` | permission changes; `before`/`after` carry `{user_id, role}` |
| `connector.create` / `.update` / `.check` | `connector` | |
| `model.create` / `.update` / `.delete`, `model_version.create` | `model` / `model_version` | |
| `annotation.create` / `.submit` / `.review` | `annotation` | `after` carries `{status, version}` and for review `{approve}` |
| `annotation.prelabel` | `annotation` | an external producer's pre-label (API-8); `after` carries `{version, model_version_id}` |
| `item.skip` | `item` | |
| `job.create` | `job` | `after` carries `{type}` — scan, export, snapshot, prelabel, import |
| `comment.create`, `comment.resolve` | `comment` | |

### idempotency_key (API-2)

`id`, `organization_id`, `endpoint` (text: `project.create`, `job.export`,
`job.snapshot`, `job.import`, `job.scan_source`, `job.thumbnail`,
`job.prelabel`, `job.rebuild_cache`), `key` (text, the header value), `target_id` (the row the
first request created), `created_at`. Unique on
`(organization_id, endpoint, key)`.

A create carrying `Idempotency-Key` looks the key up first and answers the
earlier row with **200** instead of creating again; the key row is written
in the same transaction as the create, so two concurrent retries collapse
on the unique index and the loser answers with the winner's row. Keys are
organisation-scoped and never expire.

### license_state (LIC-6, LIC-8, LIC-25, LIC-26, LIC-27, LIC-29)

One row per installation (not per organisation), created by the migration:
`id`, `slot` (always `install`, unique), `key` (text, nullable — a licence
key pasted by an admin, fetched by the licence refresh or a trial), `key_source`
(`admin` | `refresh` | `trial`, nullable), `clock_high_water` (date, nullable — the
latest UTC date seen at sign-in), `host_mismatch_since` (date, nullable —
first sign-in on a host the key in force is not bound to), `last_host` (text,
nullable — last non-loopback host seen at sign-in), `refresh_attempted_at`,
`refresh_succeeded_at`, `heartbeat_attempted_at`, `heartbeat_sent_at`
(timestamptz, nullable), `refresh_error`, `heartbeat_error` (text, nullable),
`refresh_payload`, `heartbeat_payload` (JSONB, nullable — the last body sent),
`revocations` (text, nullable — the latest verified revocation list, LIC-8),
`created_at`, `updated_at`. See *Licence key* for how it is read.

### notification (WF-5)

`id`, `user_id`, `type` (`notification_type` enum), `payload` (JSONB),
`read_at` (nullable), `created_at`

`notification_type`: `mention` | `reply` | `review`. `payload` always has
`project_id`, `item_id` and `actor_id`; `mention` / `reply` add `comment_id`
and `excerpt` (first 200 chars of the body); `review` adds `annotation_id`,
`approve` and `comment` (the reviewer's text, nullable).

E-mail (API-7): `emailed_at` (timestamptz, nullable) and `email_attempts`
(int, default 0). With `APP_SMTP_HOST` set, the worker cron
`send_notification_emails` (every minute) mails each notification younger
than `APP_NOTIFICATION_EMAIL_MAX_AGE` whose user is active, not a service
account, not erased and has `email_notifications`, then sets `emailed_at`. An
SMTP failure leaves it for the next tick; after 5 attempts it is dropped. The
mail names the actor, the project and the item, quotes the excerpt or
review comment, and links to `{APP_FRONTEND_URL}/projects/{project_id}/
annotate/{item_id}`. Teams and Slack are webhook formats (below).

### webhook / webhook_delivery (API-4, ML-9)

`webhook`: `id`, `organization_id`, `project_id` (nullable — null means
every project in the organisation), `url`, `description` (nullable),
`events` (JSONB list of event names, or `["*"]`), `secret` (HMAC key, shown
once at creation / rotation, never read back), `is_active`,
`created_by_id` (nullable), `last_delivery_at` (nullable),
`last_response_status` (nullable), `format` (`webhook_format` enum, default
`json`), `created_at`, `updated_at`

`webhook_format` (API-7): `json` — the signed event document below; `slack` —
a Slack incoming-webhook message (`{text, blocks}`); `teams` — a Microsoft
Teams Workflows message carrying one Adaptive Card (`{type: "message",
attachments: [{contentType: "application/vnd.microsoft.card.adaptive",
content}]}`). Chat formats carry a one-line summary of the event and a link
to the project; the signature headers are sent with every format.

`webhook_delivery`: `id`, `webhook_id`, `event`, `payload` (JSONB — the
document sent), `status` (`webhook_delivery_status` enum), `attempts`,
`next_attempt_at`, `response_status` (nullable), `error` (nullable),
`delivered_at` (nullable), `created_at`

`webhook_delivery_status`: `pending` | `succeeded` | `failed`

Events (`services/webhooks.py::emit_event`, all written in the same
transaction as the change they report — a dead subscriber can never slow a
request down):

| event | when | `data` |
| ----- | ---- | ------ |
| `annotation.submitted` | a version is submitted (either submit path) | `annotation_id`, `item_id`, `item_path`, `item_status`, `version`, `author_user_id` |
| `annotation.approved` / `annotation.rejected` | a verdict is recorded (single or bulk) | the same, plus `reviewer_id`, `comment`, `corrected` |
| `item.approved` | the item reaches `approved` by any path: a verdict (single, bulk or consensus) or a submit that needs no review (`review: none`, or `sampled` and outside the sample) — the one event that says "this item is done" | `item_id`, `item_path`, `annotation_id`, `version`, `via` (`review` \| `no_review`) |
| `snapshot.created` | the snapshot job commits | `snapshot_id`, `name`, `item_count`, `digest`, `blob_path`, `label_schema_version_id`, `split` |
| `job.succeeded` / `job.failed` | a project job reaches a final status | `job_id`, `type`, `status`, `result`, `error` |
| `retrain.requested` | `POST /projects/{id}/retrain` (ML-9) | `project_id`, `requested_by`, `model_id`, `note`, `snapshot` (`{id, name, digest, blob_path, item_count, label_schema_version_id, split}` or null), `ml_run` (`{ml_platform_id, run_id, run_url}` when a Databricks job was started, API-6, else null) |
| `webhook.test` | `POST /webhooks/{id}/test` | `webhook_id`, `requested_by` |

`training/` is the reference subscriber for `retrain.requested`: it trains
on the named snapshot's `native` export and registers the version with
`snapshot_id`, `snapshot_digest` and a `training_run` (see
`training/README.md`).

Delivery: the worker cron `deliver_webhooks` (every 15 s) claims due
`pending` rows (`FOR UPDATE SKIP LOCKED`, at most `APP_WEBHOOK_MAX_PER_MINUTE`
per hook per minute — a burst is spread out, never dropped) and POSTs the payload —
`{event, occurred_at, organization_id, project_id, data, delivery_id}` as
compact key-sorted JSON — with headers `X-Annotation-Event`,
`X-Annotation-Delivery` (the row id, for de-duplication) and
`X-Annotation-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of
"{t}.{body}" with the hook's secret>`. Any 2xx succeeds; anything else
(including a timeout, `APP_WEBHOOK_TIMEOUT`) schedules the next attempt with
exponential backoff from 30 s (30 s, 1, 2, 4, 8 … min, capped at 6 h) until
`APP_WEBHOOK_MAX_ATTEMPTS` is spent, then `failed`. A hook deactivated
while deliveries are pending drops them as `failed` / "webhook inactive".
Subscribers verify with `services/webhooks.py::verify` semantics: parse
`t`, reject if older than 5 min, constant-time compare.

SSRF (SEC-4): a webhook URL must be `http(s)` with a host. Unless
`APP_WEBHOOK_ALLOW_PRIVATE_URLS` is true, the worker resolves the host before
every attempt and refuses (attempt counted, error `"destination not
allowed: <reason>"`) any address that is loopback, private, link-local,
multicast, reserved or unspecified, so a hook cannot reach the cluster's
internal services or cloud metadata. Redirects are not followed.

### outbox_event

`id`, `aggregate_type`, `aggregate_id`, `type`, `payload` (JSONB),
`published_at` (nullable), `attempts`, `created_at`

Drives DATA-2: the annotation row and its outbox row are written in one
transaction; the worker publishes to blob and stamps `published_at`. The
publisher runs once a minute and claims `APP_OUTBOX_POLL_BATCH_SIZE` rows
per transaction, batch after batch, until the backlog is empty, a batch has
a failed row (storage trouble waits for the next tick instead of using up
attempts) or 45 s have passed. A
review verdict recorded in place (no corrected result) emits a second
`annotation.written` event for the same `blob_path`, carrying the new
`status` and `reviewer_id`; the publisher overwrites the document.

## Project workflow JSON (WF-1)

Stored in `project.workflow`. Every key has a default, so `{}` (and every
project created before this section existed) is the standard
annotate → review → approve flow. Unknown keys are rejected on create and
update; `PATCH` replaces the whole object.

```json
{
  "review": "required",
  "rejection_returns_to": "same_annotator",
  "allow_skip": true,
  "allow_self_review": true,
  "consensus_annotators": 1,
  "review_sample_rate": 0.1,
  "gold_every": null
}
```

| Key | Values | Effect |
| --- | ------ | ------ |
| `review` | `required` (default) \| `none` \| `sampled` | `none`: `submit` moves the item `annotating → approved`; no `review` task is opened and `start_review` / `approve` / `reject` are never legal. `sampled` (QA-7): a submit goes to review when the item is *in the sample*, else straight to approved as with `none` — see `review_sample_rate` |
| `review_sample_rate` | `0 < r ≤ 1`, default `0.1` | with `review: sampled`, an item is in the sample when the first 8 bytes of `sha256(str(item_id))`, read as an unsigned big-endian integer divided by 2⁶⁴, are `< r` — deterministic, so a retried submit decides the same way and the sample is reproducible. An item that was ever rejected is always reviewed again. Ignored otherwise |
| `gold_every` | `null` (default) \| `2` … `1000` | QA-4: when an annotator asks `POST /tasks/next` for annotate work (`type` absent or `annotate`) and has closed at least `gold_every − 1` non-gold annotate tasks in this project since their latest gold task (ever, if none), the server first opens a gold task for them on a gold item they have no gold task on yet (any status), assigned to them, and hands that out. No gold item left → the ordinary queue |
| `rejection_returns_to` | `same_annotator` (default) \| `queue` | who gets the `annotate` task a rejection opens: the version's author, or nobody (first claimant) |
| `allow_skip` | `true` (default) \| `false` | `false` removes the `skip` trigger; `POST /items/{id}/skip` answers 409 |
| `allow_self_review` | `true` (default) \| `false` | `false` refuses (403) a review verdict from the user who authored the submitted version, superusers included |
| `consensus_annotators` | `1` (default) … `10` | N > 1: every item is annotated independently by N people before review (QA-1, see *task*). Needs `review: required` (422 otherwise, `sampled` included). Changing it affects only items whose first annotate task opens afterwards |

The pure machine in `services/workflow.py` takes the config
(`next_status(source, trigger, role=…, config=…)`); without one it is the
default flow.

## Label schema JSON (TOOL-1, TOOL-2)

Stored in `label_schema_version.definition`. Validated by
`schemas/label_schema.py`.

```json
{
  "version": 1,
  "classes": [
    {
      "name": "car",
      "display_name": "Car",
      "color": "#e11d48",
      "hotkey": "1",
      "tools": ["bbox", "polygon"],
      "attributes": [
        {
          "name": "occluded",
          "type": "boolean",
          "required": false,
          "default": false
        },
        {
          "name": "make",
          "type": "select",
          "required": false,
          "options": ["Toyota", "Volvo", "Other"]
        }
      ]
    }
  ],
  "classification": [
    {
      "name": "weather",
      "type": "select",
      "required": true,
      "options": ["clear", "rain", "snow"]
    }
  ]
}
```

Attribute `type`: `text` | `number` | `select` | `multiselect` | `boolean`.
Class `tools`: `bbox` | `rbox` | `polygon` | `polyline` | `point` | `mask` |
`keypoints` | `span` | `relation` | `classification` | `ranking` | `rating` |
`segment`. `segment` classes label time intervals on `audio` and
`timeseries` items (below).
`ranking` and `rating` classes are criteria for `llm` items (below). A class
with the `rating` tool must declare its `scale`; other classes must not:
`{"min": 1, "max": 5, "labels": {"1": "Unusable", "5": "Excellent"}}` —
integers, `min < max`, at most 21 steps; `labels` (optional) name some or all
of the values. `span` classes are
entity types for text and pdf items; a `relation` class is a relation type — a
relation shape's `class` must be a class with the `relation` tool.

A class with the `keypoints` tool must declare its `skeleton` (TOOL); other
classes must not:

```json
{
  "name": "person",
  "display_name": "Person",
  "color": "#0ea5e9",
  "tools": ["keypoints", "bbox"],
  "skeleton": {
    "points": ["nose", "left_eye", "right_eye", "left_shoulder", "right_shoulder"],
    "edges": [[0, 1], [0, 2], [3, 4]]
  }
}
```

`skeleton.points` are the keypoint names, non-empty and unique, in the order
a `keypoints` shape stores them; `edges` are 0-based index pairs into
`points` (distinct ends, no duplicates in either direction), drawn as bones.
The order is the contract: a new schema version that reorders points changes
what earlier versions' shapes mean.

## Annotation result JSON (DATA-8)

Stored in `annotation.result`. Coordinates are **pixels in the original image**,
origin top-left, x right, y down. Floats allowed, never normalised.

```json
{
  "schema_version": 1,
  "media_type": "image",
  "classification": { "weather": "rain" },
  "shapes": [
    {
      "id": "b3f1c2a0-…",
      "type": "bbox",
      "class": "car",
      "attributes": { "occluded": true },
      "confidence": null,
      "bbox": [120.0, 84.5, 310.0, 240.0]
    },
    {
      "id": "…",
      "type": "polygon",
      "class": "road",
      "attributes": {},
      "points": [[10, 10], [200, 10], [200, 90]]
    },
    { "id": "…", "type": "point", "class": "defect", "point": [55.0, 12.0] },
    {
      "id": "…",
      "type": "rbox",
      "class": "ship",
      "attributes": {},
      "center": [180.0, 120.0],
      "size": [90.0, 30.0],
      "angle": 25.0
    }
  ]
}
```

- `bbox` is `[x_min, y_min, x_max, y_max]` — **not** `[x, y, w, h]`. The COCO
  exporter converts.
- COCO export of `keypoints`: the class's category gets `keypoints` (the
  skeleton's names) and `skeleton` (bones, **1-based** as COCO has them); the
  annotation has `keypoints` flat `[x, y, v, …]`, `num_keypoints`, and the
  labelled points' envelope as `bbox` / `area`. `yolo` skips them (pose
  labels are a separate layout); `yolo_pose` exports only them:
  - `labels/<stem>.txt`, one line per `keypoints` shape: `index cx cy w h`
    (the labelled points' envelope) then `x y v` per point, all normalised
    to `[0, 1]` and clamped like `yolo`; unlabelled points are `0 0 0`.
    `index` is 0-based over the schema's **keypoints classes only**, in
    schema order. Every line has `K` points, `K` the longest skeleton;
    shorter skeletons are padded with `0 0 0`. Other shapes are ignored.
  - `data.yaml`: `kpt_shape: [K, 3]`, `nc`, `names` (the keypoints classes)
    and, when every keypoints class has the same point names, `flip_idx`:
    each point maps to its mirror, found by swapping `left`/`right` in its
    name (case-insensitive), else to itself.
  - A schema with no keypoints class exports empty label files and a
    `warnings.json` entry; text and video items are skipped as in `yolo`.
- `rbox` is a rotated box: `center` `[cx, cy]`, `size` `[w, h]` (both > 0)
  and `angle` in degrees, clockwise-positive because y points down, in
  `(-180, 180]`. `w` runs along the rotated x axis. Corners are derived by
  rotating `(±w/2, ±h/2)` about the center; exporters emit them as a
  four-point polygon (COCO `segmentation`, YOLO OBB line) plus the
  axis-aligned envelope where a `bbox` is required.
- `polygon` / `polyline`: `points` is a flat list of `[x, y]` pairs, not closed
  (the first point is not repeated at the end).
- `keypoints`: `points` is one `[x, y, v]` per skeleton point of the shape's
  class, in `skeleton.points` order, with COCO visibility `v`: `0` not
  labelled (`x`, `y` are `0`), `1` labelled but occluded, `2` visible. At
  least one point is labelled. A count that does not match the class's
  skeleton is a schema violation (QA-6). Its envelope (region split,
  agreement, culling) is the bounds of the labelled points.
  ```json
  { "id": "…", "type": "keypoints", "class": "person", "attributes": {},
    "points": [[210, 80, 2], [202, 74, 2], [0, 0, 0], [180, 120, 1], [240, 118, 2]] }
  ```
- `confidence` is `null` for human annotations, `0.0–1.0` for model output
  (ML-3).
- Shape `id` is a client-generated UUID, stable across versions so a reviewer's
  diff can match shapes (WF-4).

Text items (`media_type: "text"`, TOOL). The item's object is UTF-8 text,
read by the browser through its signed `media_url`. Offsets are **Unicode
code points** (Python `str` indices, JS `Array.from(text)` indices — not
UTF-16 units), half-open `[start, end)`, no newline normalisation. Spans may
overlap and nest.

```json
{
  "schema_version": 1,
  "media_type": "text",
  "classification": { "sentiment": "neutral" },
  "shapes": [
    { "id": "s1…", "type": "span", "class": "PER", "attributes": {}, "start": 0, "end": 5, "text": "Alice" },
    { "id": "s2…", "type": "span", "class": "ORG", "attributes": {}, "start": 15, "end": 21, "text": "Contoso" },
    { "id": "r1…", "type": "relation", "class": "works_for", "attributes": {}, "from": "s1…", "to": "s2…" }
  ]
}
```

- `span`: `start` ≥ 0, `end` > `start`; `text` is optional and informational.
- `relation`: `from` / `to` are ids of other shapes in the same result, not
  relations, and differ. A dangling id is a schema violation (QA-6). Deleting
  a span in the UI deletes its relations.
- `span` / `relation` only on `text` and `pdf` items (pdf spans are
  anchored differently, see *PDF items*), geometric shapes never on text
  items;
  exporters that only understand images (COCO, YOLO) skip text and video
  items and count them in the job's warnings; `native` exports them as-is.
- Text export formats (EXP-5), for `text` and `pdf` items (other items are
  skipped into the warnings, the mirror of COCO / YOLO; pdf items as
  described under *PDF items*). The export job
  reads each text item's content from its source connector (the worker, not
  the API, touches media; items over 16 MiB are skipped with a warning).
  Both formats use the same **entity set**: the item's spans, greedily kept
  longest-first (ties: earlier start) so that none overlap; spans dropped
  for overlapping are counted in the warnings.
  - `spacy` → `annotations.jsonl`, one line per item:
    `{"text", "entities": [[start, end, label], …], "spans": {"sc": [[start,
    end, label], …]}, "relations": [[head, child, label], …],
    "classification": {…}, "meta": {"item_id", "path"}}` — offsets are
    code points, which are spaCy's character offsets (Python `str`);
    `entities` is the entity set (for `doc.ents`), `spans.sc` every span
    (for a `SpanGroup`, overlaps allowed), `relations` index into `spans.sc`.
  - `conll` → `annotations.conll`: per item a `# item_id = …` and
    `# path = …` comment, then one `token<TAB>tag` line per token and a blank
    line. Tokens are `\w+` runs and single non-space, non-word characters;
    tags are IOB2 (`B-PER`, `I-PER`, `O`) from the entity set; a token that
    an entity boundary cuts through takes the entity's tag if its first
    character is inside the entity.
- Text pre-labelling (ML-2 for `text`): `POST /predict` items carry
  `media_type` (`image` default, `text` or `pdf`); a text item's `url` is its
  signed media URL and `width` / `height` are 0. A text model answers
  `span` / `relation` shapes in code points. The reference `heuristic`
  backend offers a capitalisation-rule NER (`PER`, `ORG`, `LOC`).

Video items (`media_type: "video"`, CVAT `<track>` import, EXP-6). Shapes are
the image shapes plus: `frame` (int ≥ 0, **required** on video, forbidden on
other media), `track_id` (UUID, shared by the shapes of one object track;
null for a single-frame shape), `keyframe` (default `true`), `outside`
(default `false` — the object has left the view from this frame on). A track
is stored as its keyframes; frames between keyframes are interpolated by the
player, not stored. Interpolation is linear per `bbox` coordinate between
consecutive keyframes of a track; a keyframe with `outside: true` ends the
track until its next keyframe. The frame on screen is
`floor(currentTime × fps)` with `fps` from `item.meta.fps` (default 25). `item.meta.frame_count` and `item.meta.fps` are set when
known (import, probe).

Audio and time-series items (`media_type: "audio"` / `"timeseries"`, §5).
Their only shape is the `segment`, an interval on the item's time axis;
item-level labels are the schema's `classification`.

```json
{
  "schema_version": 1,
  "media_type": "audio",
  "classification": {"language": "fi"},
  "shapes": [
    {"id": "…", "type": "segment", "class": "speech", "attributes": {},
     "start": 1200, "end": 3400, "speaker": "Anna", "text": "Hyvää huomenta."},
    {"id": "…", "type": "segment", "class": "music", "attributes": {},
     "start": 3400, "end": 9000}
  ]
}
```

- `start` < `end`. Segments may overlap (two speakers at once).
- Audio: `start` / `end` are integer milliseconds from the beginning of the
  file. `speaker` (a free name, per item) and `text` (the transcript) are
  optional; `channels` is not allowed.
- Time series: the item is a UTF-8 CSV with a header row. The first column
  is the time axis, either numbers (any unit) or ISO 8601 timestamps (read
  as epoch milliseconds); every other column is a numeric channel, and an
  empty cell is a gap. `start` / `end` are positions on that axis (numbers,
  fractions allowed). `channels` lists the channel names the interval
  covers; absent or `null` means all of them. `text` is an optional note;
  `speaker` is not allowed.
- `segment` only on `audio` / `timeseries` items, and those take nothing
  else. COCO, YOLO, spaCy, CoNLL and `llm` skip them into the warnings.
- The `segments` export format (EXP-5) → `segments.jsonl`, one line per
  segment: `{"item_id", "path", "media_type", "class", "start", "end",
  "speaker", "text", "channels", "attributes"}`, items in path order and
  segments by `start`; plus `segments.rttm`, the NIST RTTM speaker
  diarization file for audio segments with a `speaker` (`SPEAKER <path> 1
  <start s> <duration s> <NA> <NA> <speaker> <NA> <NA>`, seconds with three
  decimals). Other media types are skipped with a warning.
- Agreement (QA-2) matches segments of the same class (and the same
  `channels`) by temporal IoU ≥ the IoU threshold, like boxes.

LLM evaluation items (`media_type: "llm"`, §5 *LLM-data*). The item's object
is a UTF-8 JSON document, read by the browser through its signed
`media_url`:

```json
{
  "messages": [
    {"role": "system", "content": "You are a support agent."},
    {"role": "user", "content": "How do I reset my password?"}
  ],
  "responses": [
    {"id": "a", "content": "Go to Settings → Security …", "model": "model-x"},
    {"id": "b", "content": "I cannot help with that."}
  ],
  "meta": {"source": "eval-2026-10"}
}
```

`messages` is the conversation so far (`role`: `system` | `user` |
`assistant` | `tool`), `responses` the candidate replies to compare, each with
an `id` unique in the document; either list may be empty but not both.
Comparing and ranking replies uses `responses`; reviewing a whole conversation
uses `messages` alone. The result's shapes are `ranking` and `rating` only,
and only on `llm` items; conversation-level judgements are the schema's
`classification`, and a rationale is a class attribute (a `text` attribute).

```json
{
  "schema_version": 1,
  "media_type": "llm",
  "classification": {"safe": true},
  "shapes": [
    {"id": "…", "type": "ranking", "class": "preference", "attributes": {"why": "b refuses"},
     "order": ["a", "b"]},
    {"id": "…", "type": "rating", "class": "helpfulness", "attributes": {},
     "target": "response:a", "value": 4},
    {"id": "…", "type": "rating", "class": "helpfulness", "attributes": {},
     "target": "message:3", "value": 2}
  ]
}
```

- `ranking`: `order` lists response ids best first, each once, at least one.
  Two responses make a pairwise preference. At most one ranking per class.
- `rating`: `value` is an integer within the class's `scale`; `target` is
  `response:<id>`, `message:<0-based index>` (one turn of the conversation)
  or `conversation` (all of it). At most one rating per (class, target).
- The platform does not read the document when validating (QA-6 checks the
  shapes against the schema only); that a target names an existing response
  or message is the annotator's job, and the `llm` export drops a ranking id
  or rating target that does not exist with a warning.
- COCO, YOLO, spaCy and CoNLL skip `llm` items into the warnings; `native`
  exports them as-is. The `llm` export format (EXP-5) → `annotations.jsonl`,
  one line per `llm` item: `{"item_id", "path", "messages", "responses",
  "classification", "rankings": {class: [ids]}, "ratings": [{class, target,
  value, attributes}], "preference_pairs": [{class, prompt, chosen,
  rejected}]}` where `prompt` is `messages` and every ranking yields one pair
  per ordered couple (`chosen` ranked above `rejected`, both the full
  response objects) — the DPO / reward-model layout. Items whose document
  cannot be read or parsed are skipped with a warning; so is every other
  media type.

PDF items (`media_type: "pdf"`, TOOL). The browser renders the document with
pdf.js from the item's signed URL (the storage must allow CORS, as for
superpixels); nothing is rasterised server-side. Shapes are the geometric
image shapes except `mask`, entity `span`s and `relation`s (below), plus
`page` (int ≥ 1, **required** on every pdf shape but a relation, forbidden
on other media). Coordinates are **PDF points** (1/72 in) of that
page at scale 1 with its `/Rotate` applied: origin top-left, x right,
y down, independent of the zoom the page was drawn at. `bbox` may carry
`text` (optional, informational): the words of the page's text layer whose
centres fall inside the box, joined by single spaces, as the annotator saw
them — scanned pages without a text layer leave it out.

```json
{ "schema_version": 1, "media_type": "pdf", "shapes": [
  { "id": "…", "type": "bbox", "class": "total", "attributes": {}, "page": 2,
    "bbox": [402.5, 611.0, 540.0, 628.5], "text": "42,00 €" } ] }
```

**Entity spans on PDFs (NER).** A pdf `span` is anchored to the page, not
to character offsets: the browser (pdf.js) and the worker (PDFium) split a
page's text differently, and a scan has no text until it is OCR'd. It
carries `page` and `boxes` — 1–256 `[x_min, y_min, x_max, y_max]` in the
page's points, one per line it covers, each non-empty — and **no**
`start` / `end` (a text span carries `start` / `end` and no `boxes`). The
span covers the words of the page whose centres fall inside one of its
boxes, the rule `bbox` `text` uses; `text` (optional, informational) is
those words joined by single spaces. The annotator builds the boxes from
the words a person selects (a contiguous run in reading order, one box per
line: consecutive words whose vertical centres lie within half the taller
word's height of each other), so two people selecting the same words store the
same boxes. A span lies on one page.

```json
{ "id": "s1…", "type": "span", "class": "PER", "attributes": {}, "page": 1,
  "boxes": [[412.0, 96.2, 470.4, 108.0], [72.0, 110.1, 118.6, 121.9]],
  "text": "Alice Smith" }
```

`relation` shapes work on pdf items as on text items, and their ends may be
any other non-relation shape of the result (a span or a geometric shape,
e.g. a key box to its value box); a relation carries no `page`, so it may
join shapes on different pages. Every other pdf shape needs `page`.

- COCO / YOLO skip pdf items with a `warnings.json` entry, as text and video;
  `native` exports them as stored.
- `spacy` / `conll` export pdf items as documents. The worker reads the
  PDF from the source connector (items over 64 MiB, undecodable or
  encrypted files, and documents over 2000 pages or 500 000 words are
  skipped into the warnings) and takes the words of every page from
  PDFium's text layer, split on whitespace, in PDFium's order, in the
  coordinates of the page's CropBox (its MediaBox when it has none), as
  pdf.js shows the page. The **document text** is the words joined by a single space, or
  by `\n` where the whitespace between them held a line break; pages are
  joined by `\n\n`. A pdf span becomes the code-point range from the first
  to the last word it covers (in document order); a span that covers no
  word (a scan without a text layer, or boxes over blank space) is skipped
  into the warnings, as is a range that also takes in words outside the
  span's boxes (counted, but still exported). The resulting ranges go
  through the text pipeline unchanged: entity set, `spans.sc`, relations
  between spans (relations with a non-span end are dropped from these
  formats), CoNLL tokens and IOB2 tags. Geometric shapes are not exported
  by these formats. `meta` gains `"media_type": "pdf"`. The text is never
  stored by the platform: it is rebuilt from the customer's file at each
  export (ARC-3).
- Agreement and consensus fusion match shapes only on the same `page`, as
  they match video shapes only on the same `frame`.
- The `thumbnail` job (run after every scan) handles pdf items too: page 1
  becomes the grid thumbnail (PDFium, `pypdfium2`), and `item.meta` gains
  `page_count`, `page_sizes` (`[[w, h], …]` in points, `/Rotate` applied, at
  most the first 2000 pages) and `text_layer` (any page has characters; a
  scan without OCR has none). The text itself is never stored: it stays in
  the customer's file (ARC-3). Undecodable or encrypted files are counted
  in the job's `errors`. Tiling stays image-only.
- Pre-labelling (ML-2) sends pdf items to models whose `/info` lists `pdf`,
  with `media_type: "pdf"`, the signed URL and `width` / `height` 0; the
  model answers pdf shapes (with `page`, in points), entity spans with
  `boxes` as above. The reference `heuristic` backend reads the text layer
  with PDFium and proposes `bbox` shapes with `text` for `bbox` schema
  classes named `amount`, `date` and `email` (rule-based, like its text
  NER), and pdf spans for `span` classes named `PER`, `ORG`, `LOC`: its
  text NER over each page's words joined as in the export, each entity's
  words grouped into one box per line. Pages without a text layer are read by
  its OCR engine first, when one is configured (`OCR_ENGINE`).
- OCR for scans: `POST /items/{id}/ocr` asks an `ocr` model for the words of
  one page; the annotator offers it on pages whose text layer is empty, and
  boxes then carry `text` exactly as on born-digital pages. The words are
  not stored — only the `text` of the boxes a person keeps, as for any PDF.

**PDF text mode** (`settings.pdf_mode: text`). A scan (source scan, upload
scan, storage event) that finds a new `.pdf` creates a **`text`** item:
`path` stays the PDF's path on the source connector (so rescans, uploads
and deletes match it as before), `meta.views` gets the PDF itself
(`{"path": <its path>, "label": "PDF"}`, a companion view), and
`meta.pdf_text` describes the extracted text:

```json
{ "status": "ready", "path": "text/<item id>.txt", "connector_id": "…",
  "sha256": "…", "pages": [[0, 1834], [1836, 3410]], "ocr_pages": [2],
  "empty_pages": [] }
```

`status` `pending` | `ready` | `failed` (`error`: why); `path` is
`text/{item_id}.txt` on `connector_id`, the project's **result** connector
when the text was written (it is read from there even if the project's
result connector changes later) — the text is labelled data, not cache, so
`rebuild_cache` never touches it. Only the platform writes `meta.pdf_text`:
`POST /projects/{id}/items` with it in `meta` is 422, and readers never take
the path from it; `pages[n]` is page n+1's code-point range
`[start, end)` in the text; `ocr_pages` were read by OCR, `empty_pages`
gave no text. No annotate task opens for the item until its text is
`ready`.

The `extract_text` job (queued after every scan while the project has such
items that are not `ready`, so a failed or interrupted extraction is
retried, and by `POST /projects/{id}/extract-text`; payload
`{"item_ids"?: [uuid], "force"?: bool}`) selects the project's text items
with `meta.pdf_text` whose status is not `ready`, and with `force` also
`ready` ones that have no annotation versions (others are counted in
`skipped_annotated`: re-extracting would move their offsets). Per item it
reads the PDF from the source (over 64 MiB → `failed`), takes the words
of every page as the spaCy / CoNLL export does (PDFium, CropBox space,
2000 pages / 500 000 words at most) and builds the **same document text**
(words joined by a space or `\n`, pages by `\n\n`), so offsets agree with
a layout-mode export of the same file. A page without a text layer is read
by the organisation's first `ocr` model (`{endpoint_url}/ocr`, §8, one page
per call, the item's signed `internal` URL), its words grouped into lines
by the span line rule (*Entity spans on PDFs*) and joined like text-layer
words; with no `ocr` model, when the model fails for that page, past 200
OCR'd pages per item, or after 3 failed pages in a row, the page is empty
and listed in `empty_pages` (a failing model is also reported in the job's
`errors`). The text is written UTF-8 (`text/plain;
charset=utf-8`) to `text/{item_id}.txt` on the result connector, `meta.
pdf_text` becomes `ready` with its `sha256`, and the item's annotate tasks
open (WF-2) when it has none. An unreadable, encrypted or over-limit file
sets `failed`. `result`: `{"selected", "extracted", "failed",
"skipped_annotated", "ocr_pages", "empty_pages", "tasks_opened",
"errors"}`.

Wherever a text item's content is read, an item with `meta.pdf_text` is
read from `meta.pdf_text.path` on the result connector instead of `path`:
`media_url` (`null` until `ready`), pre-labelling (items not `ready` are
counted in `skipped_media`), and the `spacy` / `conll` exports (items not
`ready` are skipped with the usual "source text could not be read"
warning). Everything else is an ordinary text item: spans in code points,
relations, agreement, the `spacy` / `conll` formats. A PDF that changes
after extraction (a rescan with a new `etag`) is not re-extracted, so
existing annotations keep their offsets; `extract_text` with `force`
re-extracts items without annotations. The UI marks where each page begins
in the text (from `pages`; the marker is not part of the text, so offsets
are unchanged), renders the PDF view page by page in the context panel and
turns it to the page of a clicked marker or a selected span, and
offers the `extract-text` endpoint to owners in the project settings.
`POST /projects/{id}/items` keeps the
media type it is given, so a `pdf` registered there in a text-mode project
stays a layout item.

## Quality control (QA-1 … QA-4)

Pure functions in `services/agreement.py` (metrics) and `services/fusion.py`
(fusion) operate on `AnnotationResult`s; `services/consensus.py` and
`services/gold.py` load versions and call them. No new runtime dependency:
IoU and kappas are plain Python.

**Matching shapes between two results** (used by IoU, F1, fusion). Only
shapes of the same `class` and comparable type match: `bbox` / `rbox` /
`polygon` by IoU of their axis-aligned envelopes (an approximation for
`rbox` / `polygon`, documented as such in the response), `mask` by pixel IoU
of the decoded RLE, `point` when within `point_tolerance_px` (10),
`keypoints` by object keypoint similarity (COCO OKS, symmetric: over points
labelled in either, `exp(-d² / (2 s² k²))` for a point labelled in both, 0
otherwise, `s²` the mean envelope area, at least 32² px², `k` = 0.1 for every point),
used in place of IoU; `polyline` never (ignored). Greedy: all candidate pairs with IoU ≥ `iou_threshold`
(0.5) in descending IoU, each shape used once. Spans match *exactly* (same
`class`, `start`, `end`) or *by overlap* (same `class`, overlapping ranges,
greedy by overlap length). PDF spans match exactly when `class`, `page`
and their boxes (each coordinate rounded to whole points, as a sorted set)
are equal, and by overlap when `class` and `page` are equal and their boxes
intersect, greedy by intersection area. Relations match when their class matches and
both ends matched. Video shapes additionally need the same `frame`.

**Metrics** (`ItemAgreement` for one item, `ProjectAgreement` pooled):

- Classification, per `classification` field with a scalar value
  (`select`, `boolean`, `text`, `number` compared for equality; `multiselect`
  as a sorted tuple): **Fleiss' κ** over items where every consensus
  annotator gave a value (the modal rater count; others are left out and
  counted), **Krippendorff's α** (nominal, missing values allowed), and
  pairwise **Cohen's κ** pooled over fields. κ / α are null when undefined
  (fewer than 2 units, or expected agreement 1).
- Shapes: pairwise **mean IoU** of matched pairs and **F1** at
  `iou_threshold` (2·matched / (|A| + |B|)); relations count as shapes.
- Spans: pairwise **span-F1** exact and overlap.

```json
{
  "items": 42,
  "annotators": [{"user_id": "…", "email": "…", "display_name": "…", "items": 40}],
  "classification": [{"field": "weather", "items": 40, "fleiss_kappa": 0.71, "krippendorff_alpha": 0.72}],
  "shapes": {"mean_iou": 0.83, "f1": 0.9, "iou_threshold": 0.5, "envelope_iou": true},
  "spans": {"f1_exact": 0.78, "f1_overlap": 0.91},
  "pairs": [{"a": "…", "b": "…", "items": 38, "cohen_kappa": 0.7, "mean_iou": 0.82, "shape_f1": 0.9, "span_f1_exact": 0.8, "span_f1_overlap": 0.9}]
}
```

`ItemAgreement` is the same without `items` and with per-pair `items`
omitted. Pooled values are micro-averages over pairs and items.

**Fusion** (`method: fuse`, QA-3). N = number of consensus versions;
`min_votes` (1…N) defaults to ⌈N/2⌉, so with even N a half-vote is kept:

- Classification: per field the most frequent value with ≥ `min_votes`;
  a tie or too few votes leaves the field unset and adds a `conflicts`
  entry `classification.<field>`.
- Geometric shapes: cluster by class across annotators (greedy, the match
  rule above, at most one shape per annotator per cluster); keep clusters
  with ≥ `min_votes` members. `bbox`: coordinate-wise mean, weighted by
  `confidence` when every member has one (weighted box fusion); `point`:
  mean; `keypoints`: per point, labelled when at least half the members
  label it, at the mean of their positions, visible when at least half of
  those say visible (medoid if none is left); `rbox` / `polygon` / `mask`:
  the member with the highest mean IoU to the others (medoid). Attributes: per key the majority value; ties →
  unset. Dropped clusters with ≥ 1 member add a `conflicts` entry.
- Spans: exact-match clusters (pdf spans: the exact rule above) with
  ≥ `min_votes`; relations: kept when
  ≥ `min_votes` annotators drew one of that class between shapes that fused
  into the same two fused shapes.
- Fused shapes get new UUIDs and `confidence: null`. Video shapes cluster
  per `frame`; `track_id` is carried when every member of a track's
  clusters shares a source track (otherwise null).

**Gold accuracy** (QA-4): a gold attempt is scored against the reference
with the same functions — `classification_accuracy` = matching fields /
reference fields, `shape_f1` and `mean_iou` as above, `span_f1` exact.

## Storage connector interface (SRC-1, ARC-5)

`backend/app/connectors/base.py`:

```python
class StorageConnector(Protocol):
    type: ClassVar[str]

    # Not `async def`: an async generator, iterated directly, never awaited.
    def list(
        self, prefix: str, glob: str | None = None
    ) -> AsyncIterator[ObjectInfo]: ...

    async def read(
        self, path: str, start: int | None = None, end: int | None = None
    ) -> bytes: ...

    async def write(self, path: str, data: bytes, content_type: str) -> None: ...

    async def delete(self, path: str) -> None: ...

    async def signed_url(
        self,
        path: str,
        expires_in: int = 900,
        write: bool = False,
        internal: bool = False,
    ) -> str: ...

    async def check(self) -> ConnectorCheck: ...
```

`ObjectInfo`: `path`, `size_bytes`, `etag`, `last_modified`, `content_type`.
The proxied connectors (`local`, `databricks_volume`) also implement
`SizedConnector.size(path) -> int`, so the signed proxy can answer `Range`
requests without reading the whole object.
`ConnectorCheck`: `ok: bool`, `messages: list[str]` — used by the UI's
"test connection" (SRC-7), including a CORS warning.

Default signed-URL lifetime is 900 s (15 min, AUTH-6). Read-only unless
`write=True`. By default the URL is addressed for a browser (the
connector's public endpoint, e.g. `public_account_url`); `internal=True`
addresses it for a service inside the deployment instead — the model
endpoint a `prelabel` job or `POST /items/{id}/interactive` hands it to
(§8) — so an emulator or Private Endpoint whose public name only a browser
can resolve still works. Where the two are the same, `internal` changes
nothing. The `local` connector's URL is relative to the API either way, so
a model service cannot fetch it: model-assisted labelling needs a real
object store. Connectors are registered in `connectors/registry.py` by their
`type` string and constructed from a `connector` row.

Implemented: `local`, `azure_blob`, `s3`, `gcs`, `http`.

A project's `source_connector_id` / `result_connector_id` /
`cache_connector_id` must name a connector of the caller's organisation (404
otherwise, never 403), and the result and cache connectors must accept writes
(422 for `http`), on create and PATCH.

**`http`** (read-only HTTP(S) file list; `httpx`). Files under one
`base_url` on a web server or CDN. `config`: `base_url` (required, `http` or
`https`, no query), `manifest` (path under `base_url`, default
`manifest.txt`: one path per line, `#` comments and blank lines ignored, or a
JSON array of strings), `paths` (an inline list that replaces the manifest),
`frontend_origin` (CORS check), `timeout_seconds` (default 30).
`identity_type`: `none` — the files must be readable as they are. Object
paths are relative to `base_url` and URL-decoded; an entry may be an
absolute URL only under `base_url`, and `..` / `.` segments are refused, so
the worker never fetches outside `base_url` (no SSRF through a manifest).
`list` applies prefix and glob, then `HEAD`s each file (8 in parallel) for
size, `ETag`, `Last-Modified` and type — a ranged `GET bytes=0-0` where
`HEAD` is refused — and skips files the server answers 404 for. `read`
sends a `Range` header and slices locally if the server ignores it.
`signed_url` is the file's own URL (no expiry); `write=True`, `write` and
`delete` raise `UnsupportedOperation`. `check()` reads the list, `HEAD`s
the first file, and warns on plain `http` and on CORS.

**`s3`** (AWS S3, MinIO and other S3-compatible stores; `aioboto3`).
`config`: `bucket` (required), `region`, `endpoint_url` (non-AWS or a
compose service name), `public_endpoint_url` (the host a browser uses when
it differs, like `public_account_url`), `force_path_style` (bool, MinIO),
`frontend_origin` (for the CORS check). `identity_type`: `access_key` —
the resolved secret is JSON `{"access_key_id", "secret_access_key"}`;
`iam_role` — the default AWS credential chain, optionally assuming
`config.role_arn`; `none` — anonymous (public buckets only, tests).
Signed URLs are SigV4 presigned `GET` / `PUT` (`write=True`), addressed to
`public_endpoint_url` unless `internal=True`. `check()` does `HeadBucket`
and warns when the bucket's CORS rules do not allow `frontend_origin`.

**`gcs`** (Google Cloud Storage; `google-cloud-storage`, calls run in a
thread). `config`: `bucket` (required), `project`, `endpoint_url` (an
emulator), `public_endpoint_url`, `frontend_origin`. `identity_type`:
`account_key` — the resolved secret is the service-account JSON key;
`managed_identity` — Application Default Credentials (signing through the
IAM `signBlob` API); `none` — anonymous, for a public bucket or an
emulator (docs/LOCAL_CLOUDS.md). Signed URLs are V4 (`GET` / `PUT`), addressed
to `public_endpoint_url` unless `internal=True`. With `none` there is no key
to sign with: `signed_url` is the plain object URL
(`{endpoint}/{bucket}/{path}`, default endpoint `https://storage.googleapis.com`)
and `write=True` raises `UnsupportedOperation`. `check()` reads the bucket's
metadata and warns on CORS like the others.

Both: `list` pages through the prefix and applies `glob` like the Azure
connector; `read` honours `start`/`end` as an HTTP range; a missing object
raises `ConnectorNotFound`; auth failures raise `ConnectorAuthError`.

**`sharepoint`** (SharePoint document libraries and OneDrive, Microsoft
Graph v1.0 over `httpx`; §3). `config`: `drive_id` (required: the
document library's or OneDrive's drive id), `tenant_id`, `client_id`,
`graph_url` (default `https://graph.microsoft.com/v1.0`, overridable for
national clouds and tests), `frontend_origin`. `identity_type`:
`service_principal` — the resolved secret is the app registration's client
secret (client-credentials flow, scope `https://graph.microsoft.com/.default`;
the app needs `Sites.Selected` granted on the site, or `Files.Read.All` /
`Files.ReadWrite.All`); `managed_identity` — the Azure identity of the pod
or VM (`azure-identity`), with the same Graph permissions. Paths are relative
to the drive root (`Shared Documents` is the root of a site's default
library). `list` walks folders under the prefix, recursively, with `$top=200`
paging (`@odata.nextLink`); files carry `size`, `eTag` (the ETag),
`lastModifiedDateTime` and `file.mimeType`. `read` is `GET
/drives/{id}/root:/{path}:/content`, honouring `start`/`end` as a Range.
`write` is a simple upload (`PUT …:/content`) up to 250 MB, an upload
session above. `signed_url` is the item's `@microsoft.graph.downloadUrl`: a
pre-authenticated URL Graph issues for about an hour (the TTL is Graph's,
not `APP_SIGNED_URL_TTL`), so the browser reads straight from SharePoint
(ARC-3). Upload URLs (`write=True`) raise `UnsupportedOperation`: Graph's
upload sessions need ranged chunks the browser upload does not send, so
uploads into SharePoint go through the platform's own writes (results,
exports). `check()` reads the drive (`GET /drives/{id}`) and reports its
name and type. A 404 from Graph is `ConnectorNotFound`; 401 / 403 are
`ConnectorAuthError`; 429 and 503 are retried (at most 3 times) after
`Retry-After`.

**`databricks_volume`** (Databricks Unity Catalog volumes, Files API 2.0
over `httpx`; §3). `config`: `host` (required, `https://<workspace>`),
`volume_path` (required, `/Volumes/<catalog>/<schema>/<volume>`), `client_id`
(for `service_principal`). `identity_type`: `access_key` — the resolved
secret is a personal access token; `service_principal` — the resolved
secret is a Databricks OAuth secret for `client_id` (machine-to-machine,
token from `{host}/oidc/v1/token`, scope `all-apis`). Paths are relative to
`volume_path`. `list` walks `GET /api/2.0/fs/directories{dir}` (paged by
`page_token`); `read` is `GET /api/2.0/fs/files{file}` with a Range for
`start`/`end` (sliced locally if the server ignores it); `size` is a `HEAD`
on the same URL; `write` is `PUT …?overwrite=true`; `delete` is `DELETE`.
Volumes have no presigned URLs, so `signed_url` points at the platform's
signed proxy `/api/v1/storage/proxy/{connector_id}/{path}` (the same HMAC
scheme and routes as `local`, read and write): the one other place, besides
local disk, where media passes through the API (an ARC-3 exception, sized by
`MAX_UPLOAD_BYTES` for uploads). A volume backed by an external location in
the customer's own cloud storage is better served by that storage's own
connector, which signs natively. `check()` lists the volume root.

**Local signed URLs.** Local disk has no native signing, so `LocalConnector`
returns `/api/v1/storage/local/{connector_id}/{path}?expires={unix}&sig={hmac}`
and the API serves those bytes itself — the exception to "the API never
proxies media", because there is no store to redirect to. `databricks_volume`
uses the same scheme under `/api/v1/storage/proxy/{connector_id}/{path}`,
which serves only those two connector types. `sig` is
`HMAC-SHA256(APP_SECRET_KEY, "v1:{connector_id}:{path}:{expires}")`,
url-safe-base64 without padding. The route takes no session: an `<img>` sends
no `Authorization` header, so the signature is the authorisation. It signs
reads and writes with separate signatures: a write URL signs
`"v1:w:{connector_id}:{path}:{expires}"` and is only accepted by the `PUT`
route, so a leaked read URL can never be used to overwrite an object. An
`internal` URL (sent to a model service) starts with `APP_INTERNAL_API_URL`
when that is set. `public_base_url` in the connector config makes the
browser URL absolute too; otherwise it is root-relative. Signing needs
`APP_SECRET_KEY`; without it the connector raises `ConnectorError` and
`media_url` comes back `null`.

**Secrets.** `connector.secret_ref` is a *reference* — the name of where the
credential lives — never the credential. Anything building a connector resolves
it through `await app.services.secrets.resolve_secret()` first. Passing
`secret_ref` straight to `build_connector()` authenticates with a key *name* and
fails, and because callers turn connector errors into "no preview available", it
fails silently.

A reference is a URI naming its store, so one installation can draw from
several at once:

| Reference | Store |
| --------- | ----- |
| `AZURE_STORAGE_KEY` | environment variable (bare form, still supported) |
| `env:AZURE_STORAGE_KEY` | the same, explicit |
| `file:/run/secrets/storage_key` | Docker / Kubernetes mounted secret |
| `azurekeyvault://acme-kv/storage-key?version=` | Azure Key Vault |
| `awssecrets://prod/storage-key?region=&key=` | AWS Secrets Manager |
| `gcpsecrets://acme-prod/storage-key?version=` | GCP Secret Manager |

`resolve_secret` is async (vault lookups are network calls), raises rather than
returning `None` for an unresolvable reference — a `None` would be read as "no
credential needed" and attempt anonymous access — and caches by TTL
(`APP_SECRET_CACHE_TTL`, default 300 s) so a vault is not called per request;
concurrent misses on one reference share a single lookup, and a failure is
never cached. Errors (`SecretResolutionError`, with `UnknownSecretSchemeError`
for an unregistered scheme; both `SecretError`) name the reference, never a
value, and never quote or chain the store's own exception. Cloud SDKs are
imported lazily. boto3 ships with the image (it comes with the S3 connector);
Key Vault needs the backend `keyvault` extra and GCP Secret Manager the
`gcp-secrets` extra (image: `--build-arg EXTRAS=keyvault`, compose:
`BACKEND_EXTRAS=keyvault`); without it such a reference fails to resolve with
a message naming the extra. Package layout: `services/secrets/` —
`errors`, `base` (`SecretRef`, `parse_ref`, `SecretBackend`), `backends`,
`registry` (`configure`, cache, `resolve_secret`); import from the package.

Live connector instances are pooled per process (`services/storage.py`,
`storage_for`): one per connector id, reused while the row's config and the
resolved secret are unchanged, rebuilt when either changes, at most 64 kept
(least recently used idle ones closed first), closed at shutdown. An
instance holds its SDK connection pool, credential token cache and, for Azure
with managed identity / service principal, the user delegation key.

### Event-driven discovery (SRC-3)

A scheduled or manual scan lists the whole source; storage events register
new objects as they land instead. The store's notification service posts to
`POST /api/v1/connectors/{id}/events?token=<token>` (or the token in the
`X-Event-Token` header). The token is minted by a superuser with
`POST /connectors/{id}/events/token` — `{token, path}`, shown once; only its
SHA-256 is stored (`connector.event_token_hash`), a new one replaces the old,
`DELETE` turns events off. `ConnectorRead.events_enabled` says whether a
token is set. The receiver takes no session: an unknown connector, events
off or a wrong token are all 404, so the route reveals nothing.

Accepted bodies (at most 1 MiB, 5,000 objects per delivery):

| Sender | Shape | Handshake |
| ------ | ----- | --------- |
| Azure Event Grid, Event Grid schema | array of `{eventType, subject, data}`; `Microsoft.Storage.BlobCreated` / `BlobDeleted`, path and container from `subject` (`/blobServices/default/containers/{c}/blobs/{path}`) | `Microsoft.EventGrid.SubscriptionValidationEvent` → `200 {"validationResponse": code}` |
| Azure Event Grid, CloudEvents 1.0 | one event or an array, `{specversion, type, subject, data}`, same types | `OPTIONS` with `WebHook-Request-Origin` → `200`, `WebHook-Allowed-Origin` echoed |
| S3 through SNS (HTTPS subscription) | `{Type: "Notification", Message: "<S3 event JSON>"}` | `Type: SubscriptionConfirmation` → the API `GET`s `SubscribeURL` (only `https://sns.<region>.amazonaws.com[.cn]/`), `200 {"confirmed": true}` |
| S3 / MinIO event records posted directly | `{Records: [{eventName: "ObjectCreated:*" \| "s3:ObjectCreated:*" \| "ObjectRemoved:*", s3: {bucket: {name}, object: {key}}}]}`; `key` is URL-decoded (`+` is a space) | `s3:TestEvent` is acknowledged, nothing queued |
| S3 through EventBridge (API destination) | `{source: "aws.s3", detail-type: "Object Created" \| "Object Deleted", detail: {bucket: {name}, object: {key}}}`; `key` used as sent | — |
| GCS through Pub/Sub push | `{message: {attributes: {eventType: "OBJECT_FINALIZE" \| "OBJECT_DELETE", bucketId, objectId}}}` | — |

Anything else is 422. An event for another container or bucket than the
connector's `config.container` / `config.bucket` is ignored (a connector
with neither, `local` or `http`, and an event naming none, are trusted to
the token), and so is a
delete: items are never removed by a store event, as a scan never removes
them. Each created path is routed to every project whose source connector
is this one and whose `source_prefix` / `source_glob` it matches, with a
supported extension; one `scan_source` job is queued per project with
payload `{"paths": [...], "trigger": "event"}` and no actor (no `job.create`
audit row). Answer `202 {"received", "matched", "ignored", "job_ids"}`. In
restricted licence mode (LIC-5) the route answers 503 so the sender retries
later, since a scan opens tasks.

A `scan_source` job with `paths` does not list the source. It looks each
path up (`list(prefix=path)`, exact match), upserts it exactly as a scan
would (SRC-4: an unchanged ETag is left alone), and counts a path the store
no longer has as skipped, with an entry in `errors`. The rest of the job —
result shape, thumbnail and tile chaining — is unchanged. Duplicate
deliveries are harmless: the second job finds the item unchanged.

## Blob layout (result container, §9)

```
annotations/{project_id}/{item_id}/v{version}.json
snapshots/{snapshot_id}/manifest.json
snapshots/{snapshot_id}/annotations.jsonl
exports/{job_id}/{format}.zip
imports/{upload_id}/{filename}
cache/tiles/{item_id}/{level}/{x}_{y}.jpg
cache/thumbnails/{item_id}.jpg
```

`cache/` is derived data and safe to delete and regenerate (SRC-6). It is
written to the project's effective cache connector (`cache_connector_id`,
else the result connector), so it can live in its own container with its own
lifecycle policy; `POST /projects/{id}/cache/rebuild` regenerates it.

Each `annotations/…/v{n}.json` is self-contained (DATA-3): item path, schema
version, annotator and reviewer IDs, and the result.

## REST API (§11)

Base path `/api/v1`. OpenAPI generated by FastAPI (API-1).

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/health` | liveness, no auth (OPS-2) |
| GET | `/ready` | readiness: DB + Redis reachable (OPS-2) |
| POST | `/auth/login` | local login → access token (AUTH-2); 403 `seat-limit` when the licence has no free seat (LIC-24). Body `{email, password, otp?}`: with MFA on, a correct password without `otp` is 401 `mfa-required`, a wrong `otp` 401 `mfa-invalid` (audited `auth.login_failed`, `reason: mfa`); `otp` is a current TOTP code or an unused recovery code, which is then spent. Answer `{access_token, token_type, expires_in, mfa_setup_required}` — see `APP_MFA_REQUIRED_FOR_ADMINS` |
| GET | `/auth/mfa` | the caller's MFA state `{enabled, pending, recovery_codes_left, available}` — `available` is false for SSO-only and service accounts |
| POST | `/auth/mfa/setup` | start enrolment: a new seed, kept pending until confirmed → `{secret, otpauth_uri}` (`otpauth://totp/<issuer>:<email>?secret=…&issuer=…`, issuer `Annotation`). 409 when MFA is already on or the account has no password |
| POST | `/auth/mfa/enable` | `{code}`: confirm the pending seed → `{recovery_codes}` (shown once); 422 `mfa-invalid` for a wrong code, 409 with nothing pending. Audited `auth.mfa_enable` |
| POST | `/auth/mfa/recovery-codes` | `{code}` (TOTP only): replace the recovery codes → `{recovery_codes}` |
| DELETE | `/auth/mfa` | turn MFA off: `{code}` (TOTP or recovery code) for one's own account; a superuser passes `?user_id=` for another account in the organisation and needs no code (lockout recovery). 204; audited `auth.mfa_disable`. None of the `/auth/mfa` routes accept an API key |
| GET | `/auth/providers` | `{local, oidc: {display_name, login_path} \| null}` — which sign-in methods exist; unauthenticated |
| GET | `/auth/oidc/login` | start single sign-on (AUTH-1): sets the `oidc_flow` cookie (state, nonce, PKCE verifier; signed, 10 min, path-scoped) and 303s to the provider; `?next=<path>` is where to land afterwards; `?prompt=none` asks the provider for a silent sign-in from its existing session (the frontend's re-authentication when an SSO token expires). 404 when `APP_OIDC_ISSUER` is unset |
| GET | `/auth/oidc/logout` | RP-initiated logout: 303s to the provider's `end_session_endpoint` with `client_id` and `post_logout_redirect_uri={APP_FRONTEND_URL}/login` (register that URI at the provider), or straight to `{APP_FRONTEND_URL}/login` when the provider has none. The frontend clears its own token first. 404 when `APP_OIDC_ISSUER` is unset |
| GET | `/auth/oidc/callback` | the provider's redirect target. On success 303s to `{APP_FRONTEND_URL}/auth/callback#access_token=…&expires_in=…&next=…` (fragment, so the token never reaches a server log); on any failure 303s to `{APP_FRONTEND_URL}/login?error=<code>` with one of `provider_denied`, `provider_unreachable`, `flow_expired`, `state_mismatch`, `token_exchange_failed`, `invalid_id_token`, `no_email`, `email_unverified`, `not_provisioned`, `no_organization`, `account_conflict`, `inactive`, `seat_limit`, `login_required` (a `prompt=none` attempt with no provider session: show the login page, do not retry silently) |
| GET | `/auth/me` | current user (`UserRead`, incl. `is_superuser` so the UI can show admin pages, `mfa_enabled`, `erased_at` and `email_notifications`) |
| PATCH | `/auth/me` | the caller's own preferences: `{email_notifications?}` → `UserRead` (API-7) |
| GET/POST | `/api-keys` | list the caller's keys (superuser: `?user_id=` any user in the organisation) / mint one (AUTH-4). Body `{name, scopes: ["read"], expires_at?, user_id?}`; `user_id` (superuser only) issues the key for another user or a service account in the same organisation. The response carries the `token` **once**; afterwards only metadata is readable. Not callable with an API key |
| DELETE | `/api-keys/{id}` | revoke — own key, or any key in the organisation for a superuser. 204; already revoked is idempotent |
| GET/POST | `/service-accounts` | superuser. List the organisation's service accounts / create one from `{display_name}` → `UserRead` (`is_service: true`) |
| GET/POST | `/webhooks` | list / subscribe (API-4). Organisation-wide hooks (`project_id` null): superuser. Project hooks (`project_id` set, `?project_id=` on list): that project's owners; a superuser listing a project sees both kinds. Body `{url, events: ["annotation.submitted", …] \| ["*"], project_id?, description?, is_active?}`; 422 for an unknown event. The 201 carries `secret` **once**; at rest it is AES-GCM sealed under `APP_SECRET_KEY` (a key change stops deliveries until the secret is rotated) |
| GET/PATCH/DELETE | `/webhooks/{id}` | same rights as creation. PATCH changes only the keys present; `rotate_secret: true` returns a new `secret` once (otherwise `secret` is null in the response) |
| POST | `/webhooks/{id}/test` | queue one `webhook.test` delivery for this hook (202 → the delivery row) |
| GET | `/webhooks/{id}/deliveries` | delivery log, newest first, cursor-paginated; `?status=pending\|succeeded\|failed` |
| POST | `/projects/{id}/retrain` | owner: emit `retrain.requested` to the project's subscribers (ML-9). Body `{snapshot_id?, model_id?, note?, ml_platform_id?}`; 404 for a snapshot outside the project. With `ml_platform_id` (a `databricks` platform with `config.job_id`) the Databricks job is started first (API-6, see *ml_platform*). 202 `{event, deliveries, ml_run}` — the platform trains nothing itself |
| DELETE | `/service-accounts/{id}` | superuser. Deactivates the account and revokes all its keys. 204 |
| GET | `/users` | superuser: the organisation's people (service accounts excluded — see `/service-accounts`), erased ones included, ordered by e-mail → `UserRead[]` (with `erased_at`). `?q=` filters on e-mail or display name (case-insensitive substring), `?limit=` 1–500, default 100. Not callable with an API key. Feeds the admin Users page (SEC-6 erasure) |
| GET/POST/DELETE | `/scim/token` | superuser, not callable with an API key: `{enabled}` / mint (replace) the organisation's SCIM token → `201 {token, path}`, token shown once, `path` = `/api/v1/scim/v2` / turn SCIM off → 204 (AUTH-3) |
| GET | `/scim/v2/ServiceProviderConfig`, `/ResourceTypes`, `/Schemas` | SCIM discovery (AUTH-3); SCIM bearer token |
| GET/POST | `/scim/v2/Users` | list (`filter`, `startIndex`, `count`) / create; SCIM bearer token. See *SCIM provisioning* |
| GET/PUT/PATCH/DELETE | `/scim/v2/Users/{id}` | read / replace / patch / delete (deactivate and hide) a user |
| GET/POST | `/scim/v2/Groups` | list / create |
| GET/PUT/PATCH/DELETE | `/scim/v2/Groups/{id}` | read / replace / patch (204, no body: a large group is not echoed back) / delete a group |
| GET | `/users/{id}/personal-data` | SEC-6 access: the person's own record, or any user of the organisation for a superuser → `PersonalDataExport` (see "### user"). Not callable with an API key; audited `user.export_personal_data`. 404 outside the organisation |
| POST | `/users/{id}/erase` | SEC-6 erasure, superuser, not callable with an API key. Body `{confirm_email, redact_comments?: false}`; `confirm_email` must equal the user's current e-mail (422 otherwise). 409 for one's own account or an already erased one. 200 → the pseudonymised `UserRead` |
| GET/POST | `/projects` | list / create — the creator is added as an `owner` member. The list holds only projects the caller is a member of; a superuser sees every project of the organisation. It never shows a project the caller could not open |
| GET/PATCH/DELETE | `/projects/{id}` | |
| GET/POST | `/projects/{id}/members` | list members (`{user_id, email, display_name, role, source, path_prefixes}`) / add one by `user_id` or `email` with a `role` and optional `path_prefixes` — owner only |
| PATCH/DELETE | `/projects/{id}/members/{user_id}` | change `role` and / or `path_prefixes` (`null` = whole project) / remove — owner only; the last owner cannot be demoted or removed (409) |
| GET/POST | `/projects/{id}/items` | list (filter: `status`, `media_type`, `assignee_id`, `q` path substring, `tag` exact `meta.tags` entry; cursor), each row carrying a signed `media_url` and, once generated, `thumbnail_url` for the grid / create |
| POST | `/projects/{id}/scan` | queue a source scan → job (SRC-2) |
| POST | `/projects/{id}/uploads` | owner only. Body `{files: [{path, content_type?, size_bytes?}]}` (1–500 entries, relative POSIX paths, no `..`). Answers `{prefix, uploads: [{path, url, method, headers}]}`: one write-scoped signed URL per file on the project's source connector, path prefixed with `source_prefix`; the browser `PUT`s the bytes there with the given headers, then calls `/scan` so the items appear. 409 when the connector cannot sign writes (§12 upload) |
| POST | `/projects/{id}/tiles` | owner / reviewer: queue a `tile_image` job → job (IMG-1). Body: `force` (re-tile), `item_ids` (subset) |
| POST | `/items/{id}/tiles/sign` | any project member: signed read URLs for tiles of the item's DZI pyramid (IMG-1). Body `{tiles: [[level, col, row], …]}` (1–512). → `{urls: [str, …] (same order), expires_in}` for `{meta.tiles.path}image_files/{level}/{col}_{row}.{suffix}` on the project's effective cache connector. 409 when the item has no `meta.tiles`; 422 for a level above `max_level` or a col / row outside that level's grid. An item with `meta.tiles` is shown tile by tile at the zoom level the viewport needs; the whole image is never loaded |
| POST | `/projects/{id}/thumbnails` | queue a thumbnail job → job (IMG-8). Body: `force` (regenerate existing), `item_ids` (subset) |
| POST | `/projects/{id}/cache/rebuild` | owner: queue a `rebuild_cache` job → job (SRC-6). Body: `purge` (delete the old derived blobs first, default false). 409 when the project has no effective cache connector |
| POST | `/projects/{id}/extract-text` | owner: queue an `extract_text` job → job (*PDF text mode*). Body `{item_ids?: [uuid] (1–1000), force?: bool}`: retries the project's PDF texts that are pending or failed; `force` also re-extracts `ready` ones without annotation versions. 409 when the project has no result connector |
| POST | `/projects/{id}/items/bulk` | owner / reviewer: one action on up to 500 items (WF-8), body `{action, item_ids, …}` discriminated on `action`. `assign` (`type` = `annotate` default \| `review`, `assignee_id?`, `priority?`, `deadline?` — only keys present change, `assignee_id: null` unassigns, a non-member assignee is 422; an item without a live task of that type gets one opened when its status allows, `in_progress` tasks are re-prioritised but never reassigned), `return` (put the items' `in_progress` tasks back to `open`, lock and assignee cleared), `approve` (approve each item's latest `submitted` version through the same verdict path as `POST /annotations/{id}/review`, optional `comment`), `reject` (the same path with `approve: false`; `comment` required, 1–10 000 characters, posted to every rejected item's thread), `tag` (`add` / `remove` lists → `item.meta.tags`, sorted unique, no whitespace or commas). Returns `{applied, skipped: [{item_id, reason}]}` — items the action does not fit are skipped, never a request-level error; one `item.bulk.<action>` audit row per request |
| GET | `/items/{id}` | item + signed media URL (`media_url`, null when the connector cannot sign) and `thumbnail_url` (null until generated) |
| POST | `/items/{id}/ocr` | OCR for scanned PDFs: any project member reads the words of one page of a pdf item through an `ocr` model of the organisation. Body `{model_id, page}` (1-based; 422 past `meta.page_count` when known); 404 for a model outside the caller's organisation, 409 unless `model.task = ocr` or for an item that is not a pdf. The API signs the item's URL for the model and calls `{endpoint_url}/ocr` (§8) with the model's credentials; the answer is `{page, width, height, engine, words: [{text, bbox: [x_min, y_min, x_max, y_max]}]}` in page points, clamped to the page. Nothing is stored. 503 (`model-unavailable`) when the model cannot be reached or answers nonsense; 120 s budget, as an external LLM may take that long on a dense page |
| GET | `/items/{id}/views` | any project member: the item's companion views (§5 multimodal) → `[{path, label, media_type, url}]`, signed like `media_url`; `media_type` from the extension (`null` when unknown); views outside the project's `source_prefix` are left out |
| POST | `/items/{id}/prelabels` | an external producer's pre-label (API-8). Body `{model_version_id, result, label_schema_version_id?}`; `result` is an *Annotation result JSON* in the project schema's classes (no class mapping). Owner or annotator of the project; the version must belong to a model of the organisation. Writes a `draft`, `source: model` version authored by that model version and moves a `new` item to `prelabeled`. 409 when the item already has a human version (ML-10) or is `submitted` or later → `AnnotationRead`, 201 |
| POST | `/items/{id}/interactive` | interactive segmentation (ML-7): any project member turns one click or one box into a polygon through a `segment` model of the organisation. Body `{model_id, point?: {x, y}, box?: [x_min, y_min, x_max, y_max]}` — exactly one prompt, in original-image pixels (422 otherwise); 404 for a model outside the caller's organisation, 409 unless `model.task = segment`. The API signs the item's media URL and calls `{endpoint_url}/interactive` with the model's credentials; the answer is `{type: "polygon", points: [[x, y], …≥3], confidence}` with points clamped to the image. Nothing is stored — the browser adds the polygon as an ordinary shape. 503 (`model-unavailable`) when the model cannot be reached or answers nonsense; 8 s budget, because a person is waiting on the click |
| GET | `/storage/local/{connector_id}/{path}` | media proxy for the `local` connector; unauthenticated, authorised by `?expires=&sig=` (see below). A single `Range: bytes=…` gets 206 + `Content-Range`, at most 8 MiB per answer (`bytes=0-` is capped; the browser asks again), 416 past the end; other `Range` forms are ignored. Always `Accept-Ranges: bytes`. 403 on a bad or stale signature, 404 on an unknown connector or object |
| PUT | `/storage/local/{connector_id}/{path}` | write half of the proxy: the body is stored as the object. Authorised by a *write-scoped* `?expires=&sig=` (a read signature is rejected). 204 on success, 413 over 256 MiB |
| GET/POST | `/projects/{id}/tasks` | list / assign; POST takes `priority`, `deadline` and is 409 if the item already has a live task of that type, 404 if the item is not in this project, 422 if `assignee_id` is not a member of the project |
| PATCH | `/tasks/{id}` | owner / reviewer: change `priority`, `deadline`, `assignee_id` of a live task (WF-6); 409 on `done` / `cancelled` or when reassigning an `in_progress` task; 422 when the new assignee is not a member of the project |
| POST | `/tasks/next` | claim next open task, takes the lock; returns the caller's own `in_progress` task first if one exists; `?type=annotate\|review` filters by task type (WF-2, WF-3) |
| POST | `/tasks/{id}/release` | release the lock |
| POST | `/tasks/{id}/extend` | heartbeat: push the lock's expiry forward (WF-3) |
| GET | `/items/{id}/annotations` | version history, newest first — a plain array, not paginated (bounded per item) |
| POST | `/items/{id}/annotations` | create a new version |
| POST | `/annotations/{id}/submit` | submit for review |
| POST | `/annotations/{id}/review` | approve / reject, with a comment |
| POST | `/items/{id}/split` | owner / reviewer: split an image item into region tasks (IMG-6). Body exactly one of `{grid: {rows, cols, overlap_px?}}` (1–16 each, `overlap_px` ≥ 0 default 0; cells extend by `overlap_px` on inner edges, clipped to the image) or `{regions: [[x_min, y_min, x_max, y_max], …]}` (1–256, clipped; an empty box after clipping is 422). 409 when: the item is not an image or has no `width` / `height`; its status is not `new` / `prelabeled` / `annotating` / `rejected`; it has an `in_progress` annotate task, live region tasks already, or live consensus tasks; the project has `consensus_annotators` > 1. The item's live ordinary annotate task is cancelled and one region task per region opens with its priority / deadline. → `{tasks: [TaskRead]}`; audited `item.split` |
| GET | `/items/{id}/consensus` | owner / reviewer (QA-1, QA-2, QA-3): `{expected, annotators: [{user_id, email, display_name, annotation_id, version, status, created_at}], agreement: ItemAgreement, preview: AnnotationResult, conflicts: [str]}` — the latest `consensus` version per author (drafts excluded), this item's agreement (see *Quality control*), and what `resolve` with `method: fuse` and default parameters would store. 404 outside the project, 409 when the item has no submitted consensus version |
| POST | `/items/{id}/consensus/resolve` | owner / reviewer (QA-3): turn the consensus versions into the item's annotation and approve it. Body `{method: "pick", annotation_id}` (copy one annotator's submitted consensus version) or `{method: "fuse", iou_threshold?: 0.5, min_votes?, comment?}`. Stores a `primary` version authored by the caller, `approved`, `task_id` = the live review task; closes that task and moves the item to `approved` through the same verdict path as `POST /annotations/{id}/review` (outbox publish, `annotation.approved` webhook, audit `annotation.review`). 409 unless the item is `submitted` / `in_review` with ≥ 1 submitted consensus version; 403 when the project forbids self-review and the caller authored one of the consensus versions |
| PUT/DELETE | `/items/{id}/gold` | owner / reviewer (QA-4). PUT `{annotation_id}` — an `approved` `primary` version of this item — sets `item.meta.gold_annotation_id`; 422 otherwise. DELETE clears it and cancels the item's live gold tasks. Both → `ItemRead`; audited `item.gold_set` / `item.gold_clear` |
| POST | `/projects/{id}/gold/tasks` | owner / reviewer (QA-4): open gold tasks. Body `{user_ids?, item_ids?, priority?}` — default: every member with role `annotator` × every item with a gold reference. One task per (item, user) that has no gold task of any status yet; users outside the project are 422. → `{opened, skipped}` |
| GET | `/projects/{id}/agreement` | owner / reviewer (QA-2): project-wide inter-annotator agreement over items with ≥ 2 submitted consensus versions (latest per author). `?since=` limits to items whose versions were created after it. → `ProjectAgreement` (see *Quality control*) |
| GET | `/projects/{id}/quality/annotators` | owner / reviewer (QA-4): per-annotator accuracy against gold references. → `{annotators: [{user_id, email, display_name, gold_items, classification_accuracy, shape_f1, mean_iou, span_f1, score}]}` — latest submitted `gold` version per (user, item) against the item's gold reference; ratios null without data; `score` is the mean of the non-null ratios; sorted by `score` ascending, nulls last |
| GET/POST | `/projects/{id}/schemas` | label schema versions |
| GET/POST | `/items/{id}/comments` | thread on an item, oldest first — a plain array; create with `{body, annotation_id?, parent_id?, anchor?}` (WF-5) |
| POST | `/comments/{id}/resolve` | `{"resolved": true\|false}` — author, a reviewer or an owner |
| GET | `/notifications` | the caller's, newest first; `?unread=true` |
| POST | `/notifications/{id}/read`, `/notifications/read-all` | |
| GET | `/audit` | superuser, organisation-scoped, newest first; filters `action` (prefix), `target_type`, `target_id`, `actor_id`, `since` (SEC-3); the last 30 days only without `audit_history` (LIC-33) |
| GET | `/license` | superuser: the install's licence status (LIC-1, see *Licence key*); never the key |
| PUT | `/license` | superuser: install a pasted licence key `{key}` (LIC-26); 422 `invalid-license-key` unless valid |
| DELETE | `/license` | superuser: forget the pasted key; `APP_LICENSE_KEY` is untouched |
| GET/POST | `/projects/{id}/snapshots` | list / queue a snapshot → job (EXP-1); body `{name, filter?, label_schema_version_id?, split?}` — `split` partitions the frozen set into train / val / test (EXP-3) |
| GET | `/projects/{id}/stats` | dashboard numbers (UX-5): `items` (total, by status), `tasks` (per type: open / in_progress / done / cancelled), `annotations` (versions, by source, latest version per item by status), `review` (approved, rejected, `rejection_rate` 0–1), `throughput` (one row per day for the last `?days=14` (1–90) days: versions created with status submitted / approved / rejected), `classes` (label balance over the latest non-draft version per item: shape `class` and `"{key}: {value}"` classifications, descending), `annotators` (per author of a latest version: submitted / approved / rejected). Any project member |
| GET | `/projects/{id}/snapshots/{sid}` | one snapshot |
| GET | `/projects/{id}/snapshots/{sid}/lineage` | what was trained on this snapshot (EXP-8): `{snapshot: {id, name, digest, item_count, created_at}, versions: [{id, model_id, model_name, version, snapshot_digest, training_run, created_at, items_predicted}]}` — versions linked by id or by the same digest, oldest first; `items_predicted` counts the distinct items of this project the version wrote a draft for |
| GET | `/projects/{id}/snapshots/{base}/diff/{target}` | compare two snapshots of the project (EXP-4), read from their manifest + JSONL on the result connector (409 when it has none). `{base, target: {id, name, item_count, digest, created_at, split_counts?}, items: {added, removed, changed, unchanged, split_moved}, added / removed: [{item_id, path, version, split?}], changed: [{item_id, path, from_version, to_version, shapes: {added, removed, changed}}], classes: [{name, base, target, delta}] (shape count per class, sorted by \|delta\|), truncated}` — lists are capped at 500 entries each, counts are exact. Items match by `item_id`; same `annotation_id` on both sides is unchanged |
| POST | `/projects/{id}/exports` | queue an export → job (EXP-5); body `{format, snapshot_id?, filter?, label_schema_version_id?, split?}` — `format`: `coco` \| `yolo` \| `yolo_pose` \| `native` \| `spacy` \| `conll` \| `llm` \| `segments`; `split` (`train` \| `val` \| `test`, needs `snapshot_id`) exports one partition of a split snapshot (EXP-3) |
| POST | `/projects/{id}/imports` | queue an import → job (EXP-6). Body: `format`, `path`, optional `connector_id` (default: the project's source connector), `class_mapping`, `status` (`submitted` \| `draft`), `dry_run`, `label_schema_version_id`. 422 on an unknown format |
| POST | `/projects/{id}/imports/upload` | multipart (`file` + the same fields as form values; `class_mapping` and `attribute_mapping` as JSON strings): stores the file at `imports/{upload_id}/{filename}` on the project's result connector, then queues the same job against it. 413 over 64 MiB (EXP-6) |
| GET | `/projects/{id}/jobs` | list jobs, filter by `status` / `type` |
| GET | `/jobs/{id}` | status and progress |
| POST | `/jobs/{id}/cancel` | cancel a queued or running job (ARC-4) |
| POST | `/jobs/{id}/retry` | re-queue a failed or cancelled job (ARC-4) |
| GET | `/jobs/{id}/download` | `{"url", "expires_in"}` — signed URL for a succeeded export's archive; 409 unless the job is a succeeded `export` (EXP-5) |
| GET/POST | `/connectors` | |
| POST | `/connectors/{id}/check` | test connection (SRC-7) |
| POST/DELETE | `/connectors/{id}/events/token` | superuser: mint (replace) the storage-event token → `201 {token, path}`, shown once / turn events off → 204 (SRC-3) |
| POST/OPTIONS | `/connectors/{id}/events` | storage-event receiver, token in `?token=` or `X-Event-Token`, no session; see *Event-driven discovery* (SRC-3) |
| GET/POST | `/ml-platforms` | list (any member of the organisation) / register (superuser) an MLflow, Databricks or Azure ML platform (API-6). Body `{name, kind, tracking_uri, identity_type, secret_ref?, config}`; 422 for an identity the kind does not take or missing `config` fields; 409 for a duplicate name. Read: `{id, organization_id, name, kind, tracking_uri, identity_type, has_secret, config, created_at, updated_at}` |
| GET/PATCH/DELETE | `/ml-platforms/{id}` | superuser for PATCH / DELETE |
| POST | `/ml-platforms/{id}/check` | superuser: one experiment search with the platform's credentials → `{ok, messages, info: {tracking_uri, experiments}}`; failures are `ok: false`, never an error status |
| POST | `/projects/{id}/snapshots/{sid}/mlflow` | owner: publish the snapshot to an ML platform (API-6). Body `{ml_platform_id, experiment?}` → 201 `{ml_platform_id, experiment_id, experiment_name, run_id, run_url, created}` (`created: false` and 200 when the snapshot already had a run there); 503 when the platform fails |
| GET/POST | `/models` | list (any member of the organisation) / register (superuser) (ML-1) |
| GET/PATCH/DELETE | `/models/{id}` | DELETE is soft by default, see *Deleting a model* |
| POST | `/models/{id}/check` | `GET {endpoint_url}/info` with the model's credentials → `{"ok", "messages", "info"}` (BYOM-3) |
| GET/POST | `/models/{id}/versions` | list / add a version with `class_mapping` (BYOM-2). Body also takes `snapshot_id?`, `snapshot_digest?`, `training_run?`, `parent_version_id?`, `derivation?` (EXP-8): 404 for a snapshot or parent version outside the caller's organisation, 409 when the digest does not match the snapshot, 422 for `distilled` / `quantized` without a parent |
| GET | `/models/{id}/family` | the derivation graph around a model (EXP-8): every version connected to one of its versions through `parent_version_id`, in any model of the organisation, oldest first → `{versions: [ModelVersionRead + model_name, model_task]}`. Any member of the organisation; 404 for a model outside it |
| POST | `/models/{id}/versions/import` | superuser: add a version from an MLflow run (API-6, EXP-8). Body `{ml_platform_id, run_id?, registered_model?, model_version?, version?, class_mapping?}` — `run_id`, or `registered_model` + `model_version`; 404 when the platform has no such run / version, 503 when it fails, 409 as for `POST …/versions` |
| GET | `/models/{id}/versions/{vid}` | |
| GET | `/models/{id}/versions/{vid}/metrics` | correction metrics (ML-5), `?project_id=` to scope to one project (404 outside the caller's organisation). Every item the version wrote a draft for is compared with the item's final human version (latest `submitted` / `approved` by a person, later than the draft), shape by shape on stable shape ids: `kept` (same class and geometry), `adjusted` (geometry moved), `relabeled` (class changed), `deleted` (only in the draft), `added` (only in the final). `{model_version_id, project_id, items_predicted, items_corrected, items_pending, items_accepted_unchanged, shapes: {model, kept, adjusted, relabeled, deleted, added}, precision = (kept + adjusted) / model, recall = (kept + adjusted) / final shapes, mean_iou_adjusted (bbox only), classes: [{name, …same fields…}]}`; ratios are null when the denominator is 0. Confidence and attributes never count as a change |
| POST | `/projects/{id}/prelabel` | queue a pre-labelling job → job (ML-2). Body: `model_version_id`, optional `label_schema_version_id`, `filter` (`item_status` default `[new, prelabeled]`, `path_prefix`), `limit` (dry run, BYOM-7), `confidence_threshold` (default 0), `prioritize_uncertain` (default false, ML-6: set annotate-task priority from prediction uncertainty) |

Conventions (API-2):

- The OpenAPI document is served at `/api/v1/openapi.json` and committed as
  `docs/openapi.json` (`make openapi`; `backend/tests/test_openapi_dump.py`
  fails when it drifts). The Python SDK (`sdk/`, API-3) generates its
  TypedDict models from that file, so a router change regenerates both.
- MCP server (API-8): `annotide mcp` (SDK extra `mcp`, `pip install
  "annotide[mcp]"`) serves the Model Context Protocol over stdio and
  calls this REST API with an API key (`ANNOTIDE_URL`,
  `ANNOTIDE_API_KEY`; a service account's key, so the agent's rights are
  that account's memberships). `ANNOTIDE_MODEL_VERSION_ID` is the default
  version `create_prelabel` writes under. Tools: `list_projects`,
  `get_label_schema`, `list_items`, `get_item` (metadata + signed media URL),
  `view_item` (the media itself: image as an MCP image, text as text, ≤ 5 MB),
  `get_annotations`, `claim_task`, `release_task`, `create_prelabel`. Nothing
  else: an agent cannot submit, review, delete or export.
- Cursor pagination: `?limit=50&cursor=<opaque>`, response
  `{"items": [...], "next_cursor": "…" | null}`.
- Errors are RFC 9457 problem details: `{"type", "title", "status", "detail"}`.
- Creation endpoints accept an `Idempotency-Key` header (see
  `### idempotency_key`): a replay answers the original row with 200.
- Auth is `Authorization: Bearer <token>`: a login token (JWT) or an API key
  (`ant_…`, or a legacy JWT with a `kid` claim; AUTH-4, see `### api_key`).
  An API key costs one database lookup per request; a login token costs none.
- A token issued by single sign-on carries `sso: true`; the frontend uses it
  to sign out at the provider too and to re-authenticate silently
  (`prompt=none`) instead of showing the login form when the token expires.
- Rate limits (API-5): every authenticated request counts against its
  caller — per user for a login token (`APP_RATE_LIMIT_PER_MINUTE`), per key
  for an API key (`APP_RATE_LIMIT_API_KEY_PER_MINUTE`) — and every
  `POST /auth/login` against the (client IP, sha256 of the e-mail) pair
  (`APP_RATE_LIMIT_LOGIN_PER_MINUTE`), before the password is checked.
  Fixed one-minute windows in Redis (`ratelimit:{scope}:{id}:{window}`, no
  raw e-mail). Every counted authenticated response carries
  `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset` (seconds to
  the next window; the tightest window when several apply). Over the limit:
  `429` problem `rate-limited` with `Retry-After` (seconds to the next
  window) and the same headers, `RateLimit-Remaining: 0`. If Redis is unreachable the
  request is let through and a warning logged — the limiter must never be
  the outage. `APP_RATE_LIMIT_ENABLED=false` turns it off. Unauthenticated
  routes other than login (health, local signed storage URLs) are not
  counted.

## Licence key (LIC-1)

An install without a key runs in **Community** mode (LIC-32). A key is one line,
verified offline — no call home:

```
ANN1.<base64url(payload JSON)>.<base64url(Ed25519 signature)>
```

The signature covers the ASCII bytes `ANN1.<payload part>` (the literal
prefix and the payload segment exactly as transmitted). Payload fields:
`v` (1), `kid` (vendor signing-key id), `lic` (licence UUID), `tier`
(`team` | `business` | `enterprise` | `trial`), `licensee` (customer name),
`seats` (int ≥ 1), `issued_at`, `expires_at` (ISO-8601 dates, UTC),
`features` (list of Business feature ids, may be empty; ids the install
doesn't know are ignored), and optionally `hosts` (list of hostnames, LIC-29).
Unknown fields are ignored (forward compatible); a `v` other than 1 is
invalid.

Host binding (LIC-29, `services/licensing/hosts.py`): a key whose `hosts` is
absent or empty is unbound. Otherwise each entry is a lowercase hostname
without a port (`annotate.acme.com`), or `*.` plus one (`*.acme.com`, which
matches any subdomain of `acme.com` but not `acme.com` itself); anything
else makes the key `invalid`. The *request host* is the `Host` header, or
`X-Forwarded-Host` when the direct peer is in `APP_TRUSTED_PROXIES`,
lowercased, without port or trailing dot. Loopback (`localhost`,
`*.localhost`, `127.0.0.0/8`, `::1`) and a request without a host always
match. A bound key whose `hosts` do not match the request host takes the
expired path: status `expired`, and its grace period runs from
`license_state.host_mismatch_since` rather than from `expires_at`
(`grace_ends_at` = the earlier of the two + 30 days). Sign-in on a
non-matching host sets `host_mismatch_since` to the (clock-guarded) date if
it is unset; sign-in on a matching, non-loopback host clears it. A read on
a non-matching host before any such sign-in counts from today and writes
nothing. Among several keys, a matching one is `valid` and so wins over a
mismatched one.

Verification uses the vendor public keys compiled into
`services/licensing/keys.py` (`kid` → raw 32-byte key); they are never read
from configuration, which would let an install trust its own signer. The
private key never enters the repo; `python -m app.services.licensing.issue`
generates a key pair and signs payloads for the vendor. `keys.py` carries the
production key (`vb4929e71`), so a stack built from the repo runs as
Community. For development and CI, `make dev-licence` writes `.dev/`
(gitignored): a `keys.py` trusting a throwaway key, and a compose override
that mounts it over the backend's and worker's copy and sets a Business key
signed with it (`services/licensing/devlicence.py`). `make dev`, `make seed`
and `make reset` use the override when it exists. Tests run as an unkeyed
build (`tests/conftest.py::_unkeyed_build`) and add their own keys.

Status (`services/licensing/license.py::license_status`): `community` (no
key), `valid`, `expired` (`expires_at` passed — grace and restricted mode
are LIC-5), `invalid` (malformed, bad signature, unknown `kid`). The key is
read from `APP_LICENSE_KEY` and from `license_state.key`
(`services/licensing/state.py`, LIC-26): the valid key that expires last is
in force, else the expired one that expired last, else `invalid` if any key
was set, else `community`. One licence is in force; seats never add up. A
`trial` key that has expired is left out of that choice altogether (LIC-34):
it never becomes the expired key in force, so an install whose only key is
an expired trial is `community`.

Revocation (LIC-8, `services/licensing/revocation.py`): a licence can be
revoked (refund, chargeback, leaked key) by a signed revocation list, one
line in the key format with prefix `ANNR1`: payload `{v: 1, kid, issued_at,
revoked: [{lic, at}]}` (ISO dates), signed like a key by a vendor key in
`keys.py`. Lists come from `keys.py::REVOKED_LICENSES` (`lic` → date,
compiled in) and from the licence refresh (stored in
`license_state.revocations`; a list replaces the stored one only if its
`issued_at` is later). A revoked key takes the expired path from its `at`
date — `at` is its last valid day, like `expires_at` — so grace runs to `at`
+ 30 days; the earliest of `expires_at`, `at` and a host mismatch wins. An
install that never hears of a revocation is unaffected (LIC-28).

Expiry is evaluated against the later of today and
`license_state.clock_high_water` (LIC-25), which sign-in advances and never
moves back; read-only endpoints do not write it.

Restricted mode (LIC-5): an `expired` licence keeps working for a 30-day
grace period (`grace_ends_at` = `expires_at` + 30 days, inclusive); after it,
the routes that do annotation work or make new tasks answer 403
`license-restricted` — `POST /items/{id}/annotations`,
`/annotations/{id}/submit`, `/annotations/{id}/review`,
`/projects/{id}/tasks`, `/tasks/next`, `/items/{id}/interactive`, `/items/{id}/ocr`,
`/projects/{id}/items/bulk`, `/projects/{id}/imports` (+ `/upload`),
`/projects/{id}/scan`, `/projects/{id}/prelabel`. Everything else — reading,
exports, snapshots, comments, settings, the licence itself — keeps working.
Authentication is checked first. Community and `invalid` are never restricted.

Business features (LIC-32, LIC-33, `services/licensing/features.py`): the
ids are `sso`, `scim`, `path_permissions`, `audit_history`, `seat_report`,
`sharepoint`, `ml_platforms` and `quality`. A feature is licensed while the
key in force lists it and its status is `valid`, or `expired` and not yet
`restricted`. An unlicensed feature answers 403 `license-feature` (detail
names the feature and the Business edition), after authentication:

| Feature | Refused while unlicensed | Unaffected |
| ------- | ------------------------ | ---------- |
| `sso` | `GET /auth/oidc/login`, `/auth/oidc/callback` (redirect `?error=license_feature`); group/role sync | local `POST /auth/login`; `GET /auth/providers` lists no OIDC provider |
| `scim` | `POST /scim/token`, `/scim/v2/*` (SCIM error body, status 403, checked after the token) | users and groups SCIM made; `GET` and `DELETE /scim/token` |
| `path_permissions` | `POST /projects/{id}/members` or `PATCH …/members/{user_id}` with non-empty or changed `path_prefixes` | enforcement of stored prefixes; other member changes; `DELETE` |
| `audit_history` | nothing is refused: `GET /audit` returns only events of the last 30 days (a `since` further back is moved up); clients read `features` from `GET /license` | recording |
| `seat_report` | `GET /license/seat-report` | the counts in `GET /license` |
| `sharepoint` | `POST /connectors` with type `sharepoint`, `PATCH /connectors/{id}` changing the type to it | existing SharePoint connectors |
| `ml_platforms` | `POST /ml-platforms`, `PATCH /ml-platforms/{id}`, `POST /ml-platforms/{id}/check`, `POST /projects/{id}/snapshots/{sid}/mlflow`, `POST /models/{id}/versions/import` | `GET` and `DELETE` of registered platforms, imported versions |
| `quality` | `GET /projects/{id}/agreement`, `GET /projects/{id}/quality/annotators`, `GET /items/{id}/consensus`, `POST /items/{id}/consensus/resolve`, `PUT/DELETE /items/{id}/gold`, `POST /projects/{id}/gold/tasks`; a project workflow change that turns consensus or gold on | annotation versions already written |

A build whose `VENDOR_PUBLIC_KEYS` is empty enforces no feature either, as
for seats.

Trial (LIC-34): `POST /license/trial` (superuser) asks the licence service
(`POST {APP_LICENSE_SERVER_URL}/v1/trial` `{install_id, host}`, `host` being
`APP_PUBLIC_HOSTNAME`, else the request host unless it is loopback, else
`null`) for a trial key and stores it in `license_state` (source `trial`).
Answers the `GET /license` body; 409 `trial-unavailable` when any key is in
force (a running trial included), when the stored key is already a trial
key, when this install or host already had a trial (the licence service's
409), or when licence calls are disabled (`APP_LICENSE_REFRESH_ENABLED=false`,
no `APP_LICENSE_SERVER_URL` or no install id); 503 `trial-service` when the
licence service can't be reached or answers anything else. A returned key is stored
only if it verifies, is `valid` for the request host and has tier `trial`.
Audited as `license.trial`.

`GET /license` (superuser) returns `{status, tier, licensee, seats,
expires_at, features, license_id, source, active_users, seat_limit,
grace_ends_at, restricted, hosts, host_mismatch, revoked_at}` —
`revoked_at` is the licence's revocation date or `null`; `hosts` is the key's
binding (`[]` when unbound or without a key), `host_mismatch` whether the
request host is outside it;
`source` is `env`, `admin`, `refresh`, `trial` or `null`; `tier` is the
edition (`community` | `team` | `business` | `enterprise` | `trial`),
`community` with nulls for the key's fields without a key in force; never a
key itself. `features` is the licensed Business feature ids (LIC-33), not
the key's raw list; `business_features` every id, for showing locked ones;
`owner_only` whether LIC-36 is in effect; `trial_used` whether the stored
key is a trial key, current or expired. `PUT /license` `{key}` (superuser)
stores a pasted key in `license_state` (source `admin`), replacing an
earlier one; only a currently valid key whose `hosts` match the request
host is accepted, otherwise 422 `invalid-license-key`. `DELETE /license` (superuser) forgets the stored key;
`APP_LICENSE_KEY` is unaffected. Both answer the new `GET` body and are
audited as `license.update` / `license.delete`.

Seats (LIC-23, LIC-24, `services/licensing/seats.py`): an *active user* is
an active, non-service user whose `last_seen_at` is within the last 30 days
(the LIC-4 window). `seat_limit` is 3 in Community mode and for an
`invalid` key, with no overage; with a `valid` or `expired` key it is `seats` plus an overage of 10 %
(at least 1). Sign-in (`POST /auth/login` and the OIDC callback) refuses a
user who is not already active when `active_users ≥ seat_limit`: 403
`seat-limit` for local login, `?error=seat_limit` for SSO, audited as
`auth.login_failed` with `reason: seat_limit`. Users already active are
never refused. With a key, superusers are never refused either (an admin
must always be able to get in) but they are counted. In Community mode with
more than 3 active users (after a trial or a lapsed key, LIC-36) only the
owner — the non-service superuser created first — may sign in, active or
not, with 403 `seat-limit` for everyone else; in Community no other
superuser is exempt. A build whose
`VENDOR_PUBLIC_KEYS` is empty cannot hold a valid key, so it enforces no
limit: `seat_limit` is `null`, no sign-in is refused, and `license_state` is
not read.

Seat report (LIC-30, `services/licensing/seat_report.py`): `GET
/license/seat-report?start=&end=` (superuser; ISO dates, inclusive, UTC;
default the 365 days ending today; `start` ≤ `end`, at most 1096 days,
else 422) returns `{generated_at, install_id, license_id, licensee, tier,
seats, seat_limit, start, end, peak_active_users, peak_overage, periods}`.
Seat history comes from the audit log: every `auth.login` of a non-service
user makes that user active for the next 30 days, exactly as `last_seen_at`
does. `periods` is one entry per calendar month overlapping the range,
clipped to it: `{start, end, active_users, peak_active_users, peak_at,
overage}` — `active_users` is the distinct users active at any moment of
the period, `peak_active_users` the most active at one moment (`peak_at`,
`null` when zero), `overage` = max(0, peak − `seats`) or `null` without a
key. Overage is measured against the licence in force now. The report is
not signed; the licence terms back it (`docs/LICENSING.md`).

Usage notices (LIC-31, `services/licensing/usage_notices.py`): `GET
/license/usage-notices` (superuser) returns `{window_days: 30, notices}`,
each notice `{kind, user_id, email, display_name, detail, count}`. Computed
locally from the audit log of the last 30 days and the live task locks;
nothing leaves the install, and nothing is ever blocked. Kinds:
`parallel_sign_ins` — sign-ins by one human user from two different
networks (`/24`, IPv6 `/48`; private ranges included) less than
`APP_ACCESS_TOKEN_TTL` apart, on at least 3 distinct days (`count` = days);
`parallel_tasks` — one user holding at least 3 unexpired `in_progress` task
locks now (`count` = locks); `superhuman_pace` — a human user submitting at
least 1200 annotations within one hour (`count` = the most in any 60-minute
window); `service_account_annotating` — a service account that submitted
annotations on at least 5 distinct days (`count` = days). Sorted by kind,
then email.

Licence refresh and heartbeat (LIC-27, LIC-6, `services/licensing/refresh.py`,
`services/licensing/heartbeat.py`, worker cron `licence_calls` at minute 17 of
every hour). Both `POST` JSON to `APP_LICENSE_SERVER_URL` with a 10 s timeout;
unset, nothing is ever sent. Neither outcome changes how the install behaves
except that a renewed key is stored.

- **Refresh** — on unless `APP_LICENSE_REFRESH_ENABLED=false`, only while a
  verified key (valid or expired) is in force. Due when it has not succeeded
  in 24 h and was not attempted in the last hour. `POST {url}/v1/refresh`
  `{license_id, install_id, host, active_users, version}` — `host` is
  `APP_PUBLIC_HOSTNAME`, else the last non-loopback host seen at sign-in
  (`license_state.last_host`), else `null`. The answer is `200 {key}` (`key`
  may be `null`: nothing newer), optionally with `revocations`, a revocation
  list (LIC-8) that is verified and stored when newer; an unverifiable list
  is an error. A returned key that is `valid` for the
  reported host and expires later than the stored one is stored with source
  `refresh`; any other key is an error and is not stored.
- **Heartbeat** — unless `APP_TELEMETRY_ENABLED=false` (on by default, LIC-9), any
  edition. Due when not sent in 7 days and not attempted in the last hour.
  `POST {url}/v1/heartbeat` with exactly the body of `GET
  /licensing/telemetry/preview` minus `enabled` and `notice`; any 2xx counts.

Each attempt records `…_attempted_at`, on success `…_succeeded_at` /
`heartbeat_sent_at`, the error text (or `null`), and the payload as sent, in
`license_state`. `GET /license/refresh` (superuser) returns `{enabled,
server_configured, payload, attempted_at, succeeded_at, error, last_payload}`
— `payload` is what would be sent now (`null` without a verified key).
`POST /license/refresh` (superuser) runs a refresh now, due or not, and
returns the same body; 409 `conflict` when refresh is disabled, no server is
configured or no verified key is in force; audited as `license.refresh`.
`GET /licensing/telemetry/preview` adds `server_configured`,
`attempted_at`, `sent_at`, `error` and `last_payload` for the heartbeat, and
`withheld`: what the fingerprint left out and why (LIC-17, LIC-21). Those
notes are shown to the admin only and never sent; the heartbeat's
`fingerprint` is `{signals: [{kind, digest}]}` and nothing else.

## Environment variables (OPS-1)

Prefix `APP_`. Read by `core/config.py` via `pydantic-settings`. Any new
variable is added to `.env.example` in the same change.

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `APP_ENV` | `development` | `development` \| `production` |
| `APP_DATABASE_URL` | — | `postgresql+asyncpg://…`; with `entra` auth the user is the Entra role and there is no password |
| `APP_REDIS_URL` | `redis://localhost:6379/0` | queue; with `entra` auth no password (`rediss://host:6380/0` on Azure) |
| `APP_DATABASE_AUTH` | `password` | `password` (in the URL) \| `entra`: a Microsoft Entra token for the managed identity on every new connection; connections recycled after 45 min (SEC-1) |
| `APP_REDIS_AUTH` | `password` | `password` (in the URL) \| `entra`: a token per connection, user the identity's object id; a pooled connection re-sends `AUTH` with a fresh token 2 min before its token expires (SEC-1) |
| `APP_MANAGED_IDENTITY_CLIENT_ID` | — | client id of the user-assigned managed identity for `entra` auth and Key Vault; unset = the system-assigned identity, or on AKS the workload identity in `AZURE_CLIENT_ID` |
| `APP_SECRET_KEY` | — | signs login tokens, media URLs and the SSO cookie, seals MFA and webhook secrets; required in production, where it must be at least 32 characters and not the `dev-only…` placeholder from `.env.example`. API keys do not depend on it |
| `APP_ACCESS_TOKEN_TTL` | `3600` | seconds |
| `APP_SECRET_KEY_PREVIOUS` | unset | the key `APP_SECRET_KEY` replaced, during a rotation: login tokens, media-proxy signatures, the SSO flow cookie and sealed secrets (MFA seeds, webhook secrets) made with it are still accepted; everything new uses `APP_SECRET_KEY`. Rotate: set both, run `python -m app.cli reseal-secrets`, wait `APP_ACCESS_TOKEN_TTL`, unset it. API keys (`ant_…`) do not depend on either |
| `APP_MFA_REQUIRED_FOR_ADMINS` | `false` | AUTH-2: a superuser signing in with a password and no MFA gets a token limited to `/auth/mfa` and `GET /auth/me` (`mfa_setup_required: true` in the login answer; anything else is 403 `mfa-setup-required`), and may not turn their own MFA off. SSO sign-ins follow the IdP's policy |
| `APP_SIGNED_URL_TTL` | `900` | seconds (AUTH-6) |
| `APP_INTERNAL_API_URL` | unset | how a model service reaches the API, e.g. `http://backend:8000`; prefixes `internal` signed URLs of `local` / `databricks_volume` (root-relative when unset) |
| `APP_TASK_LOCK_TTL` | `1800` | seconds (WF-3) |
| `APP_CORS_ORIGINS` | `http://localhost:5173` | comma-separated; `*` is rejected at startup (credentialed CORS) |
| `APP_TRUSTED_PROXIES` | — | comma-separated IPs/CIDRs of reverse proxies whose `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Host` are believed. Unset trusts nobody: the audit log records the direct peer (SEC-3) |
| `APP_LOG_LEVEL` | `INFO` | |
| `APP_LOG_FORMAT` | `json` | `json` \| `console` (OPS-3) |
| `APP_OTEL_ENABLED` | `false` | OpenTelemetry traces and metrics (OPS-3) over OTLP/HTTP to the operator's own collector — not vendor telemetry. Needs the backend `otel` extra (image: `--build-arg EXTRAS=otel`, compose: `BACKEND_EXTRAS=otel`); on without it, API and worker refuse to start. Spans: FastAPI requests (probes excluded), SQLAlchemy, httpx, Redis, one `job <name>` span per worker job; metric `annotation.worker.job.duration` (s, by `job.name`, `outcome` = `ok` \| `retry` \| `error`); backlog gauges `annotation.jobs.queued`, `annotation.jobs.running`, `annotation.jobs.queued.oldest_age` (s), `annotation.outbox.pending`, `annotation.outbox.pending.oldest_age` (s), `annotation.outbox.dead`, counted from the database by a worker cron each minute and reported only while fresh (OPS-4); HTTP server metrics use the stable conventions (`http.server.request.duration`, s) unless `OTEL_SEMCONV_STABILITY_OPT_IN` is set. Log lines gain `trace_id` / `span_id`. Exporter settings are the standard `OTEL_*` variables below |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` (compose: `http://otel-collector:4318`) | standard OpenTelemetry variable, read by the SDK; so are `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME` (default `annotation-api` / `annotation-worker`), `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_TRACES_SAMPLER` |
| `APP_JOB_MAX_TRIES` | `5` | attempts a background job gets before it is failed (ARC-4); counts real runs (`job.attempts`), not waits for a slot |
| `APP_JOB_MAX_RUNNING_PER_PROJECT` | `3` | jobs of one project running at once; more stay `queued` and start as slots free up |
| `APP_MODEL_DELETE_MODE` | `soft` | `DELETE /models/{id}`: `soft` hides the model and keeps its rows; `hard` removes it, 409 while annotations name one of its versions |
| `APP_THUMBNAIL_SIZE` | `256` | longest side of generated thumbnails, px (IMG-8) |
| `APP_TILE_MIN_PIXELS` | `25000000` | images with at least this many pixels (width × height) are tiled (IMG-1) |
| `APP_TILE_MAX_SOURCE_BYTES` | `12884901888` | sources larger than this (12 GiB) are not tiled (IMG-2) |
| `APP_TILE_WORK_DIR` | system temp dir | where the worker stages a source and its pyramid while tiling; needs room for about 1.5 × the largest source |
| `APP_THUMBNAIL_MAX_SOURCE_BYTES` | `67108864` | images larger than this (64 MiB) are skipped by the thumbnail job; the `tile_image` job writes their thumbnail (IMG-1) |
| `APP_OUTBOX_POLL_BATCH_SIZE` | `50` | outbox rows claimed per publisher transaction; a tick drains the backlog batch by batch (DATA-2, see *outbox_event*) |
| `APP_OUTBOX_MAX_ATTEMPTS` | `10` | failed attempts after which an outbox row is left for an operator |
| `APP_WEBHOOK_MAX_ATTEMPTS` | `8` | delivery attempts per webhook event before it is marked failed (API-4) |
| `APP_WEBHOOK_ALLOW_PRIVATE_URLS` | `false` (compose: `true`) | let webhooks reach private, loopback and link-local addresses (SEC-4); only for a receiver inside the same network |
| `APP_SMTP_HOST` | — | SMTP server for notification e-mail (API-7); unset = no e-mail |
| `APP_SMTP_PORT` | `587` | |
| `APP_SMTP_SECURITY` | `starttls` | `starttls` \| `ssl` \| `none` |
| `APP_SMTP_USERNAME` | — | SMTP login; unset = no AUTH |
| `APP_SMTP_PASSWORD` | — | secret; with `APP_SMTP_USERNAME` |
| `APP_SMTP_FROM` | `Annotide <noreply@localhost>` | sender address |
| `APP_NOTIFICATION_EMAIL_MAX_AGE` | `86400` | seconds; older unsent notifications are never mailed (no backlog storm when e-mail is first turned on) |
| `APP_WEBHOOK_TIMEOUT` | `10` | seconds to wait for a webhook subscriber |
| `APP_RATE_LIMIT_ENABLED` | `true` | API-5 request limits on/off |
| `APP_RATE_LIMIT_PER_MINUTE` | `1200` | requests per minute per signed-in user (the UI polls; several tabs add up) |
| `APP_RATE_LIMIT_API_KEY_PER_MINUTE` | `1200` | requests per minute per API key |
| `APP_RATE_LIMIT_LOGIN_PER_MINUTE` | `10` | login attempts per minute per (IP, e-mail) |
| `APP_WEBHOOK_POLL_BATCH_SIZE` | `50` | due deliveries sent per worker tick |
| `APP_WEBHOOK_MAX_PER_MINUTE` | `60` | deliveries one webhook gets per minute at most; the rest wait for the next window (attempts untouched), and one hook takes at most a quarter of that per tick so a backlog cannot crowd out other hooks |
| `APP_TELEMETRY_ENABLED` | `true` | licence heartbeat (LIC-6, LIC-9), on by default; `false` turns it off |
| `APP_LICENSE_SERVER_URL` | — | vendor licence server base URL for the refresh, heartbeat and trial; unset or empty = nothing is sent |
| `APP_LICENSE_REFRESH_ENABLED` | `true` | daily licence refresh for keyed installs (LIC-27) |
| `APP_INSTALL_ID` | — | stable id for this installation |
| `APP_LICENSE_KEY` | — | licence key (LIC-1); unset = Community mode |
| `APP_LICENSE_FINGERPRINT_SALT` | built in (`core/config.py`) | vendor-wide HMAC salt, public by design; empty = the built-in value (LIC-16) |
| `APP_PUBLIC_HOSTNAME` | — | this install's public hostname, if any |
| `APP_CLOUD_ACCOUNT_ID` | — | cloud subscription / account / project id |
| `APP_SSO_TENANT_ID` | — | OIDC tenant id, for the licence fingerprint only |
| `APP_FRONTEND_URL` | `http://localhost:5173` | where SSO sends the browser when it is done (AUTH-1) |
| `APP_OIDC_ISSUER` | — | identity provider issuer URL; setting it enables SSO. Discovery is read from `{issuer}/.well-known/openid-configuration` (AUTH-1) |
| `APP_OIDC_CLIENT_ID` | — | required with the issuer |
| `APP_OIDC_CLIENT_SECRET` | — | optional; public clients use PKCE alone |
| `APP_OIDC_REDIRECT_URI` | — | required with the issuer: the API's public `…/api/v1/auth/oidc/callback`, registered at the provider |
| `APP_OIDC_SCOPES` | `openid profile email` | |
| `APP_OIDC_DISPLAY_NAME` | `Single sign-on` | label on the login button |
| `APP_OIDC_ORGANIZATION_SLUG` | — | organisation new SSO users join; unset = the only organisation, refused if there are several |
| `APP_OIDC_GROUPS_CLAIM` | `groups` | ID token claim with the user's groups for AUTH-3 sync; empty = no group sync |
| `APP_OIDC_ADMIN_GROUPS` | — | comma-separated IdP groups whose members are superusers; unset = `is_superuser` is not managed by the IdP |
| `APP_OIDC_AUTO_PROVISION` | `true` | create an account on first SSO sign-in; `false` admits only users that already exist (matched by `idp_subject`, then a verified e-mail) |
| `APP_OIDC_TRUST_UNVERIFIED_EMAIL` | `false` | link a first SSO sign-in to an existing account by e-mail even when the ID token's `email_verified` is not `true` (refused as `email_unverified` otherwise; accounts SCIM created are linked regardless). Only for providers that release no address a user can set themselves; Entra ID sends no `email_verified` |
| `APP_SECRET_CACHE_TTL` | `300` | seconds a resolved secret stays cached |
| `APP_SECRET_FILE_ROOT` | — | restrict `file:` refs to this directory |
| `APP_AZURE_KEY_VAULT_NAME` | — | default vault for short Key Vault refs |
| `APP_AWS_REGION` | — | default region for `awssecrets://` refs |

Frontend uses Vite's `VITE_` prefix: `VITE_API_BASE_URL`.

The frontend image also takes a build arg, `CSP_MEDIA_ORIGINS`, which is baked
into the `Content-Security-Policy` header's `img-src`, `connect-src` and
`media-src`. It defaults to `https:` — every real object store — and the
compose stack widens it to `https: http://localhost:10000 http://localhost:9100
http://localhost:4443` for Azurite and the S3 and GCS emulators. The
browser fetches media straight from storage (ARC-3), so an origin missing here
is blocked before the request is made: the annotator shows an empty canvas and
the API logs nothing at all.

## Ports

| Service | Port |
| ------- | ---- |
| frontend (Vite dev) | 5173 |
| backend | 8000 |
| postgres | 5432 |
| redis | 6379 |
| mlflow (dev, `--profile mlflow`, API-6) | 5000 in the network, `MLFLOW_HOST_PORT` (5001) on the host |
| azurite (dev) | 10000, `AZURITE_HOST_PORT` on the host |
| s3: Moto, S3 + Secrets Manager (dev, `--profile emulators`) | 5000 in the network, `S3_EMULATOR_HOST_PORT` (9100) on the host |
| gcs: fake-gcs-server (dev, `--profile emulators`) | 4443, `GCS_EMULATOR_HOST_PORT` on the host |
| webhook-sink: echo receiver (dev, `--profile emulators`) | 8080 in the network, `WEBHOOK_SINK_HOST_PORT` (8099) on the host |

## Definition of done for the skeleton

- `docker compose up` brings up postgres, redis, backend, worker, frontend.
- `GET /api/v1/health` returns 200; `/ready` reports DB and Redis.
- `alembic upgrade head` creates every table above.
- The frontend loads, lists projects from the API, and opens an image with the
  Konva annotator.
- `pytest` and `vitest` pass; `ruff`, `mypy` and `tsc --noEmit` are clean.
- `playwright test` passes against a seeded stack (`frontend/e2e/`).
