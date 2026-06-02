output "service_url" {
  description = "Public URL of the deployed Cloud Run service."
  value       = google_cloud_run_v2_service.api.uri
}

output "service_account_email" {
  description = "Runtime service account. Share the Drive folder with this and grant DB privileges to its IAM user."
  value       = google_service_account.api.email
}

output "cloud_sql_iam_user" {
  description = "Cloud SQL IAM database user name (when IAM auth is enabled)."
  value       = var.create_cloud_sql_iam_user ? trimsuffix(google_service_account.api.email, ".gserviceaccount.com") : null
}
