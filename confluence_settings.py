"""Admin-editable Confluence connection settings, persisted in the DB.

The three Confluence settings (base URL, account email, API token) can be
edited by an admin from the 設定 screen instead of being baked into environment
variables. They are stored in the ``app_state`` key/value table (mirroring
``guide.py``) so they survive restarts and are shared across instances.

Precedence (resolved in ``config.py``): a DB value, when present, overrides the
matching environment variable; otherwise the env var is used. This keeps
existing env-based deployments working while letting an admin take over from the
UI.

The **API token is a secret**, so it is encrypted at rest with Fernet using a
key derived from ``SESSION_SECRET`` (already required in every mode). The base
URL and email are not secret and are stored as plain text. If ``SESSION_SECRET``
changes, previously stored tokens become undecryptable and are treated as unset
(the admin simply re-enters the token).

``config`` reads the resolved values synchronously, so this module keeps a small
in-process cache refreshed from the DB at startup and after every save/clear.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("api.confluence_settings")

KEY_BASE_URL = "confluence_base_url"
KEY_EMAIL = "confluence_email"
KEY_TOKEN = "confluence_api_token"  # stored encrypted

# In-process snapshot of the DB-stored settings, populated by ``refresh_cache``.
# ``None`` for a field means "no DB value" (config falls back to the env var).
_cache: dict[str, str | None] = {
    "base_url": None,
    "email": None,
    "api_token": None,
}


def _fernet() -> Fernet:
    """Fernet built from a key derived from ``SESSION_SECRET``."""
    secret = (os.environ.get("SESSION_SECRET") or "").encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def _encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def _decrypt(blob: str | None) -> str | None:
    """Decrypt a stored token, or ``None`` if absent/undecryptable."""
    if not blob:
        return None
    try:
        return _fernet().decrypt(blob.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, Exception) as exc:  # noqa: BLE001
        log.warning("could not decrypt stored Confluence token: %s", exc)
        return None


def _clean_url(value: str | None) -> str | None:
    v = (value or "").strip().rstrip("/")
    return v or None


def _clean(value: str | None) -> str | None:
    v = (value or "").strip()
    return v or None


# --- sync cache accessors (read by config.py) --------------------------------

def cached_base_url() -> str | None:
    return _cache["base_url"]


def cached_email() -> str | None:
    return _cache["email"]


def cached_api_token() -> str | None:
    return _cache["api_token"]


# --- async DB operations -----------------------------------------------------

async def refresh_cache(session: AsyncSession) -> None:
    """Reload the in-process cache from the DB. Call at startup and after a
    save/clear. Never raises — a failure leaves the previous cache in place."""
    from db import AppState

    try:
        base = await session.get(AppState, KEY_BASE_URL)
        email = await session.get(AppState, KEY_EMAIL)
        tok = await session.get(AppState, KEY_TOKEN)
        _cache["base_url"] = _clean_url(base.value if base else None)
        _cache["email"] = _clean(email.value if email else None)
        _cache["api_token"] = _decrypt(tok.value if tok else None)
    except Exception as exc:  # noqa: BLE001
        log.warning("refresh_cache failed: %s", exc)


async def get_settings(session: AsyncSession) -> dict:
    """DB-stored settings for the admin form. Never returns the token value —
    only whether one is stored — so the secret is not echoed back to the UI."""
    from db import AppState

    base = await session.get(AppState, KEY_BASE_URL)
    email = await session.get(AppState, KEY_EMAIL)
    tok = await session.get(AppState, KEY_TOKEN)
    return {
        "base_url": _clean_url(base.value if base else None) or "",
        "email": _clean(email.value if email else None) or "",
        "has_token": _decrypt(tok.value if tok else None) is not None,
    }


async def _upsert(session: AsyncSession, key: str, value: str) -> None:
    from db import AppState

    row = await session.get(AppState, key)
    if row is None:
        session.add(AppState(key=key, value=value))
    else:
        row.value = value


async def save_settings(
    session: AsyncSession,
    *,
    base_url: str,
    email: str,
    api_token: str,
) -> None:
    """Persist the Confluence settings (upsert into ``app_state``).

    ``base_url`` and ``email`` are stored as submitted (blank unsets them). The
    ``api_token`` is only updated when a non-empty value is given, so the admin
    can edit the URL/email without re-typing the secret; a blank token keeps the
    existing one. Refreshes the cache so ``config`` sees the new values."""
    await _upsert(session, KEY_BASE_URL, (_clean_url(base_url) or ""))
    await _upsert(session, KEY_EMAIL, (_clean(email) or ""))
    token = (api_token or "").strip()
    if token:
        await _upsert(session, KEY_TOKEN, _encrypt(token))
    await session.commit()
    await refresh_cache(session)


async def clear_settings(session: AsyncSession) -> None:
    """Delete all DB-stored Confluence settings (falls back to env vars)."""
    from db import AppState

    for key in (KEY_BASE_URL, KEY_EMAIL, KEY_TOKEN):
        row = await session.get(AppState, key)
        if row is not None:
            await session.delete(row)
    await session.commit()
    await refresh_cache(session)
