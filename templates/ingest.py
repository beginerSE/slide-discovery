"""Ingest orchestrator: download → parse → thumbnails → Gemini → DB."""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from sqlalchemy import delete, func, select, update

import config
import confluence
from db import DriveFile, IngestJob, SessionLocal, Slide, utcnow
from drive import DownloadResult, download, view_url
from gemini_embed import build_slide_embed_text, embed_text
from gemini_extract import extract_metadata
from pptx_pipeline import SlideExtract, extract_slides, render_thumbnails
from series import extract_doc_date

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

# Hard cap on how many ingest jobs (any kind) may run in parallel. Stacking
# kinds (retry / thumbs) would otherwise be unbounded; this keeps the warm
# single instance from being overwhelmed. Enforced under ``_SCHEDULE_LOCK`` by
# every scheduler before it reserves a job row.
MAX_CONCURRENT_JOBS = 6

# Background ingest tasks by job_id, so manual cleanup / stalled-job reaping can
# cooperatively cancel the in-flight task (not just flip its DB row). Only holds
# tasks started by THIS process; an orphaned job from a dead process has no
# entry here and is simply marked failed.
_RUNNING_TASKS: dict[int, asyncio.Task] = {}


def _cancel_task(job_id: int) -> None:
    task = _RUNNING_TASKS.get(job_id)
    if task is not None and not task.done():
        task.cancel()


def _ingest_complete(missing_embeddings: int) -> bool:
    """A file is fully ingested only when no current page is missing its
    embedding. Used to gate ``ready``/unchanged-skip markers."""
    return missing_embeddings == 0


class IncompleteIngest(Exception):
    """A file finished a pass but some pages are still missing embeddings.

    The file row has already been left in a resumable (``pending``) state with
    its per-page progress persisted, so the generic failure handler must NOT
    overwrite that status to ``failed``. The current run still reports the file
    as not-done so a later run finishes the remaining pages.
    """


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
        self.files: list[str] = []
        self.failures: list[str] = []
        self._last_page_flush = 0.0

    def _abort_if_not_running(self, job: IngestJob | None) -> None:
        """Cooperative, DB-driven cancellation checkpoint.

        Every progress write re-reads the job row; if it is no longer
        ``running`` (an admin cleanup / the stalled-job reaper / another
        process flipped it to a terminal status) we stop the worker right
        here. This is what makes 中断 reliable in production: on Cloud Run the
        cancel request may land on a *different* instance than the one running
        the task, so in-process ``task.cancel()`` can't reach it — but the next
        progress write on the owning instance sees the terminal DB status and
        aborts. It also stops the worker from re-populating progress fields the
        cleanup just cleared (which made a "cancelled" job look still-running).
        """
        if job is not None and job.status != "running":
            raise asyncio.CancelledError(
                f"ingest job {self.job_id} no longer running "
                f"(status={job.status}); aborting"
            )

    async def _patch(self, **fields) -> None:
        async with SessionLocal() as session:
            job = await session.get(IngestJob, self.job_id)
            if job is None:
                return
            self._abort_if_not_running(job)
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

    async def file_done(
        self, name: str | None = None, slide_count: int | None = None
    ) -> None:
        self.processed += 1
        if name:
            # Each processed file is stored as one tab-separated line
            # ``name\tslide_count\tISO8601`` so the admin job history can show
            # which file was ingested, how many slides it produced, and when.
            # Flatten tabs/newlines in a (pathological) Drive filename so the
            # one-entry-per-line / tab-delimited parsing stays intact.
            clean = (
                name.replace("\r", " ").replace("\n", " ").replace("\t", " ")
            )
            count = "" if slide_count is None else str(int(slide_count))
            at = utcnow().isoformat().replace("+00:00", "Z")
            self.files.append(f"{clean}\t{count}\t{at}")
        await self._patch(
            processed=self.processed,
            ingested_files="\n".join(self.files),
            current_file=None,
            stage=None,
            current_file_page=None,
            current_file_total=None,
        )

    async def file_failed(self, message: str, name: str | None = None) -> None:
        self.failed += 1
        if name:
            # One record per line: ``name\terror\tISO8601`` (same shape as
            # file_done), so the admin history can show WHICH file failed and
            # why. Flatten tabs/newlines so the parsing stays intact.
            def _flat(s: str) -> str:
                return s.replace("\r", " ").replace("\n", " ").replace("\t", " ")

            at = utcnow().isoformat().replace("+00:00", "Z")
            self.failures.append(f"{_flat(name)}\t{_flat(message)[:300]}\t{at}")
        await self._patch(
            failed=self.failed,
            failed_files="\n".join(self.failures),
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
            self._abort_if_not_running(job)
            job.total = max(0, (job.total or 0) - 1)
            await session.commit()

    async def heartbeat(self) -> None:
        """Bump the job's progress timestamp so a long stage (download, parse,
        a slow Gemini call) is not mistaken for a stall by the reaper."""
        await self._patch(updated_at=utcnow())

    async def finish(self, status: str, message: str | None = None) -> None:
        """Record a terminal status — but only if the job is still ``running``.

        A job that was cleaned up manually or reaped as stalled is already
        terminal; we must not resurrect it to ``done`` just because the
        (now-cancelled) task happened to reach the end first. The guard is a
        single atomic conditional UPDATE (``WHERE status='running'``) so it
        cannot race a concurrent cleanup/reaper write.
        """
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
        async with SessionLocal() as session:
            await session.execute(
                update(IngestJob)
                .where(IngestJob.id == self.job_id, IngestJob.status == "running")
                .values(**fields)
            )
            await session.commit()


def _safe_name(file_id: str) -> str:
    return "".join(c for c in file_id if c.isalnum() or c in "-_")


_FALLBACK_META = {
    "industry": "その他",
    "proposalType": "その他",
    "graphType": "なし",
    "layoutType": "タイトル中央",
    "tags": [],
    "summary": "",
    "reuseHint": "",
}


def _file_fingerprint(etag: str | None, size: int | None) -> str:
    """A stable content version for a downloaded file.

    Prefers the Drive etag/md5 (changes only when content changes); falls back
    to the byte size when no etag is available (public share-link mode). The
    value is stored on each ``Slide`` so a resumed ingest can tell an
    already-done page apart from a stale one.
    """
    if etag:
        return f"etag:{etag}"
    if size is not None:
        return f"size:{size}"
    return ""


def _page_action(
    existing_fp: str | None, current_fp: str, has_embedding: bool
) -> str:
    """Decide what work a single page still needs on a (re)ingest.

    * ``recompute`` — no row yet, or the stored fingerprint differs from the
      current file content (stale): re-run Gemini metadata + embedding.
    * ``embed_only`` — metadata is current (fingerprint matches) but the
      embedding is missing (an earlier run was interrupted before it ran):
      reuse the saved metadata, only recompute the embedding.
    * ``reuse`` — metadata and embedding are both current: skip entirely.
    """
    if not existing_fp or existing_fp != current_fp:
        return "recompute"
    if not has_embedding:
        return "embed_only"
    return "reuse"


def _slide_id_for(file_id: str, page_no: int) -> str:
    return f"gd-{_safe_name(file_id)}-p{page_no:03d}"


# How often the render progress poller samples the staging dir / refreshes the
# job stage. Module-level so tests can shrink it for fast deterministic polling.
_RENDER_POLL_SECONDS = 2.0


async def _render_thumbs_tracked(
    src_path: Path,
    thumb_out: Path,
    tracker: JobTracker,
    total: int | None,
) -> list[Path]:
    """Render PPTX thumbnails in a worker thread while keeping the job's stage
    live, so a large deck does not look frozen in the UI.

    ``render_thumbnails`` is one long blocking call (soffice PPTX->PDF, then
    pdftoppm PDF->PNG) that reports nothing on its own — for a deck with many
    slides the progress sat motionless for the whole render. We poll
    ``thumb_out`` for the PNGs pdftoppm writes there: while soffice is still
    producing the PDF none exist yet, so we show an elapsed-time "converting"
    message; once pages start landing we advance the per-page counter. Each
    write also bumps ``updated_at`` (onupdate), keeping the stalled-job reaper
    from mistaking a slow render for a crash.
    """
    start = time.monotonic()
    stop = asyncio.Event()

    async def _poll() -> None:
        while not stop.is_set():
            try:
                count = (
                    len(list(thumb_out.glob("*.png"))) if thumb_out.exists() else 0
                )
            except OSError:
                count = 0
            try:
                if count <= 0:
                    elapsed = int(time.monotonic() - start)
                    await tracker.set_stage(
                        f"PDF変換中（大きいファイルは時間がかかります・経過 {elapsed}秒）",
                        page=0,
                        total=total,
                    )
                else:
                    await tracker.set_stage(
                        "サムネイル生成中",
                        page=min(count, total) if total else count,
                        total=total,
                        throttle=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_RENDER_POLL_SECONDS)

    poller = asyncio.create_task(_poll())
    try:
        return await asyncio.to_thread(render_thumbnails, src_path, thumb_out)
    finally:
        stop.set()
        poller.cancel()
        with suppress(asyncio.CancelledError):
            await poller


async def _upsert_slide_meta(
    session,
    file_id: str,
    ex: SlideExtract,
    meta: dict,
    eff_name: str,
    fingerprint: str,
    folder_id: str = "",
    folder_name: str = "",
    file_doc_date=None,
) -> None:
    """Persist one page's metadata immediately (its own transaction).

    Called only for ``recompute`` pages, so the (expensive) Gemini result is
    durable the moment it's computed — a crash mid-file no longer discards
    finished pages. The embedding is cleared to NULL here and filled in the
    later embedding phase / backfill.
    """
    slide_id = _slide_id_for(file_id, ex.page_no)
    thumb_url = (
        f"/api/thumbnails/files/{_safe_name(file_id)}/{ex.page_no}.png"
        if ex.thumbnail_path
        else ""
    )
    now = utcnow()
    fields = dict(
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
        doc_category=meta.get("docCategory", ""),
        tags=meta["tags"],
        summary=meta["summary"],
        reuse_hint=meta["reuseHint"],
        thumbnail_path=thumb_url,
        source_url=f"{view_url(file_id)}#slide={ex.page_no}",
        access_level="internal",
        source_fingerprint=fingerprint,
        folder_id=folder_id,
        folder_name=folder_name,
        doc_date=extract_doc_date(ex.title) or file_doc_date,
    )
    row = await session.get(Slide, slide_id)
    if row is None:
        session.add(
            Slide(slide_id=slide_id, embedding=None, created_at=now, **fields)
        )
    else:
        for key, value in fields.items():
            setattr(row, key, value)
        # Metadata changed → any prior embedding is stale; the embedding phase
        # recomputes it.
        row.embedding = None
    await session.commit()


async def _backfill_slide_series_fields(
    session,
    file_id: str,
    folder_id: str,
    folder_name: str,
    file_doc_date,
) -> None:
    """Refresh file-level series fields on every slide of a file.

    ``_upsert_slide_meta`` only runs for *recomputed* pages, so reused/skipped
    pages (and legacy rows) would otherwise keep stale or empty
    ``folder_id``/``folder_name``/``doc_date`` — which ``recent_series_context``
    relies on. This makes the slide-level series fields authoritative regardless
    of how a page was (re)ingested. Per-slide ``doc_date`` keeps a
    title-derived date when present, else falls back to the file-level date.
    """
    rows = (
        await session.execute(
            select(Slide).where(Slide.file_id == file_id)
        )
    ).scalars().all()
    for row in rows:
        row.folder_id = folder_id
        row.folder_name = folder_name
        row.doc_date = extract_doc_date(row.slide_title) or file_doc_date
    if rows:
        await session.commit()


async def _ingest_one(
    session_factory,
    drive_file: DriveFile,
    force: bool,
    tracker: JobTracker,
) -> int:
    """Ingest a single DriveFile row. Returns the file's slide_count.

    Resumable: each page's metadata is persisted as soon as it's extracted and
    each embedding as soon as it's computed, both keyed by a content
    fingerprint. A run interrupted mid-file (process restart, crash) re-uses
    every page already finished for the current file content and only redoes
    what is missing. ``force`` (admin retry) ignores reuse and recomputes all
    pages.
    """
    file_id = drive_file.drive_file_id
    await tracker.set_current_file(drive_file.file_name or file_id)
    await tracker.set_stage("ダウンロード中")
    log.info(
        "ingest start file_id=%s name=%r force=%s",
        file_id, drive_file.display_name or drive_file.file_name, force,
    )

    async with session_factory() as session:
        db_row = await session.get(DriveFile, drive_file.id)
        if db_row is None:
            log.warning("ingest abort file_id=%s: drive_file row missing", file_id)
            return 0
        db_row.status = "processing"
        db_row.last_error = None
        # An admin-chosen display name (set on a name collision) overrides
        # Drive's raw name for what users see / search by.
        eff_name = db_row.display_name or db_row.file_name or file_id
        await session.commit()

    tmp = Path(tempfile.mkdtemp(prefix="drv_"))
    try:
        dl: DownloadResult = await download(file_id, tmp)
        # Filename is known after download — show it in the progress UI.
        await tracker.set_current_file(dl.file_name or file_id)
        eff_name = drive_file.display_name or dl.file_name
        fingerprint = _file_fingerprint(dl.etag, dl.size)
        # File-level meeting date: prefer the date captured at add-time, else
        # parse the now-known downloaded filename (public-mode direct links
        # have no name pre-ingest, so this is the first chance to date them).
        file_doc_date = drive_file.doc_date or extract_doc_date(dl.file_name)
        log.info(
            "ingest downloaded file_id=%s name=%r size=%s etag=%s",
            file_id, dl.file_name, dl.size, dl.etag,
        )

        if (
            not force
            and drive_file.last_ingested_at is not None
            and _file_fingerprint(drive_file.last_etag, drive_file.last_size)
            == fingerprint
        ):
            # Only fast-skip when the prior pass is genuinely complete — guard
            # against rows marked ready by older code that left some pages
            # without an embedding (they must still be finished).
            async with session_factory() as session:
                missing = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(Slide)
                            .where(
                                Slide.file_id == file_id,
                                Slide.embedding.is_(None),
                            )
                        )
                    ).scalar()
                    or 0
                )
            if _ingest_complete(missing):
                async with session_factory() as session:
                    db_row = await session.get(DriveFile, drive_file.id)
                    if db_row:
                        db_row.status = "ready"
                        db_row.file_name = dl.file_name
                        if db_row.doc_date is None and file_doc_date is not None:
                            db_row.doc_date = file_doc_date
                        await session.commit()
                    # Even an unchanged file must keep its slides' series fields
                    # current (folder may have been renamed, or rows predate the
                    # series feature) so timeline context isn't silently missing.
                    # Prefer the freshly-loaded row (drive_sync may have updated
                    # folder/date since this run was scheduled).
                    src = db_row or drive_file
                    await _backfill_slide_series_fields(
                        session,
                        file_id,
                        src.folder_id or "",
                        src.folder_name or "",
                        src.doc_date or file_doc_date,
                    )
                log.info("skip unchanged %s", file_id)
                await tracker.set_stage(None)
                return db_row.slide_count or 0

        # Parse and render in worker thread (CPU/IO heavy)
        await tracker.set_stage("スライド解析中")
        extracts: list[SlideExtract] = await asyncio.to_thread(
            extract_slides, dl.path
        )
        n_pages = len(extracts)
        log.info("ingest extracted file_id=%s pages=%d", file_id, n_pages)
        page_nos = {ex.page_no for ex in extracts}
        await tracker.set_stage("サムネイル生成中", page=0, total=n_pages)
        # Two-phase publish: render into a staging dir (a sibling of the live
        # dir, so it shares THUMB_ROOT's filesystem for an atomic rename) and
        # only swap it in AFTER a successful render+publish. A mid-render failure
        # therefore never wipes the file's existing thumbnails.
        thumb_out = THUMB_ROOT / f"{_safe_name(file_id)}.staging"
        shutil.rmtree(thumb_out, ignore_errors=True)
        thumb_paths = await _render_thumbs_tracked(
            dl.path, thumb_out, tracker, total=n_pages
        )
        log.info(
            "ingest rendered thumbnails file_id=%s count=%d",
            file_id, len(thumb_paths),
        )
        # Map page_no -> thumbnail
        for i, ex in enumerate(extracts):
            if i < len(thumb_paths):
                ex.thumbnail_path = thumb_paths[i]

        # Decide per page what work is still needed, reusing anything already
        # done for this exact file content (unless the admin forced a redo).
        async with session_factory() as session:
            existing = {
                r.page_no: r
                for r in (
                    await session.execute(
                        select(Slide).where(Slide.file_id == file_id)
                    )
                ).scalars().all()
            }
        recompute: list[SlideExtract] = []
        reused = 0
        for ex in extracts:
            row = existing.get(ex.page_no)
            action = (
                "recompute"
                if force
                else _page_action(
                    row.source_fingerprint if row else None,
                    fingerprint,
                    bool(row and row.embedding is not None),
                )
            )
            if action == "recompute":
                recompute.append(ex)
            else:
                reused += 1

        # Phase 1 — Gemini metadata for pages that need it, persisted per page.
        meta_total = len(recompute)
        meta_done = 0
        log.info(
            "ingest plan file_id=%s recompute=%d reused=%d",
            file_id, len(recompute), reused,
        )
        await tracker.set_stage(
            "メタ情報抽出中（Gemini）", page=0, total=meta_total
        )
        log.info("ingest gemini-metadata start file_id=%s pages=%d", file_id, meta_total)
        sem = asyncio.Semaphore(2)

        async def _meta(ex: SlideExtract) -> None:
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
                    result = dict(_FALLBACK_META, summary=ex.body_text[:100])
            async with session_factory() as session:
                await _upsert_slide_meta(
                    session, file_id, ex, result, eff_name, fingerprint,
                    folder_id=drive_file.folder_id or "",
                    folder_name=drive_file.folder_name or "",
                    file_doc_date=file_doc_date,
                )
            meta_done += 1
            await tracker.set_stage(
                "メタ情報抽出中（Gemini）", page=meta_done, total=meta_total,
                throttle=True,
            )

        await asyncio.gather(*[_meta(e) for e in recompute])
        log.info("ingest gemini-metadata done file_id=%s pages=%d", file_id, meta_total)

        # Gemini no longer needs the local PNGs — publish them to the active
        # backend (atomic swap locally / upload+prune on GCS), replacing the old
        # thumbnails only now that the render succeeded.
        published = await thumbnail_store.publish_file(file_id, thumb_out)
        log.info(
            "ingest thumbnails published file_id=%s ok=%s pages=%d backend=%s",
            file_id, published, len(thumb_paths),
            "gcs" if config.use_gcs_thumbnails() else "local",
        )

        # Phase 2 — embeddings for any current-page slide still missing one
        # (freshly recomputed pages + pages whose embedding was interrupted).
        async with session_factory() as session:
            pending = [
                r
                for r in (
                    await session.execute(
                        select(Slide).where(
                            Slide.file_id == file_id, Slide.embedding.is_(None)
                        )
                    )
                ).scalars().all()
                if r.page_no in page_nos
            ]
        embed_total = len(pending)
        embed_done = 0
        await tracker.set_stage(
            "ベクトル埋め込み生成中", page=0, total=embed_total
        )
        log.info("ingest embeddings start file_id=%s pages=%d", file_id, embed_total)
        embed_sem = asyncio.Semaphore(4)

        async def _embed(slide_id: str, text: str) -> None:
            nonlocal embed_done
            async with embed_sem:
                try:
                    vec = await embed_text(text, task_type="RETRIEVAL_DOCUMENT")
                except Exception as e:
                    log.warning("embed failed for %s: %s", slide_id, e)
                    vec = None
            if vec is not None:
                async with session_factory() as session:
                    row = await session.get(Slide, slide_id)
                    if row is not None:
                        row.embedding = vec
                        await session.commit()
            embed_done += 1
            await tracker.set_stage(
                "ベクトル埋め込み生成中", page=embed_done, total=embed_total,
                throttle=True,
            )

        await asyncio.gather(
            *[
                _embed(
                    r.slide_id,
                    build_slide_embed_text(
                        title=r.slide_title,
                        summary=r.summary,
                        body_text=r.slide_text,
                        industry=r.industry,
                        proposal_type=r.proposal_type,
                        graph_type=r.graph_type,
                        layout_type=r.layout_type,
                        tags=list(r.tags or []),
                        client=r.client,
                        doc_category=r.doc_category,
                    ),
                )
                for r in pending
            ]
        )
        log.info("ingest embeddings done file_id=%s pages=%d", file_id, embed_total)

        # Finalize: drop pages no longer in the deck, refresh the display name
        # on reused rows, and mark the file ready — but only if EVERY current
        # page is complete (has an embedding). If any embedding is still missing
        # (e.g. a swallowed Gemini error), leave the file resumable and do NOT
        # set the unchanged-skip markers, so a later run finishes it instead of
        # treating it as fully ingested.
        await tracker.set_stage("DB保存中", page=n_pages, total=n_pages)
        async with session_factory() as session:
            if page_nos:
                await session.execute(
                    delete(Slide).where(
                        Slide.file_id == file_id,
                        Slide.page_no.notin_(page_nos),
                    )
                )
            else:
                await session.execute(
                    delete(Slide).where(Slide.file_id == file_id)
                )
            now = utcnow()
            db_row = await session.get(DriveFile, drive_file.id)
            if db_row:
                eff_name = db_row.display_name or dl.file_name
            # Reused rows may carry an old display name (admin rename); refresh.
            await session.execute(
                update(Slide)
                .where(Slide.file_id == file_id)
                .values(file_name=eff_name)
            )
            slide_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Slide)
                        .where(Slide.file_id == file_id)
                    )
                ).scalar()
                or 0
            )
            missing_embeddings = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Slide)
                        .where(
                            Slide.file_id == file_id,
                            Slide.embedding.is_(None),
                        )
                    )
                ).scalar()
                or 0
            )
            complete = _ingest_complete(missing_embeddings)
            if db_row:
                # Keep the raw Drive name; display_name (if any) is preserved.
                db_row.file_name = dl.file_name
                db_row.slide_count = slide_count
                # Backfill the file-level meeting date once known (public-mode
                # direct links have no name at add-time).
                if db_row.doc_date is None and file_doc_date is not None:
                    db_row.doc_date = file_doc_date
                if complete:
                    db_row.status = "ready"
                    db_row.last_size = dl.size
                    db_row.last_etag = dl.etag
                    db_row.last_ingested_at = now
                    db_row.last_error = None
                else:
                    # Resumable, not fully ingested: do not set the
                    # unchanged-skip markers (last_ingested_at/size/etag).
                    db_row.status = "pending"
                    db_row.last_error = (
                        f"埋め込み未完了: {missing_embeddings}ページ"
                    )[:500]
            await session.commit()
            # Ensure every slide (including reused pages skipped by
            # _upsert_slide_meta) carries the file's series fields. Prefer the
            # freshly-loaded row over the (possibly stale) scheduled arg.
            src = db_row or drive_file
            await _backfill_slide_series_fields(
                session,
                file_id,
                src.folder_id or "",
                src.folder_name or "",
                src.doc_date or file_doc_date,
            )
        log.info(
            "ingested %s -> %d slides (recomputed=%d reused=%d missing_embed=%d)",
            file_id, slide_count, len(recompute), reused, missing_embeddings,
        )
        await tracker.set_stage(None)
        if not complete:
            raise IncompleteIngest(
                f"{file_id}: {missing_embeddings} ページの埋め込みが未完了です"
            )
        return slide_count
    except IncompleteIngest:
        # Status was already set to resumable ``pending`` above; do not let the
        # generic handler overwrite it to ``failed``. Re-raise so the run loop
        # reports this file as not-done (it resumes on a later run).
        raise
    except asyncio.CancelledError:
        # Cleaned up / reaped mid-file. Pages done so far are already persisted;
        # reset the file to pending so a later run resumes it cheaply.
        log.info("ingest cancelled for %s", file_id)
        with suppress(Exception):
            async with session_factory() as session:
                db_row = await session.get(DriveFile, drive_file.id)
                if db_row and db_row.status == "processing":
                    db_row.status = "pending"
                    await session.commit()
        raise
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

    async def _heartbeat_loop() -> None:
        # Periodically refresh the job's heartbeat so a long single stage
        # doesn't trip the stalled-job reaper. Dies with the task (and thus
        # the process), so a real crash still goes stale and gets reaped.
        while True:
            await asyncio.sleep(60)
            try:
                await tracker.heartbeat()
            except Exception:  # noqa: BLE001
                log.debug("heartbeat write failed", exc_info=True)

    heartbeat = asyncio.create_task(_heartbeat_loop())
    try:
        async with SessionLocal() as session:
            stmt = select(DriveFile)
            if only_ids:
                stmt = stmt.where(DriveFile.id.in_(only_ids))
            rows = (await session.execute(stmt)).scalars().all()
        await tracker.set_total(len(rows))
        log.info(
            "ingest run %d start: files=%d kind=%s force=%s only_ids=%s",
            job_id, len(rows), kind, force, only_ids or "all",
        )
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
                slide_count = await _ingest_one(
                    SessionLocal, row, force=force, tracker=tracker
                )
                await tracker.file_done(
                    row.display_name or row.file_name or row.drive_file_id,
                    slide_count,
                )
            except asyncio.CancelledError:
                # Cleaned up / reaped mid-file: stop owning the file and let
                # cancellation propagate (the job row is already terminal).
                async with _ACTIVE_LOCK:
                    _ACTIVE_FILES.discard(row.drive_file_id)
                raise
            except Exception as e:  # noqa: BLE001
                await tracker.file_failed(
                    str(e),
                    name=row.display_name or row.file_name or row.drive_file_id,
                )
            finally:
                async with _ACTIVE_LOCK:
                    _ACTIVE_FILES.discard(row.drive_file_id)
        await tracker.finish("done")
        log.info("ingest run %d done: files=%d", job_id, len(rows))
    except asyncio.CancelledError:
        log.info("ingest run %d cancelled", job_id)
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("ingest run failed")
        await tracker.finish("failed", message=str(e))
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
    return {"jobId": job_id}


# A running job whose progress heartbeat (``updated_at``) is older than this is
# treated as stalled (crashed mid-run) and reaped so it stops blocking
# single-flight scheduling.
STALE_JOB_SECONDS = 15 * 60


def _is_stalled(updated_at, now, threshold_seconds: int) -> bool:
    """True if a running job's last heartbeat is older than the threshold."""
    if updated_at is None:
        return False
    return (now - updated_at).total_seconds() > threshold_seconds


def _clear_job_progress(job: IngestJob, status: str, message: str) -> None:
    job.status = status
    job.finished_at = utcnow()
    job.message = message[:500]
    job.current_file = None
    job.stage = None
    job.current_file_page = None
    job.current_file_total = None


async def reap_orphaned_jobs() -> dict:
    """At startup, mark every ``running`` job failed and reset stuck files.

    A single warm instance owns ingest (see ``_ACTIVE_FILES``), so on process
    start any job still ``running`` is necessarily orphaned — its in-process
    task died with the previous process and will never resume on its own.
    Files left ``processing`` are reset to ``pending`` so the next run picks
    them up (the per-page persistence makes that resume cheap).
    """
    async with SessionLocal() as session:
        jobs = (
            await session.execute(
                select(IngestJob).where(IngestJob.status == "running")
            )
        ).scalars().all()
        for job in jobs:
            _clear_job_progress(job, "failed", "中断されました（再起動）")
        files = (
            await session.execute(
                select(DriveFile).where(DriveFile.status == "processing")
            )
        ).scalars().all()
        for f in files:
            f.status = "pending"
        if jobs or files:
            await session.commit()
    if jobs or files:
        log.info(
            "reaped orphaned jobs=%d, reset processing files=%d",
            len(jobs), len(files),
        )
    return {"jobs": len(jobs), "files": len(files)}


async def reap_stalled_jobs(threshold_seconds: int = STALE_JOB_SECONDS) -> int:
    """Fail any ``running`` job whose heartbeat is older than the threshold."""
    now = utcnow()
    reaped = 0
    async with SessionLocal() as session:
        jobs = (
            await session.execute(
                select(IngestJob).where(IngestJob.status == "running")
            )
        ).scalars().all()
        stalled_ids: list[int] = []
        for job in jobs:
            if _is_stalled(job.updated_at, now, threshold_seconds):
                _clear_job_progress(
                    job, "failed", "停滞のため中断（進捗が一定時間ありません）"
                )
                stalled_ids.append(job.id)
                reaped += 1
        if reaped:
            await session.commit()
    for jid in stalled_ids:
        _cancel_task(jid)
    if reaped:
        log.info("reaped %d stalled ingest job(s)", reaped)
    return reaped


async def cleanup_job(job_id: int) -> bool:
    """Admin action: mark a single running job as interrupted. No-op if the
    job is missing or already finished."""
    async with SessionLocal() as session:
        job = await session.get(IngestJob, job_id)
        if job is None or job.status != "running":
            return False
        _clear_job_progress(job, "failed", "手動で中断されました")
        await session.commit()
    _cancel_task(job_id)
    log.info("manually cleaned up ingest job %d", job_id)
    return True


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
        # Clear any crashed/stalled run first so a dead job can't permanently
        # block single-flight scheduling.
        await reap_stalled_jobs()
        if kind in ("manual", "sync") and await _count_running(kind) > 0:
            return False
        if await _count_running() >= MAX_CONCURRENT_JOBS:
            return False
        job_id = await _create_job(kind, actor_label)
    task = asyncio.create_task(
        run_ingest(
            only_ids=only_ids,
            force=force,
            kind=kind,
            actor_label=actor_label,
            job_id=job_id,
        )
    )
    _RUNNING_TASKS[job_id] = task
    task.add_done_callback(lambda _t, jid=job_id: _RUNNING_TASKS.pop(jid, None))
    return True


async def _regen_thumbnails_one(
    session_factory, drive_file: DriveFile, *, tracker: JobTracker
) -> int:
    """Re-render and re-publish ONLY the thumbnails for one file.

    Unlike a full re-ingest this never touches Gemini metadata or embeddings —
    it downloads the deck, renders the page PNGs, and republishes them to the
    active thumbnail backend (local disk or GCS). Used to recover thumbnails
    that failed to publish (e.g. a transient GCS/IAM problem) without paying for
    a metadata recompute. Returns the number of pages rendered.
    """
    file_id = drive_file.drive_file_id
    await tracker.set_current_file(drive_file.display_name or drive_file.file_name or file_id)
    await tracker.set_stage("ダウンロード中")
    log.info(
        "thumbnail regen start file_id=%s name=%r",
        file_id, drive_file.display_name or drive_file.file_name,
    )

    tmp = Path(tempfile.mkdtemp(prefix="thumb_"))
    try:
        dl: DownloadResult = await download(file_id, tmp)
        await tracker.set_current_file(dl.file_name or file_id)

        # Thumbnail-only regen must NOT change what users read: it republishes
        # images for ALREADY-ingested, UNCHANGED content. If the file was never
        # fully ingested, or its content changed since the last ingest, the
        # stored metadata/embeddings would no longer match the new images — so
        # refuse and tell the admin to run a full re-ingest instead.
        current_fp = _file_fingerprint(dl.etag, dl.size)
        stored_fp = _file_fingerprint(drive_file.last_etag, drive_file.last_size)
        if drive_file.last_ingested_at is None or current_fp != stored_fp:
            raise RuntimeError(
                "ファイル内容が変更されています（または未取り込み）。"
                "サムネイル再生成ではなく「再取り込み」を実行してください"
            )

        await tracker.set_stage("サムネイル生成中")
        # Two-phase publish: render into a staging dir and swap it in only after
        # a successful render+publish, so a failed regen keeps the old images.
        thumb_out = THUMB_ROOT / f"{_safe_name(file_id)}.staging"
        shutil.rmtree(thumb_out, ignore_errors=True)
        thumb_paths = await _render_thumbs_tracked(
            dl.path, thumb_out, tracker, total=None
        )
        n_pages = len(thumb_paths)
        log.info(
            "thumbnail regen rendered file_id=%s count=%d", file_id, n_pages
        )

        await tracker.set_stage("サムネイル保存中", page=0, total=n_pages)
        published = await thumbnail_store.publish_file(file_id, thumb_out)
        log.info(
            "thumbnail regen published file_id=%s ok=%s pages=%d backend=%s",
            file_id, published, n_pages,
            "gcs" if config.use_gcs_thumbnails() else "local",
        )

        # Re-attach canonical thumbnail URLs to existing slide rows. Page
        # numbering is stable (1..n), so the URLs are unchanged — but a row that
        # previously failed to get a thumbnail may have an empty path; fix those.
        async with session_factory() as session:
            slides = (
                await session.execute(
                    select(Slide).where(Slide.file_id == file_id)
                )
            ).scalars().all()
            for s in slides:
                if s.page_no <= n_pages:
                    url = f"/api/thumbnails/files/{_safe_name(file_id)}/{s.page_no}.png"
                    if s.thumbnail_path != url:
                        s.thumbnail_path = url
            await session.commit()
        await tracker.set_stage(None)
        log.info("regenerated %d thumbnails for %s", n_pages, file_id)
        return n_pages
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def run_thumbnail_regen(
    drive_file_id: int,
    *,
    actor_label: str = "",
    job_id: int | None = None,
) -> dict:
    """Run a thumbnail-only regeneration for a single drive_file as a job.

    Reuses the ``ingest_jobs`` progress machinery (kind ``thumbs``) so the admin
    UI shows live progress, and the ``_ACTIVE_FILES`` guard so it never collides
    with an ingest of the same file.
    """
    if job_id is None:
        job_id = await _create_job("thumbs", actor_label)
    tracker = JobTracker(job_id)

    async def _heartbeat_loop() -> None:
        # Keep the job's heartbeat fresh so a slow download/render isn't
        # mistaken for a stall by the reaper.
        while True:
            await asyncio.sleep(60)
            try:
                await tracker.heartbeat()
            except Exception:  # noqa: BLE001
                log.debug("regen heartbeat write failed", exc_info=True)

    heartbeat = asyncio.create_task(_heartbeat_loop())
    try:
        async with SessionLocal() as session:
            row = await session.get(DriveFile, drive_file_id)
        if row is None:
            await tracker.finish("failed", message="ファイルが見つかりません")
            return {"jobId": job_id}
        await tracker.set_total(1)

        async with _ACTIVE_LOCK:
            owned = row.drive_file_id in _ACTIVE_FILES
            if not owned:
                _ACTIVE_FILES.add(row.drive_file_id)
        if owned:
            await tracker.finish(
                "failed", message="このファイルは処理中です。完了後に再試行してください"
            )
            return {"jobId": job_id}
        try:
            await _regen_thumbnails_one(SessionLocal, row, tracker=tracker)
            await tracker.file_done(
                row.display_name or row.file_name or row.drive_file_id
            )
            await tracker.finish("done")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("thumbnail regen failed for %s", row.drive_file_id)
            await tracker.file_failed(
                str(e),
                name=row.display_name or row.file_name or row.drive_file_id,
            )
            await tracker.finish("failed", message=str(e))
        finally:
            async with _ACTIVE_LOCK:
                _ACTIVE_FILES.discard(row.drive_file_id)
    except asyncio.CancelledError:
        log.info("thumbnail regen job %d cancelled", job_id)
        raise
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
    return {"jobId": job_id}


async def schedule_thumbnail_regen_background(
    drive_file_id: int, *, actor_label: str = ""
) -> bool:
    """Kick off a thumbnail-only regen as a background task. Always allowed to
    stack (like per-file retries); the ``_ACTIVE_FILES`` guard prevents a
    same-file collision."""
    async with _SCHEDULE_LOCK:
        await reap_stalled_jobs()
        if await _count_running() >= MAX_CONCURRENT_JOBS:
            return False
        job_id = await _create_job("thumbs", actor_label)
    task = asyncio.create_task(
        run_thumbnail_regen(drive_file_id, actor_label=actor_label, job_id=job_id)
    )
    _RUNNING_TASKS[job_id] = task
    task.add_done_callback(lambda _t, jid=job_id: _RUNNING_TASKS.pop(jid, None))
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
                doc_category=row.doc_category,
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


EMBED_DOC_VERSION_KEY = "embed_doc_version"


async def invalidate_stale_embeddings() -> bool:
    """Clear ALL embeddings when the embedding document contract changed.

    ``build_slide_embed_text`` output is versioned (gemini_embed.
    EMBED_DOC_VERSION). When the stored version differs, existing vectors
    were computed from a different document format, so they are nulled and
    the regular ``embedding IS NULL`` backfill rebuilds the corpus. Pure
    data UPDATE (no DDL) so it is safe to run on every boot. Returns True
    when an invalidation happened."""
    from gemini_embed import EMBED_DOC_VERSION

    from db import AppState

    current = str(EMBED_DOC_VERSION)
    async with SessionLocal() as session:
        row = await session.get(AppState, EMBED_DOC_VERSION_KEY)
        if row is not None and row.value == current:
            return False
        await session.execute(update(Slide).values(embedding=None))
        if row is None:
            session.add(AppState(key=EMBED_DOC_VERSION_KEY, value=current))
        else:
            row.value = current
        await session.commit()
    log.info("embedding doc version -> %s: cleared all embeddings for re-embed", current)
    return True


def schedule_backfill_embeddings() -> None:
    """Fire-and-forget backfill kicked off at startup."""
    async def _run() -> None:
        try:
            await invalidate_stale_embeddings()
            await backfill_missing_embeddings()
        except Exception:
            log.exception("embedding backfill task crashed")

    asyncio.create_task(_run())


# ─────────────────────────────────────────────────────────────────────
# Confluence ingest: one Confluence Cloud page = one Slide row
# ─────────────────────────────────────────────────────────────────────


def _conf_slide_id(page_id: str) -> str:
    return f"cf-{page_id}"


def _conf_file_id(page_id: str) -> str:
    """Namespaced file id so a Confluence page never collides with a Drive
    file id (Drive ids are bare; Confluence ids are ``conf:<pageId>``)."""
    return f"conf:{page_id}"


def _conf_fingerprint(version: int) -> str:
    return f"confv:{version}"


def _stale_confluence_ids(
    existing_ids: set[str], seen_ids: set[str]
) -> set[str]:
    """Slide ids of a space's Confluence rows that no longer exist upstream.

    Pure set diff so the "prune pages deleted from the space" rule is unit
    testable without a DB: any previously-stored id we did NOT see in the
    current page listing is stale and should be removed.
    """
    return existing_ids - seen_ids


async def _upsert_confluence_page(
    session_factory,
    page: confluence.ConfluencePage,
    space_id: str,
    fingerprint: str,
) -> str:
    """Insert/refresh one Confluence page as a Slide row; returns its slide_id.

    Clears the embedding so the caller re-embeds (content is new or changed).
    Confluence pages have no thumbnail (the UI shows an icon) and are not part
    of a 定例 series, so the folder/date fields stay empty. ``source_space_id``
    records the owning space so a re-ingest can prune deleted pages.
    """
    slide_id = _conf_slide_id(page.id)
    fields = dict(
        file_id=_conf_file_id(page.id),
        file_name=page.title,
        page_no=1,
        slide_title=page.title,
        slide_text=page.text,
        industry="",
        client="",
        proposal_type="",
        graph_type="",
        layout_type="",
        tags=[],
        summary="",
        reuse_hint="",
        thumbnail_path="",
        source_url=page.url,
        access_level="internal",
        source_fingerprint=fingerprint,
        source_type="confluence",
        source_space_id=str(space_id),
        folder_id="",
        folder_name="",
        doc_date=None,
    )
    async with session_factory() as session:
        row = await session.get(Slide, slide_id)
        if row is None:
            session.add(Slide(slide_id=slide_id, embedding=None, **fields))
        else:
            for key, value in fields.items():
                setattr(row, key, value)
            row.embedding = None
        await session.commit()
    return slide_id


async def run_confluence_ingest(
    space_id: str,
    *,
    actor_label: str = "",
    job_id: int | None = None,
) -> dict:
    """Ingest every page of one Confluence space into ``slides``.

    Mirrors ``run_ingest`` job bookkeeping (one ``ingest_jobs`` row with live
    progress, a heartbeat, and cooperative DB-driven cancel). One page = one
    slide row; a page is skipped when its stored version fingerprint already
    matches and an embedding exists, else it is re-fetched and re-embedded
    through the shared Gemini embedding path.
    """
    if job_id is None:
        job_id = await _create_job("confluence", actor_label)
    tracker = JobTracker(job_id)

    async def _heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await tracker.heartbeat()
            except Exception:  # noqa: BLE001
                log.debug("heartbeat write failed", exc_info=True)

    heartbeat = asyncio.create_task(_heartbeat_loop())
    embed_sem = asyncio.Semaphore(4)

    async def _embed_one(slide_id: str, text: str) -> None:
        async with embed_sem:
            try:
                vec = await embed_text(text, task_type="RETRIEVAL_DOCUMENT")
            except Exception as e:  # noqa: BLE001
                log.warning("confluence embed failed for %s: %s", slide_id, e)
                vec = None
        if vec is not None:
            async with SessionLocal() as session:
                row = await session.get(Slide, slide_id)
                if row is not None:
                    row.embedding = vec
                    await session.commit()

    try:
        import confluence_settings

        async with SessionLocal() as session:
            await confluence_settings.refresh_cache(session)
        space = await confluence.get_space(space_id)
        if space is None:
            raise RuntimeError(f"スペースが見つかりません (id={space_id})")
        await tracker.set_current_file(f"Confluence: {space.name}")
        await tracker.set_stage("ページ一覧取得中")
        pages = await confluence.list_pages(space_id)
        await tracker.set_total(len(pages))
        log.info(
            "confluence ingest %d start: space=%s pages=%d",
            job_id, space.key, len(pages),
        )
        seen_ids: set[str] = set()
        for page in pages:
            await tracker.set_stage("ページ取得・整形中")
            fingerprint = _conf_fingerprint(page.version)
            slide_id = _conf_slide_id(page.id)
            seen_ids.add(slide_id)
            try:
                async with SessionLocal() as session:
                    existing = await session.get(Slide, slide_id)
                    unchanged = (
                        existing is not None
                        and existing.source_fingerprint == fingerprint
                        and existing.embedding is not None
                    )
                if not unchanged:
                    await _upsert_confluence_page(
                        SessionLocal, page, space.id, fingerprint
                    )
                    await _embed_one(
                        slide_id,
                        build_slide_embed_text(
                            title=page.title,
                            summary="",
                            body_text=page.text,
                            industry="",
                            proposal_type="",
                            graph_type="",
                            layout_type="",
                            tags=[],
                            client="",
                        ),
                    )
                await tracker.file_done(page.title)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                await tracker.file_failed(str(e), name=page.title or page.id)
        # Prune pages deleted from this space since the last ingest. Scoped to
        # source_space_id so other spaces' rows are never touched.
        await tracker.set_stage("削除ページの整理中")
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(Slide.slide_id).where(
                        Slide.source_type == "confluence",
                        Slide.source_space_id == str(space.id),
                    )
                )
            ).scalars().all()
            stale = _stale_confluence_ids(set(rows), seen_ids)
            if stale:
                await session.execute(
                    delete(Slide).where(Slide.slide_id.in_(stale))
                )
                await session.commit()
                log.info(
                    "confluence ingest %d pruned %d deleted pages",
                    job_id, len(stale),
                )
        await tracker.finish("done")
        log.info("confluence ingest %d done: pages=%d", job_id, len(pages))
    except asyncio.CancelledError:
        log.info("confluence ingest %d cancelled", job_id)
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("confluence ingest failed")
        await tracker.finish("failed", message=str(e))
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
    return {"jobId": job_id}


async def schedule_confluence_ingest(
    space_id: str, *, actor_label: str = ""
) -> bool:
    """Kick off a single-flight background Confluence ingest.

    Refused (returns False) while another ``confluence`` job is running, so a
    space can't be ingested twice at once. Reserves the job row under
    ``_SCHEDULE_LOCK`` just like ``schedule_ingest_background``.
    """
    async with _SCHEDULE_LOCK:
        await reap_stalled_jobs()
        if await _count_running("confluence") > 0:
            return False
        if await _count_running() >= MAX_CONCURRENT_JOBS:
            return False
        job_id = await _create_job("confluence", actor_label)
    task = asyncio.create_task(
        run_confluence_ingest(space_id, actor_label=actor_label, job_id=job_id)
    )
    _RUNNING_TASKS[job_id] = task
    task.add_done_callback(lambda _t, jid=job_id: _RUNNING_TASKS.pop(jid, None))
    return True
