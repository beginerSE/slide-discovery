"""Gemini text embedding helper.

Embeddings are NOT supported through the Replit AI Integrations proxy, so we
call the public Generative Language API directly with the user-provided
GEMINI_API_KEY (free tier, https://aistudio.google.com/apikey).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

import httpx

import config

log = logging.getLogger("ingest.embed")

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768
_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set; required for embedding-based "
            "semantic search."
        )
    return key


_genai_client = None


def _vertex_client():
    """Lazily build a Vertex-AI-backed google-genai client (ADC)."""
    global _genai_client
    if _genai_client is None:
        from google import genai

        _genai_client = genai.Client(
            vertexai=True,
            project=config.gcp_project(),
            location=config.gcp_location(),
        )
    return _genai_client


async def _embed_once_vertex(text: str, task_type: str) -> list[float]:
    from google.genai import types

    client = _vertex_client()
    resp = await client.aio.models.embed_content(
        model=EMBED_MODEL,
        contents=text[:8000] or " ",
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=EMBED_DIM,
        ),
    )
    embeddings = resp.embeddings or []
    values = embeddings[0].values if embeddings else None
    if not values:
        raise RuntimeError("vertex embedding response had no values")
    return [float(v) for v in values]


async def _embed_once(text: str, task_type: str) -> list[float]:
    if config.use_vertex_ai():
        return await _embed_once_vertex(text, task_type)
    payload = {
        "model": f"models/{EMBED_MODEL}",
        "content": {"parts": [{"text": text[:8000] or " "}]},
        "taskType": task_type,
        "outputDimensionality": EMBED_DIM,
    }
    url = f"{_API_BASE}/models/{EMBED_MODEL}:embedContent"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url, params={"key": _api_key()}, json=payload
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"embed HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
    values = (data.get("embedding") or {}).get("values") or []
    if not values:
        raise RuntimeError("embedding response had no values")
    return [float(v) for v in values]


async def embed_text(
    text: str,
    task_type: str = "RETRIEVAL_DOCUMENT",
    retries: int = 3,
) -> list[float]:
    """Return a single embedding vector for the given text."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return await _embed_once(text, task_type)
        except Exception as e:
            last = e
            log.warning(
                "embed attempt %d/%d failed: %s", attempt + 1, retries, e
            )
            await asyncio.sleep(1.2 * (attempt + 1))
    assert last is not None
    raise last


def build_slide_embed_text(
    *,
    title: str,
    summary: str,
    body_text: str,
    industry: str,
    proposal_type: str,
    graph_type: str,
    layout_type: str,
    tags: Iterable[str],
    client: str = "",
) -> str:
    tag_str = "、".join(t for t in tags if t)
    parts = [
        f"タイトル: {title}".strip(),
        f"要約: {summary}".strip(),
        f"業界: {industry} / 提案: {proposal_type} / グラフ: {graph_type} / 構図: {layout_type}",
        f"クライアント先: {client}".strip() if client else "",
        f"タグ: {tag_str}" if tag_str else "",
        f"本文: {body_text[:1500]}".strip(),
    ]
    return "\n".join(p for p in parts if p)
