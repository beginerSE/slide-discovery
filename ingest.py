"""Ingest orchestrator: download → parse → thumbnails → Gemini → DB."""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path

from sqlalchemy import delete, func, select

from db import DriveFile, IngestJob, SessionLocal, Slide, utcnow
from drive import DownloadResult, download, view_url
from gemini_embed import build_slide_embed_text, embed_text
from gemini_extract import extract_metadata
from pptx_pipeline import SlideExtract, extract_slides, render_thumbnails

import thumbnail_store
from thumbnail_store import THUMB_ROOT

log = logging.getLogger("ingest")

# In-process guard so two concurrent jobs in the SAME process never ingest the
# same Drive file at once (the persist step delete+inserts that file's slides).
# Cross-instance safety relies on running ingest on a single warm instance
# (Cloud Run: --min-instances=1 --max-instances=1 --no-cpu-throttling).
_ACTIVE_FILES: set[str] = set()
_ACTIVE_LOCK = asyncio.Lock()

# Serializes single-flight scheduling so the "is one already running?" check
# and the job-row reservation happen atomically (within this process).
_SCHEDULE_LOCK = asyncio.Lock()


class JobTracker:
    """Writes one ingest run's live progress to its ``ingest_jobs`` row.

    Each progress update is its own short transaction so any instance polling
    the status reads the latest state. Per-page sub-progress is throttled
    (~0.5s) to avoid a flood of tiny writes, but stage changes and the final
    page of a stage always flush.
    """

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        self.processed = 0
        self.failed = 0
        self._last_page_flush = 0.0

    async def _patch(self, **fields) -> None:
        async with SessionLocal() as session:
            job = await session.get(IngestJob, self.job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)
            await session.commit()

    async def set_total(self, total: int) -> None:
        await self._patch(total=total)

    async def set_current_file(self, name: str | None) -> None:
        await self._patch(current_file=name)

    async def set_stage(
        self,
        stage: str | None,
        page: int | None = None,
        total: int | None = None,
        *,
        throttle: bool = False,
    ) -> None:
        if throttle:
            is_final = page is not None and total and page >= total
            now = time.monotonic()
            if not is_final and now - self._last_page_flush < 0.5:
                return
            self._last_page_flush = now
        await self._patch(
            stage=stage, current_file_page=page, current_file_total=total
        )

    async def file_done(self) -> None:
        self.processed += 1
        await self._patch(
            processed=self.processed,
            current_file=None,
            stage=None,
            current_file_page=None,
            current_file_total=None,
        )

    async def file_failed(self, message: str) -> None:
        self.failed += 1
        await self._patch(
            failed=self.failed,
            message=message[:500],
            current_file=None,
            stage=None,
            current_file_page=None,
            current_file_total=None,
        )

    async def drop_one(self) -> None:
        """Shrink the job's total by one (a file was skipped because another
        concurrent run already owns it), so progress can still reach 100%."""
        async with SessionLocal() as session:
            job = await session.get(IngestJob, self.job_id)
            if job is None:
                return
            job.total = max(0, (job.total or 0) - 1)
            await session.commit()

    async def finish(self, status: str, message: str | None = None) -> None:
        fields: dict = {
            "status": status,
            "finished_at": utcnow(),
            "current_file": None,
            "stage": None,
            "current_file_page": None,
            "current_file_total": None,
        }
        if message is not None:
            fields["message"] = message[:500]
        await self._patch(**fields)


def _safe_name(file_id: str) -> str:
    return "".join(c for c in file_id if c.isalnum() or c in "-_")


async def _ingest_one(
    session_factory,
    drive_file: DriveFile,
    force: bool,
    tracker: JobTracker,
) -> tuple[int, int]:
    """Ingest a single DriveFile row. Returns (slides_added, slides_skipped)."""
    file_id = drive_file.drive_file_id
    await tracker.set_current_file(drive_file.file_name or file_id)
    await tracker.set_stage("ダウンロード中")

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
        await tracker.set_current_file(dl.file_name or file_id)

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
            await tracker.set_stage(None)
            return (0, 0)

        # Parse and render in worker thread (CPU/IO heavy)
        await tracker.set_stage("スライド解析中")
        extracts: list[SlideExtract] = await asyncio.to_thread(
            extract_slides, dl.path
        )
        n_pages = len(extracts)
        await tracker.set_stage("サムネイル生成中", page=0, total=n_pages)
        thumb_out = THUMB_ROOT / _safe_name(file_id)
        # Clear any prior thumbnails for this file (local + GCS) before re-render.
        await thumbnail_store.clear_file(file_id)
        thumb_paths = await asyncio.to_thread(render_thumbnails, dl.path, thumb_out)
        # Map page_no -> thumbnail
        for i, ex in enumerate(extracts):
            if i < len(thumb_paths):
                ex.thumbnail_path = thumb_paths[i]

        # Gemini metadata extraction — track completion per page
        meta_done = 0
        await tracker.set_stage("メタ情報抽出中（Gemini）", page=0, total=n_pages)
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
            await tracker.set_stage(
                "メタ情報抽出中（Gemini）", page=meta_done, total=n_pages,
                throttle=True,
            )
            return result

        gemini_results = await asyncio.gather(*[_meta(e) for e in extracts])

        # Gemini no longer needs the local PNGs — publish them to the active
        # backend (GCS in production, which also frees the ephemeral disk).
        await thumbnail_store.put_file(file_id, thumb_out)

        # Embeddings (semantic search). Track completion per page.
        embed_done = 0
        await tracker.set_stage("ベクトル埋め込み生成中", page=0, total=n_pages)
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
            await tracker.set_stage(
                "ベクトル埋め込み生成中", page=embed_done, total=n_pages,
                throttle=True,
            )
            return vec

        embeddings = await asyncio.gather(
            *[_embed(e, m) for e, m in zip(extracts, gemini_results)]
        )

        # Persist: delete existing slides for this file, insert fresh
        await tracker.set_stage("DB保存中", page=n_pages, total=n_pages)
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
        await tracker.set_stage(None)
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


async def _create_job(kind: str, actor_label: str) -> int:
    async with SessionLocal() as session:
        job = IngestJob(
            kind=kind,
            status="running",
            actor_label=actor_label,
            started_at=utcnow(),
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job.id


async def run_ingest(
    only_ids: list[int] | None = None,
    force: bool = False,
    *,
    kind: str = "manual",
    actor_label: str = "",
    job_id: int | None = None,
) -> dict:
    """Run one ingest pass over all (or specified) drive_files.

    Records a row in ``ingest_jobs`` and streams live progress to it, so any
    instance can read the status. Several runs may proceed in parallel; the
    in-process ``_ACTIVE_FILES`` guard skips a file already being ingested by
    another concurrent run in this process. ``job_id`` may be a row created by
    the caller (so single-flight scheduling can reserve it atomically);
    otherwise a fresh row is created here.
    """
    if job_id is None:
        job_id = await _create_job(kind, actor_label)

    tracker = JobTracker(job_id)
    try:
        async with SessionLocal() as session:
            stmt = select(DriveFile)
            if only_ids:
                stmt = stmt.where(DriveFile.id.in_(only_ids))
            rows = (await session.execute(stmt)).scalars().all()
        await tracker.set_total(len(rows))
        for row in rows:
            async with _ACTIVE_LOCK:
                owned = row.drive_file_id in _ACTIVE_FILES
                if not owned:
                    _ACTIVE_FILES.add(row.drive_file_id)
            if owned:
                # Another concurrent run owns this file — skip to avoid a
                # delete/insert race on its slides, and shrink our total so
                # the progress bar still completes.
                await tracker.drop_one()
                continue
            try:
                await _ingest_one(SessionLocal, row, force=force, tracker=tracker)
                await tracker.file_done()
            except Exception as e:  # noqa: BLE001
                await tracker.file_failed(f"{row.drive_file_id}: {e}")
            finally:
                async with _ACTIVE_LOCK:
                    _ACTIVE_FILES.discard(row.drive_file_id)
        await tracker.finish("done")
    except Exception as e:  # noqa: BLE001
        log.exception("ingest run failed")
        await tracker.finish("failed", message=str(e))
    return {"jobId": job_id}


async def _count_running(kind: str | None = None) -> int:
    async with SessionLocal() as session:
        stmt = (
            select(func.count())
            .select_from(IngestJob)
            .where(IngestJob.status == "running")
        )
        if kind:
            stmt = stmt.where(IngestJob.kind == kind)
        return int((await session.execute(stmt)).scalar() or 0)


async def any_running() -> bool:
    return await _count_running() > 0


async def manual_running() -> bool:
    return await _count_running("manual") > 0


async def list_jobs(limit: int = 12) -> list[dict]:
    """Recent ingest jobs (running first), newest first, for the admin UI."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(IngestJob)
                .order_by(IngestJob.started_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    rows.sort(key=lambda r: (r.status != "running", -(r.id or 0)))
    return [r.to_dict() for r in rows]


async def schedule_ingest_background(
    only_ids: list[int] | None = None,
    force: bool = False,
    *,
    kind: str = "manual",
    actor_label: str = "",
) -> bool:
    """Kick off an ingest run as a background task without awaiting.

    Full-catalog ``manual`` and ``sync`` runs are single-flight per kind (a new
    one is refused while one of the same kind is running); ``retry`` runs of
    individual files may stack and run in parallel.

    Single-flight is made atomic by reserving the ``ingest_jobs`` row under
    ``_SCHEDULE_LOCK`` before spawning the task, so two callers that both see
    zero running jobs cannot both start one.
    """
    async with _SCHEDULE_LOCK:
        if kind in ("manual", "sync") and await _count_running(kind) > 0:
            return False
        job_id = await _create_job(kind, actor_label)
    asyncio.create_task(
        run_ingest(
            only_ids=only_ids,
            force=force,
            kind=kind,
            actor_label=actor_label,
            job_id=job_id,
        )
    )
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
