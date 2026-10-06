# Azure infrastructure (Terraform)

Provisions everything the `annotide` Helm chart needs on Azure
(§15). The pods sign in to Azure with a workload identity, so no storage keys
or client secrets exist.

**No paid networking.** There are no private endpoints, private DNS zones,
NAT gateway or VPN. Every service is reached over its public endpoint,
locked down with free controls: service endpoints, resource firewalls, IP
allow-lists, Entra ID and TLS.

| Resource | Notes |
| -------- | ----- |
| AKS | Azure CNI overlay + Cilium network policy; workload identity; KEDA add-on (OPS-5); Entra ID + Azure RBAC, local accounts off; Container Insights; one managed outbound IP |
| PostgreSQL Flexible Server 16 | TLS required; Entra sign-in only (no password): the workload identity is an Entra administrator and owns the database, `postgres_admins` are the others; firewall admits the cluster's outbound IP and `admin_ip_ranges`; `citext` and `pgcrypto` allow-listed |
| Azure Cache for Redis | TLS only (port 6380, TLS 1.2); Entra sign-in only, access keys off; the workload identity has the *Data Owner* access policy; firewall admits the cluster's outbound IP |
| Storage account | Containers `media`, `results`, `cache`; HTTPS only, TLS 1.2, no anonymous access, no shared keys; versioning and 14-day soft delete; CORS for `public_url`; the cluster over a service endpoint, browsers from `storage_allowed_ip_ranges` |
| Key Vault | RBAC, purge protection; default deny, the cluster over a service endpoint and `admin_ip_ranges`; the workload identity can read secrets, for `azurekeyvault://` references (AUTH-7) |
| Budget | Optional (`budget_amount`), alerts at 50 % forecast, 80 % and 100 % actual |

## Who reaches what

| Who | Reaches | How |
| --- | ------- | --- |
| Pods | Storage, Key Vault | Service endpoint on the AKS subnet (free) |
| Pods | PostgreSQL, Redis | Public endpoint; the firewall admits only the cluster's outbound IP (neither service supports service endpoints) |
| Annotators' browsers | Storage | Public endpoint, limited to `storage_allowed_ip_ranges`, or anyone when empty. Signed, expiring URLs are still required for every read and write (ARC-3). |
| Admins | Key Vault secrets, PostgreSQL (`psql`, `pg_dump`) | `admin_ip_ranges`; PostgreSQL also needs the person in `postgres_admins` |
| Admins | Kubernetes API | `api_authorized_ip_ranges` |

The trade-off: PostgreSQL and Redis face the internet behind their
firewalls. Neither has a password or key to leak: both accept only
Microsoft Entra tokens, which the pods get for the workload identity and
renew before they expire (`APP_DATABASE_AUTH` / `APP_REDIS_AUTH` = `entra`).
The firewall, TLS and Entra sign-in are the controls; keep `admin_ip_ranges`
short. A deployment that needs SEC-1's private networking must add private
endpoints itself; this module leaves them out on purpose.

## Apply

```sh
cp terraform.tfvars.example terraform.tfvars   # never commit it
az login && export ARM_SUBSCRIPTION_ID=<subscription id>
terraform init
terraform plan -out plan.tfplan
terraform apply plan.tfplan
```

Put people, groups or the CI principal in `cluster_admin_object_ids`, or
nobody can use kubectl once local accounts are off. Set
`api_authorized_ip_ranges` in production. Put whoever takes backups in
`postgres_admins`: with password sign-in off, nobody else can reach the
database.

## Install the chart

```sh
$(terraform output -raw get_credentials_command)
kubelogin convert-kubeconfig -l azurecli
terraform output -raw helm_values > values.azure.yaml   # holds secrets; do not commit
helm upgrade --install annotation ../../helm/annotide \
  --namespace annotation --create-namespace \
  -f values.azure.yaml -f your-overrides.yaml   # image tags, ingress host, TLS
```

`helm_values` sets the database and Redis URLs (no passwords; the database
user is the workload identity's name), Entra sign-in for both
(`APP_DATABASE_AUTH`, `APP_REDIS_AUTH`), the token signing key, the
Key Vault name (`APP_AZURE_KEY_VAULT_NAME`), and the workload identity: the
service account's client-id annotation plus the
`azure.workload.identity/use` pod label. The release name and namespace
must match `helm_release_name` and `kubernetes_namespace`, because the
federated credential trusts exactly that service account.

Then register the storage in the app (Connectors page, or the API) with
`terraform output connector_config`. The identity is `managed_identity`
with no client id: the workload-identity webhook provides it. Signed URLs
are user-delegation SAS, so no account key is ever needed.

## HTTPS

`ingress_enabled` (default on) turns on AKS application routing, a managed
NGINX ingress controller. Certificates come from cert-manager and Let's
Encrypt, both free:

```sh
helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
  --namespace cert-manager --create-namespace --set crds.enabled=true

kubectl apply -f - <<'YAML'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: you@example.com          # expiry warnings
    privateKeySecretRef: { name: letsencrypt-account }
    solvers:
      - http01:
          ingress: { ingressClassName: webapprouting.kubernetes.azure.com }
YAML
```

Point a DNS A record for your host at the ingress controller's public IP
(`kubectl get svc -n app-routing-system nginx`), then in your chart values:

```yaml
ingress:
  enabled: true
  className: webapprouting.kubernetes.azure.com
  clusterIssuer: letsencrypt     # adds the cert-manager annotation and a TLS block
  hosts:
    - host: annotate.example.com
```

The public IP is the one networking cost (a few euros a month); there are
still no private endpoints, private DNS zones or NAT gateways.

## What this does not do
- **KEDA's PostgreSQL scaler.** It needs its own libpq connection string (see
  the chart's `worker.autoscaling`).
- **Customer-managed keys (SEC-2).** Encryption at rest uses Azure's
  platform keys.

Validated with `terraform validate` and Trivy, never applied from this repo:
`terraform apply` needs your approval and your subscription.
