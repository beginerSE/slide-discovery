"""Google Drive access.

Two backends, selected at call time by ``config.use_drive_api()``:

* **public share-link** (dev default) — scrapes the public
  ``embeddedfolderview`` page and downloads via ``uc?export=download``. No
  auth; the folder/file must be shared as "anyone with the link".
* **Drive API** (GCP mode) — authenticated listing + download via the Drive
  v3 API using ADC. Works on private files the service account can read
  (shared with the SA, or a shared drive the SA is a member of). Also handles
  native Google Slides by exporting them to PPTX.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

import config

log = logging.getLogger("ingest.drive")

_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_GSLIDES_MIME = "application/vnd.google-apps.presentation"
_FOLDER_MIME = "application/vnd.google-apps.folder"

_FOLDER_PATTERNS = [
    re.compile(r"/drive/folders/([a-zA-Z0-9_-]{20,})"),
    re.compile(r"/drive/u/\d+/folders/([a-zA-Z0-9_-]{20,})"),
]

_ID_PATTERNS = [
    re.compile(r"/file/d/([a-zA-Z0-9_-]{20,})"),
    re.compile(r"/document/d/([a-zA-Z0-9_-]{20,})"),
    re.compile(r"/presentation/d/([a-zA-Z0-9_-]{20,})"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]{20,})"),
    re.compile(r"^([a-zA-Z0-9_-]{20,})$"),
]


def extract_folder_id(text: str) -> str | None:
    s = text.strip()
    if not s:
        return None
    for pat in _FOLDER_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    return None


def extract_file_id(text: str) -> str | None:
    s = text.strip()
    if not s:
        return None
    # Don't misclassify a folder URL as a file
    if extract_folder_id(s):
        return None
    for pat in _ID_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    return None


def parse_share_input(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Parse pasted text into (file_entries, folder_ids).

    file_entries: list of (file_id, original_line)
    folder_ids:   list of folder IDs (de-duped, in pasted order)
    """
    files: list[tuple[str, str]] = []
    seen_files: set[str] = set()
    folders: list[str] = []
    seen_folders: set[str] = set()
    for raw_line in re.split(r"[\r\n,]+", text or ""):
        line = raw_line.strip()
        if not line:
            continue
        folder_id = extract_folder_id(line)
        if folder_id:
            if folder_id not in seen_folders:
                seen_folders.add(folder_id)
                folders.append(folder_id)
            continue
        fid = extract_file_id(line)
        if fid and fid not in seen_files:
            seen_files.add(fid)
            files.append((fid, line))
    return files, folders


# Split folder HTML on each entry boundary (tolerant of attribute order).
_FOLDER_ENTRY_SPLIT_RE = re.compile(r'class="flip-entry"', re.IGNORECASE)
# Within a single entry block, find id="entry-..." and the title text.
_ENTRY_ID_RE = re.compile(r'id="entry-([a-zA-Z0-9_-]{20,})"', re.IGNORECASE)
_ENTRY_TITLE_RE = re.compile(
    r'class="flip-entry-title"[^>]*>([^<]+)<', re.IGNORECASE
)

_PPTX_EXT_RE = re.compile(r"\.pptx?$", re.IGNORECASE)

# A subfolder entry in 'embeddedfolderview' links to /drive/folders/<id> and
# renders a `drive-sprite-folder-*` icon. It does NOT carry the
# `application/vnd.google-apps.folder` mime string (file entries link to
# /file/d/<id>/view and carry a real type/<mime> icon), so folder detection
# must key off the folder link / sprite, not the mime.
_FOLDER_HREF_RE = re.compile(r"/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]{20,})")
_FOLDER_SPRITE_RE = re.compile(r"drive-sprite-folder", re.IGNORECASE)


def _unescape_html(s: str) -> str:
    import html as _html
    return _html.unescape(s).strip()


def _parse_public_folder_entries(
    html: str,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Parse an 'embeddedfolderview' page into (pptx_files, subfolders).

    Both lists carry ``(id, name)`` — ``name`` is the entry's display title
    (the folder name for subfolders, the file name for files). A flip-entry is
    recognised as a folder when it links to ``/drive/folders/<id>`` or renders
    the Drive folder sprite icon (``drive-sprite-folder-*``); the legacy
    ``application/vnd.google-apps.folder`` mime is accepted as a fallback.
    """
    chunks = _FOLDER_ENTRY_SPLIT_RE.split(html)[1:]
    files: list[tuple[str, str]] = []
    folders: list[tuple[str, str]] = []
    seen_files: set[str] = set()
    seen_folders: set[str] = set()
    for chunk in chunks:
        id_m = _ENTRY_ID_RE.search(chunk)
        if not id_m:
            continue
        eid = id_m.group(1)
        title_m = _ENTRY_TITLE_RE.search(chunk)
        name = _unescape_html(title_m.group(1)) if title_m else ""
        folder_m = _FOLDER_HREF_RE.search(chunk)
        if folder_m or _FOLDER_SPRITE_RE.search(chunk) or _FOLDER_MIME in chunk:
            sub = folder_m.group(1) if folder_m else eid
            if sub not in seen_folders:
                seen_folders.add(sub)
                folders.append((sub, name))
            continue
        if not title_m:
            continue
        if eid in seen_files:
            continue
        if not _PPTX_EXT_RE.search(name):
            continue
        seen_files.add(eid)
        files.append((eid, name))
    return files, folders


def _parse_public_folder_html(html: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Backward-compatible wrapper around :func:`_parse_public_folder_entries`.

    Returns ``(pptx_files, subfolder_ids)`` (folder names dropped). Kept for the
    flat folder listing and its unit tests; new callers that need folder names
    should use :func:`_parse_public_folder_entries`.
    """
    files, folders = _parse_public_folder_entries(html)
    return files, [fid for fid, _ in folders]


async def _list_folder_tree_public(folder_id: str) -> dict:
    """Walk a public Drive folder, returning its full subfolder tree.

    Returns ``{"rootId", "rootName", "folders", "files"}`` where ``folders`` is
    ``[{"id","name","parentId"}]`` (subfolders only, named from their parent's
    listing) and ``files`` is ``[{"fileId","name","parentId"}]``. The public
    'embeddedfolderview' page does not expose the root folder's own name, so
    ``rootName`` is "" (the caller labels it). Same error semantics as the flat
    lister: only the *top* folder being unreadable raises; subfolders skip.
    """
    folders_out: list[dict] = []
    files_out: list[dict] = []
    seen_files: set[str] = set()
    seen_folders: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = [folder_id]
    timeout = httpx.Timeout(30.0, read=60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            is_top = current == folder_id
            url = f"https://drive.google.com/embeddedfolderview?id={current}#list"
            try:
                r = await client.get(url)
                not_found = r.status_code == 404
                r.raise_for_status()
                html = r.text
            except Exception as e:  # noqa: BLE001 — HTTP 404/403/429/5xx, network
                if not is_top:
                    log.warning("subfolder unreadable, skipping id=%s: %s", current, e)
                    continue
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
                    raise RuntimeError(
                        f"フォルダが見つかりません (id={folder_id}). "
                        "共有設定を確認してください。"
                    ) from e
                raise RuntimeError(
                    "フォルダの中身を取得できませんでした。"
                    "「リンクを知っている全員」に公開されているか確認してください。"
                ) from e
            if not_found or (
                "flip-entries" not in html and "flip-entry" not in html
            ):
                if is_top:
                    raise RuntimeError(
                        "フォルダの中身を取得できませんでした。"
                        "「リンクを知っている全員」に公開されているか確認してください。"
                    )
                log.warning("subfolder unreadable, skipping id=%s", current)
                continue
            files, subfolders = _parse_public_folder_entries(html)
            for fid, name in files:
                if fid in seen_files:
                    continue
                seen_files.add(fid)
                files_out.append({"fileId": fid, "name": name, "parentId": current})
            for sub, sname in subfolders:
                if sub not in seen_folders:
                    seen_folders.add(sub)
                    folders_out.append(
                        {"id": sub, "name": sname, "parentId": current}
                    )
                if sub not in visited:
                    stack.append(sub)
    return {
        "rootId": folder_id,
        "rootName": "",
        "folders": folders_out,
        "files": files_out,
    }


async def _list_folder_files_public(folder_id: str) -> list[tuple[str, str, str]]:
    """List .ppt/.pptx files under a public Drive folder, recursing subfolders.

    Returns ``(file_id, file_name, parent_folder_id)`` (immediate parent folder).
    Thin flattening of :func:`_list_folder_tree_public`.
    """
    tree = await _list_folder_tree_public(folder_id)
    return [(f["fileId"], f["name"], f["parentId"]) for f in tree["files"]]


def view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


@dataclass
class DownloadResult:
    file_id: str
    path: Path
    size: int
    etag: str | None
    file_name: str


async def _resolve_confirm(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """Some Drive files return an HTML 'virus scan' interstitial. Follow it."""
    r = await client.get(url, follow_redirects=True)
    ct = r.headers.get("content-type", "")
    if "text/html" not in ct.lower():
        return r
    html = r.text
    token_match = re.search(r"confirm=([0-9A-Za-z_-]+)", html)
    uuid_match = re.search(r'name="uuid"\s+value="([^"]+)"', html)
    form_action = re.search(r'action="(https://[^"]+)"', html)
    if form_action:
        action = form_action.group(1).replace("&amp;", "&")
        params: dict[str, str] = {}
        for m in re.finditer(r'name="([^"]+)"\s+value="([^"]+)"', html):
            params[m.group(1)] = m.group(2)
        r2 = await client.get(action, params=params, follow_redirects=True)
        return r2
    if token_match:
        sep = "&" if "?" in url else "?"
        r2 = await client.get(
            f"{url}{sep}confirm={token_match.group(1)}"
            + (f"&uuid={uuid_match.group(1)}" if uuid_match else ""),
            follow_redirects=True,
        )
        return r2
    return r


def _filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"filename\*=UTF-8''([^;]+)", value)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1))
    m = re.search(r'filename="([^"]+)"', value)
    if m:
        return m.group(1)
    return None


async def _download_public(file_id: str, out_dir: Path) -> DownloadResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    timeout = httpx.Timeout(60.0, read=300.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await _resolve_confirm(client, url)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct.lower():
            raise RuntimeError(
                f"Drive returned HTML instead of file for {file_id}. "
                "Check that the link is shared as 'Anyone with the link'."
            )
        file_name = (
            _filename_from_disposition(resp.headers.get("content-disposition"))
            or f"{file_id}.pptx"
        )
        target = out_dir / f"{file_id}.pptx"
        target.write_bytes(resp.content)
        return DownloadResult(
            file_id=file_id,
            path=target,
            size=len(resp.content),
            etag=resp.headers.get("etag"),
            file_name=file_name,
        )


# --- Authenticated Drive API backend (ADC) ---------------------------------

def _drive_service():
    """Build a Drive v3 client authenticated with ADC."""
    from googleapiclient.discovery import build

    creds = config.adc_credentials(_DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _is_pptx(name: str, mime: str) -> bool:
    return bool(_PPTX_EXT_RE.search(name or "")) or mime in (_PPTX_MIME, _GSLIDES_MIME)


def is_pptx(name: str, mime: str) -> bool:
    """Public alias of :func:`_is_pptx` for callers outside this module."""
    return _is_pptx(name, mime)


async def _list_folder_tree_api(folder_id: str) -> dict:
    """Walk a folder via the Drive API, returning its full subfolder tree.

    Returns ``{"rootId", "rootName", "folders", "files"}`` (see
    :func:`_list_folder_tree_public`). The root folder's own name is fetched via
    a metadata call (best-effort, "" on failure).
    """
    def _run() -> dict:
        svc = _drive_service()
        folders_out: list[dict] = []
        files_out: list[dict] = []
        seen_files: set[str] = set()
        seen_folders: set[str] = set()
        visited: set[str] = set()
        stack: list[str] = [folder_id]
        try:
            meta = (
                svc.files()
                .get(fileId=folder_id, fields="id, name", supportsAllDrives=True)
                .execute()
            )
            root_name = meta.get("name", "")
        except Exception:  # noqa: BLE001 — name is a best-effort label
            root_name = ""
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            page_token: str | None = None
            while True:
                resp = (
                    svc.files()
                    .list(
                        q=f"'{current}' in parents and trashed = false",
                        fields="nextPageToken, files(id, name, mimeType)",
                        pageSize=1000,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                        pageToken=page_token,
                    )
                    .execute()
                )
                for f in resp.get("files", []):
                    fid = f.get("id")
                    name = f.get("name", "")
                    mime = f.get("mimeType", "")
                    if not fid:
                        continue
                    if mime == _FOLDER_MIME:
                        if fid not in seen_folders:
                            seen_folders.add(fid)
                            folders_out.append(
                                {"id": fid, "name": name, "parentId": current}
                            )
                        if fid not in visited:
                            stack.append(fid)
                        continue
                    if fid in seen_files:
                        continue
                    if not _is_pptx(name, mime):
                        continue
                    seen_files.add(fid)
                    files_out.append(
                        {"fileId": fid, "name": name, "parentId": current}
                    )
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return {
            "rootId": folder_id,
            "rootName": root_name,
            "folders": folders_out,
            "files": files_out,
        }

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        raise RuntimeError(
            f"Drive API でフォルダを取得できませんでした (id={folder_id}): {e}"
        ) from e


async def _list_folder_files_api(folder_id: str) -> list[tuple[str, str, str]]:
    """List .ppt/.pptx files under a folder via the Drive API (flat).

    Thin flattening of :func:`_list_folder_tree_api`.
    """
    tree = await _list_folder_tree_api(folder_id)
    return [(f["fileId"], f["name"], f["parentId"]) for f in tree["files"]]


async def _download_api(file_id: str, out_dir: Path) -> DownloadResult:
    out_dir.mkdir(parents=True, exist_ok=True)

    def _run() -> DownloadResult:
        from googleapiclient.http import MediaIoBaseDownload

        svc = _drive_service()
        meta = (
            svc.files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, size, md5Checksum",
                supportsAllDrives=True,
            )
            .execute()
        )
        name = meta.get("name", f"{file_id}.pptx")
        mime = meta.get("mimeType", "")
        if mime == _GSLIDES_MIME:
            request = svc.files().export_media(fileId=file_id, mimeType=_PPTX_MIME)
            if not _PPTX_EXT_RE.search(name):
                name = f"{name}.pptx"
        else:
            request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        data = buf.getvalue()
        target = out_dir / f"{file_id}.pptx"
        target.write_bytes(data)
        return DownloadResult(
            file_id=file_id,
            path=target,
            size=len(data),
            etag=meta.get("md5Checksum"),
            file_name=name,
        )

    return await asyncio.to_thread(_run)


# --- Public dispatchers (select backend per config) ------------------------

async def list_folder_files(folder_id: str) -> list[tuple[str, str, str]]:
    """List .ppt/.pptx files inside a Drive folder, recursing subfolders.

    Returns ``(file_id, file_name, parent_folder_id)`` — ``parent_folder_id`` is
    the immediate folder each file was found in (the recurring-meeting series
    key), which may be a subfolder of the requested ``folder_id``.
    """
    if config.use_drive_api():
        return await _list_folder_files_api(folder_id)
    return await _list_folder_files_public(folder_id)


async def list_folder_tree(folder_id: str) -> dict:
    """Walk a Drive folder and return its full subfolder tree (not flattened).

    Returns ``{"rootId", "rootName", "folders", "files"}`` — ``folders`` and
    ``files`` each carry their immediate ``parentId`` so the caller can rebuild
    the nesting (see :mod:`folder_tree`). Used by the admin "フォルダ構成の確認"
    view to show which files in which subfolder are ingested vs not.
    """
    if config.use_drive_api():
        return await _list_folder_tree_api(folder_id)
    return await _list_folder_tree_public(folder_id)


async def download(file_id: str, out_dir: Path) -> DownloadResult:
    """Download a Drive file to ``out_dir`` as ``<file_id>.pptx``."""
    mode = "api" if config.use_drive_api() else "public"
    log.info("download start id=%s mode=%s", file_id, mode)
    dl = (
        await _download_api(file_id, out_dir)
        if config.use_drive_api()
        else await _download_public(file_id, out_dir)
    )
    log.info(
        "download done id=%s name=%r size=%s etag=%s",
        file_id, dl.file_name, dl.size, dl.etag,
    )
    return dl


@dataclass(frozen=True)
class DriveFileMetadata:
    name: str = ""
    parent_id: str = ""


async def _fetch_file_metadata_api(file_id: str) -> DriveFileMetadata:
    def _run() -> DriveFileMetadata:
        svc = _drive_service()
        meta = (
            svc.files()
            .get(fileId=file_id, fields="name, parents", supportsAllDrives=True)
            .execute()
        )
        parents = meta.get("parents") or []
        return DriveFileMetadata(
            name=meta.get("name", ""),
            parent_id=parents[0] if parents else "",
        )

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001
        log.warning("file metadata lookup failed (id=%s): %s", file_id, e)
        return DriveFileMetadata()


async def fetch_file_metadata(file_id: str) -> DriveFileMetadata:
    """Best-effort file name and immediate parent folder id without download."""
    if config.use_drive_api():
        return await _fetch_file_metadata_api(file_id)
    return DriveFileMetadata()


async def fetch_file_name(file_id: str) -> str:
    """Best-effort display name for a Drive file id *without downloading* it.

    Used at add time so name-collision detection can run for direct file
    links (not just folder listings). In Drive API/ADC mode this is a cheap
    ``files().get`` metadata call. In public share-link mode the name is only
    known after the file is downloaded at ingest time, so this returns "".
    """
    return (await fetch_file_metadata(file_id)).name


async def fetch_folder_name(folder_id: str) -> str:
    """Best-effort display name for a Drive *folder* id.

    Used at add time so a recurring-meeting series can be labelled with its
    (client) folder name. In Drive API/ADC mode this is a cheap
    ``files().get`` metadata call; in public share-link mode it returns "" —
    the folder id alone still keys the series, the name is just a label.
    """
    if not folder_id or not config.use_drive_api():
        return ""
    return (await _fetch_file_metadata_api(folder_id)).name


# --- Drive Changes API (incremental sync, Drive API / ADC mode only) --------


@dataclass
class DriveChange:
    """A single entry from the Drive Changes feed."""

    file_id: str
    removed: bool  # file deleted, or no longer accessible to the SA
    trashed: bool
    name: str
    mime: str
    parents: list[str]


async def get_changes_start_token() -> str:
    """Return an opaque page token marking 'now' in the Drive changes feed.

    Only meaningful in Drive API mode. Future calls to :func:`list_changes`
    with this token return everything that changed *after* this point.
    """

    def _run() -> str:
        svc = _drive_service()
        resp = svc.changes().getStartPageToken(supportsAllDrives=True).execute()
        return resp.get("startPageToken", "")

    return await asyncio.to_thread(_run)


async def list_changes(page_token: str) -> tuple[list[DriveChange], str]:
    """Drain the Drive changes feed from ``page_token``.

    Returns ``(changes, new_start_token)``. Persist ``new_start_token`` and
    pass it on the next poll. Raises on an invalid/expired token — callers
    should reset by fetching a fresh start token (and rely on the periodic
    full reconcile to catch anything missed in the gap).

    Note: this drains the user/SA corpus feed (My Drive + items shared with
    the service account). Shared Drives the SA is a *member* of may need a
    per-drive token for full coverage; the scheduled reconcile scan backstops
    that case.
    """

    def _run() -> tuple[list[DriveChange], str]:
        svc = _drive_service()
        changes: list[DriveChange] = []
        token: str | None = page_token
        new_start = page_token
        while token:
            resp = (
                svc.changes()
                .list(
                    pageToken=token,
                    spaces="drive",
                    pageSize=200,
                    includeRemoved=True,
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    fields=(
                        "nextPageToken, newStartPageToken, "
                        "changes(fileId, removed, "
                        "file(id, name, mimeType, trashed, parents))"
                    ),
                )
                .execute()
            )
            for ch in resp.get("changes", []):
                fid = ch.get("fileId")
                if not fid:
                    continue
                f = ch.get("file") or {}
                changes.append(
                    DriveChange(
                        file_id=fid,
                        removed=bool(ch.get("removed")),
                        trashed=bool(f.get("trashed")),
                        name=f.get("name", ""),
                        mime=f.get("mimeType", ""),
                        parents=list(f.get("parents") or []),
                    )
                )
            if resp.get("newStartPageToken"):
                new_start = resp["newStartPageToken"]
            token = resp.get("nextPageToken")
        return changes, new_start

    return await asyncio.to_thread(_run)
