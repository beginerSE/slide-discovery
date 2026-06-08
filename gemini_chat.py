"""Gemini text-generation helper for the conversational ("対話検索") search.

Mirrors the dual-mode design of ``gemini_embed``: in dev we call the public
Generative Language API with ``GEMINI_API_KEY``; in gcp mode we use Vertex AI
via ADC. The answer is grounded ONLY in the retrieved slides and is asked to
cite the source file name + page number, NotebookLM-style.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

import config
from gemini_embed import _API_BASE, _api_key, _vertex_client

log = logging.getLogger("api.chat")

CHAT_MODEL = "gemini-2.5-flash"

_SYSTEM_INSTRUCTION = (
    "あなたは社内の提案スライド検索アシスタントです。"
    "ユーザーの質問に対し、以下の「参考スライド」に書かれている情報だけを"
    "根拠にして、日本語で簡潔に回答してください。\n"
    "ルール:\n"
    "- 該当する資料があれば、必ず「ファイル名」と「ページ番号」を明記する"
    "（例:「〇〇.pptx の 12ページ目で説明しています」）。\n"
    "- 複数の資料が該当する場合は、関連度の高い順に挙げる。\n"
    "- 参考スライドに無い内容は推測せず、答えない。\n"
    "- 質問に合致する資料が無い場合は「該当する資料は見つかりませんでした。」"
    "とだけ答える。\n"
    "- 箇条書きや短い文章で、要点を分かりやすくまとめる。\n"
    "- 「直近の定例の流れ」が与えられた場合は、その定例シリーズの時系列"
    "（新しい順）を踏まえ、最近の経緯や変化を補足してよい。その際は"
    "どの回（日付・ファイル名）の内容かを明記する。"
)


def _build_series_block(series: list[dict] | None) -> str:
    """Format the recent-meetings timeline for the prompt. Pure helper so the
    series-context contract is unit-testable. Returns '' when no series."""
    if not series:
        return ""
    lines: list[str] = []
    for f in series:
        date_label = f.get("docDate") or "日付不明"
        lines.append(f"■ {date_label} / {f.get('fileName') or '（不明）'}")
        for s in f.get("slides", []):
            title = s.get("slideTitle") or "（無題）"
            summary = (s.get("summary") or "").strip().replace("\n", " ")
            line = f"  - p{s.get('pageNo')}: {title}"
            if summary:
                line += f" — {summary[:200]}"
            lines.append(line)
    return "\n".join(lines)


def build_chat_prompt(
    question: str,
    slides: list[dict],
    series: list[dict] | None = None,
) -> str:
    """Assemble the grounded prompt from the retrieved slides. Pure function
    so the formatting/citation contract is unit-testable without a network
    call. ``series`` (optional) is the chronological recent-meetings timeline
    for the detected 定例シリーズ."""
    blocks: list[str] = []
    for i, s in enumerate(slides, start=1):
        body = (s.get("slideText") or "").strip().replace("\n", " ")
        parts = [
            f"[{i}] ファイル名: {s.get('fileName') or '（不明）'}"
            f" / ページ: {s.get('pageNo')}",
            f"タイトル: {s.get('slideTitle') or '（無題）'}",
        ]
        if s.get("summary"):
            parts.append(f"要約: {s['summary']}")
        if s.get("industry") or s.get("client") or s.get("proposalType"):
            parts.append(
                f"業界: {s.get('industry') or '-'}"
                f" / クライアント先: {s.get('client') or '-'}"
                f" / 提案種別: {s.get('proposalType') or '-'}"
            )
        if body:
            parts.append(f"本文抜粋: {body[:600]}")
        blocks.append("\n".join(parts))
    context = "\n\n".join(blocks) if blocks else "（参考スライドはありません）"
    series_block = _build_series_block(series)
    series_section = (
        f"=== 直近の定例の流れ（新しい順）===\n{series_block}\n\n"
        if series_block
        else ""
    )
    return (
        f"{_SYSTEM_INSTRUCTION}\n\n"
        f"=== 参考スライド ===\n{context}\n\n"
        f"{series_section}"
        f"=== 質問 ===\n{question}\n\n=== 回答 ==="
    )


async def _generate_once_vertex(prompt: str) -> str:
    from google.genai import types

    client = _vertex_client()
    resp = await client.aio.models.generate_content(
        model=CHAT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2, max_output_tokens=1024
        ),
    )
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("vertex generate response had no text")
    return text


async def _generate_once(prompt: str) -> str:
    if config.use_vertex_ai():
        return await _generate_once_vertex(prompt)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024},
    }
    url = f"{_API_BASE}/models/{CHAT_MODEL}:generateContent"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, params={"key": _api_key()}, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"generate HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("generate response had no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("generate response had no text")
    return text


async def generate_answer(
    question: str,
    slides: list[dict],
    retries: int = 2,
    series: list[dict] | None = None,
) -> str:
    """Generate a grounded answer citing source file + page. Raises on
    repeated failure so the caller can degrade gracefully (show sources
    without an AI answer). ``series`` optionally adds the 定例シリーズ
    chronological context."""
    prompt = build_chat_prompt(question, slides, series=series)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return await _generate_once(prompt)
        except Exception as e:
            last = e
            log.warning(
                "chat generate attempt %d/%d failed: %s",
                attempt + 1,
                retries,
                e,
            )
            await asyncio.sleep(1.0 * (attempt + 1))
    assert last is not None
    raise last
