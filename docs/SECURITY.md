# Security overview (SEC-10)

SEC-10 calls for a penetration test before the first production customer.
The test itself has to be done by an independent tester. This page gives
the architecture and the trust boundaries; testers get a separate brief
with the areas to test first and the known residual risks.

## Architecture in one paragraph

A FastAPI backend and an arq worker share one image and one PostgreSQL
database; Redis carries only job ids and rate-limit counters. The browser
never sends media through the API: it reads and writes the customer's own
object storage on short-lived signed URLs (ARC-3, `APP_SIGNED_URL_TTL`).
The two exceptions are local disk and Databricks volumes, which go through
an HMAC-signed proxy route. The API calls out to registered model
endpoints, webhook receivers, SMTP, an optional OIDC provider, ML platforms,
and, if configured, the vendor licence server.

## Trust boundaries

| Boundary | Control | Where |
| -------- | ------- | ----- |
| Browser → API | Bearer JWT (login, SSO) or API key (`ant_…`, stored as sha256; legacy `kid` JWT), scopes, rate limits | `api/deps.py`, `services/api_keys.py`, `services/rate_limit.py` |
| Organisation isolation | Every row lookup scoped to the caller's organisation; cross-org ids are 404 | `services/repository.py::get_or_404` |
| Project roles | owner / reviewer / annotator / viewer per project | `ensure_project_member` |
| Folder limits (§4) | Items outside a member's `path_prefixes` are 404 | `ensure_item_member`, list / queue filters |
| Browser → storage | Signed URLs scoped to one object, with a TTL; CORS checked | connectors' `signed_url`, `check()` |
| Signed media proxy | HMAC over (connector, path, expiry), read and write scopes apart; served sandboxed (`Content-Security-Policy: sandbox`, PDFs excepted for the browser viewer; `nosniff`), anything but media, PDF and plain data as a download | `api/v1/storage.py`, `core/security.py` |
| API → outside | Webhooks refuse private, loopback, link-local and metadata addresses (SEC-4); the HTTP connector stays under its `base_url`, and no connector redirect leads from a public host to an internal one or to metadata | `core/netguard.py`, `connectors/http.py`, `connectors/sharepoint.py` |
| Imports | Archives are capped in member count and unpacked size before anything is read | `importers/base.py::expand_archive` |
| Reference model service | Optional bearer key (`MODEL_API_KEY`) on every route but the probes; media URLs must be http(s) and may not reach cloud metadata endpoints | `model-service/app/main.py`, `backends/heuristic.py::refuse_metadata_url` |
| Secrets | Connectors and models hold references (`azurekeyvault://`, `awssecrets://`, `gcpsecrets://`, `env:`, `file:`), never values (AUTH-7) | `services/secrets/` |
| Licensing | Keys verified offline against compiled-in public keys; no check depends on reaching a vendor service (LIC-28) | `services/licensing/` |

## Reporting

Report findings against requirement ids (SEC-*, AUTH-*, LIC-*). Fixes ship
with a regression test, like everything else.
