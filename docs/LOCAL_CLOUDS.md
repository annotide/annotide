# Local cloud emulators

Every storage connector, the AWS secret store and outbound webhooks can be
exercised on one machine, with no cloud account. Azurite is always part of
`make dev`. The rest come up with the compose profile `emulators`:

| Cloud service | Emulator | In the network | On the host |
| ------------- | -------- | -------------- | ----------- |
| Azure Blob Storage | Azurite | `http://azurite:10000` | `AZURITE_HOST_PORT` (10000) |
| AWS S3 | Moto | `http://s3:5000` | `S3_EMULATOR_HOST_PORT` (9100) |
| AWS Secrets Manager | Moto (same server) | `http://s3:5000` | 9100 |
| Google Cloud Storage | fake-gcs-server | `http://gcs:4443` | `GCS_EMULATOR_HOST_PORT` (4443) |
| Webhook receiver | http-https-echo | `http://webhook-sink:8080` | `WEBHOOK_SINK_HOST_PORT` (8099) |
| E-mail (SMTP) | Mailpit | `mailpit:1025` | `MAILPIT_SMTP_HOST_PORT` (1025), UI on `MAILPIT_HOST_PORT` (8025) |

Host ports bind to `127.0.0.1` only. Moto's own port 5000 is not published
as 5000 because macOS AirPlay holds it.

## Commands

```sh
make emulators        # start Azurite, Moto, fake-gcs-server and the echo receiver
make seed-emulators   # after `make seed`: demo projects on S3 and GCS + a webhook
make integration      # connector, secret and webhook tests against all of them
E2E_EMULATORS=1 E2E_BASE_URL=http://localhost:5173 npx playwright test emulators
```

`make seed-emulators` creates a `media` bucket on each emulator, uploads the
same sample photos as `make seed`, and adds a connector and a project for
each. The projects are **Demo on S3: traffic objects** and **Demo on GCS:
traffic objects**. They behave like the Azure demo: scan, annotate, review,
export. It also registers a webhook for every event to the echo receiver.
Every delivery, with its headers and signature, appears in:

```sh
docker compose logs -f webhook-sink
```

There is one hook per format: signed JSON (`/annotation-events`), Slack
(`/slack`) and Microsoft Teams (`/teams`), so the chat messages (API-7) can
be read in the same log. Use **Send test** on the Webhooks page to trigger
one on demand.

Notification e-mail goes to Mailpit once the stack is started with it as
the SMTP server:

```sh
APP_SMTP_HOST=mailpit APP_SMTP_PORT=1025 APP_SMTP_SECURITY=none \
  docker compose up -d backend worker
```

Mention someone in a comment and the mail appears at http://localhost:8025
within a minute.

`make integration` runs `backend/tests/test_integration_emulators.py` from the
local venv (`make install`). Nothing in it is mocked. Each connector writes,
lists, reads byte ranges and deletes real objects. Every signed URL is
fetched the way the browser fetches it: plain HTTP with an `Origin` header,
checking the CORS answer. An `awssecrets://` reference resolves through Moto,
and a webhook delivery is checked against its HMAC signature as the receiver
saw it. CI runs the same file in the `integration` job. Without
`RUN_INTEGRATION=1` the tests skip, so `make test` does not need the emulators.

## What the emulators do not prove

- **Real identities.** Managed identity, IAM roles, workload identity and
  service-account keys are only exercised against the real clouds. Moto
  accepts any key pair. The GCS connector runs as `identity_type: none`.
- **GCS uploads from the browser.** Anonymous access has nothing to sign an
  upload URL with, so a `none` GCS connector refuses `write` URLs. Reads work,
  and the connector writes results through the API itself. Signing is
  unit-tested offline with a throwaway key (`tests/test_connector_gcs.py`).
- **Bucket CORS on GCS.** fake-gcs-server answers every origin itself and
  keeps no bucket CORS rules, so the connector check shows a CORS warning
  there. Moto and Azurite enforce the rule as the real services do.
- **Azure Key Vault and GCP Secret Manager.** Neither has a usable local
  emulator. They are covered by unit tests with fakes.
- **Real Slack and Teams.** The echo receiver shows the bodies; whether a
  workspace renders them is checked against a real channel.

Test installations in each real cloud (`azure-infra/`, `google-infra/`,
`aws-infra/`) are the next step for the identity paths above.
