"""Recurring-meeting ("定例") series helpers.

A *series* is the set of decks that live directly under the same Drive folder
(by convention a client-name folder). Files are ordered chronologically by a
date extracted from the file name or slide title (e.g. ``20250101``), so the
conversational ("対話検索") assistant can catch up on the most recent meetings.

``extract_doc_date`` is a pure, network-free function so the date-parsing
contract is unit-testable. ``recent_series_context`` reads the DB to assemble
the chronological context fed into the chat prompt.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import Slide

# Ordered most-specific first. Every pattern anchors the year to 20xx so we do
# not mistake arbitrary long digit runs (sizes, ids) for dates.
_DATE_PATTERNS: list[re.Pattern[str]] = [
    # 2025年1月1日 / 2025年01月01日
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    # 2025-01-01 / 2025/01/01 / 2025.01.01 / 2025_01_01
    re.compile(r"(20\d{2})[._/\-](\d{1,2})[._/\-](\d{1,2})"),
    # 20250101 (no separators)
    re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"),
    # 2025年1月 (year+month only) -> first of month
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月(?!\s*\d)"),
    # 2025-01 / 2025/01 (year+month only) -> first of month
    re.compile(r"(?<!\d)(20\d{2})[._/\-](0[1-9]|1[0-2])(?!\d)"),
    # 202501 (year+month only) -> first of month
    re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)"),
]


def extract_doc_date(text: str | None) -> date | None:
    """Best-effort parse of a meeting date embedded in a file/slide name.

    Returns the first plausible date found, or ``None``. Year+month-only
    matches resolve to the first day of the month. Invalid calendar dates
    (e.g. month 13, day 32) are skipped.
    """
    if not text:
        return None
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(text):
            groups = m.groups()
            year = int(groups[0])
            month = int(groups[1])
            day = int(groups[2]) if len(groups) >= 3 else 1
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            try:
                return date(year, month, day)
            except ValueError:
                continue
    return None


async def recent_series_context(
    session: AsyncSession,
    folder_id: str,
    *,
    limit_files: int = 6,
    per_file: int = 4,
) -> list[dict]:
    """Assemble the recent-meetings timeline for one series (folder).

    Returns a chronological (newest first) list of files, each as::

        {"fileId", "fileName", "docDate" (ISO str or None), "slides": [..]}

    where ``slides`` is up to ``per_file`` representative slides (page order)
    summarised for the prompt. Files are ordered by ``doc_date`` (newest
    first, undated last) then by most-recent ingest.
    """
    if not folder_id:
        return []
    rows = (
        await session.execute(
            select(Slide).where(Slide.folder_id == folder_id)
        )
    ).scalars().all()
    return group_series_files(rows, limit_files=limit_files, per_file=per_file)


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def group_series_files(
    rows,
    *,
    limit_files: int = 6,
    per_file: int = 4,
) -> list[dict]:
    """Group ``Slide`` rows by file and order them chronologically.

    Pure (no DB) so the grouping/ordering contract is unit-testable: any object
    exposing ``file_id / file_name / doc_date / created_at / page_no /
    slide_title / summary`` works. A file's chronology key is its ``doc_date``
    (dated newest first, undated last) with the most-recent ingest as
    tiebreaker; within a file, slides stay in page order regardless of
    per-slide ``created_at`` jitter.
    """
    by_file: dict[str, dict] = {}
    for s in rows:
        entry = by_file.get(s.file_id)
        if entry is None:
            entry = {
                "fileId": s.file_id,
                "fileName": s.file_name,
                "docDate": s.doc_date,
                "_created": s.created_at,
                "_slides": [],
            }
            by_file[s.file_id] = entry
        entry["_slides"].append(s)
        if s.created_at and (
            entry["_created"] is None or s.created_at > entry["_created"]
        ):
            entry["_created"] = s.created_at

    ordered = sorted(
        by_file.values(),
        key=lambda e: (
            e["docDate"] is not None,
            e["docDate"] or date.min,
            e["_created"] or _EPOCH,
        ),
        reverse=True,
    )

    files: list[dict] = []
    for e in ordered[:limit_files]:
        slides = sorted(e["_slides"], key=lambda s: s.page_no)[:per_file]
        files.append(
            {
                "fileId": e["fileId"],
                "fileName": e["fileName"],
                "docDate": e["docDate"].isoformat() if e["docDate"] else None,
                "slides": [
                    {
                        "pageNo": s.page_no,
                        "slideTitle": s.slide_title,
                        "summary": s.summary,
                    }
                    for s in slides
                ],
            }
        )
    return files
