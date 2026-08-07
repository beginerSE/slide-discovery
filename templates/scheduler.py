"""Auto-sync scheduler.

Two modes, chosen by ``config.use_drive_api()``:

* **dev (public share-link)** — there's no change feed, so we re-scan all
  registered files hourly (``_tick`` → full ``run_ingest``).
* **prod (Drive API / ADC)** — we poll the Drive Changes feed every few
  minutes (``_changes_tick``) to react to new/updated/removed decks promptly,
  plus a low-frequency full reconcile (``_tick`` daily) as a safety net for
  anything the change feed can't cover (e.g. token resets, shared-drive gaps).

The embedding backfill runs in both modes.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from drive_sync import sync_drive_changes
from ingest import (
    backfill_missing_embeddings,
    reap_stalled_jobs,
    schedule_ingest_background,
)

log = logging.getLogger("ingest.scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _tick():
    # Single-flight per kind: schedule_ingest_background refuses a new "sync"
    # run while one is already running, so an hourly tick can't pile up.
    started = await schedule_ingest_background(
        only_ids=None, force=False, kind="sync", actor_label="自動同期"
    )
    if not started:
        log.info("scheduled ingest skipped: sync job already running")
        return
    log.info("scheduled ingest tick start")


async def _changes_tick():
    try:
        result = await sync_drive_changes()
        if result.get("changes") or result.get("initialized") or result.get("reset"):
            log.info("drive changes sync: %s", result)
    except Exception:
        log.exception("drive changes sync crashed")


async def _reaper_tick():
    # Fail any ingest job that has stopped making progress (crashed mid-run)
    # so it stops blocking single-flight scheduling and clears from the UI.
    try:
        reaped = await reap_stalled_jobs()
        if reaped:
            log.info("reaped %d stalled ingest job(s)", reaped)
    except Exception:
        log.exception("stalled-job reaper crashed")


async def _embedding_tick():
    # Picks up any slide whose embedding was cleared since the last run
    # — including rows the admin just edited via the metadata screen
    # (PATCH /api/admin/slides sets embedding=None when search-relevant
    # fields change). Without this, edits could leave embeddings NULL
    # until the next process restart.
    try:
        result = await backfill_missing_embeddings()
        if result.get("filled") or result.get("failed"):
            log.info(
                "scheduled embedding backfill: filled=%d failed=%d remaining=%d",
                result.get("filled", 0),
                result.get("failed", 0),
                result.get("remaining", 0),
            )
    except Exception:
        log.exception("scheduled embedding backfill crashed")


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone="UTC")
    if config.use_drive_api():
        _scheduler.add_job(
            _changes_tick,
            "interval",
            minutes=5,
            id="drive-changes-tick",
            max_instances=1,
        )
        _scheduler.add_job(
            _tick,
            "interval",
            hours=24,
            id="ingest-reconcile-tick",
            max_instances=1,
        )
        log.info(
            "APScheduler started (drive-changes=5m, reconcile=24h, "
            "embedding-backfill=5m)"
        )
    else:
        _scheduler.add_job(
            _tick, "interval", hours=1, id="ingest-tick", max_instances=1
        )
        log.info("APScheduler started (ingest=1h, embedding-backfill=5m)")
    _scheduler.add_job(
        _embedding_tick,
        "interval",
        minutes=5,
        id="embedding-backfill-tick",
        max_instances=1,
    )
    _scheduler.add_job(
        _reaper_tick,
        "interval",
        minutes=5,
        id="stalled-job-reaper-tick",
        max_instances=1,
    )
    _scheduler.start()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
