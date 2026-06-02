"""Ingest orchestrator: download → parse → thumbnails → Gemini → DB."""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from db import DriveFile, SessionLocal, Slide, utcnow
from drive import DownloadResult, download, view_url
from gemini_embed import build_slide_embed_text, embed_text
from gemini_extract import extract_metadata
from pptx_pipeline import SlideExtract, extract_slides, render_thumbnails

log = logging.getLogger("ingest")

# Where PNG thumbnails are persisted and served from.
THUMB_ROOT = Path(__file__).parent / "data" / "thumbnails"


class JobState:
    """Single-flight in-memory job state."""

    def __init__(self) -> None:
        self.status: str = "idle"  # idle | running | done | failed
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.total: int = 0
        self.processed: int = 0
        self.failed: int = 0
        self.current_file: str | None = None
        self.stage: str | None = None
        self.current_file_page: int | None = None
        self.current_file_total: int | None = None
        self.message: str | None = None
        self._lock = asyncio.Lock()

    def set_stage(
        self,
        stage: str | None,
        page: int | None = None,
        total: int | None = None,
    ) -> None:
        self.stage = stage
        self.current_file_page = page
        self.current_file_total = total

    def snapshot(self) -> dict:
        def iso(d: datetime | None) -> str | None:
            return (
                d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if d
                else None
            )

        return {
            "status": self.status,
            "startedAt": iso(self.started_at),
            "finishedAt": iso(self.finished_at),
            "total": self.total,
            "processed": self.processed,
            "failed": self.failed,
            "currentFile": self.current_file,
            "stage": self.stage,
            "currentFilePage": self.current_file_page,
            "currentFileTotal": self.current_file_total,
            "message": self.message,
        }


JOB = JobState()


def _safe_name(file_id: str) -> str:
    return "".join(c for c in file_id if c.isalnum() or c in "-_")


async def _ingest_one(
    session_factory,
    drive_file: DriveFile,
    force: bool,
) -> tuple[int, int]:
    """Ingest a single DriveFile row. Returns (slides_added, slides_skipped)."""
    file_id = drive_file.drive_file_id
    JOB.current_file = drive_file.file_name or file_id
    JOB.set_stage("ダウンロード中")

    async with session_factory() as session:
        db_row = await session.get(DriveFile, drive_file.id)
        if db_row is None:
            return (0, 0)
        db_row.status = "processing"
        db_row.last_error = None
        await session.commit()

    tmp = Path(tempfile.mkdtemp(prefix="drv_"))
    slides_added = 0
    try:
        dl: DownloadResult = await download(file_id, tmp)
        # Filename is known after download — show it in the progress UI.
        JOB.current_file = dl.file_name or file_id

        if (
            not force
            and drive_file.last_size == dl.size
            and drive_file.last_ingested_at is not None
        ):
            async with session_factory() as session:
                db_row = await session.get(DriveFile, drive_file.id)
                if db_row:
                    db_row.status = "ready"
                    db_row.file_name = dl.file_name
                    await session.commit()
            log.info("skip unchanged %s", file_id)
            JOB.set_stage(None)
            return (0, 0)

        # Parse and render in worker thread (CPU/IO heavy)
        JOB.set_stage("スライド解析中")
        extracts: list[SlideExtract] = await asyncio.to_thread(
            extract_slides, dl.path
        )
        n_pages = len(extracts)
        JOB.set_stage("サムネイル生成中", page=0, total=n_pages)
        thumb_out = THUMB_ROOT / _safe_name(file_id)
        if thumb_out.exists():
            shutil.rmtree(thumb_out)
        thumb_paths = await asyncio.to_thread(render_thumbnails, dl.path, thumb_out)
        # Map page_no -> thumbnail
        for i, ex in enumerate(extracts):
            if i < len(thumb_paths):
                ex.thumbnail_path = thumb_paths[i]

        # Gemini metadata extraction — track completion per page
        meta_done = 0
        JOB.set_stage("メタ情報抽出中（Gemini）", page=0, total=n_pages)
        sem = asyncio.Semaphore(2)

        async def _meta(ex: SlideExtract) -> dict:
            nonlocal meta_done
            async with sem:
                try:
                    result = await extract_metadata(
                        slide_text=f"{ex.title}\n\n{ex.body_text}",
                        thumbnail=ex.thumbnail_path,
                        file_name=dl.file_name,
                        page_no=ex.page_no,
                    )
                except Exception as e:
                    log.warning(
                        "gemini failed for %s p%d: %s", file_id, ex.page_no, e
                    )
                    result = {
                        "industry": "その他",
                        "proposalType": "その他",
                        "graphType": "なし",
                        "layoutType": "タイトル中央",
                        "tags": [],
                        "summary": ex.body_text[:100],
                        "reuseHint": "",
                    }
            meta_done += 1
            JOB.set_stage(
                "メタ情報抽出中（Gemini）", page=meta_done, total=n_pages
            )
            return result

        gemini_results = await asyncio.gather(*[_meta(e) for e in extracts])

        # Embeddings (semantic search). Track completion per page.
        embed_done = 0
        JOB.set_stage("ベクトル埋め込み生成中", page=0, total=n_pages)
        embed_sem = asyncio.Semaphore(4)

        async def _embed(ex: SlideExtract, meta: dict) -> list[float] | None:
            nonlocal embed_done
            text = build_slide_embed_text(
                title=ex.title,
                summary=meta.get("summary", ""),
                body_text=ex.body_text,
                industry=meta.get("industry", ""),
                proposal_type=meta.get("proposalType", ""),
                graph_type=meta.get("graphType", ""),
                layout_type=meta.get("layoutType", ""),
                tags=meta.get("tags", []),
                client=meta.get("client", ""),
            )
            async with embed_sem:
                try:
                    vec = await embed_text(text, task_type="RETRIEVAL_DOCUMENT")
                except Exception as e:
                    log.warning(
                        "embed failed for %s p%d: %s", file_id, ex.page_no, e
                    )
                    vec = None
            embed_done += 1
            JOB.set_stage(
                "ベクトル埋め込み生成中", page=embed_done, total=n_pages
            )
            return vec

        embeddings = await asyncio.gather(
            *[_embed(e, m) for e, m in zip(extracts, gemini_results)]
        )

        # Persist: delete existing slides for this file, insert fresh
        JOB.set_stage("DB保存中", page=n_pages, total=n_pages)
        async with session_factory() as session:
            db_row = await session.get(DriveFile, drive_file.id)
            # An admin-chosen display name (set on a name collision) overrides
            # Drive's raw name for what users see / search by.
            eff_name = (
                db_row.display_name if db_row and db_row.display_name else dl.file_name
            )
            await session.execute(
                delete(Slide).where(Slide.file_id == file_id)
            )
            now = utcnow()
            for ex, meta, emb in zip(extracts, gemini_results, embeddings):
                slide_id = f"gd-{_safe_name(file_id)}-p{ex.page_no:03d}"
                thumb_url = (
                    f"/api/thumbnails/files/{_safe_name(file_id)}/{ex.page_no}.png"
                    if ex.thumbnail_path
                    else ""
                )
                session.add(
                    Slide(
                        slide_id=slide_id,
                        file_id=file_id,
                        file_name=eff_name,
                        page_no=ex.page_no,
                        slide_title=ex.title,
                        slide_text=ex.body_text,
                        industry=meta["industry"],
                        client=meta.get("client", ""),
                        proposal_type=meta["proposalType"],
                        graph_type=meta["graphType"],
                        layout_type=meta["layoutType"],
                        tags=meta["tags"],
                        summary=meta["summary"],
                        reuse_hint=meta["reuseHint"],
                        thumbnail_path=thumb_url,
                        source_url=f"{view_url(file_id)}#slide={ex.page_no}",
                        access_level="internal",
                        embedding=emb,
                        created_at=now,
                        updated_at=now,
                    )
                )
                slides_added += 1

            if db_row:
                db_row.status = "ready"
                db_row.last_size = dl.size
                db_row.last_etag = dl.etag
                db_row.last_ingested_at = now
                db_row.last_error = None
                # Keep the raw Drive name; display_name (if any) is preserved.
                db_row.file_name = dl.file_name
                db_row.slide_count = slides_added
            await session.commit()
        log.info("ingested %s -> %d slides", file_id, slides_added)
        JOB.set_stage(None)
        return (slides_added, 0)
    except Exception as e:
        log.exception("ingest failed for %s", file_id)
        async with session_factory() as session:
            db_row = await session.get(DriveFile, drive_file.id)
            if db_row:
                db_row.status = "failed"
                db_row.last_error = str(e)[:500]
                await session.commit()
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def run_ingest(
    only_ids: list[int] | None = None,
    force: bool = False,
) -> dict:
    """Run ingestion for all (or specified) drive_files. Single-flight."""
    if JOB._lock.locked():
        return {"started": False, "reason": "already running"}

    async with JOB._lock:
        JOB.status = "running"
        JOB.started_at = utcnow()
        JOB.finished_at = None
        JOB.processed = 0
        JOB.failed = 0
        JOB.current_file = None
        JOB.message = None
        try:
            async with SessionLocal() as session:
                stmt = select(DriveFile)
                if only_ids:
                    stmt = stmt.where(DriveFile.id.in_(only_ids))
                rows = (await session.execute(stmt)).scalars().all()
            JOB.total = len(rows)
            for row in rows:
                try:
                    await _ingest_one(SessionLocal, row, force=force)
                    JOB.processed += 1
                except Exception as e:
                    JOB.failed += 1
                    JOB.message = f"{row.drive_file_id}: {e}"
            JOB.status = "done"
            JOB.finished_at = utcnow()
            JOB.current_file = None
            JOB.set_stage(None)
            return JOB.snapshot()
        except Exception as e:
            JOB.status = "failed"
            JOB.message = str(e)
            JOB.finished_at = utcnow()
            log.exception("ingest run failed")
            return JOB.snapshot()


def schedule_ingest_background(only_ids: list[int] | None = None, force: bool = False):
    """Kick off ingest as a background task without awaiting."""
    if JOB._lock.locked():
        return False
    asyncio.create_task(run_ingest(only_ids=only_ids, force=force))
    return True


async def backfill_missing_embeddings(batch_limit: int = 200) -> dict:
    """Compute embeddings for any slides that don't have one yet.

    Used to upgrade pre-existing rows (e.g. seed data, or slides ingested
    before embeddings were enabled).
    """
    filled = 0
    failed = 0
    async with SessionLocal() as session:
        stmt = select(Slide).where(Slide.embedding.is_(None)).limit(batch_limit)
        rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            return {"filled": 0, "failed": 0, "remaining": 0}
        sem = asyncio.Semaphore(4)

        async def _one(row: Slide) -> None:
            nonlocal filled, failed
            text = build_slide_embed_text(
                title=row.slide_title,
                summary=row.summary,
                body_text=row.slide_text,
                industry=row.industry,
                proposal_type=row.proposal_type,
                graph_type=row.graph_type,
                layout_type=row.layout_type,
                tags=list(row.tags or []),
                client=row.client,
            )
            async with sem:
                try:
                    row.embedding = await embed_text(text, task_type="RETRIEVAL_DOCUMENT")
                    filled += 1
                except Exception as e:
                    log.warning("backfill embed failed for %s: %s", row.slide_id, e)
                    failed += 1

        await asyncio.gather(*[_one(r) for r in rows])
        await session.commit()

        remaining = (
            await session.execute(
                select(func.count()).select_from(Slide).where(Slide.embedding.is_(None))
            )
        ).scalar() or 0
    log.info(
        "embedding backfill: filled=%d failed=%d remaining=%d",
        filled, failed, remaining,
    )
    return {"filled": filled, "failed": failed, "remaining": int(remaining)}


def schedule_backfill_embeddings() -> None:
    """Fire-and-forget backfill kicked off at startup."""
    async def _run() -> None:
        try:
            await backfill_missing_embeddings()
        except Exception:
            log.exception("embedding backfill task crashed")

    asyncio.create_task(_run())
