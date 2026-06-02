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

def use_cloud_sql() -> bool:
    """Use the Cloud SQL Python Connector when an instance is configured."""
    return bool(os.environ.get("INSTANCE_CONNECTION_NAME"))


def cloud_sql_iam_auth() -> bool:
    """IAM database authentication (ADC) instead of a password.

    Defaults to ON in GCP mode (the recommended, key-less path) and OFF
    otherwise. Set ``CLOUD_SQL_IAM_AUTH`` explicitly to override.
    """
    raw = os.environ.get("CLOUD_SQL_IAM_AUTH")
    if raw is not None:
        return _truthy(raw)
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


# --- Google Drive -----------------------------------------------------------

def use_drive_api() -> bool:
    """Use the authenticated Drive API (ADC) instead of public share links."""
    raw = os.environ.get("DRIVE_API_AUTH") or os.environ.get("USE_DRIVE_API")
    if raw is not None:
        return _truthy(raw)
    return is_gcp()


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
    }


def log_config() -> None:
    """Log the selected backend per integration + hints for missing env.

    Called at startup so an accidental mode flip (or missing required env in
    GCP mode) surfaces in logs before the first runtime failure.
    """
    cfg = describe()
    log.info("runtime config: %s", cfg)
    if use_cloud_sql():
        # Mirror the fallbacks used in db.py (_make_cloud_sql_engine).
        fallbacks = {"CLOUD_SQL_DB": "PGDATABASE", "CLOUD_SQL_USER": "PGUSER"}
        missing = [
            k
            for k, pg in fallbacks.items()
            if not (os.environ.get(k) or os.environ.get(pg))
        ]
        if missing:
            log.warning("Cloud SQL selected but missing env: %s", ", ".join(missing))
    if use_vertex_ai() and not gcp_project():
        log.warning("Vertex AI selected but no GCP project resolvable (set GCP_PROJECT)")
