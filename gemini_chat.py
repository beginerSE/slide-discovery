"""Gemini text-generation helper for the conversational ("対話検索") search.

Mirrors the dual-mode design of ``gemini_embed``: in dev we call the public
Generative Language API with ``GEMINI_API_KEY``; in gcp mode we use Vertex AI
via ADC. The answer is grounded ONLY in the retrieved slides and is asked to
cite the source file name + page number, NotebookLM-style.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

import config
from gemini_embed import _API_BASE, _api_key, _vertex_client

log = logging.getLogger("api.chat")

CHAT_MODEL = "gemini-2.5-flash"

_SYSTEM_INSTRUCTION = (
    "あなたは「社内スライド検索」のAIアシスタントです。"
    "社内のスライド資料（営業提案資料・分析結果の報告資料など）を"
    "AIで横断・詳細に検索して回答します。"
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
    "- 分析結果や数値を尋ねる質問（「〜の分析結果は？」「〜の数値は？」など）"
    "には、参考スライドに書かれている具体的な数値・指標・結論をそのまま"
    "引用して答える。数値を勝手に丸めたり計算し直したりしない。\n"
    "- 資料種別（営業提案資料 / 分析結果）が与えられた場合は、提案内容か"
    "分析結果かを区別して回答する（例: 提案時の想定値か、実測の分析結果か）。\n"
    "- 「直近の定例の流れ」が与えられた場合は、その定例シリーズの時系列"
    "（新しい順）を踏まえ、最近の経緯や変化を補足してよい。その際は"
    "どの回（日付・ファイル名）の内容かを明記する。"
)


# 概要モード用のシステム指示。「簡潔に」ではなく、シリーズ全体を統合して
# 詳しく説明させる。出典明記・推測禁止のルールは通常モードと共通。
_OVERVIEW_INSTRUCTION = (
    "あなたは「社内スライド検索」のAIアシスタントです。"
    "ユーザーはプロジェクトの概要・これまでの経緯・全体像を知りたがっています。"
    "以下の「参考スライド」と「定例の流れ」に書かれている情報だけを根拠にして、"
    "日本語で【詳細に】説明してください。\n"
    "ルール:\n"
    "- 次の構成を目安に、分かる範囲でまとめる: "
    "①プロジェクトの背景・目的 ②主な内容・論点 ③時系列の経緯"
    "（各回の要点を日付順に）④直近の状況・今後の予定。\n"
    "- 根拠とした資料は、必ず「ファイル名」と「ページ番号」"
    "（定例の流れの場合は日付・ファイル名）を明記する。\n"
    "- 参考資料に無い内容は推測せず、書かない。分からない項目は"
    "「資料からは読み取れませんでした」と明記する。\n"
    "- 数値・指標・結論は資料の記載をそのまま引用し、勝手に丸めたり"
    "計算し直したりしない。\n"
    "- 見出しや箇条書きを使い、読みやすく構造化する。"
)

# 概要・経緯・全体像を問う質問の検出（純関数・ネットワーク不要）。
# 誤検出しても出典ルールは同じで害が小さいため、再現率寄りに広めに取る。
_OVERVIEW_PATTERNS = re.compile(
    "|".join(
        (
            "概要",
            "全体像",
            "経緯",
            "これまで",
            "今まで",
            "まとめて",
            "総括",
            "振り返",
            "おさらい",
            "どんなプロジェクト",
            "どういうプロジェクト",
            "どんな案件",
            "どういう案件",
            "背景と目的",
            "全体の流れ",
            "キャッチアップ",
        )
    )
)


def is_overview_question(question: str) -> bool:
    """True when the question asks for a project overview / history
    (「概要を教えて」「これまでの経緯は?」) rather than a pinpoint lookup.
    Pure keyword heuristic: deterministic, zero-latency, unit-testable;
    misfires are benign (the answer just gets broader context)."""
    return bool(_OVERVIEW_PATTERNS.search(question or ""))


def _build_series_block(
    series: list[dict] | None, *, summary_chars: int = 200
) -> str:
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
                line += f" — {summary[:summary_chars]}"
            lines.append(line)
    return "\n".join(lines)


def build_chat_prompt(
    question: str,
    slides: list[dict],
    series: list[dict] | None = None,
    overview: bool = False,
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
            # 異常に長い要約への防御: 本文抜粋(2000字)と同様に上限を設ける。
            parts.append(f"要約: {str(s['summary'])[:1000]}")
        if (
            s.get("industry")
            or s.get("client")
            or s.get("proposalType")
            or s.get("docCategory")
        ):
            parts.append(
                f"業界: {s.get('industry') or '-'}"
                f" / クライアント先: {s.get('client') or '-'}"
                f" / スライド種別: {s.get('proposalType') or '-'}"
                f" / 資料種別: {s.get('docCategory') or '-'}"
            )
        if body:
            # 分析・報告資料は本文後半に数値詳細や結論が来ることが多い。
            # 埋め込み（3000字）でヒットした根拠がGeminiにも渡るよう、
            # 回答コンテキストにも本文を2000字まで含める（topK≤20 でも
            # Gemini 2.5 Flash のコンテキストに余裕で収まる）。
            parts.append(f"本文抜粋: {body[:2000]}")
        blocks.append("\n".join(parts))
    context = "\n\n".join(blocks) if blocks else "（参考スライドはありません）"
    # 概要モードでは各回の要約を長めに渡す（統合説明の材料になるため）。
    series_block = _build_series_block(
        series, summary_chars=500 if overview else 200
    )
    series_section = (
        f"=== 直近の定例の流れ（新しい順）===\n{series_block}\n\n"
        if series_block
        else ""
    )
    instruction = _OVERVIEW_INSTRUCTION if overview else _SYSTEM_INSTRUCTION
    return (
        f"{instruction}\n\n"
        f"=== 参考スライド ===\n{context}\n\n"
        f"{series_section}"
        f"=== 質問 ===\n{question}\n\n=== 回答 ==="
    )


def _count_dated(series: list[dict] | None) -> int:
    """Number of files in the series that carry a parsed meeting date — the
    hierarchy signal that this folder is a genuine recurring ('定例') series."""
    return sum(1 for f in (series or []) if f.get("docDate"))


async def should_use_series(
    question: str,
    series_name: str,
    series: list[dict] | None,
) -> bool:
    """Decide whether the recurring-meeting timeline is worth feeding into the
    answer for THIS question — judged from the file hierarchy plus an AI
    relevance check.

    The hierarchy gate runs first with NO network call: a real 定例 series
    needs at least two dated files (a single "直近1回分" is not a flow worth
    referencing). Only then do we ask Gemini whether the question is actually
    about the series' progression (経緯・変化・進捗) rather than a one-off
    "this topic" lookup. On any AI error we keep the (already dated, multi-file)
    series so behavior degrades to the hierarchy signal alone.
    """
    if _count_dated(series) < 2:
        return False
    listing = "\n".join(
        f"- {f.get('docDate') or '日付不明'} {f.get('fileName') or '（不明）'}"
        for f in (series or [])
    )
    prompt = (
        "あなたは社内資料検索アシスタントの補助判定器です。\n"
        "下記は、同じフォルダ階層にある定期更新資料（定例シリーズの候補）の"
        "一覧です。\n"
        f"フォルダ名: {series_name or '（不明）'}\n"
        f"資料一覧（新しい順）:\n{listing}\n\n"
        f"ユーザーの質問: {question}\n\n"
        "この質問に答える際、上記シリーズの『時系列の流れ』"
        "（前回からの変化・最近の進捗・これまでの経緯・直近の状況）を"
        "踏まえることが有用ですか？\n"
        "・特定テーマの資料を一覧的に探すだけの質問なら不要。\n"
        "・経緯・変化・進捗・直近の状況などを問う質問なら有用。\n"
        "必ず『はい』または『いいえ』の一語だけで答えてください。"
    )
    try:
        out = (await _generate_once(prompt)).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("series relevance judge failed; keeping series: %s", e)
        return True
    head = out.lstrip("　 \"'「『").lower()
    return head.startswith("はい") or head.startswith(("yes", "true"))


async def _generate_once_vertex(prompt: str, max_tokens: int = 1024) -> str:
    from google.genai import types

    client = _vertex_client()
    resp = await client.aio.models.generate_content(
        model=CHAT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2, max_output_tokens=max_tokens
        ),
    )
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("vertex generate response had no text")
    return text


async def _generate_once(prompt: str, max_tokens: int = 1024) -> str:
    if config.use_vertex_ai():
        return await _generate_once_vertex(prompt, max_tokens)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
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
    overview: bool = False,
) -> str:
    """Generate a grounded answer citing source file + page. Raises on
    repeated failure so the caller can degrade gracefully (show sources
    without an AI answer). ``series`` optionally adds the 定例シリーズ
    chronological context. ``overview`` switches to the detailed
    project-overview instruction with a larger output budget."""
    prompt = build_chat_prompt(question, slides, series=series, overview=overview)
    max_tokens = 4096 if overview else 1024
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return await _generate_once(prompt, max_tokens)
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
