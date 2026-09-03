"""Authentication: session-cookie based, two roles (user / admin)."""
from __future__ import annotations

import logging
import re

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db import User, get_session
from perf_metrics import timed

log = logging.getLogger("api.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _hash_password(plain: str) -> str:
    # bcrypt has a 72-byte limit; truncate (consistent with verify below)
    pw = plain.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def _verify_password(plain: str, hashed: str) -> bool:
    pw = plain.encode("utf-8")[:72]
    try:
        return bcrypt.checkpw(pw, hashed.encode("utf-8"))
    except ValueError:
        return False


def _reject_local_auth_in_iap_mode() -> None:
    """IAP モードでは IAP が唯一の認証経路。ローカルの新規登録・パスワード
    ログインを無効化し、IAP 未許可ユーザーがアカウントを作る/使う抜け道を
    塞ぐ（本番は IAP が全リクエストを遮るが、多層防御として拒否する）。"""
    import config

    if config.iap_enabled():
        raise HTTPException(
            status_code=403,
            detail="この環境では Google アカウントで自動的にログインされます",
        )


class RegisterBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    password: str = Field(..., min_length=6, max_length=200)
    displayName: str = Field("", max_length=100)


class LoginBody(BaseModel):
    email: str
    password: str


class AuthUser(BaseModel):
    id: int
    email: str
    displayName: str
    role: str
    canUpload: bool = False
    createdAt: str


class AuthResponse(BaseModel):
    user: AuthUser


# bcrypt hashes never equal this sentinel, so IAP-provisioned accounts can
# never be logged into with a password (_verify_password returns False).
IAP_PASSWORD_SENTINEL = "!iap"


async def _iap_auto_login(request: Request, session: AsyncSession) -> User | None:
    """IAP モード時: 検証済みの Google アカウントメールに基づきユーザーを
    取得（なければ自動作成）し、セッションを確立する。"""
    from iap_auth import verified_iap_email

    with timed("auth_iap"):
        email = await verified_iap_email(request)
    if not email:
        return None
    with timed("auth_db"):
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
    if user is None:
        # 最初のユーザーを admin にする既存ルールは IAP 自動作成にも適用
        # （IAP 専用の新規環境でも管理者が存在できるように）。
        user_count = (await session.execute(select(func.count(User.id)))).scalar() or 0
        user = User(
            email=email,
            password_hash=IAP_PASSWORD_SENTINEL,
            display_name=email.split("@", 1)[0],
            role="admin" if user_count == 0 else "user",
        )
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            # 同時アクセスで同じメールが並行作成された場合は既存行を使う
            await session.rollback()
            user = (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if user is None:
                return None
        else:
            await session.refresh(user)
            log.info("IAP auto-provisioned user id=%s role=%s", user.id, user.role)
    if hasattr(request, "session"):
        request.session["user_id"] = user.id
    return user


async def _current_user_optional(
    request: Request, session: AsyncSession
) -> User | None:
    import config as _config

    uid = request.session.get("user_id") if hasattr(request, "session") else None
    if uid:
        with timed("auth_db"):
            user = await session.get(User, int(uid))
    else:
        user = None

    if not _config.iap_enabled():
        return user

    # IAP モードでは IAP が唯一の認証経路: セッションよりも IAP の検証済み
    # アイデンティティを常に優先する。共有PCでの Google アカウント切替後に
    # 旧ユーザーのセッションが生き残ったり、IAP を経ないリクエストが
    # セッションだけで通ることを防ぐ（検証済みトークンは exp まで
    # キャッシュされるため、リクエストごとの再検証コストはほぼゼロ）。
    from iap_auth import verified_iap_email

    with timed("auth_iap"):
        email = await verified_iap_email(request)
    if not email:
        # 有効な IAP アサーションが無ければセッションがあっても未認証扱い
        if user is not None and hasattr(request, "session"):
            request.session.clear()
        return None
    if user is not None and user.email == email:
        return user
    # セッションが無い、または IAP のアイデンティティと不一致 → IAP 側で確立
    if user is not None:
        log.info(
            "IAP identity changed (session user id=%s -> %s); re-authenticating",
            user.id, email,
        )
        if hasattr(request, "session"):
            request.session.clear()
    return await _iap_auto_login(request, session)


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    u = await _current_user_optional(request, session)
    if u is None:
        raise HTTPException(status_code=401, detail="ログインが必要です")
    return u


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="管理者権限が必要です")
    return user


@router.post("/register", response_model=AuthResponse)
async def register(
    body: RegisterBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _reject_local_auth_in_iap_mode()
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="メールアドレスの形式が不正です")
    existing = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="このメールアドレスは登録済みです")
    # First user becomes admin
    user_count = (await session.execute(select(func.count(User.id)))).scalar() or 0
    role = "admin" if user_count == 0 else "user"
    user = User(
        email=email,
        password_hash=_hash_password(body.password),
        display_name=body.displayName or email.split("@", 1)[0],
        role=role,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    request.session["user_id"] = user.id
    log.info("registered user id=%s role=%s", user.id, user.role)
    return AuthResponse(user=AuthUser(**user.to_dict()))


@router.post("/login", response_model=AuthResponse)
async def login(
    body: LoginBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _reject_local_auth_in_iap_mode()
    email = body.email.strip().lower()
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if not user or not _verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=401, detail="メールアドレスまたはパスワードが違います"
        )
    request.session["user_id"] = user.id
    return AuthResponse(user=AuthUser(**user.to_dict()))


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(
    request: Request, session: AsyncSession = Depends(get_session)
):
    u = await _current_user_optional(request, session)
    if u is None:
        return {"user": None}
    return {"user": AuthUser(**u.to_dict())}
