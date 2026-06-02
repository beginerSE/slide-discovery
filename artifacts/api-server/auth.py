"""Authentication: session-cookie based, two roles (user / admin)."""
from __future__ import annotations

import logging
import re

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import User, get_session

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


async def _current_user_optional(
    request: Request, session: AsyncSession
) -> User | None:
    uid = request.session.get("user_id") if hasattr(request, "session") else None
    if not uid:
        return None
    user = await session.get(User, int(uid))
    return user


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
