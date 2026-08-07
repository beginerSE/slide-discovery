"""Incremental Drive change detection (Drive API / ADC mode only).

In production (Drive API via ADC) we poll the Drive Changes feed instead of
re-scanning every registered folder/file on a fixed schedule. Each poll:

* re-ingests tracked files that were modified (``_ingest_one`` still skips
  unchanged content by size, so this is cheap when nothing really changed),
* drops tracked files (and their slides) that were trashed/removed,
* auto-registers and ingests *new* ``.pptx``/Google Slides files that appear
  inside a watched folder (see :class:`db.DriveFolder`).

In dev (public share-link) mode this is a no-op — the scheduler keeps running
the existing full scan there.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select

import config
import drive
from db import AddLog, AppState, DriveFile, DriveFolder, Slide, SessionLocal, utcnow
from drive import view_url
from ingest import schedule_ingest_background
from series import extract_doc_date

log = logging.getLogger("ingest.drive_sync")

_TOKEN_KEY = "drive_changes_page_token"


async def _load_token(session) -> str | None:
    row = await session.get(AppState, _TOKEN_KEY)
    return row.value if row and row.value else None


async def _save_token(session, token: str) -> None:
    row = await session.get(AppState, _TOKEN_KEY)
    if row:
        row.value = token
        row.updated_at = utcnow()
    else:
        session.add(AppState(key=_TOKEN_KEY, value=token))


async def _reset_token(reason: str) -> None:
    """Drop any saved token so the next poll re-initialises from 'now'."""
    log.warning("resetting drive changes token: %s", reason)
    async with SessionLocal() as session:
        token = await drive.get_changes_start_token()
        await _save_token(session, token)
        await session.commit()


async def sync_drive_changes() -> dict:
    """Poll the Drive Changes feed once and apply the diff.

    Safe to call in any mode: returns immediately in dev. Single results dict
    is returned for logging/inspection.
    """
    if not config.use_drive_api():
        return {"ran": False, "reason": "drive_api_disabled"}

    # First run: record a baseline token and pick up changes from here on.
    async with SessionLocal() as session:
        token = await _load_token(session)
        if not token:
            token = await drive.get_changes_start_token()
            await _save_token(session, token)
            await session.commit()
            log.info("initialized drive changes token (baseline=now)")
            return {"ran": True, "initialized": True, "changes": 0}

    try:
        changes, new_token = await drive.list_changes(token)
    except Exception as e:  # noqa: BLE001 — token may be expired/invalid
        await _reset_token(f"list_changes failed: {e}")
        return {"ran": True, "reset": True, "error": str(e)}

    reingest_ids: list[int] = []
    removed = 0
    added = 0

    async with SessionLocal() as session:
        tracked = {
            r.drive_file_id: r
            for r in (await session.execute(select(DriveFile))).scalars().all()
        }
        folders = {
            f.drive_folder_id
            for f in (await session.execute(select(DriveFolder))).scalars().all()
        }

        new_rows: dict[str, DriveFile] = {}
        folder_name_cache: dict[str, str] = {}
        for ch in changes:
            existing = tracked.get(ch.file_id)
            if ch.removed or ch.trashed:
                if existing:
                    await session.execute(
                        delete(Slide).where(Slide.file_id == ch.file_id)
                    )
                    await session.delete(existing)
                    tracked.pop(ch.file_id, None)
                    # If this file was *added* earlier in the same drain,
                    # drop it from the pending-ingest set too.
                    new_rows.pop(ch.file_id, None)
                    removed += 1
                continue
            if existing:
                # Modified tracked file → refresh its series fields from the
                # change payload (the file may have moved folders or been
                # renamed) so the re-ingest backfills slides with current
                # values, then re-ingest just this one. A None id means it's a
                # brand-new row added earlier in this same drain (recurring
                # file_id); it's already collected via new_rows, so skip to
                # avoid a None creeping into the id list.
                new_folder_id = next(
                    (p for p in ch.parents if p in folders), existing.folder_id
                )
                if new_folder_id and new_folder_id != existing.folder_id:
                    existing.folder_id = new_folder_id
                    if new_folder_id not in folder_name_cache:
                        folder_name_cache[new_folder_id] = (
                            await drive.fetch_folder_name(new_folder_id)
                        )
                    existing.folder_name = folder_name_cache[new_folder_id]
                # A rename may newly expose (or change) a date; never wipe an
                # existing date just because the new name has none.
                existing.doc_date = extract_doc_date(ch.name) or existing.doc_date
                if existing.id is not None:
                    reingest_ids.append(existing.id)
                continue
            # Brand-new file: register it only if it's a deck inside a folder
            # we're watching.
            if (
                folders
                and drive.is_pptx(ch.name, ch.mime)
                and any(p in folders for p in ch.parents)
            ):
                folder_id = next((p for p in ch.parents if p in folders), "")
                folder_name = ""
                if folder_id:
                    if folder_id not in folder_name_cache:
                        folder_name_cache[folder_id] = await drive.fetch_folder_name(
                            folder_id
                        )
                    folder_name = folder_name_cache[folder_id]
                row = DriveFile(
                    drive_file_id=ch.file_id,
                    share_url=view_url(ch.file_id),
                    file_name=ch.name,
                    status="pending",
                    folder_id=folder_id,
                    folder_name=folder_name,
                    doc_date=extract_doc_date(ch.name),
                )
                session.add(row)
                # Record auto-registration in the addition-history log so
                # admins can see Drive-folder-synced files alongside manual
                # adds (actor=None renders as "自動同期").
                session.add(
                    AddLog(
                        actor_user_id=None,
                        actor_label="自動同期",
                        action="auto",
                        drive_file_id=ch.file_id,
                        share_url=view_url(ch.file_id),
                        file_name=ch.name,
                    )
                )
                # Track immediately: the same file_id can recur within one
                # changes drain, and a second insert would violate the
                # unique constraint and abort the whole tick (before token
                # save). Subsequent entries for it now hit the re-ingest path.
                tracked[ch.file_id] = row
                new_rows[ch.file_id] = row

        await session.commit()
        # expire_on_commit=False keeps the autoincrement PK populated after
        # commit, so no refresh round-trip is needed.
        added = len(new_rows)
        for r in new_rows.values():
            reingest_ids.append(r.id)

        await _save_token(session, new_token)
        await session.commit()

    unique_ids = sorted(set(reingest_ids))
    if unique_ids:
        started = await schedule_ingest_background(
            only_ids=unique_ids, force=False, kind="sync", actor_label="自動同期"
        )
        if not started:
            # Refused: another sync is already running, or the parallel-job cap
            # is hit. These ids will be re-detected and retried on the next tick.
            log.warning(
                "drive sync reingest deferred (%d file(s)); a sync job is "
                "already running or the parallel-job cap is reached",
                len(unique_ids),
            )

    return {
        "ran": True,
        "changes": len(changes),
        "reingest": len(unique_ids),
        "added": added,
        "removed": removed,
    }
