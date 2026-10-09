"""PPTX text extraction + LibreOffice PNG thumbnail generation."""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

log = logging.getLogger("ingest.pptx")

# Small batches amortize pdftoppm process startup without turning the entire
# document back into one large restart boundary.
RENDER_BATCH_PAGES = 8
CHECKPOINT_WORKERS = 4
MAX_PENDING_CHECKPOINTS = 8


@dataclass
class SlideExtract:
    page_no: int
    title: str
    body_text: str
    shape_types: list[str] = field(default_factory=list)
    has_chart: bool = False
    has_table: bool = False
    has_picture: bool = False
    thumbnail_path: Path | None = None


def _shape_text(shape) -> str:
    if not shape.has_text_frame:
        return ""
    return "\n".join(p.text for p in shape.text_frame.paragraphs).strip()


def extract_slides(pptx_path: Path) -> list[SlideExtract]:
    prs = Presentation(str(pptx_path))
    out: list[SlideExtract] = []
    for idx, slide in enumerate(prs.slides, start=1):
        title = ""
        bodies: list[str] = []
        shape_types: list[str] = []
        has_chart = has_table = has_picture = False
        for shape in slide.shapes:
            try:
                stype = shape.shape_type
            except Exception:
                stype = None
            if stype is not None:
                shape_types.append(str(stype).split(".")[-1])
            if shape.has_chart:
                has_chart = True
            if stype == MSO_SHAPE_TYPE.TABLE or getattr(shape, "has_table", False):
                has_table = True
                try:
                    for row in shape.table.rows:
                        for cell in row.cells:
                            t = cell.text.strip()
                            if t:
                                bodies.append(t)
                except Exception:
                    pass
            if stype == MSO_SHAPE_TYPE.PICTURE:
                has_picture = True
            txt = _shape_text(shape)
            if not txt:
                continue
            if not title and shape == slide.shapes.title:
                title = txt
            else:
                bodies.append(txt)
        if not title and bodies:
            first = bodies[0].splitlines()[0]
            if len(first) < 80:
                title = first
        body_text = "\n".join(bodies).strip()
        body_text = re.sub(r"\n{3,}", "\n\n", body_text)[:4000]
        out.append(
            SlideExtract(
                page_no=idx,
                title=title or "(無題)",
                body_text=body_text,
                shape_types=sorted(set(shape_types)),
                has_chart=has_chart,
                has_table=has_table,
                has_picture=has_picture,
            )
        )
    return out


def render_thumbnails(
    pptx_path: Path,
    out_dir: Path,
    dpi: int = 110,
    *,
    existing_pages: set[int] | None = None,
    on_page: Callable[[int, Path], None] | None = None,
) -> list[Path]:
    """Convert PPTX to per-page PNG using LibreOffice + pdftoppm.

    ``existing_pages`` are already-rendered checkpoints in ``out_dir`` and are
    not regenerated. ``on_page`` runs after each new page is complete, allowing
    the caller to persist it before the next page starts.

    Returns the complete sorted list of PNG paths (one per slide).
    """
    started = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("render start: PPTX->PDF %s (dpi=%d)", pptx_path.name, dpi)
    with tempfile.TemporaryDirectory(prefix="pptx_") as tmp:
        tmp_dir = Path(tmp)
        # Give this conversion its OWN LibreOffice user profile. By default every
        # `soffice` invocation shares ~/.config/libreoffice, which is locked while
        # in use — so two conversions running at once make the second fail
        # intermittently (often surfaced as the misleading "failed to launch
        # javaldx / java may not function correctly" warning). An isolated,
        # throwaway profile per run removes the lock contention. `-norestore`
        # avoids reopening a crashed previous session's docs.
        profile_dir = tmp_dir / "lo_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        # 1) PPTX → PDF
        result = subprocess.run(
            [
                "soffice",
                f"-env:UserInstallation={profile_dir.as_uri()}",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                str(tmp_dir),
                str(pptx_path),
            ],
            capture_output=True,
            text=True,
            timeout=240,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice conversion failed: {result.stderr or result.stdout}"
            )
        pdfs = list(tmp_dir.glob("*.pdf"))
        if not pdfs:
            raise RuntimeError("LibreOffice produced no PDF output")
        pdf = pdfs[0]
        log.info("render: PDF->PNG %s", pptx_path.name)
        known = set(existing_pages or ())
        page_count = len(Presentation(str(pptx_path)).slides)
        missing = [
            page_no
            for page_no in range(1, page_count + 1)
            if not (page_no in known and (out_dir / f"{page_no}.png").is_file())
        ]
        batches: list[list[int]] = []
        for page_no in missing:
            if (
                not batches
                or page_no != batches[-1][-1] + 1
                or len(batches[-1]) >= RENDER_BATCH_PAGES
            ):
                batches.append([])
            batches[-1].append(page_no)

        executor = (
            ThreadPoolExecutor(
                max_workers=CHECKPOINT_WORKERS,
                thread_name_prefix="thumb-checkpoint",
            )
            if on_page is not None
            else None
        )
        pending: list[Future] = []

        def checkpoint(page_no: int, target: Path) -> None:
            if executor is None or on_page is None:
                return
            pending.append(executor.submit(on_page, page_no, target))
            # Bound queued uploads and surface callback failures promptly.
            if len(pending) >= MAX_PENDING_CHECKPOINTS:
                pending.pop(0).result()

        try:
            for batch_index, page_nos in enumerate(batches):
                first, last = page_nos[0], page_nos[-1]
                batch_dir = tmp_dir / f"render-{batch_index}"
                batch_dir.mkdir()
                page_prefix = batch_dir / "page"
                r2 = subprocess.run(
                    [
                        "pdftoppm", "-png",
                        "-f", str(first), "-l", str(last),
                        "-r", str(dpi), str(pdf), str(page_prefix),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=240,
                )
                if r2.returncode != 0:
                    raise RuntimeError(
                        f"pdftoppm failed on pages {first}-{last}: "
                        f"{r2.stderr or r2.stdout}"
                    )
                rendered = sorted(
                    batch_dir.glob("page-*.png"),
                    key=lambda path: int(path.stem.rsplit("-", 1)[-1]),
                )
                rendered_page_nos = [
                    int(path.stem.rsplit("-", 1)[-1]) for path in rendered
                ]
                if rendered_page_nos != page_nos:
                    raise RuntimeError(
                        f"pdftoppm produced pages {rendered_page_nos} for "
                        f"requested pages {page_nos}"
                    )
                for page_no, rendered_path in zip(page_nos, rendered):
                    target = out_dir / f"{page_no}.png"
                    rendered_path.replace(target)
                    checkpoint(page_no, target)

            for future in pending:
                future.result()
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=False)

        final = [out_dir / f"{page_no}.png" for page_no in range(1, page_count + 1)]
        missing_outputs = [path for path in final if not path.is_file()]
        if missing_outputs:
            raise RuntimeError(
                f"thumbnail render incomplete: {len(missing_outputs)} pages missing"
            )
        elapsed = time.monotonic() - started
        log.info(
            "render done: %s -> %d pages (%d reused, %d rendered, %d batches, "
            "%.2fs, %.2f pages/s)",
            pptx_path.name,
            len(final),
            page_count - len(missing),
            len(missing),
            len(batches),
            elapsed,
            len(missing) / elapsed if elapsed else 0.0,
        )
        return final
