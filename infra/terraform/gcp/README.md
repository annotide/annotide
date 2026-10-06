# Google Cloud infrastructure (Terraform)

Provisions everything the `annotide` Helm chart needs on Google Cloud (§15).
The pods sign in to Cloud Storage and Secret Manager as a Google service
account through Workload Identity, so no service-account keys exist.

**Private by default, one Cloud NAT.** Nodes have no public address; Cloud
SQL and Memorystore have private addresses only (private services access,
free). The nodes reach Google APIs and Cloud Storage over Private Google
Access (free); everything else outbound (image pulls, model endpoints,
webhooks) leaves through Cloud NAT on one reserved IP. Cloud NAT and, once
installed, the ingress controller's load balancer are the paid networking.

| Resource | Notes |
| -------- | ----- |
| VPC | One subnet with secondary ranges for pods and services; Private Google Access; flow logs (sampled); Cloud Router and Cloud NAT on one reserved IP; a private services access range for Cloud SQL and Memorystore |
| GKE | Regional, REGULAR release channel; private nodes; control plane limited to `api_authorized_ip_ranges`; Dataplane V2 (enforces NetworkPolicies); Workload Identity; shielded nodes; no client certificates |
| Node pool | `node_machine_type`, Container-Optimized OS; its own service account (logs and metrics only, never the default compute account); GKE metadata server; secure boot; cluster autoscaler between `node_count.min` and `node_count.max` |
| Cloud SQL PostgreSQL 16 | Enterprise edition; private IP only; encrypted connections only; 14 backups and 7 days of point-in-time recovery; connection and lock logging; deletion protection; optional regional HA |
| Memorystore Redis 7.2 | Private services access; AUTH string; replica with failover (`STANDARD_HA`) by default. TLS off by default, see "Redis TLS" |
| Cloud Storage buckets | `media`, `results`, `cache`; uniform access, public access prevention enforced; versioning, 14-day soft delete and recovery of overwritten objects; CORS for `public_url` |
| IAM | A workload service account that only the chart's Kubernetes service account can use: the three buckets, signing its own URLs (`signBlob`), and Secret Manager secrets whose id starts with `<prefix>-annotation-` |
| Budget | Optional (`budget_amount` with `billing_account`), alerts at 50 % forecast, 80 % and 100 % actual |

## Who reaches what

| Who | Reaches | How |
| --- | ------- | --- |
| Pods | Cloud Storage, Secret Manager, IAM | Private Google Access, as the workload service account |
| Pods | Cloud SQL, Memorystore | Private IPs in the VPC (private services access) |
| Pods | Anything else | Cloud NAT (`terraform output nat_public_ip`) |
| Annotators' browsers | Cloud Storage | Public endpoint, signed URLs only (ARC-3) |
| Admins | Kubernetes API | `api_authorized_ip_ranges`, with IAM on the project |
| Admins | PostgreSQL (`psql`, `pg_dump`) | From a pod in the cluster; see "Database access" |

PostgreSQL and Redis sign in with a password and an AUTH string: the app has
no Cloud SQL IAM sign-in (`APP_DATABASE_AUTH` / `APP_REDIS_AUTH` know only
`password` and `entra`). Neither has a public address, and the credentials
live only in the Terraform state and the chart's Secret. Keep the state in an
access-controlled backend (a GCS backend).

### Redis TLS

`redis_tls` is off by default, and turning it on breaks the API and worker
for now. Memorystore's certificate is signed by a per-instance Google CA that
is not in the system trust store, and the app cannot be given a CA yet (arq
reads only host, port, password and database from `APP_REDIS_URL`, and the
chart cannot mount a CA file). Until it can, Redis is reachable only from
inside the VPC and requires its AUTH string. INSTALL.md asks for `rediss://`
in production; this module does not meet that yet.

## Apply

```sh
cp terraform.tfvars.example terraform.tfvars   # never commit it
gcloud auth application-default login
terraform init
terraform plan -out plan.tfplan
terraform apply plan.tfplan
```

The first apply turns on the APIs it needs (Compute, GKE, Cloud SQL,
Memorystore, Service Networking, Secret Manager, IAM Credentials). Set
`api_authorized_ip_ranges` in production.

## Install the chart

```sh
$(terraform output -raw get_credentials_command)
terraform output -raw helm_values > values.gcp.yaml   # holds secrets; do not commit
helm upgrade --install annotation ../../helm/annotide \
  --namespace annotation --create-namespace \
  -f values.gcp.yaml -f your-overrides.yaml   # image tags, ingress host, TLS
```

`helm_values` sets the database and Redis URLs (with their generated
credentials), the token signing key, `APP_TRUSTED_PROXIES` (the pod range),
and the service account's `iam.gke.io/gcp-service-account` annotation. The
release name and namespace must match `helm_release_name` and
`kubernetes_namespace`, because the workload service account trusts exactly
that Kubernetes service account.

Then register the storage in the app (Connectors page, or the API) with
`terraform output connector_config`: type `gcs`, identity
`managed_identity`, no secret. Signed URLs are signed through the IAM
`signBlob` API as the workload service account, which this module allows.

**Secret references (AUTH-7).** Create secrets in Secret Manager whose id
starts with `<prefix>-annotation-` (`terraform output secrets_prefix`) and
reference them as `gcpsecrets://<project>/<id>`. The published backend image
does not include the Secret Manager client: build your own with
`--build-arg EXTRAS=gcp-secrets` to use these references.

## HTTPS

Traefik behind a passthrough Network Load Balancer, with certificates from
cert-manager and Let's Encrypt, both free:

```sh
helm upgrade --install traefik traefik --repo https://traefik.github.io/charts \
  --namespace traefik --create-namespace \
  --set service.spec.externalTrafficPolicy=Local

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
          ingress: { ingressClassName: traefik }
YAML
```

`externalTrafficPolicy: Local` keeps the client's address, so the audit log
records it (SEC-3). Point a DNS A record for your host at the load balancer
(`kubectl get svc -n traefik traefik`), then in your chart values:

```yaml
ingress:
  enabled: true
  className: traefik
  clusterIssuer: letsencrypt     # adds the cert-manager annotation and a TLS block
  hosts:
    - host: annotate.example.com
networkPolicy:
  enabled: true                  # Dataplane V2 enforces it
  ingressFrom:
    - namespaceSelector:
        matchLabels: { kubernetes.io/metadata.name: traefik }
```

## Worker autoscaling (KEDA)

The chart's `worker.autoscaling` (OPS-5) needs KEDA, which GKE does not
bundle:

```sh
helm upgrade --install keda keda --repo https://kedacore.github.io/charts \
  --namespace keda --create-namespace
```

KEDA's PostgreSQL scaler takes its own libpq connection string (see the
chart's `worker.autoscaling.postgresConnection`); by default it is
`database.url` without `+asyncpg`.

## Database access

Cloud SQL has no public address. Backups and `psql` run from a pod:

```sh
URL=$(terraform output -raw helm_values | yq '.database.url' | sed 's/+asyncpg//; s/ssl=require/sslmode=require/')
kubectl -n annotation run psql --rm -it --restart=Never --image=postgres:16 -- psql "$URL"
```

See [docs/BACKUP.md](../../../docs/BACKUP.md) for what to back up; Cloud SQL
backups and point-in-time recovery cover the database itself.

## What this does not do

- **IAM sign-in to Cloud SQL, and Redis TLS.** See above.
- **A browser IP allow-list on the buckets.** Cloud Storage IP filtering
  would also have to admit the cluster's VPC; it is left out.
- **Customer-managed keys (SEC-2).** Encryption at rest uses Google's
  default keys.

Validated with `terraform validate` and Trivy, never applied from this repo:
`terraform apply` needs your approval and your project.
