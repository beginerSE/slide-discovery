"""社内スライド検索 — FastAPI backend."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import timezone
from pathlib import Path
from time import monotonic, perf_counter
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, literal, select, text, true, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

import config
from admin_routes import ingest_router, router as admin_router
from auth import router as auth_router
from db import (
    FTS_EXPR,
    SEARCH_EXPR,
    DriveFile,
    Slide,
    SessionLocal,
    get_session,
    init_db,
    utcnow,
)
from gemini_embed import embed_text
import thumbnail_store
from ingest import reap_orphaned_jobs, schedule_backfill_embeddings
from perf_metrics import (
    add_timing,
    begin_request,
    current_timings,
    end_request,
    format_server_timing,
    timed,
)
from search_query import (
    ParsedQuery,
    normalize_sources,
    parse_search_query,
)
from search_quality import (
    SEMANTIC_MIN_SIMILARITY,
    keyword_match_payload,
    keyword_rank_sql,
    semantic_fit_tier,
    semantic_match_payload,
)
from scheduler import start_scheduler, stop_scheduler
from thumbnail import render_thumbnail_svg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("api")

BASE_DIR = Path(__file__).parent
SEED_PATH = BASE_DIR / "slides.json"


async def _seed_if_empty() -> None:
    if not SEED_PATH.exists():
        return
    async with SessionLocal() as session:
        count = (await session.execute(select(func.count(Slide.slide_id)))).scalar() or 0
        if count > 0:
            return
        items = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        from datetime import datetime
        for it in items:
            session.add(
                Slide(
                    slide_id=it["slideId"],
                    file_id=it["fileId"],
                    file_name=it["fileName"],
                    page_no=int(it["pageNo"]),
                    slide_title=it["slideTitle"],
                    slide_text=it.get("slideText", ""),
                    industry=it["industry"],
                    client=it.get("client", ""),
                    proposal_type=it["proposalType"],
                    graph_type=it["graphType"],
                    layout_type=it["layoutType"],
                    doc_category=it.get("docCategory", ""),
                    tags=it.get("tags", []),
                    summary=it.get("summary", ""),
                    reuse_hint=it.get("reuseHint", ""),
                    thumbnail_path=it["thumbnailPath"],
                    source_url=it["sourceUrl"],
                    access_level=it.get("accessLevel", "internal"),
                    created_at=datetime.fromisoformat(
                        it["createdAt"].replace("Z", "+00:00")
                    ),
                    updated_at=datetime.fromisoformat(
                        it["updatedAt"].replace("Z", "+00:00")
                    ),
                )
            )
        await session.commit()
        log.info("seeded %d slides from slides.json", len(items))


# Backend (DB + scheduler) init status, surfaced via /api/healthz so a failed
# DB connection is diagnosable instead of crashing the container at startup.
_startup_state: dict[str, object] = {
    "dbInitialized": False,
    "dbError": None,
    "schedulerStarted": False,
}


async def _initialize_backend() -> None:
    """Initialize the DB + scheduler off the request-serving critical path.

    Cloud Run kills any container that does not start listening on ``$PORT``
    within the startup timeout. Running ``init_db()`` *before* the server binds
    means a slow or unreachable database turns into an opaque "failed to listen
    on PORT" timeout with no usable error. Running it as a background task lets
    the port open immediately, so the real cause is visible in the logs and at
    ``/api/healthz`` (degraded mode) instead.
    """
    try:
        await init_db()
        await _seed_if_empty()
        from db import SessionLocal

        async with SessionLocal() as _s:
            # Load admin-editable Confluence settings (DB) into config's cache
            # so the resolved values are correct on the very first request.
            try:
                from confluence_settings import refresh_cache

                await refresh_cache(_s)
            except Exception as exc:  # noqa: BLE001
                log.warning("confluence settings cache refresh failed: %s", exc)
            try:
                # Warm the shared initial-search facet cache while the instance
                # is starting in the background. The first user request should
                # not pay for the corpus-wide aggregation.
                await get_filters(
                    q=None,
                    industry=None,
                    client=None,
                    proposalType=None,
                    graphType=None,
                    layoutType=None,
                    docCategory=None,
                    tag=None,
                    source=None,
                    session=_s,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("search facet cache warm-up failed: %s", exc)
        # Clear jobs/files left "running"/"processing" by a previous process
        # that died mid-ingest, so they don't block scheduling or show forever.
        await reap_orphaned_jobs()
        schedule_backfill_embeddings()
        start_scheduler()
        _startup_state["schedulerStarted"] = True
        _startup_state["dbInitialized"] = True
        _startup_state["dbError"] = None
        log.info("backend initialization complete")
    except Exception as exc:  # noqa: BLE001
        _startup_state["dbInitialized"] = False
        _startup_state["dbError"] = f"{type(exc).__name__}: {exc}"
        log.exception("backend initialization failed; serving in degraded mode")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.log_config()
    task = asyncio.create_task(_initialize_backend())
    try:
        yield
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if _startup_state["schedulerStarted"]:
            stop_scheduler()


app = FastAPI(title="社内スライド検索 API", version="1.0.0", lifespan=lifespan)

_session_secret = os.environ.get("SESSION_SECRET")
if not _session_secret:
    raise RuntimeError("SESSION_SECRET is required")
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    session_cookie="slide_search_session",
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 24 * 14,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    token = begin_request()
    started = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        total_ms = (perf_counter() - started) * 1000
        add_timing("total", total_ms)
        timings = current_timings()
        server_timing = format_server_timing(timings)
        if server_timing:
            response.headers["Server-Timing"] = server_timing
        response.headers["X-Response-Time"] = f"{total_ms:.1f}ms"
        details = " ".join(f"{name}={duration:.1f}ms" for name, duration in timings)
        log.info(
            "%s %s -> %s %.1fms%s",
            request.method,
            request.url.path,
            status_code,
            total_ms,
            f" [{details}]" if details else "",
        )
        return response
    finally:
        end_request(token)


@app.get("/api/healthz")
def healthz():
    return {
        "status": "ok",
        "config": config.describe(),
        "db": {
            "initialized": bool(_startup_state["dbInitialized"]),
            "error": _startup_state["dbError"],
            "schedulerStarted": bool(_startup_state["schedulerStarted"]),
        },
    }


async def _all_slides(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(Slide))).scalars().all()
    return [r.to_dict() for r in rows]


_SLIDE_CARD_COLUMNS = (
    Slide.slide_id,
    Slide.file_id,
    Slide.file_name,
    Slide.page_no,
    Slide.slide_title,
    Slide.industry,
    Slide.client,
    Slide.proposal_type,
    Slide.graph_type,
    Slide.layout_type,
    Slide.doc_category,
    Slide.tags,
    Slide.thumbnail_path,
    Slide.source_url,
    Slide.source_type,
    Slide.access_level,
    Slide.folder_id,
    Slide.folder_name,
    Slide.doc_date,
    Slide.created_at,
    Slide.updated_at,
)


def _slide_card_dict(row) -> dict:
    """Serialize a lightweight selected-column row for slide-card templates."""
    values = row._mapping

    def iso(value):
        return (
            value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if value
            else None
        )

    doc_date = values[Slide.doc_date]
    return {
        "slideId": values[Slide.slide_id],
        "fileId": values[Slide.file_id],
        "fileName": values[Slide.file_name],
        "pageNo": values[Slide.page_no],
        "slideTitle": values[Slide.slide_title],
        "slideText": "",
        "industry": values[Slide.industry],
        "client": values[Slide.client],
        "proposalType": values[Slide.proposal_type],
        "graphType": values[Slide.graph_type],
        "layoutType": values[Slide.layout_type],
        "docCategory": values[Slide.doc_category],
        "tags": list(values[Slide.tags] or []),
        "summary": "",
        "reuseHint": "",
        "thumbnailPath": values[Slide.thumbnail_path],
        "sourceUrl": values[Slide.source_url],
        "sourceType": values[Slide.source_type],
        "accessLevel": values[Slide.access_level],
        "folderId": values[Slide.folder_id],
        "folderName": values[Slide.folder_name],
        "docDate": doc_date.isoformat() if doc_date else None,
        "createdAt": iso(values[Slide.created_at]),
        "updatedAt": iso(values[Slide.updated_at]),
    }


_wide_semantic_retrieval: ContextVar[bool] = ContextVar(
    "wide_semantic_retrieval",
    default=False,
)


def _build_keyword_where(parsed: ParsedQuery) -> tuple[Optional[str], dict]:
    """Build a SQL WHERE fragment + bind params for a parsed keyword query.

    Positive terms reuse the existing two-index predicate (FTS OR trigram
    ILIKE); exclusions use a trigram ``NOT ILIKE`` so CJK substrings are
    honoured. Returns ``(None, {})`` when there is nothing to filter."""
    params: dict = {}
    idx = 0

    def _term_pred(term: str) -> str:
        nonlocal idx
        p = f"kw{idx}"
        idx += 1
        params[f"{p}_like"] = f"%{term}%"
        # A term only contains whitespace when it came from a quoted
        # phrase ("foo bar"); the tokenizer splits everything else on
        # whitespace. For phrases, FTS would match the tokens anywhere
        # (non-adjacent), breaking phrase semantics and diverging from
        # the substring-based facet counts in /api/filters. So phrases
        # use substring ILIKE only, keeping search and facet counts consistent.
        if " " in term:
            return f"({SEARCH_EXPR} ILIKE :{p}_like)"
        params[f"{p}_fts"] = term
        return (
            f"({FTS_EXPR} @@ websearch_to_tsquery('simple', :{p}_fts) "
            f"OR {SEARCH_EXPR} ILIKE :{p}_like)"
        )

    clauses: list[str] = []
    or_parts: list[str] = []
    for group in parsed.or_groups:
        and_parts = [_term_pred(t) for t in group]
        if and_parts:
            or_parts.append("(" + " AND ".join(and_parts) + ")")
    if or_parts:
        clauses.append("(" + " OR ".join(or_parts) + ")")
    for term in parsed.excludes:
        p = f"kw{idx}"
        idx += 1
        params[f"{p}_like"] = f"%{term}%"
        clauses.append(f"({SEARCH_EXPR} NOT ILIKE :{p}_like)")

    if not clauses:
        return None, {}
    return " AND ".join(clauses), params


def _facet_filter(s: dict, industry, client, proposalType, graphType, layoutType, docCategory, tag) -> bool:
    if industry and s["industry"] != industry:
        return False
    if client and s.get("client", "") != client:
        return False
    if proposalType and s["proposalType"] != proposalType:
        return False
    if graphType and s["graphType"] != graphType:
        return False
    if layoutType and s["layoutType"] != layoutType:
        return False
    if docCategory and s.get("docCategory", "") != docCategory:
        return False
    if tag and tag not in s.get("tags", []):
        return False
    return True


@app.get("/api/slides")
async def search_slides(
    q: Optional[str] = None,
    mode: str = Query("keyword", pattern="^(keyword|semantic)$"),
    industry: Optional[str] = None,
    client: Optional[str] = None,
    proposalType: Optional[str] = None,
    graphType: Optional[str] = None,
    layoutType: Optional[str] = None,
    docCategory: Optional[str] = None,
    tag: Optional[str] = None,
    source: Optional[List[str]] = Query(None),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    q_clean = (q or "").strip()
    # パワポ / コンフル の検索対象フィルター。両方（または未指定）は None＝全件。
    source_restrict = normalize_sources(source)
    active_facets = [
        ("業界", industry),
        ("クライアント先", client),
        ("スライド種別", proposalType),
        ("グラフ", graphType),
        ("構図", layoutType),
        ("資料種別", docCategory),
        ("タグ", tag),
    ]
    facet_label = "、".join(f"{name}={val}" for name, val in active_facets if val)

    # Shared facet filters (pushed down to Postgres via indexed columns /
    # the jsonb_path_ops GIN index on `tags`).
    def _apply_facets(stmt):
        if industry:
            stmt = stmt.where(Slide.industry == industry)
        if client:
            stmt = stmt.where(Slide.client == client)
        if proposalType:
            stmt = stmt.where(Slide.proposal_type == proposalType)
        if graphType:
            stmt = stmt.where(Slide.graph_type == graphType)
        if layoutType:
            stmt = stmt.where(Slide.layout_type == layoutType)
        if docCategory:
            stmt = stmt.where(Slide.doc_category == docCategory)
        if tag:
            stmt = stmt.where(
                text("tags @> CAST(:tag_json AS jsonb)").bindparams(
                    tag_json=json.dumps([tag])
                )
            )
        if source_restrict is not None:
            stmt = stmt.where(Slide.source_type.in_(source_restrict))
        return stmt

    parsed = parse_search_query(q_clean)

    # Semantic mode: rank by Gemini embedding cosine distance.
    if mode == "semantic" and q_clean:
        # Embed only the positive terms (operators stripped); exclusions
        # are applied as hard NOT filters below.
        embed_query = " ".join(parsed.positive_terms) or q_clean
        try:
            with timed("ai_embed"):
                qvec = await embed_text(embed_query, task_type="RETRIEVAL_QUERY")
        except Exception as e:
            log.warning("semantic embed failed, falling back to keyword: %s", e)
            qvec = None
        if qvec is not None:
            distance = Slide.embedding.cosine_distance(qvec).label("distance")
            sem_stmt = select(Slide, distance).where(Slide.embedding.is_not(None))
            sem_stmt = _apply_facets(sem_stmt)
            # Public semantic search excludes the weak tail entirely, including
            # from the reported total. Conversational retrieval opts out below
            # so detailed answers and recurring-series inference keep the broad
            # source window they were designed around.
            wide_retrieval = _wide_semantic_retrieval.get()
            if not wide_retrieval:
                sem_stmt = sem_stmt.where(
                    distance <= 1.0 - SEMANTIC_MIN_SIMILARITY
                )
            # Honour exclusion terms even in semantic mode.
            for i, term in enumerate(parsed.excludes):
                sem_stmt = sem_stmt.where(
                    text(f"{SEARCH_EXPR} NOT ILIKE :sem_excl_{i}").bindparams(
                        **{f"sem_excl_{i}": f"%{term}%"}
                    )
                )
            sem_stmt = sem_stmt.order_by(
                distance.asc(),
                Slide.created_at.desc(),
            )
            if wide_retrieval:
                total_count = func.count().over().label("total_count")
                paged_stmt = (
                    sem_stmt.add_columns(total_count)
                    .limit(limit)
                    .offset(offset)
                )
                with timed("db_search"):
                    wide_rows = (await session.execute(paged_stmt)).all()
                rows = [
                    (slide_row, dist, None)
                    for slide_row, dist, _row_total in wide_rows
                ]
                if wide_rows:
                    total = int(wide_rows[0][2])
                elif offset:
                    with timed("db_search_count"):
                        total = int(
                            (
                                await session.execute(
                                    select(func.count()).select_from(
                                        sem_stmt.subquery()
                                    )
                                )
                            ).scalar()
                            or 0
                        )
                else:
                    total = 0
            else:
                # Public search applies a second, context-aware stage after the
                # absolute floor. Fetch lightweight candidates first so the
                # accepted total is exact before pagination.
                candidate_stmt = sem_stmt.with_only_columns(
                    Slide.slide_id,
                    Slide.file_id,
                    Slide.folder_id,
                    Slide.industry,
                    Slide.proposal_type,
                    Slide.doc_category,
                    distance,
                    maintain_column_froms=True,
                )
                with timed("db_search"):
                    candidate_rows = (
                        await session.execute(candidate_stmt)
                    ).all()
                accepted: list[tuple[str, float, str]] = []
                if candidate_rows:
                    leader_row = candidate_rows[0]
                    leader = {
                        "slideId": leader_row.slide_id,
                        "fileId": leader_row.file_id,
                        "folderId": leader_row.folder_id,
                        "industry": leader_row.industry,
                        "proposalType": leader_row.proposal_type,
                        "docCategory": leader_row.doc_category,
                    }
                    leader_similarity = 1.0 - float(leader_row.distance)
                    for row in candidate_rows:
                        similarity = 1.0 - float(row.distance)
                        candidate = {
                            "slideId": row.slide_id,
                            "fileId": row.file_id,
                            "folderId": row.folder_id,
                            "industry": row.industry,
                            "proposalType": row.proposal_type,
                            "docCategory": row.doc_category,
                        }
                        tier = semantic_fit_tier(
                            candidate,
                            leader,
                            similarity,
                            leader_similarity,
                        )
                        if tier:
                            accepted.append((row.slide_id, similarity, tier))
                total = len(accepted)
                page = accepted[offset : offset + limit]
                page_ids = [slide_id for slide_id, _, _ in page]
                if page_ids:
                    with timed("db_search_page"):
                        page_slides = (
                            (
                                await session.execute(
                                    select(Slide).where(
                                        Slide.slide_id.in_(page_ids)
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                    slides_by_id = {
                        slide.slide_id: slide for slide in page_slides
                    }
                    rows = [
                        (slides_by_id[slide_id], 1.0 - similarity, tier)
                        for slide_id, similarity, tier in page
                    ]
                else:
                    rows = []
            items: list[dict] = []
            for slide_row, dist, fit_tier in rows:
                s = slide_row.to_dict()
                similarity = max(0.0, min(1.0, 1.0 - float(dist)))
                s["similarityScore"] = round(similarity, 3)
                if fit_tier:
                    s["semanticFitTier"] = fit_tier
                s.update(semantic_match_payload(s, parsed, similarity))
                items.append(s)
            return {"total": int(total), "items": items}

    # Keyword (default) mode: push all filters down to Postgres so the
    # GIN / btree indexes can do the work instead of looping in Python.
    stmt = select(Slide)
    stmt = _apply_facets(stmt)
    if q_clean:
        # Parsed into AND/OR groups + exclusions. Each positive term keeps
        # the two index-backed predicates OR'd together so the planner can
        # BitmapOr both GIN indexes:
        #   * FTS_EXPR @@ websearch_to_tsquery(...)  -> slides_fts_idx
        #   * SEARCH_EXPR ILIKE %term%               -> slides_search_trgm_idx
        # Exclusions add `SEARCH_EXPR NOT ILIKE %term%`.
        where_sql, where_params = _build_keyword_where(parsed)
        if where_sql:
            stmt = stmt.where(text(where_sql).bindparams(**where_params))

    total_count = func.count().over().label("total_count")
    stmt = stmt.add_columns(total_count)
    if q_clean:
        rank_sql, rank_params = keyword_rank_sql(parsed)
        stmt = stmt.order_by(
            text(f"({rank_sql}) DESC").bindparams(**rank_params),
            Slide.created_at.desc(),
            Slide.slide_id.asc(),
        )
    else:
        stmt = stmt.order_by(Slide.created_at.desc(), Slide.slide_id.asc())
    stmt = stmt.limit(limit).offset(offset)
    with timed("db_search"):
        rows = (await session.execute(stmt)).all()
    if rows:
        total = int(rows[0][1])
    elif offset:
        with timed("db_search_count"):
            total = int(
                (
                    await session.execute(
                        select(func.count()).select_from(
                            stmt.limit(None).offset(None).subquery()
                        )
                    )
                ).scalar()
                or 0
            )
    else:
        total = 0

    items = []
    for row, _row_total in rows:
        s = row.to_dict()
        if q_clean:
            s.update(keyword_match_payload(s, parsed))
        elif facet_label:
            s["matchReason"] = f"フィルター一致: {facet_label}"
            s["matchEvidence"] = []
            s["matchSnippet"] = None
        else:
            s["matchReason"] = "全件表示"
            s["matchEvidence"] = []
            s["matchSnippet"] = None
        items.append(s)
    return {"total": int(total), "items": items}


class AskBody(BaseModel):
    question: str
    topK: int = 8
    seriesId: Optional[str] = None
    # パワポ / コンフル の検索対象フィルター（search_slides と同じ意味）。
    sources: Optional[List[str]] = None


@app.post("/api/ask")
async def ask_question(
    body: AskBody, session: AsyncSession = Depends(get_session)
):
    """Conversational ("対話検索") answer. Retrieves the most relevant slides
    via semantic search, then asks Gemini to answer grounded ONLY in those
    slides, citing source file + page (NotebookLM-style). Degrades
    gracefully: if answer generation fails (e.g. no GEMINI_API_KEY) we still
    return the retrieved source slides with answer=None."""
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")

    from gemini_chat import generate_answer, is_overview_question, should_use_series
    from series import recent_series_context

    # 概要・経緯タイプの質問（「概要を教えて」「これまでの経緯は?」）は、
    # ピンポイント検索ではなく統合的・詳細な説明モードで扱う。
    overview = is_overview_question(question)

    top_k = max(1, min(int(body.topK or 8), 20))
    if overview:
        # 俯瞰質問は材料が多いほど良い（シリーズが特定できない場合の保険も兼ねる）
        top_k = max(top_k, 16)
    with timed("chat_retrieval"):
        token = _wide_semantic_retrieval.set(True)
        try:
            res = await search_slides(
                q=question,
                mode="semantic",
                limit=top_k,
                offset=0,
                source=body.sources,
                session=session,
            )
        finally:
            _wide_semantic_retrieval.reset(token)
    sources = res["items"]
    if not sources:
        return {
            "question": question,
            "answer": "該当する資料は見つかりませんでした。",
            "sources": [],
            "degraded": False,
            "topK": top_k,
        }

    # Detect the 定例シリーズ (recurring-meeting series = Drive folder): use an
    # explicit seriesId if given, else infer it from the top hit's folder.
    series_id = (body.seriesId or "").strip()
    explicit_series = bool(series_id)
    if not series_id:
        series_id = (sources[0].get("folderId") or "").strip()
    series_context: list[dict] = []
    series_name = ""
    if series_id:
        try:
            # 概要モードはシリーズ全体を広く読む（ファイル数・枚数とも拡大）
            if overview:
                with timed("db_series"):
                    series_context = await recent_series_context(
                        session, series_id, limit_files=12, per_file=8
                    )
            else:
                with timed("db_series"):
                    series_context = await recent_series_context(session, series_id)
            # Prefer a name already in the retrieved sources; otherwise (e.g.
            # an explicit seriesId whose folder is absent from the top hits)
            # look it up so the UI label is correct.
            series_name = next(
                (
                    s.get("folderName")
                    for s in sources
                    if s.get("folderId") == series_id and s.get("folderName")
                ),
                "",
            )
            if not series_name:
                row = (
                    await session.execute(
                        select(DriveFile.folder_name)
                        .where(DriveFile.folder_id == series_id)
                        .where(DriveFile.folder_name != "")
                        .limit(1)
                    )
                ).scalar_one_or_none()
                series_name = row or ""
            # 自動判定 mode: let the AI judge — from the file hierarchy (the
            # folder's dated files) plus the question — whether the series'
            # time-series flow is actually relevant. An explicit user choice is
            # always honored and skips the judge.
            # 概要モードでは時系列こそが本題なので判定をスキップして常に使う。
            if not explicit_series and series_context and not overview:
                keep = await should_use_series(
                    question, series_name, series_context
                )
                if not keep:
                    series_context = []
        except Exception as e:
            log.warning("ask: series context failed: %s", e)

    answer: Optional[str] = None
    degraded = False
    try:
        with timed("ai_answer"):
            answer = await generate_answer(
                question, sources, series=series_context or None, overview=overview
            )
    except Exception as e:
        log.warning("ask: answer generation failed: %s", e)
        degraded = True
    return {
        "question": question,
        "answer": answer,
        "sources": sources,
        "degraded": degraded,
        "seriesId": series_id,
        "seriesName": series_name,
        "seriesCount": len(series_context),
        "topK": top_k,
    }


@app.get("/api/slides/{slide_id}")
async def get_slide(slide_id: str, session: AsyncSession = Depends(get_session)):
    row = await session.get(Slide, slide_id)
    if not row:
        raise HTTPException(status_code=404, detail="slide not found")
    return row.to_dict()


@app.get("/api/slides/{slide_id}/similar")
async def get_similar(slide_id: str, session: AsyncSession = Depends(get_session)):
    with timed("db_slide"):
        src_row = await session.get(Slide, slide_id)
    if not src_row:
        raise HTTPException(status_code=404, detail="slide not found")
    return await _get_similar_for_row(src_row, session)


async def _get_similar_for_row(
    src_row: Slide, session: AsyncSession, *, compact: bool = False
) -> list[dict]:
    slide_id = src_row.slide_id
    src = src_row.to_dict()
    src_tags = set(src.get("tags", []))

    # Fetch only the columns needed to score candidates. The previous
    # implementation loaded every slide's full text and embedding into Python,
    # which made a detail-page navigation scale with the entire corpus payload.
    columns = (
        Slide.slide_id,
        Slide.industry,
        Slide.proposal_type,
        Slide.graph_type,
        Slide.layout_type,
        Slide.doc_category,
        Slide.tags,
    )
    src_vec = src_row.embedding
    if src_vec is not None:
        distance = Slide.embedding.cosine_distance(src_vec).label("distance")
        candidate_stmt = select(*columns, distance).where(Slide.slide_id != slide_id)
    else:
        candidate_stmt = select(*columns).where(Slide.slide_id != slide_id)

    with timed("db_similar_candidates"):
        candidate_rows = (await session.execute(candidate_stmt)).all()

    scored: list[tuple[str, float, list[str]]] = []
    for row in candidate_rows:
        sid = row[0]
        industry = row[1] or ""
        proposal_type = row[2] or ""
        graph_type = row[3] or ""
        layout_type = row[4] or ""
        doc_category = row[5] or ""
        tags = list(row[6] or [])
        dist = row[7] if src_vec is not None else None
        score = 0.0
        reasons: list[str] = []
        if industry == src["industry"]:
            score += 1
            reasons.append(f"業界が同じ ({industry})")
        if proposal_type == src["proposalType"]:
            score += 1
            reasons.append(f"スライド種別が同じ ({proposal_type})")
        if graph_type == src["graphType"] and graph_type != "なし":
            score += 2
            reasons.append(f"グラフ種別が同じ ({graph_type})")
        if layout_type == src["layoutType"]:
            score += 2
            reasons.append(f"構図が同じ ({layout_type})")
        if doc_category and doc_category == src.get("docCategory"):
            score += 1
            reasons.append(f"資料種別が同じ ({doc_category})")
        overlap = src_tags & set(tags)
        if overlap:
            score += len(overlap)
            reasons.append(f'共通タグ: {"、".join(sorted(overlap))}')

        facet_score = min(score / 8.0, 1.0)
        sem_score = (
            max(0.0, min(1.0, 1.0 - float(dist))) if dist is not None else None
        )
        if sem_score is not None:
            combined = 0.6 * facet_score + 0.4 * sem_score
            if sem_score >= 0.6:
                reasons.append(f'意味が近い (類似度 {sem_score:.2f})')
        else:
            combined = facet_score

        if combined <= 0 and not reasons:
            continue
        scored.append((sid, combined, reasons))

    scored.sort(key=lambda item: item[1], reverse=True)
    top = scored[:8]
    if not top:
        return []
    top_ids = [sid for sid, _score, _reasons in top]
    with timed("db_similar_results"):
        if compact:
            result_rows = (
                await session.execute(
                    select(*_SLIDE_CARD_COLUMNS).where(Slide.slide_id.in_(top_ids))
                )
            ).all()
            by_id = {
                row._mapping[Slide.slide_id]: _slide_card_dict(row)
                for row in result_rows
            }
        else:
            full_rows = (
                await session.execute(
                    select(Slide).where(Slide.slide_id.in_(top_ids))
                )
            ).scalars().all()
            by_id = {row.slide_id: row.to_dict() for row in full_rows}
    out: list[dict] = []
    for sid, combined, reasons in top:
        item = by_id.get(sid)
        if item is None:
            continue
        item = dict(item)
        item["similarityScore"] = round(combined, 3)
        item["similarityReason"] = " / ".join(reasons) if reasons else "意味が近い"
        out.append(item)
    return out


_BASE_FILTERS_CACHE_TTL = 60.0
_base_filters_cache: tuple[float, dict] | None = None
_base_filters_lock = asyncio.Lock()


@app.get("/api/filters")
async def get_filters(
    q: Optional[str] = None,
    industry: Optional[str] = None,
    client: Optional[str] = None,
    proposalType: Optional[str] = None,
    graphType: Optional[str] = None,
    layoutType: Optional[str] = None,
    docCategory: Optional[str] = None,
    tag: Optional[str] = None,
    source: Optional[List[str]] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """Return facet counts. Counts are contextual: when computing the
    counts for facet field X, all *other* active filters and the keyword
    query are applied, but the filter on X itself is excluded. This is
    standard faceted-search behaviour — each chip's number tells the
    user how many slides they'd see if they added that chip on top of
    their current selection (so they never see a 0-result chip for the
    fields they're actively narrowing on, and they can still discover
    other values within the same field)."""

    global _base_filters_cache

    q_clean = (q or "").strip()
    source_restrict = normalize_sources(source)
    parsed = parse_search_query(q_clean)
    selected = {
        "industry": industry,
        "client": client,
        "proposalType": proposalType,
        "graphType": graphType,
        "layoutType": layoutType,
        "docCategory": docCategory,
        "tag": tag,
    }
    cacheable = (
        not q_clean
        and not any(selected.values())
        and source_restrict is None
    )
    now = monotonic()
    if cacheable and _base_filters_cache is not None:
        cached_at, cached = _base_filters_cache
        if now - cached_at < _BASE_FILTERS_CACHE_TTL:
            return copy.deepcopy(cached)

    async def _compute() -> dict:
        where_sql, where_params = _build_keyword_where(parsed)
        columns = {
            "industry": Slide.industry,
            "client": Slide.client,
            "proposalType": Slide.proposal_type,
            "graphType": Slide.graph_type,
            "layoutType": Slide.layout_type,
            "docCategory": Slide.doc_category,
        }

        def apply_context(stmt, skip_field: str | None = None):
            if source_restrict is not None:
                stmt = stmt.where(Slide.source_type.in_(source_restrict))
            if where_sql:
                stmt = stmt.where(text(where_sql).bindparams(**where_params))
            for field, column in columns.items():
                value = selected[field]
                if field != skip_field and value:
                    stmt = stmt.where(column == value)
            if skip_field != "tag" and selected["tag"]:
                stmt = stmt.where(
                    text("tags @> CAST(:facet_tag_json AS jsonb)").bindparams(
                        facet_tag_json=json.dumps([selected["tag"]])
                    )
                )
            return stmt

        statements = []
        for field, column in columns.items():
            stmt = select(
                literal(field).label("facet"),
                column.label("value"),
                func.count().label("count"),
            ).where(column != "")
            statements.append(apply_context(stmt, field).group_by(column))

        tag_values = (
            func.jsonb_array_elements_text(Slide.tags)
            .table_valued("value")
            .alias("tag_values")
        )
        tag_stmt = (
            select(
                literal("tag").label("facet"),
                tag_values.c.value.label("value"),
                func.count().label("count"),
            )
            .select_from(Slide)
            .join(tag_values, true())
            .where(tag_values.c.value != "")
        )
        tag_stmt = apply_context(tag_stmt, "tag").group_by(tag_values.c.value)
        tag_grouped = tag_stmt.order_by(func.count().desc()).limit(40).subquery()
        statements.append(
            select(
                tag_grouped.c.facet,
                tag_grouped.c.value,
                tag_grouped.c.count,
            )
        )

        # Corpus metadata rides on the same round trip. It is deliberately
        # independent of active filters: source-toggle visibility and home-page
        # totals describe the whole indexed corpus.
        statements.extend(
            [
                select(
                    literal("_source").label("facet"),
                    Slide.source_type.label("value"),
                    func.count().label("count"),
                ).group_by(Slide.source_type),
                select(
                    literal("_meta").label("facet"),
                    literal("totalSlides").label("value"),
                    func.count(Slide.slide_id).label("count"),
                ),
                select(
                    literal("_meta").label("facet"),
                    literal("totalFiles").label("value"),
                    func.count(func.distinct(Slide.file_id)).label("count"),
                ),
            ]
        )

        grouped = union_all(*statements).subquery()
        aggregate_stmt = select(
            grouped.c.facet, grouped.c.value, grouped.c.count
        ).order_by(grouped.c.facet, grouped.c.count.desc(), grouped.c.value)
        with timed("db_filters"):
            rows = (await session.execute(aggregate_stmt)).all()

        keys = {
            "industry": "industries",
            "client": "clients",
            "proposalType": "proposalTypes",
            "graphType": "graphTypes",
            "layoutType": "layoutTypes",
            "docCategory": "docCategories",
            "tag": "tags",
        }
        result = {key: [] for key in keys.values()}
        result.update(totalSlides=0, totalFiles=0, hasConfluence=False)
        for facet_name, value, count in rows:
            count = int(count or 0)
            if facet_name in keys:
                result[keys[facet_name]].append({"value": value, "count": count})
            elif facet_name == "_source" and value == "confluence" and count > 0:
                result["hasConfluence"] = True
            elif facet_name == "_meta" and value in ("totalSlides", "totalFiles"):
                result[value] = count
        return result

    if not cacheable:
        return await _compute()
    async with _base_filters_lock:
        now = monotonic()
        if _base_filters_cache is not None:
            cached_at, cached = _base_filters_cache
            if now - cached_at < _BASE_FILTERS_CACHE_TTL:
                return copy.deepcopy(cached)
        computed = await _compute()
        _base_filters_cache = (monotonic(), computed)
        return copy.deepcopy(computed)


@app.get("/api/stats")
async def get_stats(session: AsyncSession = Depends(get_session)):
    filters = await get_filters(
        q=None,
        industry=None,
        client=None,
        proposalType=None,
        graphType=None,
        layoutType=None,
        docCategory=None,
        tag=None,
        source=None,
        session=session,
    )
    return await build_stats(session, filters)


async def build_stats(session: AsyncSession, filters: dict) -> dict:
    with timed("db_recent"):
        recent_rows = (
            await session.execute(
                select(*_SLIDE_CARD_COLUMNS)
                .order_by(Slide.created_at.desc())
                .limit(6)
            )
        ).all()
    return {
        "totalSlides": int(filters.get("totalSlides") or 0),
        "totalFiles": int(filters.get("totalFiles") or 0),
        "industries": filters.get("industries") or [],
        "proposalTypes": filters.get("proposalTypes") or [],
        "graphTypes": filters.get("graphTypes") or [],
        "layoutTypes": filters.get("layoutTypes") or [],
        "docCategories": filters.get("docCategories") or [],
        "recentSlides": [_slide_card_dict(row) for row in recent_rows],
    }


@app.get("/api/thumbnails/{slide_id}.svg")
async def get_thumbnail_svg(
    slide_id: str, session: AsyncSession = Depends(get_session)
):
    row = await session.get(Slide, slide_id)
    if not row:
        raise HTTPException(status_code=404, detail="slide not found")
    svg = render_thumbnail_svg(row.to_dict())
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/api/thumbnails/files/{file_id}/{page_no}.png")
async def get_thumbnail_png(file_id: str, page_no: int):
    data = await thumbnail_store.get(file_id, page_no)
    if data is None:
        raise HTTPException(status_code=404, detail="thumbnail not found")
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(ingest_router)

# Static assets + server-rendered HTML UI. The web router is included LAST
# so its catch-all 404 route never shadows the JSON API routes above.
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

from web_routes import templates, web_router  # noqa: E402

app.include_router(web_router)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # API stays JSON; the HTML UI gets a rendered error page so users never
    # see a raw JSON body in the browser.
    if request.url.path.startswith("/api"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail},
        )
    template = "forbidden.html" if exc.status_code == 403 else "not_found.html"
    return templates.TemplateResponse(
        request,
        template,
        {"user": None, "active_nav": ""},
        status_code=exc.status_code,
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
