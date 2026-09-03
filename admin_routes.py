"""Admin + ingest API routes."""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_admin
from db import AddLog, DriveFile, DriveFolder, Slide, User, get_session, utcnow
from drive import (
    fetch_file_metadata,
    fetch_folder_name,
    list_folder_files,
    parse_share_input,
    view_url,
)
from series import extract_doc_date
from ingest import (
    cleanup_job,
    list_jobs,
    schedule_ingest_background,
    schedule_thumbnail_regen_background,
)

log = logging.getLogger("api.admin")
router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)
ingest_router = APIRouter(
    prefix="/api/ingest", tags=["ingest"], dependencies=[Depends(require_admin)]
)


# ─────────────────────────────────────────────────────────────────────
# Name-collision helpers (pure, unit-tested in tests/test_dedup.py)
# ─────────────────────────────────────────────────────────────────────
_RENAME_SUFFIX_RE = re.compile(r"\s*\(\d+\)$")


def _split_ext(name: str) -> tuple[str, str]:
    """Split a .ppt/.pptx file name into (base, ext). ext keeps its dot."""
    low = name.lower()
    for ext in (".pptx", ".ppt"):
        if low.endswith(ext):
            return name[: -len(ext)], name[-len(ext):]
    return name, ""


def next_available_name(name: str, taken: set[str]) -> str:
    """Return ``name`` with a ``(n)`` suffix (before the extension) that is
    not already in ``taken``. Existing ``(n)`` suffixes are stripped first so
    repeated renames don't stack (e.g. ``deck (1) (2).pptx``)."""
    base, ext = _split_ext(name)
    base = _RENAME_SUFFIX_RE.sub("", base)
    n = 1
    while True:
        candidate = f"{base} ({n}){ext}"
        if candidate not in taken:
            return candidate
        n += 1


async def resolve_input_entries(
    text: str, session: AsyncSession
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Parse pasted links + expand any folder URLs into individual files.

    Returns ``(entries, folder_errors)`` where each entry is
    ``(drive_file_id, share_url, file_name, folder_id)``. In Drive API mode,
    direct file links are enriched with their immediate parent folder; in
    public-link mode their name/folder remain unknown until ingest. Registers
    explicitly pasted folders so the incremental change
    poller can watch them, but does NOT register or commit DriveFile rows —
    callers decide how to handle collisions first.
    """
    file_entries, folder_ids = parse_share_input(text)
    if not file_entries and not folder_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                "有効な共有リンクが見つかりません。"
                "ファイルの共有URL、もしくはフォルダの共有URLを貼り付けてください。"
            ),
        )

    folder_errors: list[str] = []
    seen_file_ids: set[str] = {fid for fid, _ in file_entries}
    known_names: dict[str, str] = {}
    file_folder: dict[str, str] = {}
    registered_folders: set[str] = set()

    async def _ensure_folder_watched(fid: str) -> None:
        # Register each (sub)folder we discover so the incremental change
        # poller watches it too; safe to call repeatedly within one call.
        if not fid or fid in registered_folders:
            return
        registered_folders.add(fid)
        existing_folder = (
            await session.execute(
                select(DriveFolder).where(DriveFolder.drive_folder_id == fid)
            )
        ).scalar_one_or_none()
        if existing_folder is None:
            session.add(
                DriveFolder(
                    drive_folder_id=fid,
                    share_url=f"https://drive.google.com/drive/folders/{fid}",
                )
            )

    for folder_id in folder_ids:
        await _ensure_folder_watched(folder_id)
        try:
            items = await list_folder_files(folder_id)
        except Exception as e:  # noqa: BLE001
            log.warning("folder listing failed: %s: %s", folder_id, e)
            folder_errors.append(f"{folder_id}: {e}")
            continue
        if not items:
            folder_errors.append(
                f"{folder_id}: フォルダ内（サブフォルダ含む）に .pptx "
                "ファイルが見つかりませんでした"
            )
            continue
        for fid, fname, parent_id in items:
            parent_id = parent_id or folder_id
            await _ensure_folder_watched(parent_id)
            known_names[fid] = fname
            file_folder.setdefault(fid, parent_id)
            if fid in seen_file_ids:
                continue
            seen_file_ids.add(fid)
            file_entries.append((fid, view_url(fid)))

    entries: list[tuple[str, str, str, str]] = []
    for file_id, original in file_entries:
        url = original if original.startswith("http") else view_url(file_id)
        name = known_names.get(file_id, "")
        if not name:
            # Direct file link (not from a folder listing): one metadata call
            # supplies both the name and its immediate parent folder in Drive
            # API mode. Public-link mode cannot discover either before ingest.
            metadata = await fetch_file_metadata(file_id)
            name = metadata.name
            if metadata.parent_id:
                file_folder.setdefault(file_id, metadata.parent_id)
        entries.append((file_id, url, name, file_folder.get(file_id, "")))
    return entries, folder_errors


async def resolve_folder_names(folder_ids) -> dict[str, str]:
    """Best-effort ``folder_id -> folder_name`` map (one lookup per id)."""
    names: dict[str, str] = {}
    for fid in {f for f in folder_ids if f}:
        names[fid] = await fetch_folder_name(fid)
    return names


class AddLinksBody(BaseModel):
    text: str


class AddLinksResponse(BaseModel):
    added: int
    skipped: int
    items: list[dict]


@router.get("/drive-files")
async def list_drive_files(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(DriveFile).order_by(DriveFile.added_at.desc()))
    ).scalars().all()
    return {"items": [r.to_dict() for r in rows]}


@router.post("/drive-files")
async def add_drive_files(
    body: AddLinksBody, session: AsyncSession = Depends(get_session)
) -> AddLinksResponse:
    """JSON API: register links, skipping files already tracked by Drive ID.

    Name collisions between *different* Drive files are not prompted here (the
    interactive overwrite/rename/skip flow lives in the HTML admin UI); this
    endpoint just registers everything new, preserving its long-standing
    contract for API clients.
    """
    entries, folder_errors = await resolve_input_entries(body.text, session)
    if not entries:
        msg = "登録できるファイルがありませんでした"
        if folder_errors:
            msg += " / " + " ; ".join(folder_errors)
        raise HTTPException(status_code=400, detail=msg)

    folder_names = await resolve_folder_names({e[3] for e in entries})
    added: list[DriveFile] = []
    skipped = 0
    for file_id, url, name, folder_id in entries:
        existing = (
            await session.execute(
                select(DriveFile).where(DriveFile.drive_file_id == file_id)
            )
        ).scalar_one_or_none()
        if existing:
            skipped += 1
            continue
        row = DriveFile(
            drive_file_id=file_id,
            share_url=url,
            file_name=name,
            status="pending",
            folder_id=folder_id,
            folder_name=folder_names.get(folder_id, ""),
            doc_date=extract_doc_date(name),
        )
        session.add(row)
        added.append(row)
    await session.commit()
    for r in added:
        await session.refresh(r)
    return AddLinksResponse(
        added=len(added),
        skipped=skipped,
        items=[r.to_dict() for r in added],
    )


@router.delete("/drive-files/{drive_file_id}")
async def delete_drive_file(
    drive_file_id: int, session: AsyncSession = Depends(get_session)
):
    row = await session.get(DriveFile, drive_file_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    # Detach slides for this file (keep slides as-is, or delete; we delete)
    from sqlalchemy import delete as sql_delete

    await session.execute(sql_delete(Slide).where(Slide.file_id == row.drive_file_id))
    drive_file_id_str = row.drive_file_id
    await session.delete(row)
    await session.commit()
    # Drop this file's thumbnails (local + GCS) so storage isn't leaked.
    import thumbnail_store

    await thumbnail_store.clear_file(drive_file_id_str)
    return {"deleted": True}


# ─────────────────────────────────────────────────────────────────────
# Slide metadata management (manual review / correction of AI output)
# ─────────────────────────────────────────────────────────────────────
# Fields the admin is allowed to edit. Anything that is the product of
# the ingest pipeline and meant to be human-curated lives here. We
# deliberately do NOT expose immutable provenance fields (slideId,
# fileId, fileName, pageNo, thumbnailPath, sourceUrl, timestamps).
_EDITABLE_FIELDS = {
    "slideTitle": "slide_title",
    "summary": "summary",
    "reuseHint": "reuse_hint",
    "industry": "industry",
    "client": "client",
    "proposalType": "proposal_type",
    "graphType": "graph_type",
    "layoutType": "layout_type",
    "docCategory": "doc_category",
    "accessLevel": "access_level",
    "tags": "tags",
    "slideText": "slide_text",
}

# When any of these change, the cached Gemini embedding becomes stale
# because they all contribute to the embedded text built by
# gemini_embed.build_slide_embed_text. Keep this set in sync with that
# function — listing fields that aren't actually embedded would cause
# pointless recomputations. (reuseHint is intentionally NOT here: it's
# author-facing prose, never fed into the embedding.)
_EMBEDDING_INPUT_FIELDS = {
    "slideTitle",
    "slideText",
    "summary",
    "industry",
    "client",
    "proposalType",
    "graphType",
    "layoutType",
    "docCategory",
    "tags",
}


class UpdateSlideBody(BaseModel):
    slideTitle: Optional[str] = None
    summary: Optional[str] = None
    reuseHint: Optional[str] = None
    industry: Optional[str] = None
    client: Optional[str] = None
    proposalType: Optional[str] = None
    graphType: Optional[str] = None
    layoutType: Optional[str] = None
    docCategory: Optional[str] = None
    accessLevel: Optional[str] = None
    tags: Optional[list[str]] = None
    slideText: Optional[str] = None


class AdminSlideList(BaseModel):
    total: int
    items: list[dict]


@router.get("/slides", response_model=AdminSlideList)
async def admin_list_slides(
    q: Optional[str] = None,
    fileId: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """Admin-only paginated slide listing used by the metadata review
    screen. Keeps the search simple (ILIKE on a few text columns) since
    the public /api/slides endpoint already handles the heavy
    full-text/semantic search path."""
    stmt = select(Slide)
    if fileId:
        stmt = stmt.where(Slide.file_id == fileId)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Slide.slide_title.ilike(like),
                Slide.file_name.ilike(like),
                Slide.summary.ilike(like),
                Slide.industry.ilike(like),
                Slide.proposal_type.ilike(like),
                Slide.doc_category.ilike(like),
            )
        )
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0
    stmt = (
        stmt.order_by(Slide.file_name.asc(), Slide.page_no.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return AdminSlideList(total=int(total), items=[r.to_dict() for r in rows])


@router.get("/slides/{slide_id}")
async def admin_get_slide(
    slide_id: str, session: AsyncSession = Depends(get_session)
):
    row = await session.get(Slide, slide_id)
    if not row:
        raise HTTPException(status_code=404, detail="slide not found")
    return row.to_dict()


@router.patch("/slides/{slide_id}")
async def admin_update_slide(
    slide_id: str,
    body: UpdateSlideBody,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(Slide, slide_id)
    if not row:
        raise HTTPException(status_code=404, detail="slide not found")

    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status_code=400, detail="no fields to update")

    invalidates_embedding = False
    for api_key, value in payload.items():
        if api_key not in _EDITABLE_FIELDS:
            continue
        column = _EDITABLE_FIELDS[api_key]
        if api_key == "tags":
            cleaned = [str(t).strip() for t in (value or []) if str(t).strip()]
            # de-dup while preserving order
            seen: set[str] = set()
            cleaned = [t for t in cleaned if not (t in seen or seen.add(t))]
            setattr(row, column, cleaned)
        else:
            setattr(row, column, value if value is not None else "")
        if api_key in _EMBEDDING_INPUT_FIELDS:
            invalidates_embedding = True

    if invalidates_embedding:
        # Drop the cached vector so the hourly backfill regenerates it
        # against the freshly-edited text. Search keeps working in the
        # meantime via the keyword/GIN path.
        row.embedding = None

    row.updated_at = utcnow()
    await session.commit()
    await session.refresh(row)
    return row.to_dict()


# ─────────────────────────────────────────────────────────────────────
# File-level common-attribute management
# ─────────────────────────────────────────────────────────────────────
# A "file" is a single source PPTX (one file_id) that explodes into many
# slide rows. Several attributes are conceptually file-wide rather than
# per-slide: the industry, the client (クライアント先), the proposal type
# and the shared tag set. This screen lets an admin set those once and
# fan them out to every slide of the file, instead of editing N slides
# by hand. When the slides currently disagree on a value we surface it as
# "mixed" so the admin knows they're about to overwrite divergent data.
_FILE_COMMON_FIELDS = {
    "industry": "industry",
    "client": "client",
    "proposalType": "proposal_type",
    "docCategory": "doc_category",
}


def _tags_key(tags: list[str]) -> tuple[str, ...]:
    # Order-insensitive identity for a tag set.
    return tuple(sorted(tags or []))


def _majority(values: list[str]) -> str:
    """Most common non-empty value (the file's dominant default). Ties are
    broken by first-seen order (Counter.most_common is stable). Returns ""
    when every slide is empty."""
    counts = Counter(v for v in values if v)
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


# Client-name inference from a proposal file name. Japanese proposal decks are
# almost always named after the client — either with a company suffix
# (株式会社○○ / ○○株式会社) or an honorific (○○様 / ○○御中). We use those
# high-precision signals first and fall back to the first "name-like" token.
_CLIENT_NAME_CHARS = r"0-9A-Za-z一-龥々〆\u3005ぁ-んァ-ヴー＆&()（）・"
_COMPANY_SUFFIXES = "株式会社|有限会社|合同会社|合資会社|合名会社"
_HONORIFIC_RE = re.compile(
    rf"([{_CLIENT_NAME_CHARS}]{{1,40}})\s*(?:様|御中)"
)
_COMPANY_RE = re.compile(
    rf"((?:{_COMPANY_SUFFIXES})[{_CLIENT_NAME_CHARS}]{{1,30}}"
    rf"|[{_CLIENT_NAME_CHARS}]{{1,30}}(?:{_COMPANY_SUFFIXES}))"
)
_CLIENT_SPLIT_RE = re.compile(r"[\s_\u3000|/／\\\[\]【】「」（）()、,，。\-–—~～]+")
_DATE_TOKEN_RE = re.compile(
    r"^(?:\d{1,4}[年月日._/-]?){1,3}\d{0,4}$|^v?\d+(?:\.\d+)?$", re.IGNORECASE
)
_CLIENT_STOPWORDS = {
    "提案書", "提案", "ご提案", "御提案", "提案資料", "資料", "企画書", "企画",
    "見積", "見積書", "御見積", "お見積", "プレゼン", "プレゼンテーション",
    "ドラフト", "draft", "final", "最終", "最終版", "版", "コピー", "copy",
    "サンプル", "sample", "テンプレート", "template", "報告書", "報告", "案",
}
_DOC_SUFFIXES = (
    "提案書", "提案", "資料", "企画書", "企画", "見積書", "見積",
    "報告書", "報告", "案", "版", "書",
)


def _infer_client_from_filename(file_name: str) -> str:
    name = (file_name or "").strip()
    name = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name)  # drop a file extension
    if not name:
        return ""
    # 1) Honorific marker (highest precision): "○○様" / "○○御中".
    m = _HONORIFIC_RE.search(name)
    if m:
        cand = m.group(1).strip(" 　・-_")
        if cand and cand not in _CLIENT_STOPWORDS:
            return cand
    # 2) Company suffix, prefix or suffix form.
    m = _COMPANY_RE.search(name)
    if m:
        return m.group(1).strip(" 　・-_")
    # 3) Conservative fallback: first name-like token that is not a date,
    #    number or generic document word.
    for tok in _CLIENT_SPLIT_RE.split(name):
        tok = tok.strip(" 　・-_")
        if not tok or tok in _CLIENT_STOPWORDS or tok.isdigit():
            continue
        if _DATE_TOKEN_RE.match(tok) or tok.endswith(_DOC_SUFFIXES):
            continue
        if not re.search(r"[一-龥々〆ぁ-んァ-ヴ]", tok):
            continue  # require a kanji/kana char to avoid generic latin words
        return tok
    return ""


class FileCommon(BaseModel):
    fileId: str
    fileName: str
    slideCount: int
    industry: str
    client: str
    proposalType: str
    docCategory: str
    tags: list[str]
    industryMixed: bool
    clientMixed: bool
    proposalTypeMixed: bool
    docCategoryMixed: bool
    tagsMixed: bool


class FileCommonList(BaseModel):
    items: list[FileCommon]


class UpdateFileCommonBody(BaseModel):
    industry: Optional[str] = None
    client: Optional[str] = None
    proposalType: Optional[str] = None
    docCategory: Optional[str] = None
    tags: Optional[list[str]] = None


class UpdateFileCommonResult(BaseModel):
    updatedSlides: int
    file: FileCommon


def _summarize_file(file_id: str, rows: list[Slide]) -> FileCommon:
    industries = {r.industry or "" for r in rows}
    clients = {r.client or "" for r in rows}
    proposals = {r.proposal_type or "" for r in rows}
    doc_categories = {r.doc_category or "" for r in rows}
    tag_keys = [_tags_key(list(r.tags or [])) for r in rows]
    tag_sets = set(tag_keys)
    file_name = rows[0].file_name if rows else ""

    # Defaults: when slides disagree (or are empty) fall back to the file's
    # dominant value so "ファイル管理" is pre-filled instead of blank. For the
    # client, when no slide has one we infer the company from the file name.
    client_default = _majority([r.client or "" for r in rows])
    if not client_default:
        client_default = _infer_client_from_filename(file_name)

    nonempty_tag_keys = [k for k in tag_keys if k]
    if len(tag_sets) == 1:
        tags_default = list(rows[0].tags or []) if rows else []
    elif nonempty_tag_keys:
        tags_default = list(Counter(nonempty_tag_keys).most_common(1)[0][0])
    else:
        tags_default = []

    return FileCommon(
        fileId=file_id,
        fileName=file_name,
        slideCount=len(rows),
        industry=_majority([r.industry or "" for r in rows]),
        client=client_default,
        proposalType=_majority([r.proposal_type or "" for r in rows]),
        docCategory=_majority([r.doc_category or "" for r in rows]),
        tags=tags_default,
        industryMixed=len(industries) > 1,
        clientMixed=len(clients) > 1,
        proposalTypeMixed=len(proposals) > 1,
        docCategoryMixed=len(doc_categories) > 1,
        tagsMixed=len(tag_sets) > 1,
    )


@router.get("/files", response_model=FileCommonList)
async def admin_list_files(session: AsyncSession = Depends(get_session)):
    """Group every slide by its source file and report the common values
    for the file-wide attributes (industry / client / proposalType /
    tags), flagging any field where the slides currently disagree."""
    rows = (
        await session.execute(
            select(Slide).order_by(Slide.file_name.asc(), Slide.page_no.asc())
        )
    ).scalars().all()

    grouped: dict[str, list[Slide]] = {}
    order: list[str] = []
    for r in rows:
        if r.file_id not in grouped:
            grouped[r.file_id] = []
            order.append(r.file_id)
        grouped[r.file_id].append(r)

    return FileCommonList(
        items=[_summarize_file(fid, grouped[fid]) for fid in order]
    )


@router.patch("/files/{file_id}", response_model=UpdateFileCommonResult)
async def admin_update_file(
    file_id: str,
    body: UpdateFileCommonBody,
    session: AsyncSession = Depends(get_session),
):
    """Apply the provided file-wide attributes to every slide of the
    file. Omitted fields are left untouched. Because all of these fields
    feed the embedding, any change drops the cached vectors for the
    affected slides so the hourly backfill regenerates them."""
    rows = (
        await session.execute(select(Slide).where(Slide.file_id == file_id))
    ).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="file not found")

    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status_code=400, detail="no fields to update")

    cleaned_tags: Optional[list[str]] = None
    if "tags" in payload:
        seen: set[str] = set()
        cleaned_tags = [
            t
            for t in (
                str(x).strip() for x in (payload["tags"] or [])
            )
            if t and not (t in seen or seen.add(t))
        ]

    now = utcnow()
    for row in rows:
        for api_key, column in _FILE_COMMON_FIELDS.items():
            if api_key in payload:
                setattr(row, column, payload[api_key] or "")
        if cleaned_tags is not None:
            row.tags = list(cleaned_tags)
        # Every file-common field is an embedding input, so any change
        # invalidates the cached vector.
        row.embedding = None
        row.updated_at = now

    await session.commit()
    for row in rows:
        await session.refresh(row)
    return UpdateFileCommonResult(
        updatedSlides=len(rows),
        file=_summarize_file(file_id, rows),
    )


class RetryBody(BaseModel):
    force: Optional[bool] = True


@router.post("/drive-files/{drive_file_id}/retry")
async def retry_drive_file(
    drive_file_id: int,
    body: RetryBody | None = None,
    session: AsyncSession = Depends(get_session),
    actor_label: str = "",
):
    row = await session.get(DriveFile, drive_file_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    started = await schedule_ingest_background(
        only_ids=[row.id], force=True, kind="retry", actor_label=actor_label
    )
    return {"started": started, "jobs": await list_jobs()}


async def regen_thumbnails(
    drive_file_id: int,
    *,
    session: AsyncSession,
    actor: Optional[User] = None,
):
    """Re-render and re-publish ONLY this file's thumbnails (no Gemini recompute).

    Records the action in the add-history audit log, then schedules the
    thumbnail-only regen as a background job. This is a plain helper (not an HTTP
    route) — the HTML/HTMX admin UI calls it directly with an explicit session,
    matching the single-source-of-truth pattern used across the admin handlers.
    """
    row = await session.get(DriveFile, drive_file_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    await log_addition(
        session,
        actor=actor,
        action="サムネイル再生成",
        drive_file_id=row.drive_file_id,
        share_url=view_url(row.drive_file_id),
        file_name=row.display_name or row.file_name or row.drive_file_id,
    )
    await session.commit()
    started = await schedule_thumbnail_regen_background(
        row.id, actor_label=_actor_label(actor)
    )
    return {"started": started, "jobs": await list_jobs()}


class RunBody(BaseModel):
    force: bool = False


@ingest_router.post("/run")
async def run_now(body: RunBody | None = None, actor_label: str = ""):
    force = bool(body and body.force)
    started = await schedule_ingest_background(
        only_ids=None, force=force, kind="manual", actor_label=actor_label
    )
    return {"started": started, "jobs": await list_jobs()}


@ingest_router.post("/jobs/{job_id}/cleanup")
async def cleanup_job_now(job_id: int):
    cleaned = await cleanup_job(job_id)
    return {"cleaned": cleaned, "jobs": await list_jobs()}


@ingest_router.get("/status")
async def status():
    return {"jobs": await list_jobs()}


# ─────────────────────────────────────────────────────────────────────
# Add-history log + user upload-permission management (used by the HTML UI)
# ─────────────────────────────────────────────────────────────────────


def _actor_label(actor: Optional[User]) -> str:
    if actor is None:
        return "自動同期"
    return actor.display_name or actor.email


async def log_addition(
    session: AsyncSession,
    *,
    actor: Optional[User],
    action: str,
    drive_file_id: str,
    share_url: str,
    file_name: str,
    note: str = "",
) -> None:
    """Record one Drive-link addition. Caller commits the session."""
    session.add(
        AddLog(
            actor_user_id=(actor.id if actor else None),
            actor_label=_actor_label(actor),
            action=action,
            drive_file_id=drive_file_id,
            share_url=share_url,
            file_name=file_name,
            note=note,
        )
    )


async def list_add_logs(session: AsyncSession, limit: int = 300) -> list[dict]:
    rows = (
        await session.execute(
            select(AddLog).order_by(AddLog.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return [r.to_dict() for r in rows]


async def list_users(session: AsyncSession) -> list[dict]:
    rows = (
        await session.execute(select(User).order_by(User.created_at.asc()))
    ).scalars().all()
    return [r.to_dict() for r in rows]


async def set_user_upload(
    session: AsyncSession, user_id: int, value: bool
) -> Optional[dict]:
    """Flip a non-admin user's upload permission. Admins are always allowed,
    so their flag is left untouched. Returns the updated dict or None."""
    user = await session.get(User, user_id)
    if user is None:
        return None
    if user.role != "admin":
        user.can_upload = value
        await session.commit()
    return user.to_dict()
