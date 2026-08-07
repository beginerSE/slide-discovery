"""CSRF protection for the server-rendered (SSR) UI layer.

The HTML UI uses cookie sessions, so every state-changing browser request
(native form POST or HTMX mutation) must carry a synchronizer token that
matches the one stored in the session. The token is exposed to templates via
a Jinja context processor (``csrf_token``) and validated by the ``verify_csrf``
dependency, which is attached to the whole web router.

Tokens are accepted from the ``X-CSRF-Token`` header (used by HTMX requests via
a global ``hx-headers`` on ``<body>``) or the ``csrf_token`` form field (used by
native, non-HTMX forms). The JSON ``/api/*`` surface is intentionally not
covered here — it is a separate programmatic API.
"""
from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request

CSRF_SESSION_KEY = "csrf_token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def ensure_csrf_token(request: Request) -> str:
    """Return the session CSRF token, creating one on first use."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def csrf_context(request: Request) -> dict:
    """Jinja context processor: makes ``csrf_token`` available in templates."""
    return {"csrf_token": ensure_csrf_token(request)}


async def verify_csrf(request: Request) -> None:
    """Reject unsafe-method requests without a valid synchronizer token."""
    if request.method in _SAFE_METHODS:
        return
    expected = request.session.get(CSRF_SESSION_KEY)
    submitted = request.headers.get("X-CSRF-Token")
    if not submitted:
        try:
            form = await request.form()
        except Exception:
            form = None
        if form is not None:
            submitted = form.get("csrf_token")
    if (
        not expected
        or not submitted
        or not hmac.compare_digest(str(submitted), str(expected))
    ):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
