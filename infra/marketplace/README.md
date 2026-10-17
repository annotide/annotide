# Azure Marketplace package

Annotide is listed on Microsoft Marketplace as an **Azure Container offer,
Kubernetes application**, BYOL plan (offer `annotide`, plan `byol`, cluster
extension type `Annotide.Annotide`). Customers install it from the Azure
portal onto a new or existing AKS cluster as a cluster extension. Microsoft
copies the bundle built here into its own public registry.

## Files

| File | What it is |
| ---- | ---------- |
| `manifest.yaml` | The package manifest for `container-package-app` (cpa); `build.sh` sets `version` and `registryServer` |
| `values.yaml` | Merged over the chart's values in the bundle: every image under `global.azure.images`, a generated secret key, in-cluster PostgreSQL and Valkey on a retained Azure Disk |
| `mainTemplate.json` | The ARM template. It may only create `Microsoft.ContainerService/managedClusters` (optional new cluster) and the `Microsoft.KubernetesConfiguration/extensions` resource. The `DONOTMODIFY` variables are filled in by Marketplace |
| `createUIDefinition.json` | The portal form: cluster, database, installation name, licence key, optional public hostname |
| `build.sh` | Assembles `build/` (git-ignored): the chart, the merged values, the template, the form and the manifest |

## The install form

- **Cluster:** create a new AKS cluster, or pick one in the resource group.
- **Database:** "Deploy in the cluster" by default, with a warning and a
  required acknowledgement of the risks (no backups, no failover, data
  deleted with the cluster; [chart README](../helm/annotide/README.md#risks-of-running-the-database-in-the-cluster)).
  An optional ZRS disk. "Use existing services" asks for PostgreSQL and
  Redis connection strings instead, which are passed as protected settings.
- **Annotide:** the installation name (also the Helm release), an optional
  licence key (protected), and an optional public hostname for an existing
  ingress controller and cert-manager ClusterIssuer. Without a hostname, the
  app is reached with `kubectl port-forward`.

Form values reach the chart as cluster extension settings, which may arrive as
strings. So the template only passes `"true"` / `"false"` strings, and the
chart compares the in-cluster flags as strings (`ap.postgresqlOn`,
`ap.valkeyOn`). It never addresses list entries, which is why `ingress.host`
is a scalar.

## Build and verify locally

```sh
infra/marketplace/build.sh 0.3.0            # needs yq v4
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/infra/marketplace/build:/data" -w /data \
  --entrypoint /bin/bash mcr.microsoft.com/container-package-app:latest \
  -c "cpa verify --telemetryOptOut"
```

CI runs the same check (`marketplace` job in `ci.yml`). Last local run:
cpa 1.3.59, 2026-10-17, with 0 failures for the manifest, the Helm chart and
the Marketplace package.

Try the form in the Azure portal's
[createUiDefinition sandbox](https://portal.azure.com/#view/Microsoft_Azure_CreateUIDef/SandboxBlade)
by pasting `createUIDefinition.json`.

## Release

The `marketplace` job in `release.yml` runs on every `vX.Y.Z` tag, after
the images are on GHCR. It assembles the bundle for that version, signs in to
Azure with OIDC, and runs `cpa buildbundle`, which pushes the CNAB to the
publishing registry. Then, in Partner Center, open the `byol` plan, go to
**Technical configuration**, choose the new bundle version, and publish.
Without the repository variable `MARKETPLACE_ACR` the job only verifies.

One-time setup (the company's Azure, in the tenant that owns the Partner
Center account):

1. A container registry (Basic). Microsoft's ingestion app
   `32597670-3e15-4def-8851-614ff48c1efa` needs a service principal with
   `AcrPull` on it, and the subscription needs the
   `Microsoft.PartnerCenterIngestion` resource provider.
2. An Entra app with `AcrPush` on the registry and a federated credential
   for `repo:annotide/annotide:environment:marketplace`.
3. Repository variables `MARKETPLACE_ACR` (for example
   `annotidemarketplace.azurecr.io`), `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`
   and `AZURE_SUBSCRIPTION_ID`, and a `marketplace` environment. No secrets
   are needed.

Each bundle version can be pushed once. A new chart or image needs a new
release version (`cpa buildbundle --force` only for a version not yet
attached to the offer).
