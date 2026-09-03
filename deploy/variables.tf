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

# --- Thumbnail storage (Cloud Storage) --------------------------------------

variable "thumbnail_bucket" {
  type        = string
  description = "GCS bucket name for slide thumbnails (persists across instances/restarts). Defaults to `slide_discovery`, matching config.py's gcp default. Do NOT set this empty for a gcp deployment: the app still defaults to `slide_discovery` at runtime, so an empty value only skips creating/IAM-wiring the bucket and breaks thumbnails. Override only to use a different bucket name."
  default     = "slide_discovery"
}

variable "create_thumbnail_bucket" {
  type        = bool
  description = "Create the thumbnail GCS bucket. Set false if the bucket already exists / is managed elsewhere (IAM is still granted)."
  default     = true
}

variable "thumbnail_bucket_location" {
  type        = string
  description = "Location for the thumbnail bucket (defaults to the Cloud Run region)."
  default     = ""
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

variable "startup_cpu_boost" {
  type        = bool
  description = "Temporarily allocate extra CPU while a Cloud Run instance starts."
  default     = true
}

variable "memory" {
  type = string
  # The ingest pipeline shells out to headless LibreOffice (soffice) for the
  # PPTX -> PDF conversion, which has a large working set. Plus python-pptx
  # loads the whole deck in memory. 512Mi (the gcloud default) is too small and
  # trips Cloud Run's OOM ("Memory limit of 512 MiB exceeded"); 2Gi gives
  # headroom for large decks.
  default = "2Gi"
}

variable "allow_unauthenticated" {
  type        = bool
  description = "Expose the service publicly (the app still enforces its own session auth). Set false when fronting with IAP (see iap_audience)."
  default     = true
}

variable "iap_audience" {
  type        = string
  description = <<-EOT
    IAP を使う場合に設定する、IAP アサーション JWT の expected audience。
    形式: /projects/PROJECT_NUMBER/global/backendServices/BACKEND_SERVICE_ID
    （GCP コンソール → Security → Identity-Aware Proxy → 対象リソースの
    「⋮ → Get JWT audience code」で確認できる）。
    設定するとアプリは IAP 自動ログインモードになる: 検証済み Google
    アカウントでの自動アカウント作成・ログインが有効になり、ローカルの
    新規登録・パスワードログインは無効化される。
    IAP を使う場合は allow_unauthenticated = false とし、IAP のバックエンド
    （ロードバランサ経由）だけがサービスを呼べるようにすること。
  EOT
  default     = ""

  validation {
    condition     = var.iap_audience == "" || can(regex("^/projects/", var.iap_audience))
    error_message = "iap_audience must start with /projects/ (JWT audience code from the IAP console)."
  }
}

variable "extra_env" {
  type        = map(string)
  description = "Additional plain env vars to set on the service (e.g. overrides)."
  default     = {}
}
