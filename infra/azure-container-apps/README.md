# Annotide on Azure Container Apps (scales to zero)

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fannotide%2Fannotide%2Fmain%2Finfra%2Fazure-container-apps%2Fazuredeploy.json)

The cheapest way to run Annotide for a person or a team on Azure when it is
not used all day: the app scales to zero replicas when idle and wakes on the
first request. Only the database bills while nobody works. For a cluster
with failover and managed services, use the [Helm chart](../helm/annotide/README.md)
or the [Terraform setup](../terraform/azure/README.md) instead.

## What it deploys

| Resource | Size | Bills while idle |
| -------- | ---- | ---------------- |
| Container App: nginx, API, worker and Valkey in one replica | 1.75 vCPU / 3.5 GiB, 0 to 1 replicas | No |
| PostgreSQL Flexible Server | Burstable B1ms, 32 GB, 7-day backups | Yes |
| Storage account and container `annotide` for media and results | Standard LRS | Storage used only |
| User-assigned identity with Storage Blob Data Contributor on it | | No |
| Log Analytics | 30 days, capped at 0.5 GB a day | Ingestion only |

The replica starts on HTTP traffic, while the worker has a backlog (queued or
running jobs, annotations to publish to storage, webhooks due:
[`worker-backlog.sql`](worker-backlog.sql), counted in PostgreSQL by KEDA),
and for a quarter of an hour every night at 03:00 UTC for the licence
refresh. It scales in after about five idle minutes. The first request after
that takes a few seconds while the containers start.

**Approximate cost**, pay-as-you-go, West Europe, October 2026: about €18 a
month idle (PostgreSQL), plus about €0.17 for each hour the app is awake.
Container Apps' monthly free grant covers the first 25 hours or so. Used
every working day, about €40–45 a month; a few hours a week, about €20.
Check your region in the [pricing calculator](https://azure.microsoft.com/pricing/calculator/).

## Deploy

With the button above (pick a resource group and region; every field has a
default), or with the Azure CLI:

```sh
az group create -n annotide-rg -l westeurope
az deployment group create -g annotide-rg \
  --template-file infra/azure-container-apps/main.bicep \
  --parameters licenseKey=<key, or leave out for Community>
```

The outputs give the address (`url`), the app name and the storage
details. Then create the first administrator. Open the address once so the
app is awake, then within five minutes:

```sh
az containerapp exec -g annotide-rg -n annotide --container api \
  --command "python -m app.cli create-superuser --email you@example.com"
```

It asks for the password. There is no sign-up page by design (AUTH-2).

## Connect storage

Settings → Connectors → add an **Azure Blob** connector with the deployment
outputs: account `storageAccount`, container `storageContainer`, identity
type **managed identity**, client ID `identityClientId`. The browser reads
and uploads media directly through signed URLs; the account already allows
the app's address (CORS). Your own existing storage works the same way once
the identity has Storage Blob Data Contributor on it and its CORS allows the
app's address.

## Upgrade

Deploy the template again with the new release:

```sh
az deployment group create -g annotide-rg \
  --template-file infra/azure-container-apps/main.bicep \
  --parameters annotideVersion=<new version> licenseKey=<key>
```

The new revision migrates the database in its init container before it
starts. Back up first ([BACKUP.md](../../docs/BACKUP.md)): migrations are
one-way. A re-run also sets a new PostgreSQL password on the server and the
app together; the app's secret key stays (below).

## Good to know

- **One replica.** The queue lives in the replica, so the app never runs
  more than one. That fits a team; tiling very large images (whole-slide,
  satellite) is slow on 0.75 vCPU and limited by the replica's temporary
  disk. Use the Helm chart for those.
- **The secret key** (`APP_SECRET_KEY`) is key2 of the storage account. The
  account has shared key access off, so the key cannot read data; it is
  only a stable random value that survives re-deployments. Do not
  regenerate key2 casually: it rotates the app's key. To rotate on purpose,
  set the old value as `APP_SECRET_KEY_PREVIOUS` on both containers first
  and follow [CONTRACTS.md → `APP_SECRET_KEY_PREVIOUS`](../../docs/CONTRACTS.md#environment-variables-ops-1).
- **PostgreSQL** accepts connections from Azure services only (no fixed
  outbound address on the consumption plan), with a password and TLS. Add
  your own address in its Networking page to connect with `psql`.
- **Settings** (SMTP, single sign-on, telemetry: [CONTRACTS.md → Environment
  variables](../../docs/CONTRACTS.md#environment-variables-ops-1)) are
  environment variables on the `api` and `worker` containers:
  `az containerapp update -g annotide-rg -n annotide --container-name api --set-env-vars KEY=value`,
  and the same for `worker`.
- **Stopping completely:** `az postgres flexible-server stop` stops the only
  idle cost; Azure starts it again after seven days. The app wakes by
  itself.

## Changing the template

Edit `main.bicep`, then rebuild the ARM template the button uses (CI checks
that the two match):

```sh
az bicep build --file infra/azure-container-apps/main.bicep \
  --outfile infra/azure-container-apps/azuredeploy.json
```

`backend/tests/test_container_apps_backlog.py` runs `worker-backlog.sql`
against the schema, so a renamed table or column fails CI rather than
leaving work unprocessed while the app sleeps.
