# AWS infrastructure for the annotation platform's Helm chart (§15, SEC-1).
#
# One VPC across three availability zones; EKS runs the chart. Nodes,
# PostgreSQL and Redis sit in private subnets with no public address. The
# only paid networking is one NAT gateway (image pulls, model endpoints) and,
# once the ingress controller is installed, its load balancer. The cluster
# reaches S3 through a free gateway endpoint. TLS everywhere; one KMS key
# encrypts Kubernetes secrets, the database, Redis and the buckets. The pods
# sign in to S3 and Secrets Manager with an IAM role (IRSA): no access keys
# exist anywhere. See README.md for the apply / install steps.

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  name            = "${var.prefix}-annotation"
  service_account = var.helm_release_name
  azs             = slice(data.aws_availability_zones.available.names, 0, 3)
  # 10.40.0.0/20, 10.40.16.0/20, 10.40.32.0/20: nodes and pods (VPC CNI gives
  # pods addresses from the node's subnet), PostgreSQL, Redis.
  private_cidrs = [for i in range(3) : cidrsubnet(var.vpc_cidr, 4, i)]
  # 10.40.48.0/24 ...: load balancers and the NAT gateway only.
  public_cidrs = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, 48 + i)]
  # Bucket names are global.
  bucket_prefix = "${local.name}-${random_string.suffix.result}"
  # `awssecrets://` references the workload may read (AUTH-7).
  secrets_prefix = "${var.prefix}/annotation/"
  oidc_issuer    = trimprefix(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://")
}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

# --- Encryption ------------------------------------------------------------

resource "aws_kms_key" "this" {
  description             = "${local.name}: EKS secrets, RDS, ElastiCache, S3"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "this" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.this.key_id
}

# --- Network ---------------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name}-vpc" }
}

# Nothing uses the default security group; leave it with no rules.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name}-igw" }
}

resource "aws_subnet" "public" {
  count             = 3
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = local.public_cidrs[count.index]

  tags = {
    Name                     = "${local.name}-public-${local.azs[count.index]}"
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_subnet" "private" {
  count             = 3
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = local.private_cidrs[count.index]

  tags = {
    Name                              = "${local.name}-private-${local.azs[count.index]}"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 3
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One NAT gateway, so the cluster has one fixed outbound address (model
# endpoints and webhook receivers can allow-list it). A zone outage takes
# egress down with it; add one per zone if that matters.
resource "aws_eip" "nat" {
  domain = "vpc"

  tags = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id

  tags = { Name = "${local.name}-nat" }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = 3
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Free: S3 traffic from the cluster bypasses the NAT gateway, and the bucket
# policy recognises it by this endpoint's id.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name}-s3" }
}

# --- EKS -------------------------------------------------------------------

resource "aws_iam_role" "cluster" {
  name = "${local.name}-eks-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

# Envelope encryption of Kubernetes secrets with the KMS key.
resource "aws_iam_role_policy" "cluster_kms" {
  name = "kms"
  role = aws_iam_role.cluster.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Encrypt", "kms:Decrypt", "kms:ListGrants", "kms:DescribeKey"]
      Resource = aws_kms_key.this.arn
    }]
  })
}

resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${local.name}-eks/cluster"
  retention_in_days = 30
}

# The public endpoint is restricted by var.api_authorized_ip_ranges; it stays
# open only when that is left empty, which README.md tells production not to
# do. Nodes use the private endpoint.
#trivy:ignore:AVD-AWS-0040
#trivy:ignore:AVD-AWS-0041
resource "aws_eks_cluster" "this" {
  name     = "${local.name}-eks"
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  # IAM access entries only (cluster_admin_arns); no aws-auth ConfigMap and
  # no implicit admin for whoever ran terraform.
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  vpc_config {
    subnet_ids              = aws_subnet.private[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = length(var.api_authorized_ip_ranges) > 0 ? var.api_authorized_ip_ranges : ["0.0.0.0/0"]
  }

  encryption_config {
    resources = ["secrets"]

    provider {
      key_arn = aws_kms_key.this.arn
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_iam_role_policy.cluster_kms,
    aws_cloudwatch_log_group.cluster,
  ]
}

resource "aws_eks_access_entry" "admins" {
  for_each      = var.cluster_admin_arns
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
}

resource "aws_eks_access_policy_association" "admins" {
  for_each      = var.cluster_admin_arns
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.admins[each.key].principal_arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}

resource "aws_iam_role" "node" {
  name = "${local.name}-eks-node"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEKS_CNI_Policy", "AmazonEC2ContainerRegistryReadOnly"])
  role       = aws_iam_role.node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/${each.value}"
}

# IMDSv2 with a hop limit of one: pods cannot borrow the node's role, they get
# only their own (IRSA). Root volumes are encrypted with the AWS-managed EBS
# key, which the Auto Scaling service can use without a key policy change.
resource "aws_launch_template" "node" {
  name_prefix            = "${local.name}-node-"
  update_default_version = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = 50
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name}-node" }
  }
}

resource "aws_eks_node_group" "system" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "system"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id
  ami_type        = "AL2023_x86_64_STANDARD"
  instance_types  = var.node_instance_types

  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }

  scaling_config {
    desired_size = var.node_count.min
    min_size     = var.node_count.min
    max_size     = var.node_count.max
  }

  update_config {
    max_unavailable_percentage = 33
  }

  # A cluster autoscaler owns the current size once installed.
  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.node]
}

# The VPC CNI enforces the chart's NetworkPolicies once its network policy
# agent is on.
resource "aws_eks_addon" "this" {
  for_each = {
    vpc-cni    = jsonencode({ enableNetworkPolicy = "true" })
    kube-proxy = null
    coredns    = null
  }
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  configuration_values        = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  # CoreDNS needs nodes to schedule on.
  depends_on = [aws_eks_node_group.system]
}

# --- Workload identity (IRSA) ----------------------------------------------

resource "aws_iam_openid_connect_provider" "eks" {
  url            = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "workload" {
  name = "${local.name}-workload"

  # Exactly the chart's service account, nothing else in the cluster.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:${var.kubernetes_namespace}:${local.service_account}"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "workload" {
  name = "annotation"
  role = aws_iam_role.workload.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Buckets"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketCORS"]
        Resource = [for bucket in aws_s3_bucket.this : bucket.arn]
      },
      {
        # Presigned URLs carry this role's permissions (ARC-3).
        Sid    = "Objects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts",
        ]
        Resource = [for bucket in aws_s3_bucket.this : "${bucket.arn}/*"]
      },
      {
        Sid      = "BucketKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.this.arn
      },
      {
        # `awssecrets://<prefix>/annotation/...` references (AUTH-7).
        Sid      = "SecretReferences"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = "arn:${data.aws_partition.current.partition}:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${local.secrets_prefix}*"
      },
    ]
  })
}

# --- PostgreSQL (private; TLS required) ------------------------------------

resource "aws_db_subnet_group" "this" {
  name       = "${local.name}-pg"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "postgres" {
  name        = "${local.name}-pg"
  description = "PostgreSQL: the EKS cluster only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "postgres" {
  security_group_id            = aws_security_group.postgres.id
  description                  = "EKS nodes and pods"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

resource "aws_db_parameter_group" "this" {
  name   = "${local.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }
}

# The app signs in with a password (APP_DATABASE_AUTH=password): it has no
# RDS IAM token sign-in. The database is unreachable from outside the VPC.
resource "random_password" "postgres" {
  length  = 40
  special = false
}

# citext and pgcrypto are available on RDS without allow-listing; the
# migrations create them.
resource "aws_db_instance" "this" {
  identifier                      = "${local.name}-pg"
  engine                          = "postgres"
  engine_version                  = "16"
  instance_class                  = var.postgres_instance_class
  allocated_storage               = var.postgres_storage_gb
  max_allocated_storage           = var.postgres_storage_gb * 4
  storage_type                    = "gp3"
  storage_encrypted               = true
  kms_key_id                      = aws_kms_key.this.arn
  db_name                         = "annotation"
  username                        = "annotation"
  password                        = random_password.postgres.result
  parameter_group_name            = aws_db_parameter_group.this.name
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.postgres.id]
  publicly_accessible             = false
  multi_az                        = var.postgres_multi_az
  backup_retention_period         = 14
  copy_tags_to_snapshot           = true
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${local.name}-pg-final"
  auto_minor_version_upgrade      = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.this.arn
}

# --- Redis (private; TLS only) ---------------------------------------------

resource "aws_elasticache_subnet_group" "this" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "Redis: the EKS cluster only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "redis" {
  security_group_id            = aws_security_group.redis.id
  description                  = "EKS nodes and pods"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

resource "random_password" "redis" {
  length  = 64
  special = false
}

# ElastiCache's TLS certificates chain to a public CA, so `rediss://` verifies
# with the system trust store and no extra configuration.
resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = "${local.name}-redis"
  description                = "${local.name} job queue"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  port                       = 6379
  num_cache_clusters         = var.redis_node_count
  automatic_failover_enabled = var.redis_node_count > 1
  multi_az_enabled           = var.redis_node_count > 1
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.this.arn
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result
  snapshot_retention_limit   = 7
  auto_minor_version_upgrade = true
}

# --- Media storage ---------------------------------------------------------

resource "aws_s3_bucket" "this" {
  for_each = toset(["media", "results", "cache"])
  bucket   = "${local.bucket_prefix}-${each.value}"
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = aws_s3_bucket.this
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  rule {
    bucket_key_enabled = true

    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this.arn
    }
  }
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Overwritten and deleted objects stay recoverable for 14 days.
resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  rule {
    id     = "noncurrent-14-days"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 14
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

# The browser reads and uploads media straight from the bucket (ARC-3,
# SRC-7). S3 answers the OPTIONS preflight itself.
resource "aws_s3_bucket_cors_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  cors_rule {
    allowed_origins = [var.public_url]
    allowed_methods = ["GET", "HEAD", "PUT"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

# TLS only. With storage_allowed_ip_ranges set, objects are readable and
# writable only from those addresses (browsers) or through the VPC endpoint
# (the cluster); presigned URLs are still required either way.
data "aws_iam_policy_document" "bucket" {
  for_each = aws_s3_bucket.this

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [each.value.arn, "${each.value.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  dynamic "statement" {
    for_each = length(var.storage_allowed_ip_ranges) > 0 ? [1] : []
    content {
      sid       = "DenyObjectsOutsideAllowList"
      effect    = "Deny"
      actions   = ["s3:GetObject", "s3:PutObject"]
      resources = ["${each.value.arn}/*"]

      principals {
        type        = "*"
        identifiers = ["*"]
      }

      condition {
        test     = "NotIpAddress"
        variable = "aws:SourceIp"
        values   = var.storage_allowed_ip_ranges
      }

      condition {
        test     = "StringNotEquals"
        variable = "aws:SourceVpce"
        values   = [aws_vpc_endpoint.s3.id]
      }
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  policy   = data.aws_iam_policy_document.bucket[each.key].json

  depends_on = [aws_s3_bucket_public_access_block.this]
}

# --- Application secrets ---------------------------------------------------

resource "random_password" "secret_key" {
  length  = 64
  special = false
}

locals {
  database_url = format(
    "postgresql+asyncpg://%s:%s@%s:%d/%s?ssl=require",
    aws_db_instance.this.username,
    urlencode(random_password.postgres.result),
    aws_db_instance.this.address,
    aws_db_instance.this.port,
    aws_db_instance.this.db_name,
  )
  redis_url = format(
    "rediss://:%s@%s:%d/0",
    urlencode(random_password.redis.result),
    aws_elasticache_replication_group.this.primary_endpoint_address,
    aws_elasticache_replication_group.this.port,
  )
}

# --- Budget ----------------------------------------------------------------

# Counts resources tagged app=annotide, once `app` is an active cost
# allocation tag (Billing console, or `aws ce update-cost-allocation-tags-status`).
resource "aws_budgets_budget" "this" {
  count        = var.budget_amount > 0 ? 1 : 0
  name         = "${local.name}-budget"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_amount)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:app$annotide"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.budget_contact_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.budget_contact_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.budget_contact_emails
  }
}
