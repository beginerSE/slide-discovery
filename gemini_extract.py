"""Gemini-based slide metadata extraction (via Replit AI Integrations)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

import config

log = logging.getLogger("ingest.gemini")

MODEL = "gemini-2.5-flash"

ALLOWED = {
    "industry": [
        "小売", "製造", "広告", "EC", "金融", "通信", "ヘルスケア", "人材",
        "建設", "教育", "公共", "エネルギー", "物流", "その他",
    ],
    "proposalType": [
        "新規提案", "現状分析", "施策提案", "効果検証", "ロードマップ",
        "競合比較", "コンセプト", "その他",
    ],
    "graphType": [
        "棒グラフ", "折れ線", "円グラフ", "ファネル", "散布図",
        "テーブル", "ロードマップ", "なし",
    ],
    "layoutType": [
        "タイトル中央", "左右比較", "上下分割", "4象限",
        "Before/After", "ロードマップ", "リスト",
    ],
}

PROMPT = """あなたは提案書スライドのメタデータを整理するアシスタントです。
与えられたスライドのテキストとサムネイル画像を見て、以下の項目をJSONで返してください。
タイトルや本文が短くても、推測でよいので必ず全項目を埋めること。

【重要：必ず画像を観察すること】
graphType はスライドのテキストではなくサムネイル画像の見た目で判定してください。
画像にグラフが描かれている場合は必ず該当する種別を選び、「なし」にしない。
判定の目安：
  - 縦/横の棒が並んでいる図 → 「棒グラフ」
  - 折れ線でトレンドを示す図 → 「折れ線」
  - 円/ドーナツ型でパーセンテージを示す図 → 「円グラフ」
  - 上から下へ徐々に細くなる段階図 → 「ファネル」
  - 点が散らばっている図 → 「散布図」
  - 行と列のセルが並ぶ表 → 「テーブル」
  - 時系列で工程を示す矢印/段階図 → 「ロードマップ」
  - グラフや表が一切なくテキスト/画像中心 → 「なし」
グラフが見える場合は tags にもグラフ種別（例: 円グラフ）と、グラフが示している主題を含めてください。

- industry: {industries}
- proposalType: {proposal_types}
- graphType: {graph_types}
- layoutType: {layout_types}
- tags: 関連する短いキーワードを2〜5個（日本語）。グラフが見える場合は必ずグラフ種別を1つ含める。
- summary: 1〜2文（80字以内）のスライド要約（日本語）。グラフが描かれている場合は要約にも「〜の円グラフ」のようにグラフ種別を明記する。
- reuseHint: 似た案件で再利用するときのヒント（1文、日本語）

回答はJSONオブジェクトのみで、他の文字を含めないこと。
"""


def _build_client() -> genai.Client:
    # In GCP mode, route metadata extraction through Vertex AI using ADC
    # (no API key on disk; the attached service account supplies creds).
    if config.use_vertex_ai():
        return genai.Client(
            vertexai=True,
            project=config.gcp_project(),
            location=config.gcp_location(),
        )
    # Prefer a direct GEMINI_API_KEY against the public Gemini API. The
    # Replit AI Integrations proxy currently rejects
    # `gemini-2.5-flash:generateContent` with INVALID_ENDPOINT, so we only
    # fall back to it if no direct key is configured.
    direct_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if direct_key:
        return genai.Client(api_key=direct_key)
    base_url = os.environ.get("AI_INTEGRATIONS_GEMINI_BASE_URL")
    api_key = os.environ.get("AI_INTEGRATIONS_GEMINI_API_KEY")
    if not base_url or not api_key:
        raise RuntimeError(
            "Gemini credentials missing: set GEMINI_API_KEY or the "
            "AI_INTEGRATIONS_GEMINI_* env vars"
        )
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(base_url=base_url),
    )


_client: genai.Client | None = None


def _client_once() -> genai.Client:
    global _client
    if _client is None:
        _client = _build_client()
    return _client


def _coerce(value: Any, allowed: list[str], fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        v = value.strip()
        if v in allowed:
            return v
        # case-insensitive match
        for a in allowed:
            if a.lower() == v.lower():
                return a
        return fallback if fallback in allowed else allowed[-1]
    return fallback if fallback in allowed else allowed[-1]


def _coerce_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        out = [str(t).strip().lstrip("#") for t in value if str(t).strip()]
        return out[:5]
    if isinstance(value, str):
        parts = [p.strip().lstrip("#") for p in re.split(r"[,、\s]+", value) if p.strip()]
        return parts[:5]
    return []


def _build_prompt() -> str:
    return PROMPT.format(
        industries=" / ".join(ALLOWED["industry"]),
        proposal_types=" / ".join(ALLOWED["proposalType"]),
        graph_types=" / ".join(ALLOWED["graphType"]),
        layout_types=" / ".join(ALLOWED["layoutType"]),
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        # find first {...} block
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _sync_extract(slide_text: str, thumbnail: Path | None, file_name: str, page_no: int) -> dict:
    client = _client_once()
    user_text = (
        f"ファイル名: {file_name}\n"
        f"ページ番号: {page_no}\n"
        f"スライド抽出テキスト:\n{slide_text[:3000]}\n"
    )
    parts: list[Any] = [_build_prompt(), user_text]
    if thumbnail and thumbnail.exists():
        try:
            data = thumbnail.read_bytes()
            parts.append(
                types.Part.from_bytes(
                    data=data,
                    mime_type="image/png",
                )
            )
        except Exception as e:
            log.warning("failed to attach thumbnail: %s", e)

    resp = client.models.generate_content(
        model=MODEL,
        contents=parts,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=1024,
            temperature=0.4,
        ),
    )
    text = resp.text or ""
    raw = _extract_json(text)
    return {
        "industry": _coerce(raw.get("industry"), ALLOWED["industry"], "その他"),
        "proposalType": _coerce(raw.get("proposalType"), ALLOWED["proposalType"], "その他"),
        "graphType": _coerce(raw.get("graphType"), ALLOWED["graphType"], "なし"),
        "layoutType": _coerce(raw.get("layoutType"), ALLOWED["layoutType"], "タイトル中央"),
        "tags": _coerce_tags(raw.get("tags")),
        "summary": str(raw.get("summary") or "").strip()[:200],
        "reuseHint": str(raw.get("reuseHint") or "").strip()[:200],
    }


async def extract_metadata(
    slide_text: str,
    thumbnail: Path | None,
    file_name: str,
    page_no: int,
    retries: int = 3,
) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return await asyncio.to_thread(
                _sync_extract, slide_text, thumbnail, file_name, page_no
            )
        except Exception as e:
            last = e
            log.warning(
                "gemini extract attempt %d/%d failed: %s", attempt + 1, retries, e
            )
            await asyncio.sleep(1.5 * (attempt + 1))
    assert last is not None
    raise last
