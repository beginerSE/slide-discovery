"""Cloud IAP (Identity-Aware Proxy) authentication.

本番環境は Cloud Run + IAP でアクセス管理している。IAP を通過したリクエスト
には Google が署名した JWT が ``X-Goog-IAP-JWT-Assertion`` ヘッダーで付与
される。ここではその JWT を Google の公開鍵（ES256）で検証し、Google
アカウントのメールアドレスを取り出す。

- ``IAP_AUDIENCE`` が設定されている場合のみ有効（開発環境では従来の
  メール＋パスワードログインのまま）。
- ``X-Goog-Authenticated-User-Email`` は署名がなく偽装され得るため使わない。
- 検証済みトークンは短時間キャッシュする（IAP は同じ JWT を約10分間
  使い回すため、リクエストごとの再検証を避ける）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from fastapi import Request

import config

log = logging.getLogger("api.iap")

# IAP signs its assertion JWTs with these public keys (ES256).
_IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
_IAP_ISSUER = "https://cloud.google.com/iap"

# Public-key cache: IAP keys rotate rarely; refresh every 12h.
_CERTS_TTL = 12 * 3600
_certs: dict | None = None
_certs_fetched_at: float = 0.0
_certs_lock = asyncio.Lock()

# Verified-token cache: token -> (email, expires_at_unix). Bounded.
_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_CACHE_MAX = 512


async def _get_certs() -> dict:
    global _certs, _certs_fetched_at
    now = time.monotonic()
    if _certs is not None and now - _certs_fetched_at < _CERTS_TTL:
        return _certs
    async with _certs_lock:
        if _certs is not None and time.monotonic() - _certs_fetched_at < _CERTS_TTL:
            return _certs
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_IAP_CERTS_URL)
            resp.raise_for_status()
            _certs = resp.json()
            _certs_fetched_at = time.monotonic()
            log.info("fetched IAP public keys (%d keys)", len(_certs))
    return _certs


def _decode(token: str, certs: dict, audience: str) -> dict:
    """Verify signature / audience / expiry and return the claims.

    ``google.auth.jwt.decode`` checks the ES256 signature against the given
    certs, plus ``exp``/``iat`` and ``aud``. Issuer is checked separately.
    """
    from google.auth import jwt as google_jwt

    claims = google_jwt.decode(token, certs=certs, audience=audience)
    if claims.get("iss") != _IAP_ISSUER:
        raise ValueError(f"unexpected issuer: {claims.get('iss')!r}")
    return claims


async def verified_iap_email(request: Request) -> Optional[str]:
    """Return the verified Google-account email for this request, or None.

    None when IAP mode is off, the header is absent, or verification fails
    (a failure is logged — it may indicate header forgery or a mis-set
    ``IAP_AUDIENCE``).
    """
    audience = config.iap_audience()
    if not audience:
        return None
    token = request.headers.get("x-goog-iap-jwt-assertion")
    if not token:
        return None

    cached = _token_cache.get(token)
    if cached is not None:
        email, exp = cached
        if time.time() < exp:
            return email
        _token_cache.pop(token, None)

    try:
        certs = await _get_certs()
        # Signature verification is CPU-bound & synchronous; keep the event
        # loop free.
        claims = await asyncio.to_thread(_decode, token, certs, audience)
    except Exception as exc:  # noqa: BLE001 — any failure means "not authenticated"
        log.warning("IAP JWT verification failed: %s", exc)
        return None

    email = (claims.get("email") or "").strip().lower()
    if not email:
        log.warning("IAP JWT verified but has no email claim")
        return None

    if len(_token_cache) >= _TOKEN_CACHE_MAX:
        _token_cache.clear()
    _token_cache[token] = (email, float(claims.get("exp") or time.time() + 300))
    return email
