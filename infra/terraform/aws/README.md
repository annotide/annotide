# AWS infrastructure (Terraform)

Provisions everything the `annotide` Helm chart needs on AWS (§15). The pods
sign in to S3 and Secrets Manager with an IAM role for their service account
(IRSA), so no access keys exist.

**Private by default, one NAT gateway.** Nodes, PostgreSQL and Redis live in
private subnets with no public address. The cluster reaches S3 through a
free gateway endpoint; everything else outbound (image pulls, model
endpoints, webhooks) leaves through a single NAT gateway, so the cluster has
one fixed public IP. The NAT gateway and, once installed, the ingress
controller's load balancer are the paid networking.

| Resource | Notes |
| -------- | ----- |
| VPC | Three private /20s (nodes, pods, PostgreSQL, Redis) and three public /24s (load balancers, NAT) across three availability zones; one NAT gateway; S3 gateway endpoint; the default security group has no rules |
| KMS key | Rotated yearly; encrypts Kubernetes secrets, RDS, ElastiCache, Performance Insights and the buckets |
| EKS | Access entries only (no `aws-auth`, no implicit admin for whoever ran terraform); public API endpoint limited to `api_authorized_ip_ranges`, private endpoint for nodes; secrets envelope-encrypted; all control-plane logs; VPC CNI with its network policy agent on; IRSA |
| Node group | `node_instance_types` in the private subnets, AL2023; IMDSv2 with a hop limit of one (pods cannot borrow the node's role); encrypted root volumes |
| RDS PostgreSQL 16 | Private subnets, no public access; `rds.force_ssl`; KMS; 14-day backups; deletion protection and a final snapshot; optional Multi-AZ. `citext` and `pgcrypto` need no allow-listing |
| ElastiCache Redis 7.1 | Private subnets; TLS in transit (public-CA certificate), KMS at rest, AUTH token; a replica with automatic failover unless `redis_node_count = 1`; 7-day snapshots |
| S3 buckets | `media`, `results`, `cache`; all public access blocked, owner-enforced; SSE-KMS with bucket keys; versioning, 14-day recovery of overwritten or deleted objects; TLS-only bucket policy; CORS for `public_url`; optional source-IP allow-list for browsers |
| IAM | A workload role that only the chart's service account can assume: the three buckets, the KMS key, and Secrets Manager names under `<prefix>/annotation/` |
| Budget | Optional (`budget_amount`), alerts at 50 % forecast, 80 % and 100 % actual |

## Who reaches what

| Who | Reaches | How |
| --- | ------- | --- |
| Pods | S3 | Gateway endpoint (free), with the workload role |
| Pods | PostgreSQL, Redis | Inside the VPC; their security groups admit only the cluster security group |
| Pods | Anything else | NAT gateway (`terraform output nat_public_ip`) |
| Annotators' browsers | S3 | Public endpoint, presigned URLs only (ARC-3); limited to `storage_allowed_ip_ranges` when set |
| Admins | Kubernetes API | `api_authorized_ip_ranges`, as one of `cluster_admin_arns` |
| Admins | PostgreSQL (`psql`, `pg_dump`) | From a pod in the cluster; see "Database access" |

PostgreSQL and Redis sign in with a password and an AUTH token: the app has
no RDS IAM or ElastiCache IAM sign-in (`APP_DATABASE_AUTH` /
`APP_REDIS_AUTH` know only `password` and `entra`). Neither has a public
address, both require TLS, and the credentials are generated here and live
only in the Terraform state and the chart's Secret. Keep the state in an
encrypted, access-controlled backend (an S3 backend with KMS).

## Apply

```sh
cp terraform.tfvars.example terraform.tfvars   # never commit it
aws sso login                                  # or any credential the provider accepts
terraform init
terraform plan -out plan.tfplan
terraform apply plan.tfplan
```

Put people's roles or the CI role in `cluster_admin_arns`, or nobody can use
kubectl: the cluster grants nothing to whoever created it. Set
`api_authorized_ip_ranges` in production.

## Install the chart

```sh
$(terraform output -raw get_credentials_command)
terraform output -raw helm_values > values.aws.yaml   # holds secrets; do not commit
helm upgrade --install annotation ../../helm/annotide \
  --namespace annotation --create-namespace \
  -f values.aws.yaml -f your-overrides.yaml   # image tags, ingress host, TLS
```

`helm_values` sets the database and Redis URLs (with their generated
credentials), the token signing key, `APP_AWS_REGION` (the default region
for `awssecrets://` references), `APP_TRUSTED_PROXIES` (the private
subnets, where pods get their addresses), and the service account's
`eks.amazonaws.com/role-arn` annotation. The release name and namespace must
match `helm_release_name` and `kubernetes_namespace`, because the workload
role trusts exactly that service account.

Then register the storage in the app (Connectors page, or the API) with
`terraform output connector_config`: type `s3`, identity `iam_role`, no
secret. The default AWS credential chain picks up IRSA, and presigned URLs
carry the workload role's permissions.

**Secret references (AUTH-7).** Create secrets in Secrets Manager under the
`<prefix>/annotation/` name prefix (`terraform output secrets_prefix`) and
reference them as `awssecrets://<prefix>/annotation/<name>`, adding
`?key=<field>` for one field of a JSON secret. The workload cannot read any
other secret.

## HTTPS

The cluster has no ingress controller of its own. Traefik behind a Network
Load Balancer, with certificates from cert-manager and Let's Encrypt, both
free:

```sh
helm upgrade --install traefik traefik --repo https://traefik.github.io/charts \
  --namespace traefik --create-namespace \
  --set service.spec.externalTrafficPolicy=Local \
  --set 'service.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-type=nlb'

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
records it (SEC-3). Point a DNS CNAME for your host at the load balancer
(`kubectl get svc -n traefik traefik`), then in your chart values:

```yaml
ingress:
  enabled: true
  className: traefik
  clusterIssuer: letsencrypt     # adds the cert-manager annotation and a TLS block
  hosts:
    - host: annotate.example.com
networkPolicy:
  enabled: true                  # the VPC CNI enforces it
  ingressFrom:
    - namespaceSelector:
        matchLabels: { kubernetes.io/metadata.name: traefik }
```

## Node autoscaling

`node_count` sets the node group's bounds, but EKS does not move between
them by itself. Install the Cluster Autoscaler or Karpenter to scale nodes;
Terraform ignores the group's current size so it does not fight them.

## Worker autoscaling (KEDA)

The chart's `worker.autoscaling` (OPS-5) needs KEDA, which EKS does not
bundle:

```sh
helm upgrade --install keda keda --repo https://kedacore.github.io/charts \
  --namespace keda --create-namespace
```

KEDA's PostgreSQL scaler takes its own libpq connection string (see the
chart's `worker.autoscaling.postgresConnection`); by default it is
`database.url` without `+asyncpg`.

## Database access

PostgreSQL has no public address. Backups and `psql` run from a pod:

```sh
URL=$(terraform output -raw helm_values | yq '.database.url' | sed 's/+asyncpg//; s/ssl=require/sslmode=require/')
kubectl -n annotation run psql --rm -it --restart=Never --image=postgres:16 -- psql "$URL"
```

See [docs/BACKUP.md](../../../docs/BACKUP.md) for what to back up; RDS
snapshots and point-in-time recovery cover the database itself.

## What this does not do

- **IAM sign-in to RDS and ElastiCache.** The app supports only passwords
  there (see above).
- **A NAT gateway per zone.** One zone's outage takes outbound traffic down;
  add one gateway and route table per zone if that matters.
- **VPC flow logs, S3 access logs.** Turn them on if your audit policy needs
  them; they cost per GB.
- **Budget scoping.** The budget counts resources tagged `app=annotide`
  only once `app` is an active cost allocation tag in the Billing console.

Validated with `terraform validate` and Trivy, never applied from this repo:
`terraform apply` needs your approval and your account.
