output "region" {
  description = "AWS region holding everything."
  value       = var.region
}

output "eks_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.this.name
}

output "get_credentials_command" {
  description = "Points kubectl and helm at the cluster (as one of cluster_admin_arns)."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${aws_eks_cluster.this.name}"
}

output "bucket_names" {
  description = "Media buckets: media, results, cache."
  value       = { for key, bucket in aws_s3_bucket.this : key => bucket.bucket }
}

output "secrets_prefix" {
  description = "Secrets Manager names the workload may read, for awssecrets:// references."
  value       = local.secrets_prefix
}

output "postgres_endpoint" {
  description = "PostgreSQL host; reachable from inside the VPC only."
  value       = aws_db_instance.this.address
}

output "nat_public_ip" {
  description = "The cluster's outbound address, for allow-lists on model endpoints and webhook receivers."
  value       = aws_eip.nat.public_ip
}

output "workload_role_arn" {
  description = "IAM role the pods sign in to AWS with (IRSA)."
  value       = aws_iam_role.workload.arn
}

output "connector_config" {
  description = "Connector to register in the app for the media bucket (identity iam_role, no secret)."
  value = {
    type          = "s3"
    identity_type = "iam_role"
    config = {
      bucket        = aws_s3_bucket.this["media"].bucket
      region        = var.region
      identity_type = "iam_role"
    }
  }
}

output "helm_values" {
  description = "Values for the annotide chart: terraform output -raw helm_values > values.aws.yaml"
  sensitive   = true
  value = yamlencode({
    publicUrl = var.public_url
    database  = { url = local.database_url }
    redis     = { url = local.redis_url }
    secrets   = { secretKey = random_password.secret_key.result }
    config = {
      # Default region for awssecrets:// references without ?region=.
      APP_AWS_REGION = var.region
      # Pods (and so the ingress controller) take addresses from the private
      # subnets: their X-Forwarded-For carries the client address for the
      # audit log and rate limits (SEC-3).
      APP_TRUSTED_PROXIES = join(",", local.private_cidrs)
    }
    serviceAccount = {
      create = true
      name   = var.helm_release_name
      annotations = {
        "eks.amazonaws.com/role-arn" = aws_iam_role.workload.arn
      }
    }
  })
}
