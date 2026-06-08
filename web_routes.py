"""Server-rendered HTML UI (Jinja2 + HTMX).

This module is the entire user-facing layer. It reuses the existing JSON
API handlers (search, slide detail, admin, ingest) unchanged by calling
them directly with an explicit AsyncSession, and renders their results
into HTML fragments/pages instead of JSON. HTMX drives the partial
updates (live search, ingest polling, inline saves).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from auth import (
    LoginBody,
    RegisterBody,
    _current_user_optional,
    login as auth_login,
    register as auth_register,
)
from csrf import csrf_context, verify_csrf
from db import DriveFile, SessionLocal, Slide
from series import extract_doc_date
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy import delete as sql_delete

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"),
    context_processors=[csrf_context],
)

_JST = timezone(timedelta(hours=9))


def _jst(value: Optional[str], fmt: str = "%Y/%m/%d %H:%M") -> str:
    """Render a UTC ISO-8601 timestamp string (as emitted by the model
    ``to_dict`` serializers) in Japan Standard Time. Returns "" for an
    empty/None value so templates can guard with their own fallbacks."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_JST).strftime(fmt)


templates.env.filters["jst"] = _jst

web_router = APIRouter(dependencies=[Depends(verify_csrf)])

PAGE_SIZE = 50
SEARCH_LIMIT = 60

# Facet fields surfaced on the public search screen (layoutType is
# intentionally omitted to mirror the original React home page).
_FACET_FIELDS = ("industry", "client", "proposalType", "graphType", "tag")

_SLIDE_TEXT_FIELDS = (
    "slideTitle",
    "summary",
    "reuseHint",
    "industry",
    "client",
    "proposalType",
    "graphType",
    "layoutType",
    "accessLevel",
    "slideText",
)


def _safe_next(value: Optional[str]) -> str:
    """Only allow same-site relative redirects (avoid open redirect)."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _parse_tags(raw: Optional[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in (raw or "").split("\n"):
        t = line.strip().lstrip("#").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _read_facets(request: Request) -> dict:
    qp = request.query_params
    return {f: (qp.get(f) or "").strip() for f in _FACET_FIELDS}


def _user_dict(user) -> Optional[dict]:
    return user.to_dict() if user else None


# ─────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────


def _login_redirect(request: Request) -> RedirectResponse:
    nxt = quote(request.url.path, safe="/")
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


# ─────────────────────────────────────────────────────────────────────
# Search context (shared by GET / and the /ui/search partial)
# ─────────────────────────────────────────────────────────────────────


async def _search_context(session, q: str, facets: dict, mode: str = "keyword") -> dict:
    import main

    mode = "semantic" if mode == "semantic" else "keyword"
    has_query = bool(q)
    has_active = bool(q) or any(facets.values())

    filters = await main.get_filters(
        q=q or None,
        industry=facets["industry"] or None,
        client=facets["client"] or None,
        proposalType=facets["proposalType"] or None,
        graphType=facets["graphType"] or None,
        tag=facets["tag"] or None,
        session=session,
    )
    ctx = {
        "q": q,
        "facets": facets,
        "mode": mode,
        "has_active": has_active,
        "has_query": has_query,
        "filters": filters,
    }
    if has_active:
        res = await main.search_slides(
            q=q or None,
            mode=mode,
            industry=facets["industry"] or None,
            client=facets["client"] or None,
            proposalType=facets["proposalType"] or None,
            graphType=facets["graphType"] or None,
            tag=facets["tag"] or None,
            limit=SEARCH_LIMIT,
            offset=0,
            session=session,
        )
        ctx["items"] = res["items"]
        ctx["total"] = res["total"]
    else:
        ctx["stats"] = await main.get_stats(session=session)
    return ctx


@web_router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    async with SessionLocal() as session:
        user = await _current_user_optional(request, session)
        if user is None:
            return _login_redirect(request)
        q = (request.query_params.get("q") or "").strip()
        mode = (request.query_params.get("mode") or "keyword").strip()
        facets = _read_facets(request)
        ctx = await _search_context(session, q, facets, mode)
        ctx.update(request=request, user=_user_dict(user), active_nav="/")
        return templates.TemplateResponse(request, "home.html", ctx)


@web_router.get("/ui/search", response_class=HTMLResponse)
async def ui_search(request: Request):
    async with SessionLocal() as session:
        user = await _current_user_optional(request, session)
        if user is None:
            return HTMLResponse("", status_code=401)
        q = (request.query_params.get("q") or "").strip()
        mode = (request.query_params.get("mode") or "keyword").strip()
        facets = _read_facets(request)
        ctx = await _search_context(session, q, facets, mode)
        ctx.update(request=request)
        return templates.TemplateResponse(request, "_search_response.html", ctx)


# ─────────────────────────────────────────────────────────────────────
# Conversational ("対話検索") search — NotebookLM-style Q&A
# ─────────────────────────────────────────────────────────────────────


async def _series_options(session) -> list[dict]:
    """Distinct 定例シリーズ (Drive folders) that have ingested slides, for the
    chat series picker. Ordered by display name."""
    rows = (
        await session.execute(
            select(Slide.folder_id, Slide.folder_name)
            .where(Slide.folder_id != "")
            .distinct()
        )
    ).all()
    options = [
        {"id": fid, "name": fname or fid}
        for fid, fname in rows
        if fid
    ]
    options.sort(key=lambda o: o["name"])
    return options


@web_router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    async with SessionLocal() as session:
        user = await _current_user_optional(request, session)
        if user is None:
            return _login_redirect(request)
        series = await _series_options(session)
        return templates.TemplateResponse(
            request,
            "chat.html",
            {
                "request": request,
                "user": _user_dict(user),
                "active_nav": "/chat",
                "series_options": series,
            },
        )


@web_router.post("/ui/chat", response_class=HTMLResponse)
async def ui_chat(request: Request):
    import main

    async with SessionLocal() as session:
        user = await _current_user_optional(request, session)
        if user is None:
            return HTMLResponse("", status_code=401)
        form = await request.form()
        question = (form.get("question") or "").strip()
        if not question:
            return HTMLResponse("", status_code=204)
        series_id = (form.get("seriesId") or "").strip() or None
        result = await main.ask_question(
            main.AskBody(question=question, seriesId=series_id),
            session=session,
        )
        return templates.TemplateResponse(
            request,
            "_chat_turn.html",
            {
                "request": request,
                "question": question,
                "answer": result["answer"],
                "sources": result["sources"],
                "degraded": result["degraded"],
                "series_name": result.get("seriesName"),
                "series_count": result.get("seriesCount") or 0,
            },
        )


@web_router.get("/slides/{slide_id}", response_class=HTMLResponse)
async def slide_detail(request: Request, slide_id: str):
    import main

    async with SessionLocal() as session:
        user = await _current_user_optional(request, session)
        if user is None:
            return _login_redirect(request)
        try:
            slide = await main.get_slide(slide_id, session=session)
        except HTTPException:
            return templates.TemplateResponse(request, 
                "not_found.html",
                {"request": request, "user": _user_dict(user), "active_nav": ""},
                status_code=404,
            )
        similar = await main.get_similar(slide_id, session=session)
        return templates.TemplateResponse(request, 
            "slide_detail.html",
            {
                "request": request,
                "user": _user_dict(user),
                "active_nav": "",
                "slide": slide,
                "similar": similar,
            },
        )


# ─────────────────────────────────────────────────────────────────────
# Auth pages
# ─────────────────────────────────────────────────────────────────────


@web_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    nxt = _safe_next(request.query_params.get("next"))
    async with SessionLocal() as session:
        user = await _current_user_optional(request, session)
        if user is not None:
            return RedirectResponse(nxt, status_code=303)
    return templates.TemplateResponse(request, 
        "login.html", {"request": request, "next": nxt, "mode": "login"}
    )


@web_router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""
    nxt = _safe_next(form.get("next"))
    async with SessionLocal() as session:
        try:
            await auth_login(LoginBody(email=email, password=password), request, session)
        except HTTPException as exc:
            return templates.TemplateResponse(request, 
                "login.html",
                {
                    "request": request,
                    "error": exc.detail,
                    "email": email,
                    "next": nxt,
                    "mode": "login",
                },
                status_code=exc.status_code,
            )
    return RedirectResponse(nxt, status_code=303)


@web_router.post("/register", response_class=HTMLResponse)
async def register_submit(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""
    display_name = (form.get("displayName") or "").strip()
    nxt = _safe_next(form.get("next"))
    async with SessionLocal() as session:
        try:
            await auth_register(
                RegisterBody(email=email, password=password, displayName=display_name),
                request,
                session,
            )
        except HTTPException as exc:
            return templates.TemplateResponse(request, 
                "login.html",
                {
                    "request": request,
                    "error": exc.detail,
                    "email": email,
                    "next": nxt,
                    "mode": "register",
                },
                status_code=exc.status_code,
            )
    return RedirectResponse(nxt, status_code=303)


@web_router.post("/logout")
async def logout_submit(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ─────────────────────────────────────────────────────────────────────
# Admin: ingest management (/admin)
# ─────────────────────────────────────────────────────────────────────


async def _require_admin(request: Request, session):
    """Return (user, error_response). error_response is set when the
    request should not proceed (redirect to login / forbidden page)."""
    user = await _current_user_optional(request, session)
    if user is None:
        return None, _login_redirect(request)
    if user.role != "admin":
        return user, templates.TemplateResponse(request, 
            "forbidden.html",
            {"request": request, "user": _user_dict(user), "active_nav": ""},
            status_code=403,
        )
    return user, None


async def _require_can_add(request: Request, session):
    """Allow admins and users granted upload permission to add Drive links."""
    user = await _current_user_optional(request, session)
    if user is None:
        return None, _login_redirect(request)
    if user.role != "admin" and not user.can_upload:
        return user, templates.TemplateResponse(
            request,
            "forbidden.html",
            {"request": request, "user": _user_dict(user), "active_nav": ""},
            status_code=403,
        )
    return user, None


async def _drive_files(session) -> list[dict]:
    from admin_routes import list_drive_files

    return (await list_drive_files(session=session))["items"]


async def _status_partial(request: Request, files_count: int):
    from ingest import list_jobs, manual_running

    return templates.TemplateResponse(
        request,
        "_admin_status.html",
        {
            "request": request,
            "jobs": await list_jobs(),
            "manual_running": await manual_running(),
            "files_count": files_count,
        },
    )


@web_router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    from ingest import list_jobs, manual_running

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        files = await _drive_files(session)
        return templates.TemplateResponse(request, 
            "admin.html",
            {
                "request": request,
                "user": _user_dict(user),
                "active_nav": "/admin",
                "jobs": await list_jobs(),
                "manual_running": await manual_running(),
                "files_count": len(files),
                "files": files,
            },
        )


@web_router.get("/ui/admin/status", response_class=HTMLResponse)
async def ui_admin_status(request: Request):
    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        files = await _drive_files(session)
        return await _status_partial(request, len(files))


def _files_partial(request: Request, files: list[dict], is_running: bool, **extra):
    return templates.TemplateResponse(request, 
        "_admin_files.html",
        {"request": request, "files": files, "is_running": is_running, **extra},
    )


@web_router.get("/ui/admin/files", response_class=HTMLResponse)
async def ui_admin_files(request: Request):
    from ingest import any_running

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        files = await _drive_files(session)
        return _files_partial(request, files, await any_running())


@web_router.post("/ui/admin/run", response_class=HTMLResponse)
async def ui_admin_run(request: Request):
    from admin_routes import RunBody, run_now

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        await run_now(
            RunBody(force=False),
            actor_label=(user.display_name or user.email),
        )
        files = await _drive_files(session)
        return await _status_partial(request, len(files))


@web_router.post("/ui/admin/jobs/{job_id}/cleanup", response_class=HTMLResponse)
async def ui_admin_cleanup_job(request: Request, job_id: int):
    from admin_routes import cleanup_job_now

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        await cleanup_job_now(job_id)
        files = await _drive_files(session)
        return await _status_partial(request, len(files))


def _conflicts_partial(
    request: Request, conflicts: list[dict], flash=None, flash_error=False
):
    resp = templates.TemplateResponse(
        request,
        "_admin_conflicts.html",
        {
            "request": request,
            "conflicts": conflicts,
            "flash": flash,
            "flash_error": flash_error,
        },
    )
    # Tell the (non-polling) conflict region's sibling file list to refresh now.
    resp.headers["HX-Trigger"] = "refreshAdminFiles"
    return resp


@web_router.post("/ui/admin/add", response_class=HTMLResponse)
async def ui_admin_add(request: Request):
    from admin_routes import log_addition, next_available_name, resolve_input_entries

    async with SessionLocal() as session:
        user, err = await _require_can_add(request, session)
        if err is not None:
            return err
        form = await request.form()
        text = form.get("text") or ""
        try:
            entries, folder_errors = await resolve_input_entries(text, session)
        except HTTPException as exc:
            return _conflicts_partial(request, [], flash=exc.detail, flash_error=True)

        existing_rows = (
            await session.execute(select(DriveFile))
        ).scalars().all()
        existing_ids = {r.drive_file_id for r in existing_rows}
        taken = {r.effective_name for r in existing_rows if r.effective_name}

        from admin_routes import resolve_folder_names

        folder_names = await resolve_folder_names({e[3] for e in entries})
        registered = 0
        skipped = 0
        conflicts: list[dict] = []
        for file_id, url, name, folder_id in entries:
            if file_id in existing_ids:
                skipped += 1
                continue
            if name and name in taken:
                conflicts.append(
                    {
                        "fid": file_id,
                        "url": url,
                        "name": name,
                        "folder_id": folder_id,
                        "suggested": next_available_name(name, taken),
                    }
                )
                continue
            session.add(
                DriveFile(
                    drive_file_id=file_id,
                    share_url=url,
                    file_name=name,
                    status="pending",
                    folder_id=folder_id,
                    folder_name=folder_names.get(folder_id, ""),
                    doc_date=extract_doc_date(name),
                )
            )
            await log_addition(
                session,
                actor=user,
                action="add",
                drive_file_id=file_id,
                share_url=url,
                file_name=name,
            )
            existing_ids.add(file_id)
            if name:
                taken.add(name)
            registered += 1
        await session.commit()

        parts = []
        if registered:
            parts.append(f"新規 {registered} 件")
        if skipped:
            parts.append(f"既存 {skipped} 件")
        flash = (" / ".join(parts) + " を登録しました") if parts else None
        if folder_errors:
            ferr = " ; ".join(folder_errors)
            flash = (flash + " / " + ferr) if flash else ferr
        if not flash and not conflicts:
            flash = "登録できるファイルがありませんでした"
        return _conflicts_partial(request, conflicts, flash=flash)


@web_router.post("/ui/admin/add/resolve", response_class=HTMLResponse)
async def ui_admin_add_resolve(request: Request):
    from admin_routes import log_addition, next_available_name

    async with SessionLocal() as session:
        user, err = await _require_can_add(request, session)
        if err is not None:
            return err
        form = await request.form()
        fids = form.getlist("cf_fid")
        urls = form.getlist("cf_url")
        names = form.getlist("cf_name")
        folders = form.getlist("cf_folder")
        if len(folders) < len(fids):
            folders = folders + [""] * (len(fids) - len(folders))

        from admin_routes import resolve_folder_names

        folder_names = await resolve_folder_names(set(folders))

        existing_rows = (
            await session.execute(select(DriveFile))
        ).scalars().all()
        existing_ids = {r.drive_file_id for r in existing_rows}
        taken = {r.effective_name for r in existing_rows if r.effective_name}
        deleted_ids: set[str] = set()

        overwrote = renamed = skipped = 0
        for file_id, url, name, folder_id in zip(fids, urls, names, folders):
            if file_id in existing_ids:
                # Already registered in the meantime — leave as-is.
                continue
            decision = form.get(f"decision_{file_id}") or "rename"
            if decision == "skip":
                skipped += 1
                continue
            if decision == "overwrite":
                for victim in existing_rows:
                    if (
                        victim.drive_file_id in deleted_ids
                        or victim.effective_name != name
                    ):
                        continue
                    await session.execute(
                        sql_delete(Slide).where(
                            Slide.file_id == victim.drive_file_id
                        )
                    )
                    await session.delete(victim)
                    deleted_ids.add(victim.drive_file_id)
                    taken.discard(victim.effective_name)
                session.add(
                    DriveFile(
                        drive_file_id=file_id,
                        share_url=url,
                        file_name=name,
                        status="pending",
                        folder_id=folder_id,
                        folder_name=folder_names.get(folder_id, ""),
                        doc_date=extract_doc_date(name),
                    )
                )
                await log_addition(
                    session,
                    actor=user,
                    action="overwrite",
                    drive_file_id=file_id,
                    share_url=url,
                    file_name=name,
                )
                existing_ids.add(file_id)
                taken.add(name)
                overwrote += 1
            else:  # rename → keep separate under a disambiguated name
                new_name = next_available_name(name, taken)
                session.add(
                    DriveFile(
                        drive_file_id=file_id,
                        share_url=url,
                        file_name=name,
                        display_name=new_name,
                        status="pending",
                        folder_id=folder_id,
                        folder_name=folder_names.get(folder_id, ""),
                        doc_date=extract_doc_date(name),
                    )
                )
                await log_addition(
                    session,
                    actor=user,
                    action="rename",
                    drive_file_id=file_id,
                    share_url=url,
                    file_name=new_name,
                    note=f"元名: {name}",
                )
                existing_ids.add(file_id)
                taken.add(new_name)
                renamed += 1
        await session.commit()

        parts = []
        if overwrote:
            parts.append(f"上書き {overwrote} 件")
        if renamed:
            parts.append(f"別管理 {renamed} 件")
        if skipped:
            parts.append(f"スキップ {skipped} 件")
        flash = ("、".join(parts) + " を適用しました") if parts else "適用する項目がありませんでした"
        return _conflicts_partial(request, [], flash=flash)


@web_router.post("/ui/admin/files/{drive_file_id}/retry", response_class=HTMLResponse)
async def ui_admin_retry(request: Request, drive_file_id: int):
    from admin_routes import RetryBody, retry_drive_file
    from ingest import any_running

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        flash = None
        flash_error = False
        try:
            await retry_drive_file(
                drive_file_id,
                RetryBody(force=True),
                session=session,
                actor_label=(user.display_name or user.email),
            )
            flash = "再取り込みを開始しました"
        except HTTPException as exc:
            flash = exc.detail
            flash_error = True
        files = await _drive_files(session)
        return _files_partial(
            request,
            files,
            await any_running(),
            flash=flash,
            flash_error=flash_error,
        )


@web_router.post(
    "/ui/admin/files/{drive_file_id}/regen-thumbnails",
    response_class=HTMLResponse,
)
async def ui_admin_regen_thumbnails(request: Request, drive_file_id: int):
    from admin_routes import regen_thumbnails
    from ingest import any_running

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        flash = None
        flash_error = False
        try:
            await regen_thumbnails(
                drive_file_id, session=session, actor=user
            )
            flash = "サムネイル再生成を開始しました"
        except HTTPException as exc:
            flash = exc.detail
            flash_error = True
        files = await _drive_files(session)
        return _files_partial(
            request,
            files,
            await any_running(),
            flash=flash,
            flash_error=flash_error,
        )


@web_router.post("/ui/admin/files/{drive_file_id}/delete", response_class=HTMLResponse)
async def ui_admin_delete(request: Request, drive_file_id: int):
    from admin_routes import delete_drive_file
    from ingest import any_running

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        flash = None
        flash_error = False
        try:
            await delete_drive_file(drive_file_id, session=session)
            flash = "削除しました"
        except HTTPException as exc:
            flash = exc.detail
            flash_error = True
        files = await _drive_files(session)
        return _files_partial(
            request,
            files,
            await any_running(),
            flash=flash,
            flash_error=flash_error,
        )


# ─────────────────────────────────────────────────────────────────────
# Admin: file-level common attributes (/admin/files)
# ─────────────────────────────────────────────────────────────────────


async def _file_snapshots(session):
    from admin_routes import admin_list_files

    return (await admin_list_files(session=session)).items


@web_router.get("/admin/files", response_class=HTMLResponse)
async def admin_files_page(request: Request):
    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        files = await _file_snapshots(session)
        return templates.TemplateResponse(request, 
            "admin_files.html",
            {
                "request": request,
                "user": _user_dict(user),
                "active_nav": "/admin/files",
                "files": files,
                "total_files": len(files),
            },
        )


@web_router.get("/ui/admin/files-list", response_class=HTMLResponse)
async def ui_admin_files_list(request: Request):
    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        all_files = await _file_snapshots(session)
        needle = (request.query_params.get("q") or "").strip().lower()
        files = (
            [f for f in all_files if needle in (f.fileName or "").lower()]
            if needle
            else all_files
        )
        return templates.TemplateResponse(request, 
            "_file_list.html",
            {"request": request, "files": files, "total_files": len(all_files)},
        )


@web_router.post("/ui/admin/files/{file_id}", response_class=HTMLResponse)
async def ui_admin_file_save(request: Request, file_id: str):
    from admin_routes import UpdateFileCommonBody, admin_update_file

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        snapshots = await _file_snapshots(session)
        current = next((f for f in snapshots if f.fileId == file_id), None)
        if current is None:
            return HTMLResponse("not found", status_code=404)

        form = await request.form()
        changed: dict = {}
        for field in ("industry", "client", "proposalType"):
            val = form.get(field) or ""
            if val != (getattr(current, field) or ""):
                changed[field] = val
        tags_new = _parse_tags(form.get("tags"))
        if tags_new != list(current.tags or []):
            changed["tags"] = tags_new

        if not changed:
            return templates.TemplateResponse(request, 
                "_file_card.html", {"request": request, "file": current}
            )

        result = await admin_update_file(
            file_id, UpdateFileCommonBody(**changed), session=session
        )
        return templates.TemplateResponse(request, 
            "_file_card.html",
            {
                "request": request,
                "file": result.file,
                "saved": True,
                "updated": result.updatedSlides,
            },
        )


# ─────────────────────────────────────────────────────────────────────
# Admin: slide metadata (/admin/slides)
# ─────────────────────────────────────────────────────────────────────


async def _slides_context(session, q: str, page: int) -> dict:
    from admin_routes import admin_list_slides

    res = await admin_list_slides(
        q=q or None,
        fileId=None,
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
        session=session,
    )
    return {
        "items": res.items,
        "total": res.total,
        "q": q,
        "page": page,
        "page_size": PAGE_SIZE,
    }


@web_router.get("/admin/slides", response_class=HTMLResponse)
async def admin_slides_page(request: Request):
    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        ctx = await _slides_context(session, "", 0)
        ctx.update(
            request=request, user=_user_dict(user), active_nav="/admin/slides"
        )
        return templates.TemplateResponse(request, "admin_slides.html", ctx)


@web_router.get("/ui/admin/slides-list", response_class=HTMLResponse)
async def ui_admin_slides_list(request: Request):
    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        q = (request.query_params.get("q") or "").strip()
        try:
            page = max(0, int(request.query_params.get("page") or 0))
        except ValueError:
            page = 0
        ctx = await _slides_context(session, q, page)
        ctx.update(request=request)
        return templates.TemplateResponse(request, "_slide_list.html", ctx)


@web_router.get("/admin/slides/{slide_id}", response_class=HTMLResponse)
async def admin_slide_edit_page(request: Request, slide_id: str):
    from admin_routes import admin_get_slide

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        try:
            slide = await admin_get_slide(slide_id, session=session)
        except HTTPException:
            return templates.TemplateResponse(request, 
                "not_found.html",
                {"request": request, "user": _user_dict(user), "active_nav": ""},
                status_code=404,
            )
        return templates.TemplateResponse(request, 
            "admin_slide_edit.html",
            {
                "request": request,
                "user": _user_dict(user),
                "active_nav": "/admin/slides",
                "slide": slide,
            },
        )


@web_router.post("/admin/slides/{slide_id}", response_class=HTMLResponse)
async def admin_slide_save(request: Request, slide_id: str):
    from admin_routes import (
        UpdateSlideBody,
        _EMBEDDING_INPUT_FIELDS,
        admin_get_slide,
        admin_update_slide,
    )

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        try:
            current = await admin_get_slide(slide_id, session=session)
        except HTTPException:
            return HTMLResponse("not found", status_code=404)

        form = await request.form()
        changed: dict = {}
        for field in _SLIDE_TEXT_FIELDS:
            val = (form.get(field) or "").replace("\r\n", "\n")
            if val != (current.get(field) or ""):
                changed[field] = val
        tags_new = _parse_tags(form.get("tags"))
        if tags_new != list(current.get("tags") or []):
            changed["tags"] = tags_new

        if not changed:
            return templates.TemplateResponse(request, 
                "_slide_edit_form.html", {"request": request, "slide": current}
            )

        updated = await admin_update_slide(
            slide_id, UpdateSlideBody(**changed), session=session
        )
        embedding_invalidated = bool(set(changed) & _EMBEDDING_INPUT_FIELDS)
        return templates.TemplateResponse(request, 
            "_slide_edit_form.html",
            {
                "request": request,
                "slide": updated,
                "saved": True,
                "embedding_invalidated": embedding_invalidated,
            },
        )


# ─────────────────────────────────────────────────────────────────────
# Contribute (permitted users): add Drive links
# ─────────────────────────────────────────────────────────────────────


@web_router.get("/contribute", response_class=HTMLResponse)
async def contribute_page(request: Request):
    async with SessionLocal() as session:
        user, err = await _require_can_add(request, session)
        if err is not None:
            return err
        return templates.TemplateResponse(
            request,
            "contribute.html",
            {
                "request": request,
                "user": _user_dict(user),
                "active_nav": "/contribute",
            },
        )


# ─────────────────────────────────────────────────────────────────────
# Admin: addition history log (/admin/logs)
# ─────────────────────────────────────────────────────────────────────


@web_router.get("/admin/logs", response_class=HTMLResponse)
async def admin_logs_page(request: Request):
    from admin_routes import list_add_logs

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        logs = await list_add_logs(session)
        return templates.TemplateResponse(
            request,
            "admin_logs.html",
            {
                "request": request,
                "user": _user_dict(user),
                "active_nav": "/admin/logs",
                "logs": logs,
            },
        )


# ─────────────────────────────────────────────────────────────────────
# Admin: user upload-permission management (/admin/users)
# ─────────────────────────────────────────────────────────────────────


@web_router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    from admin_routes import list_users

    async with SessionLocal() as session:
        user, err = await _require_admin(request, session)
        if err is not None:
            return err
        users = await list_users(session)
        return templates.TemplateResponse(
            request,
            "admin_users.html",
            {
                "request": request,
                "user": _user_dict(user),
                "active_nav": "/admin/users",
                "users": users,
            },
        )


@web_router.post(
    "/ui/admin/users/{user_id}/toggle-upload", response_class=HTMLResponse
)
async def ui_admin_user_toggle(request: Request, user_id: int):
    from admin_routes import set_user_upload
    from db import User

    async with SessionLocal() as session:
        admin_user, err = await _require_admin(request, session)
        if err is not None:
            return err
        target = await session.get(User, user_id)
        if target is None:
            return HTMLResponse("not found", status_code=404)
        updated = await set_user_upload(session, user_id, not target.can_upload)
        return templates.TemplateResponse(
            request,
            "_admin_user_toggle.html",
            {"request": request, "u": updated},
        )


# ─────────────────────────────────────────────────────────────────────
# Catch-all 404 (HTML for UI paths, JSON for unknown /api paths)
# ─────────────────────────────────────────────────────────────────────


@web_router.get("/{full_path:path}", response_class=HTMLResponse)
async def catch_all(request: Request, full_path: str):
    if full_path.startswith("api/") or full_path == "api":
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"error": "not found"})
    async with SessionLocal() as session:
        user = await _current_user_optional(request, session)
    return templates.TemplateResponse(request, 
        "not_found.html",
        {"request": request, "user": _user_dict(user), "active_nav": ""},
        status_code=404,
    )
