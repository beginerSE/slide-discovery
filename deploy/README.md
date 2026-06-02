# Deploying the API server to Google Cloud (Cloud Run)

This directory turns the GCP-native design (Cloud SQL + Vertex AI + Drive API
via ADC, documented in the root `replit.md`) into a reproducible deployment.

- **`../Dockerfile`** — builds the FastAPI container (runs uvicorn on `$PORT`).
- **`terraform/`** — the single source of truth for the production footprint:
  required APIs, the runtime service account + IAM roles, the optional Cloud
  SQL IAM database user, and the Cloud Run service wired with the GCP env vars
  that `config.py` reads.

In GCP mode the app authenticates entirely through **ADC** — the Cloud Run
service account below is the identity for Cloud SQL, Vertex AI, and Drive. No
key files are created or baked into the image.

---

## Prerequisites (one-time)

1. A GCP project with billing enabled, and the `gcloud` CLI + `terraform`
   installed and authenticated (`gcloud auth login`,
   `gcloud auth application-default login`).
2. A Cloud SQL **Postgres** instance (the `pgvector` and `pg_trgm` extensions
   are created automatically at startup by `db.py:init_db`). Note its
   connection name `project:region:instance`.
3. An Artifact Registry Docker repo to hold the image, e.g.:
   ```sh
   gcloud artifacts repositories create slide-search \
     --repository-format=docker --location=us-central1
   ```
4. A `SESSION_SECRET` in Secret Manager (required in all modes):
   ```sh
   printf '%s' "$(openssl rand -hex 32)" | \
     gcloud secrets create slide-search-session-secret --data-file=-
   ```
   Grant the runtime service account access **after** the first `terraform
   apply` creates it (see step 4 below), or pre-create the SA.

## Deploy

```sh
# 1. Build + push the image (run from artifacts/api-server, the build context).
cd ..
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/PROJECT/slide-search/api:latest

# 2. Provision everything.
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars   # then edit values
terraform init
terraform apply \
  -var="image=us-central1-docker.pkg.dev/PROJECT/slide-search/api:latest"
```

`terraform apply` prints the **service URL**, the **service account email**,
and the **Cloud SQL IAM user**. Redeploying a new image is just steps 1–2 again
with a new tag.

---

## Connecting Cloud SQL

The app connects through the **Cloud SQL Python Connector** (`db.py`), which
opens an authenticated connection over the Cloud SQL Admin API. The service
account therefore only needs `roles/cloudsql.client` (granted by Terraform) —
you do **not** need to attach the instance to Cloud Run or run a separate proxy
for public-IP connectivity.

**IAM database auth (default, key-less):**

1. Terraform creates the Cloud SQL IAM user for the service account
   (`create_cloud_sql_iam_user = true`) and grants
   `roles/cloudsql.instanceUser`.
2. The IAM user has *login* rights but no table privileges yet. Connect once as
   an admin and grant them (the user name is the SA email **without** the
   `.gserviceaccount.com` suffix, shown in the `cloud_sql_iam_user` output):
   ```sql
   GRANT ALL ON ALL TABLES IN SCHEMA public TO "<cloud_sql_iam_user>";
   GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "<cloud_sql_iam_user>";
   GRANT CREATE ON SCHEMA public TO "<cloud_sql_iam_user>";  -- init_db creates tables/extensions
   ALTER DEFAULT PRIVILEGES IN SCHEMA public
     GRANT ALL ON TABLES TO "<cloud_sql_iam_user>";
   ```
   The first startup runs `init_db`, which needs `CREATE` to make the `vector`/
   `pg_trgm` extensions, tables, and indexes.

**Password auth instead:** set `create_cloud_sql_iam_user = false`, provide
`cloud_sql_user`, and add `CLOUD_SQL_PASSWORD` (via Secret Manager + a small
edit to `main.tf`, or `extra_env`) together with `CLOUD_SQL_IAM_AUTH = "false"`.

**Private IP:** set `cloud_sql_private_ip = true` and give the Cloud Run
service VPC egress (a Serverless VPC Access connector or Direct VPC egress) to
the network the instance lives on.

---

## Granting Drive folder access

Drive ingest in GCP mode uses the authenticated Drive API via ADC (the same
service account). Drive permissions are **not** managed by IAM/Terraform — you
share the content with the service account like any other user:

1. Note the service account email from the `service_account_email` output.
2. In Google Drive, open the source folder (or Shared Drive) → **Share** and
   add that email with **Viewer** access.
3. The Drive API is enabled by Terraform; the app requests the
   `drive.readonly` scope at runtime. Google Slides files are exported to PPTX
   automatically during ingest.

---

## Verifying

```sh
curl -s "$(terraform output -raw service_url)/api/healthz" | jq
```

`/api/healthz` returns the resolved config (no secrets). Expect:

```json
{
  "runtimeEnv": "gcp",
  "db": "cloud_sql",
  "cloudSqlIamAuth": true,
  "gemini": "vertex_ai",
  "drive": "drive_api"
}
```

If any value is wrong, the corresponding env var/role is missing — see the
table in the root `replit.md`.
