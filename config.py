"""Central runtime/auth configuration.

This module is the single source of truth for *which backend each external
integration talks to* and *how it authenticates*. The app runs in two modes:

* **dev (Replit)** — the default. DB via ``DATABASE_URL``, Gemini via the
  public Generative Language API + ``GEMINI_API_KEY``, and Google Drive via
  the public share-link fallback. Nothing here requires GCP.
* **gcp (production)** — DB via Cloud SQL (Cloud SQL Python Connector), Gemini
  via Vertex AI, and Drive via the authenticated Drive API. All three use
  **ADC (Application Default Credentials)**: in production the attached service
  account (Cloud Run / GCE / GKE Workload Identity) supplies credentials with
  no key file on disk. Locally you may point ``GOOGLE_APPLICATION_CREDENTIALS``
  at a service-account key for testing.

Each backend is selected independently by the presence of its env vars, so you
can flip one integration to GCP at a time. ``RUNTIME_ENV`` only changes the
*defaults* used when a per-integration toggle is left unset.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

log = logging.getLogger("config")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def runtime_env() -> str:
    """Return the coarse runtime mode: ``"gcp"`` or ``"dev"``.

    This is an *explicit* master switch driven solely by ``RUNTIME_ENV``
    (defaults to ``"dev"``). It is intentionally NOT inferred from individual
    integration signals like ``INSTANCE_CONNECTION_NAME`` — otherwise enabling
    one backend (e.g. Cloud SQL) would implicitly flip unrelated backends
    (Drive/Vertex) and break a partial rollout. Each integration is selected
    independently: Cloud SQL keys off ``INSTANCE_CONNECTION_NAME`` directly,
    and Vertex/Drive read their own toggles (falling back to this master
    switch only when their toggle is unset). Setting ``RUNTIME_ENV=gcp`` is
    the one-flag way to turn the whole app GCP-native.
    """
    explicit = (os.environ.get("RUNTIME_ENV") or "").strip().lower()
    if explicit in ("gcp", "prod", "production"):
        return "gcp"
    return "dev"


def is_gcp() -> bool:
    return runtime_env() == "gcp"


# --- GCP project / location -------------------------------------------------

def gcp_project() -> str | None:
    return (
        os.environ.get("GCP_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT_ID")
        or None
    )


def gcp_location() -> str:
    return (
        os.environ.get("GCP_LOCATION")
        or os.environ.get("GOOGLE_CLOUD_LOCATION")
        or "us-central1"
    )


# --- Cloud SQL --------------------------------------------------------------

def instance_connection_name() -> str | None:
    """Cloud SQL instance connection name (``project:region:instance``).

    Accepts ``INSTANCE_CONNECTION_NAME`` or the alias
    ``CLOUD_SQL_CONNECTION_NAME``.
    """
    return (
        os.environ.get("INSTANCE_CONNECTION_NAME")
        or os.environ.get("CLOUD_SQL_CONNECTION_NAME")
        or None
    )


def use_cloud_sql() -> bool:
    """Use the Cloud SQL Python Connector when an instance is configured."""
    return bool(instance_connection_name())


def cloud_sql_db() -> str | None:
    """Cloud SQL database name. Accepts ``CLOUD_SQL_DB`` / ``DB_NAME`` / ``PGDATABASE``."""
    return (
        os.environ.get("CLOUD_SQL_DB")
        or os.environ.get("DB_NAME")
        or os.environ.get("PGDATABASE")
        or None
    )


def cloud_sql_user() -> str | None:
    """Cloud SQL user. Accepts ``CLOUD_SQL_USER`` / ``DB_USER`` / ``PGUSER``."""
    return (
        os.environ.get("CLOUD_SQL_USER")
        or os.environ.get("DB_USER")
        or os.environ.get("PGUSER")
        or None
    )


def cloud_sql_password() -> str | None:
    """Cloud SQL password. Accepts ``CLOUD_SQL_PASSWORD`` / ``DB_PASSWORD`` / ``PGPASSWORD``."""
    return (
        os.environ.get("CLOUD_SQL_PASSWORD")
        or os.environ.get("DB_PASSWORD")
        or os.environ.get("PGPASSWORD")
        or None
    )


def cloud_sql_iam_auth() -> bool:
    """IAM database authentication (ADC) instead of a password.

    Resolution order:
    1. ``CLOUD_SQL_IAM_AUTH`` if set explicitly wins.
    2. If an explicit Cloud SQL password (``CLOUD_SQL_PASSWORD`` / ``DB_PASSWORD``)
       is supplied, default to password auth (IAM off) — otherwise a password
       set in GCP mode would be silently ignored.
    3. Otherwise default to ON in GCP mode (the key-less ADC path) and OFF
       elsewhere.
    """
    raw = os.environ.get("CLOUD_SQL_IAM_AUTH")
    if raw is not None:
        return _truthy(raw)
    # An *explicit* Cloud SQL password implies password auth. PGPASSWORD is
    # intentionally excluded — it is commonly set by generic Postgres tooling
    # (e.g. the Replit dev DB) and must not silently flip the auth mode.
    if os.environ.get("CLOUD_SQL_PASSWORD") or os.environ.get("DB_PASSWORD"):
        return False
    return is_gcp()


def cloud_sql_private_ip() -> bool:
    return _truthy(os.environ.get("CLOUD_SQL_PRIVATE_IP"))


# --- Vertex AI (Gemini) -----------------------------------------------------

def use_vertex_ai() -> bool:
    """Route Gemini (embeddings + extraction) through Vertex AI via ADC."""
    raw = os.environ.get("USE_VERTEX_AI") or os.environ.get(
        "GOOGLE_GENAI_USE_VERTEXAI"
    )
    if raw is not None:
        return _truthy(raw)
    # Auto-enable in GCP mode only when a project is resolvable; Vertex
    # requires a project id.
    return is_gcp() and gcp_project() is not None


# --- Thumbnail storage (Cloud Storage) --------------------------------------

# Default GCS bucket for slide thumbnails in production. Lives in the same
# project as Cloud Run; ADC (the runtime service account) supplies credentials.
DEFAULT_THUMBNAIL_BUCKET = "slide_discovery"


def thumbnail_bucket() -> str | None:
    """GCS bucket for slide thumbnails. Accepts ``THUMBNAIL_BUCKET`` /
    ``GCS_THUMBNAIL_BUCKET``.

    Resolution order:
    1. An explicit ``THUMBNAIL_BUCKET`` / ``GCS_THUMBNAIL_BUCKET`` env var.
    2. In gcp (production) mode, defaults to ``slide_discovery`` — a bucket in
       the same project as Cloud Run — so thumbnails persist in Cloud Storage
       (surviving instance restarts, shared across instances) with no extra
       config.
    3. Otherwise (dev) unset → thumbnails stay on local disk.
    """
    explicit = (
        os.environ.get("THUMBNAIL_BUCKET")
        or os.environ.get("GCS_THUMBNAIL_BUCKET")
    )
    if explicit:
        return explicit
    if is_gcp():
        return DEFAULT_THUMBNAIL_BUCKET
    return None


def use_gcs_thumbnails() -> bool:
    return bool(thumbnail_bucket())


def thumbnail_prefix() -> str:
    """Object-name prefix (folder) for thumbnails within the bucket."""
    return (os.environ.get("THUMBNAIL_PREFIX") or "thumbnails").strip("/")


# --- Google Drive -----------------------------------------------------------

def use_drive_api() -> bool:
    """Use the authenticated Drive API (ADC) instead of public share links."""
    raw = os.environ.get("DRIVE_API_AUTH") or os.environ.get("USE_DRIVE_API")
    if raw is not None:
        return _truthy(raw)
    return is_gcp()


# --- Cloud IAP (Identity-Aware Proxy) ----------------------------------------

def iap_audience() -> str | None:
    """Expected audience of the IAP assertion JWT.

    ``/projects/PROJECT_NUMBER/apps/PROJECT_ID`` (App Engine) or
    ``/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID`` (backend
    service, Cloud Run 経由). 設定されている場合のみ IAP 自動ログインが有効。
    """
    v = (os.environ.get("IAP_AUDIENCE") or "").strip()
    return v or None


def iap_enabled() -> bool:
    return bool(iap_audience())


# --- Confluence -------------------------------------------------------------

def confluence_base_url() -> str | None:
    """Confluence Cloud site base URL (e.g. ``https://your-site.atlassian.net``).

    Stored without a trailing slash and without the ``/wiki`` suffix. Same path
    in dev and prod — Confluence always authenticates with an API token (there
    is no GCP-native variant), mirroring the Gemini dev/prod split.
    """
    from confluence_settings import cached_base_url

    db = cached_base_url()
    if db:
        return db
    v = (os.environ.get("CONFLUENCE_BASE_URL") or "").strip().rstrip("/")
    return v or None


def confluence_email() -> str | None:
    """Atlassian account email that owns the API token (Basic-auth username).

    A DB-stored value (set by an admin in the 設定 screen) overrides the env."""
    from confluence_settings import cached_email

    db = cached_email()
    if db:
        return db
    v = (os.environ.get("CONFLUENCE_EMAIL") or "").strip()
    return v or None


def confluence_api_token() -> str | None:
    """Confluence Cloud API token (Basic-auth password).

    A DB-stored value (set by an admin in the 設定 screen) overrides the env."""
    from confluence_settings import cached_api_token

    db = cached_api_token()
    if db:
        return db
    v = (os.environ.get("CONFLUENCE_API_TOKEN") or "").strip()
    return v or None


def confluence_enabled() -> bool:
    """True only when all three Confluence settings are present."""
    return bool(
        confluence_base_url() and confluence_email() and confluence_api_token()
    )


# --- ADC --------------------------------------------------------------------

@lru_cache(maxsize=8)
def _adc(scopes: tuple[str, ...]) -> Any:
    import google.auth

    creds, project = google.auth.default(
        scopes=list(scopes) if scopes else None
    )
    log.info(
        "loaded ADC credentials (project=%s, scopes=%s)",
        project or gcp_project(),
        ",".join(scopes) if scopes else "default",
    )
    return creds


def adc_credentials(scopes: list[str] | None = None) -> Any:
    """Return Application Default Credentials for the given OAuth scopes.

    Raises a clear error (via ``google.auth.default``) when no ADC is
    available — e.g. running locally without ``GOOGLE_APPLICATION_CREDENTIALS``
    and outside GCP.
    """
    return _adc(tuple(scopes or ()))


def describe() -> dict:
    """Snapshot of the resolved configuration (no secrets), for /healthz."""
    return {
        "runtimeEnv": runtime_env(),
        "gcpProject": gcp_project(),
        "gcpLocation": gcp_location() if is_gcp() else None,
        "db": "cloud_sql" if use_cloud_sql() else "database_url",
        "cloudSqlIamAuth": cloud_sql_iam_auth() if use_cloud_sql() else None,
        "gemini": "vertex_ai" if use_vertex_ai() else "generative_language_api",
        "drive": "drive_api" if use_drive_api() else "public_share_link",
        "thumbnails": "gcs" if use_gcs_thumbnails() else "local_disk",
        "confluence": "configured" if confluence_enabled() else "disabled",
        "iapAuth": "enabled" if iap_enabled() else "disabled",
    }


def log_config() -> None:
    """Log the selected backend per integration + hints for missing env.

    Called at startup so an accidental mode flip (or missing required env in
    GCP mode) surfaces in logs before the first runtime failure.
    """
    cfg = describe()
    log.info("runtime config: %s", cfg)
    if use_cloud_sql():
        # Mirror the resolution used in db.py (_make_cloud_sql_engine).
        missing = []
        if not cloud_sql_db():
            missing.append("CLOUD_SQL_DB/DB_NAME")
        if not cloud_sql_user():
            missing.append("CLOUD_SQL_USER/DB_USER")
        if not cloud_sql_iam_auth() and not cloud_sql_password():
            missing.append("CLOUD_SQL_PASSWORD/DB_PASSWORD")
        if missing:
            log.warning("Cloud SQL selected but missing env: %s", ", ".join(missing))
    if use_vertex_ai() and not gcp_project():
        log.warning("Vertex AI selected but no GCP project resolvable (set GCP_PROJECT)")
