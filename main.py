"""社内スライド検索 — FastAPI backend."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import Counter
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, text
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
from search_query import (
    ParsedQuery,
    normalize_sources,
    parse_search_query,
    query_matches,
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
        # Load admin-editable Confluence settings (DB) into config's cache so
        # the resolved values are correct on the very first request.
        try:
            from confluence_settings import refresh_cache
            from db import SessionLocal

            async with SessionLocal() as _s:
                await refresh_cache(_s)
        except Exception as exc:  # noqa: BLE001
            log.warning("confluence settings cache refresh failed: %s", exc)
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
    response = await call_next(request)
    log.info("%s %s -> %s", request.method, request.url.path, response.status_code)
    return response


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


def _match_reason_for(slide: dict, parsed: ParsedQuery) -> str:
    terms = parsed.positive_terms
    if not terms:
        return ""
    haystacks = [
        ("slideTitle", slide["slideTitle"]),
        ("summary", slide.get("summary", "")),
        ("slideText", slide.get("slideText", "")),
        ("tags", " ".join(slide.get("tags", []))),
        ("client", slide.get("client", "")),
        ("fileName", slide.get("fileName", "")),
    ]
    labels = {
        "slideTitle": "タイトル一致",
        "summary": "概要一致",
        "slideText": "本文一致",
        "tags": "タグ一致",
        "client": "クライアント一致",
        "fileName": "ファイル名一致",
    }
    # Report the field/term of the first positive term that hits, so the
    # reason stays meaningful for multi-term (AND/OR) queries.
    for term in terms:
        needle = term.lower()
        for field_name, value in haystacks:
            if value and needle in value.lower():
                return f'{labels[field_name]}: 「{term}」'
    return f'一致: 「{terms[0]}」'


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
        # use substring ILIKE only — which is exactly what query_matches
        # does in-memory, keeping search and facet counts consistent.
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
            qvec = await embed_text(embed_query, task_type="RETRIEVAL_QUERY")
        except Exception as e:
            log.warning("semantic embed failed, falling back to keyword: %s", e)
            qvec = None
        if qvec is not None:
            distance = Slide.embedding.cosine_distance(qvec).label("distance")
            sem_stmt = select(Slide, distance).where(Slide.embedding.is_not(None))
            sem_stmt = _apply_facets(sem_stmt)
            # Honour exclusion terms even in semantic mode.
            for i, term in enumerate(parsed.excludes):
                sem_stmt = sem_stmt.where(
                    text(f"{SEARCH_EXPR} NOT ILIKE :sem_excl_{i}").bindparams(
                        **{f"sem_excl_{i}": f"%{term}%"}
                    )
                )
            count_stmt = select(func.count()).select_from(sem_stmt.subquery())
            total = (await session.execute(count_stmt)).scalar() or 0
            sem_stmt = sem_stmt.order_by(distance.asc()).limit(limit).offset(offset)
            rows = (await session.execute(sem_stmt)).all()
            items: list[dict] = []
            for slide_row, dist in rows:
                s = slide_row.to_dict()
                similarity = max(0.0, min(1.0, 1.0 - float(dist)))
                s["similarityScore"] = round(similarity, 3)
                s["matchReason"] = (
                    f"自然文検索: 「{q_clean}」と意味が近い "
                    f"(類似度 {s['similarityScore']:.2f})"
                )
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

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    stmt = stmt.order_by(Slide.created_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()

    items = []
    for row in rows:
        s = row.to_dict()
        if q_clean:
            match_reason = _match_reason_for(s, parsed)
        elif facet_label:
            match_reason = f"フィルター一致: {facet_label}"
        else:
            match_reason = "全件表示"
        s["matchReason"] = match_reason
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

    top_k = max(1, min(int(body.topK or 8), 20))
    res = await search_slides(
        q=question,
        mode="semantic",
        limit=top_k,
        offset=0,
        source=body.sources,
        session=session,
    )
    sources = res["items"]
    if not sources:
        return {
            "question": question,
            "answer": "該当する資料は見つかりませんでした。",
            "sources": [],
            "degraded": False,
        }

    from gemini_chat import generate_answer, should_use_series
    from series import recent_series_context

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
            if not explicit_series and series_context:
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
        answer = await generate_answer(
            question, sources, series=series_context or None
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
    }


@app.get("/api/slides/{slide_id}")
async def get_slide(slide_id: str, session: AsyncSession = Depends(get_session)):
    row = await session.get(Slide, slide_id)
    if not row:
        raise HTTPException(status_code=404, detail="slide not found")
    return row.to_dict()


@app.get("/api/slides/{slide_id}/similar")
async def get_similar(slide_id: str, session: AsyncSession = Depends(get_session)):
    src_row = await session.get(Slide, slide_id)
    if not src_row:
        raise HTTPException(status_code=404, detail="slide not found")
    src = src_row.to_dict()
    src_tags = set(src.get("tags", []))

    # Vector distances (cosine) — keyed by slide_id, if source has an embedding.
    vec_sim: dict[str, float] = {}
    src_vec = src_row.embedding
    if src_vec is not None:
        distance = Slide.embedding.cosine_distance(src_vec).label("distance")
        stmt = (
            select(Slide.slide_id, distance)
            .where(Slide.embedding.is_not(None))
            .where(Slide.slide_id != slide_id)
        )
        for sid, dist in (await session.execute(stmt)).all():
            vec_sim[sid] = max(0.0, min(1.0, 1.0 - float(dist)))

    all_slides = await _all_slides(session)
    scored = []
    for s in all_slides:
        if s["slideId"] == slide_id:
            continue
        score = 0.0
        reasons: list[str] = []
        if s["industry"] == src["industry"]:
            score += 1
            reasons.append(f'業界が同じ ({s["industry"]})')
        if s["proposalType"] == src["proposalType"]:
            score += 1
            reasons.append(f'スライド種別が同じ ({s["proposalType"]})')
        if s["graphType"] == src["graphType"] and s["graphType"] != "なし":
            score += 2
            reasons.append(f'グラフ種別が同じ ({s["graphType"]})')
        if s["layoutType"] == src["layoutType"]:
            score += 2
            reasons.append(f'構図が同じ ({s["layoutType"]})')
        if s.get("docCategory") and s.get("docCategory") == src.get("docCategory"):
            score += 1
            reasons.append(f'資料種別が同じ ({s["docCategory"]})')
        overlap = src_tags & set(s.get("tags", []))
        if overlap:
            score += len(overlap)
            reasons.append(f'共通タグ: {"、".join(sorted(overlap))}')

        # Facet/tag score normalized to 0..1 (cap at 8 like before).
        facet_score = min(score / 8.0, 1.0)
        sem_score = vec_sim.get(s["slideId"])
        if sem_score is not None:
            # Blend: 60% facets/tags, 40% semantic similarity.
            combined = 0.6 * facet_score + 0.4 * sem_score
            if sem_score >= 0.6:
                reasons.append(f'意味が近い (類似度 {sem_score:.2f})')
        else:
            combined = facet_score

        if combined <= 0 and not reasons:
            continue
        item = dict(s)
        item["similarityScore"] = round(combined, 3)
        item["similarityReason"] = " / ".join(reasons) if reasons else "意味が近い"
        scored.append(item)
    scored.sort(key=lambda x: x["similarityScore"], reverse=True)
    return scored[:8]


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

    q_clean = (q or "").strip()
    slides = await _all_slides(session)
    # Keep facet counts consistent with the パワポ / コンフル search filter.
    source_restrict = normalize_sources(source)
    if source_restrict is not None:
        slides = [s for s in slides if s.get("sourceType") in source_restrict]
    parsed = parse_search_query(q_clean)

    # Pre-filter once with q. Uses the same AND/OR/exclusion parsing as
    # the search endpoint so facet counts agree with actual results.
    def _matches_query(s: dict) -> bool:
        if parsed.is_empty:
            return True
        haystack = " ".join(
            [
                s.get("slideTitle") or "",
                s.get("slideText") or "",
                s.get("summary") or "",
                s.get("fileName") or "",
                " ".join(s.get("tags") or []),
                s.get("industry") or "",
                s.get("client") or "",
                s.get("proposalType") or "",
                s.get("graphType") or "",
                s.get("layoutType") or "",
                s.get("docCategory") or "",
            ]
        )
        return query_matches(parsed, haystack)

    q_filtered = [s for s in slides if _matches_query(s)]

    selected = {
        "industry": industry,
        "client": client,
        "proposalType": proposalType,
        "graphType": graphType,
        "layoutType": layoutType,
        "docCategory": docCategory,
        "tag": tag,
    }

    def _apply_others(rows: list[dict], skip_field: str) -> list[dict]:
        """Apply every active filter except the one for skip_field."""
        out = rows
        if skip_field != "industry" and selected["industry"]:
            out = [s for s in out if s.get("industry") == selected["industry"]]
        if skip_field != "client" and selected["client"]:
            out = [s for s in out if s.get("client") == selected["client"]]
        if skip_field != "proposalType" and selected["proposalType"]:
            out = [s for s in out if s.get("proposalType") == selected["proposalType"]]
        if skip_field != "graphType" and selected["graphType"]:
            out = [s for s in out if s.get("graphType") == selected["graphType"]]
        if skip_field != "layoutType" and selected["layoutType"]:
            out = [s for s in out if s.get("layoutType") == selected["layoutType"]]
        if skip_field != "docCategory" and selected["docCategory"]:
            out = [s for s in out if s.get("docCategory") == selected["docCategory"]]
        if skip_field != "tag" and selected["tag"]:
            out = [s for s in out if selected["tag"] in (s.get("tags") or [])]
        return out

    def facet(field: str) -> list[dict]:
        rows = _apply_others(q_filtered, field)
        c = Counter(s[field] for s in rows if s.get(field))
        return [{"value": v, "count": n} for v, n in c.most_common()]

    tag_rows = _apply_others(q_filtered, "tag")
    tags_counter: Counter[str] = Counter()
    for s in tag_rows:
        tags_counter.update(s.get("tags") or [])
    # Cap tag chips at 40 so the panel doesn't explode when the dataset
    # grows; the most common tags are what users actually scan for.
    tag_facets = [
        {"value": v, "count": n} for v, n in tags_counter.most_common(40)
    ]

    return {
        "industries": facet("industry"),
        "clients": facet("client"),
        "proposalTypes": facet("proposalType"),
        "graphTypes": facet("graphType"),
        "layoutTypes": facet("layoutType"),
        "docCategories": facet("docCategory"),
        "tags": tag_facets,
    }


@app.get("/api/stats")
async def get_stats(session: AsyncSession = Depends(get_session)):
    slides = await _all_slides(session)
    files = {s["fileId"] for s in slides}
    recent = sorted(slides, key=lambda s: s["createdAt"], reverse=True)[:6]

    def facet(field: str) -> list[dict]:
        c = Counter(s[field] for s in slides if s.get(field))
        return [{"value": v, "count": n} for v, n in c.most_common()]

    return {
        "totalSlides": len(slides),
        "totalFiles": len(files),
        "industries": facet("industry"),
        "proposalTypes": facet("proposalType"),
        "graphTypes": facet("graphType"),
        "layoutTypes": facet("layoutType"),
        "docCategories": facet("docCategory"),
        "recentSlides": recent,
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
