output "state_bucket" {
  description = "Bucket del estado remoto (usar en infra/envs/backends/*.s3.tfbackend)."
  value       = aws_s3_bucket.state.bucket
}

output "state_bucket_versioning" {
  value = aws_s3_bucket_versioning.state.versioning_configuration[0].status
}
