variable "project_id" {
  type        = string
  description = "Target GCP project id."
}

variable "region" {
  type        = string
  description = "Cloud Run region (also used as GCP_LOCATION for Vertex AI)."
  default     = "us-central1"
}

variable "image" {
  type        = string
  description = "Fully-qualified container image, e.g. us-central1-docker.pkg.dev/PROJECT/REPO/api:TAG. Build/push first (see README)."
}

variable "service_name" {
  type        = string
  description = "Cloud Run service name."
  default     = "slide-search-api"
}

variable "service_account_id" {
  type        = string
  description = "Account id (local part) for the runtime service account."
  default     = "slide-search-api"
}

# --- Cloud SQL --------------------------------------------------------------

variable "instance_connection_name" {
  type        = string
  description = "Cloud SQL instance connection name: project:region:instance."
}

variable "cloud_sql_instance_name" {
  type        = string
  description = "Short Cloud SQL instance name (last segment of the connection name). Used to create the IAM DB user."
}

variable "cloud_sql_db" {
  type        = string
  description = "Cloud SQL database name."
}

variable "cloud_sql_user" {
  type        = string
  description = "DB user when NOT using IAM auth (password auth). Ignored when create_cloud_sql_iam_user is true."
  default     = ""
}

variable "create_cloud_sql_iam_user" {
  type        = bool
  description = "Create a Cloud SQL IAM database user for the service account (key-less IAM auth, the GCP-mode default)."
  default     = true
}

variable "cloud_sql_private_ip" {
  type        = bool
  description = "Connect to Cloud SQL over private IP (requires VPC connectivity)."
  default     = false
}

# --- Secrets ----------------------------------------------------------------

variable "session_secret_name" {
  type        = string
  description = "Secret Manager secret id holding SESSION_SECRET."
  default     = "slide-search-session-secret"
}

# --- Service shape ----------------------------------------------------------

variable "min_instances" {
  type        = number
  description = "Minimum Cloud Run instances. Keep >=1 so the background scheduler stays alive."
  default     = 1
}

variable "max_instances" {
  type    = number
  default = 4
}

variable "cpu" {
  type    = string
  default = "1"
}

variable "memory" {
  type    = string
  default = "1Gi"
}

variable "allow_unauthenticated" {
  type        = bool
  description = "Expose the service publicly (the app still enforces its own session auth)."
  default     = true
}

variable "extra_env" {
  type        = map(string)
  description = "Additional plain env vars to set on the service (e.g. overrides)."
  default     = {}
}
