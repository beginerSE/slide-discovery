"""Confluence Cloud REST client (API-token auth) + storage-format HTML→text.

Reads a whole space's pages so the ingest pipeline can index them alongside
PowerPoint slides. The auth path is the same in dev and prod: HTTP Basic auth
with ``CONFLUENCE_EMAIL`` (username) + ``CONFLUENCE_API_TOKEN`` (password)
against the ``CONFLUENCE_BASE_URL`` site. ``config`` is the single source of
truth for those settings.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

import config

log = logging.getLogger("confluence")

# Confluence Cloud v2 page-list page size.
_PAGE_LIMIT = 100
_TIMEOUT = httpx.Timeout(30.0)

# Storage-format block elements after/around which we insert a newline so the
# extracted plain text keeps paragraph/list/table structure. Namespaced tags
# (e.g. ``ac:layout``) are matched on their local name.
_BLOCK_TAGS = {
    "p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "ul", "ol", "blockquote", "pre",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    @staticmethod
    def _is_block(tag: str) -> bool:
        return tag.split(":")[-1] in _BLOCK_TAGS

    def handle_starttag(self, tag, attrs):
        if self._is_block(tag):
            self._parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if self._is_block(tag):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if self._is_block(tag):
            self._parts.append("\n")

    def handle_data(self, data):
        if data:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str | None) -> str:
    """Convert Confluence storage-format XHTML to readable plain text."""
    if not html:
        return ""
    # Confluence wraps macro/code bodies in CDATA; drop the markers so the
    # inner text is parsed as ordinary character data instead of a markup decl.
    cleaned = html.replace("<![CDATA[", "").replace("]]>", "")
    parser = _TextExtractor()
    try:
        parser.feed(cleaned)
        parser.close()
    except Exception:  # noqa: BLE001 — a malformed page must not kill ingest
        log.debug("confluence html parse failed", exc_info=True)
    raw = parser.text()
    # Collapse runs of blank lines and strip per-line whitespace.
    out: list[str] = []
    blank = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            out.append(stripped)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


@dataclass
class ConfluenceSpace:
    id: str
    key: str
    name: str


@dataclass
class ConfluencePage:
    id: str
    title: str
    version: int
    url: str
    text: str


def enabled() -> bool:
    return config.confluence_enabled()


def _auth() -> tuple[str, str]:
    email = config.confluence_email()
    token = config.confluence_api_token()
    if not email or not token:
        raise RuntimeError("Confluence の認証情報（メール / API トークン）が未設定です")
    return email, token


def _base() -> str:
    base = config.confluence_base_url()
    if not base:
        raise RuntimeError("CONFLUENCE_BASE_URL が未設定です")
    return base


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_base(),
        auth=_auth(),
        timeout=_TIMEOUT,
        headers={"Accept": "application/json"},
    )


async def list_spaces() -> list[ConfluenceSpace]:
    """Every space visible to the token (id + key + name), name-sorted."""
    spaces: list[ConfluenceSpace] = []
    async with _client() as client:
        path: str = "/wiki/api/v2/spaces?limit=250"
        while path:
            resp = await client.get(path)
            resp.raise_for_status()
            data = resp.json()
            for s in data.get("results", []):
                spaces.append(
                    ConfluenceSpace(
                        id=str(s.get("id")),
                        key=s.get("key") or "",
                        name=s.get("name") or s.get("key") or str(s.get("id")),
                    )
                )
            path = (data.get("_links") or {}).get("next") or ""
    spaces.sort(key=lambda s: s.name.lower())
    return spaces


async def get_space(space_id: str) -> ConfluenceSpace | None:
    for s in await list_spaces():
        if s.id == str(space_id):
            return s
    return None


def _page_url(site_base: str, webui: str) -> str:
    """Absolute page URL from the response ``_links.base`` + page ``webui``."""
    if not webui:
        return ""
    if webui.startswith("http"):
        return webui
    return f"{site_base.rstrip('/')}{webui}"


async def list_pages(space_id: str) -> list[ConfluencePage]:
    """Every page in a space with storage body + version, paginated."""
    pages: list[ConfluencePage] = []
    async with _client() as client:
        path: str = (
            f"/wiki/api/v2/spaces/{space_id}/pages"
            f"?body-format=storage&limit={_PAGE_LIMIT}"
        )
        while path:
            resp = await client.get(path)
            resp.raise_for_status()
            data = resp.json()
            links = data.get("_links") or {}
            site_base = links.get("base") or f"{_base()}/wiki"
            for p in data.get("results", []):
                body = (
                    ((p.get("body") or {}).get("storage") or {}).get("value")
                    or ""
                )
                version = ((p.get("version") or {}).get("number")) or 0
                webui = ((p.get("_links") or {}).get("webui")) or ""
                pages.append(
                    ConfluencePage(
                        id=str(p.get("id")),
                        title=p.get("title") or "(無題)",
                        version=int(version),
                        url=_page_url(site_base, webui),
                        text=html_to_text(body),
                    )
                )
            path = links.get("next") or ""
    return pages
