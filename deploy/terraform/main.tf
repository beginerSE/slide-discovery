# Reproducible Cloud Run deployment for the 提案スライド検索 API server.
#
# This is the single source of truth for the production GCP footprint:
#   * required Google APIs
#   * a dedicated runtime service account + the documented IAM roles
#   * (optionally) the Cloud SQL IAM database user for the service account
#   * the Cloud Run service itself, wired with the GCP-native env vars that
#     config.py reads (RUNTIME_ENV=gcp, GCP_PROJECT, INSTANCE_CONNECTION_NAME…)
#
# The container image must be built and pushed first (see ../README.md), then:
#   terraform apply -var="image=REGION-docker.pkg.dev/PROJECT/REPO/api:TAG"

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  # APIs the app touches at runtime. Drive is only needed when ingesting from
  # Drive; Vertex only when USE_VERTEX_AI is on — enabling them is harmless.
  services = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "aiplatform.googleapis.com",
    "drive.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
  ]
}

resource "google_project_service" "enabled" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

# --- Runtime service account (the app's ADC identity in production) ----------

resource "google_service_account" "api" {
  account_id   = var.service_account_id
  display_name = "提案スライド検索 API runtime"
  description  = "ADC identity for the Cloud Run API: Cloud SQL, Vertex AI, Drive."
}

# Documented roles (replit.md → GCP prerequisites):
#   cloudsql.client       — open Cloud SQL connections via the Python connector
#   cloudsql.instanceUser — required for IAM database authentication
#   aiplatform.user       — call Vertex AI (Gemini embeddings + extraction)
locals {
  sa_roles = [
    "roles/cloudsql.client",
    "roles/cloudsql.instanceUser",
    "roles/aiplatform.user",
  ]
}

resource "google_project_iam_member" "api_roles" {
  for_each = toset(local.sa_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.api.email}"
}

# Cloud SQL IAM database user for the service account. Required when
# CLOUD_SQL_IAM_AUTH is on (the default in GCP mode). After this is created
# you must still GRANT table privileges to the user inside Postgres — see
# ../README.md. Disable with create_cloud_sql_iam_user=false if you use
# password auth instead.
resource "google_sql_user" "iam_sa" {
  count = var.create_cloud_sql_iam_user ? 1 : 0
  # IAM SA users are named by the SA email WITHOUT the ".gserviceaccount.com".
  name     = trimsuffix(google_service_account.api.email, ".gserviceaccount.com")
  instance = var.cloud_sql_instance_name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"

  depends_on = [google_project_service.enabled]
}

# --- Cloud Run service ------------------------------------------------------

resource "google_cloud_run_v2_service" "api" {
  name     = var.service_name
  location = var.region

  template {
    service_account = google_service_account.api.email

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        # Keep the instance warm so the APScheduler hourly ingest + 5-min
        # embedding backfill keep running between requests.
        cpu_idle = false
      }

      # GCP-native backend selection (read by config.py). RUNTIME_ENV=gcp is
      # the master switch; the rest target the specific Cloud SQL instance.
      env {
        name  = "RUNTIME_ENV"
        value = "gcp"
      }
      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_LOCATION"
        value = var.region
      }
      env {
        name  = "INSTANCE_CONNECTION_NAME"
        value = var.instance_connection_name
      }
      env {
        name  = "CLOUD_SQL_DB"
        value = var.cloud_sql_db
      }
      env {
        name  = "CLOUD_SQL_USER"
        # For IAM auth the DB user is the SA email without the domain suffix.
        value = var.create_cloud_sql_iam_user ? trimsuffix(google_service_account.api.email, ".gserviceaccount.com") : var.cloud_sql_user
      }
      env {
        name  = "CLOUD_SQL_PRIVATE_IP"
        value = var.cloud_sql_private_ip ? "true" : "false"
      }

      # SESSION_SECRET is required in all modes. Sourced from Secret Manager
      # so it never lives in this config or in plaintext env.
      env {
        name = "SESSION_SECRET"
        value_source {
          secret_key_ref {
            secret  = var.session_secret_name
            version = "latest"
          }
        }
      }

      # Extra env (e.g. CLOUD_SQL_IAM_AUTH overrides, USE_VERTEX_AI) if needed.
      dynamic "env" {
        for_each = var.extra_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_project_iam_member.api_roles,
  ]
}

# Allow public (unauthenticated) access to the API when requested. The app
# enforces its own session-based auth, so this only exposes the HTTP surface.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.allow_unauthenticated ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
