"""Shared ranking and human-readable match evidence for slide search.

Keyword SQL ordering and result explanations intentionally use the same field
specification. Changing a weight here therefore changes both what ranks first
and what the user is told matched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import case, func, literal

from search_query import ParsedQuery


SEMANTIC_MIN_SIMILARITY = 0.69
SEMANTIC_STRONG_SIMILARITY = 0.76
SEMANTIC_NEAR_TOP_GAP = 0.02
_MAX_RANK_TERMS = 12
_SNIPPET_LENGTH = 170
_UNSPECIFIED_FACETS = {"", "その他", "不明", "未設定", "other", "unknown"}
_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LOWER = "abcdefghijklmnopqrstuvwxyz"
_FACET_TRIM_CHARS = (
    " \t\n\r\v\f"
    "\u001c\u001d\u001e\u001f\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
_ASCII_LOWER_TABLE = str.maketrans(_ASCII_UPPER, _ASCII_LOWER)


@dataclass(frozen=True)
class SearchField:
    field: str
    label: str
    dict_key: str
    sql_expr: str
    weight: int


# Higher-value fields come first. The order is also used for evidence chips.
SEARCH_FIELDS = (
    SearchField(
        "title", "タイトル", "slideTitle", "coalesce(slide_title,'')", 140
    ),
    SearchField("summary", "概要", "summary", "coalesce(summary,'')", 50),
    SearchField("tags", "タグ", "tags", "coalesce(tags::text,'')", 45),
    SearchField("body", "本文", "slideText", "coalesce(slide_text,'')", 28),
    SearchField(
        "client",
        "属性: クライアント",
        "client",
        "coalesce(client,'')",
        28,
    ),
    SearchField(
        "industry", "属性: 業界", "industry", "coalesce(industry,'')", 24
    ),
    SearchField(
        "proposalType",
        "属性: スライド種別",
        "proposalType",
        "coalesce(proposal_type,'')",
        24,
    ),
    SearchField(
        "docCategory",
        "属性: 資料種別",
        "docCategory",
        "coalesce(doc_category,'')",
        24,
    ),
    SearchField(
        "graphType",
        "属性: グラフ",
        "graphType",
        "coalesce(graph_type,'')",
        20,
    ),
    SearchField(
        "layoutType",
        "属性: 構図",
        "layoutType",
        "coalesce(layout_type,'')",
        18,
    ),
    SearchField(
        "fileName", "ファイル名", "fileName", "coalesce(file_name,'')", 20
    ),
)


def _rank_terms(parsed: ParsedQuery) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for term in parsed.positive_terms:
        key = term.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= _MAX_RANK_TERMS:
            break
    return terms


def _normalize_facet(value: object) -> str:
    """Normalize domain facets identically in Python and PostgreSQL."""
    return str(value or "").strip(_FACET_TRIM_CHARS).translate(
        _ASCII_LOWER_TABLE
    )


def _meaningful_equal(left: object, right: object) -> bool:
    left_value = _normalize_facet(left)
    right_value = _normalize_facet(right)
    return (
        left_value not in _UNSPECIFIED_FACETS
        and right_value not in _UNSPECIFIED_FACETS
        and left_value == right_value
    )


def _is_meaningful(value: object) -> bool:
    return _normalize_facet(value) not in _UNSPECIFIED_FACETS


def semantic_fit_tier(
    candidate: dict,
    leader: dict,
    similarity: float,
    leader_similarity: float,
) -> str | None:
    """Classify public semantic results using progressively weaker evidence.

    The leader and independently strong matches are retained without relying on
    sometimes-missing facets. Borderline matches need corroboration from the
    leading result's document, industry, or (only very near the leader) use.
    """
    if candidate.get("slideId") == leader.get("slideId"):
        return "leader"
    if similarity >= SEMANTIC_STRONG_SIMILARITY:
        return "strong"
    if _meaningful_equal(candidate.get("fileId"), leader.get("fileId")):
        return "same_document"
    if _meaningful_equal(candidate.get("industry"), leader.get("industry")):
        return "same_industry"
    candidate_industry = candidate.get("industry")
    leader_industry = leader.get("industry")
    if (
        not _is_meaningful(candidate_industry)
        and not _is_meaningful(leader_industry)
        and leader_similarity - similarity <= SEMANTIC_NEAR_TOP_GAP
        and (
            _meaningful_equal(
                candidate.get("proposalType"),
                leader.get("proposalType"),
            )
            or _meaningful_equal(
                candidate.get("docCategory"),
                leader.get("docCategory"),
            )
        )
    ):
        return "near_top_same_use"
    return None


def semantic_fit_tier_sql(
    candidate: dict,
    leader: dict,
    similarity,
    leader_similarity,
):
    """SQL equivalent of semantic_fit_tier for exact DB-side paging/counts."""

    def normalized(value):
        return func.translate(
            func.btrim(func.coalesce(value, ""), _FACET_TRIM_CHARS),
            _ASCII_UPPER,
            _ASCII_LOWER,
        )

    def meaningful(value):
        return normalized(value).not_in(tuple(_UNSPECIFIED_FACETS))

    def meaningful_equal(left, right):
        return (
            meaningful(left)
            & meaningful(right)
            & (normalized(left) == normalized(right))
        )

    candidate_industry = candidate["industry"]
    leader_industry = leader["industry"]
    return case(
        (
            candidate["slideId"] == leader["slideId"],
            literal("leader"),
        ),
        (
            similarity >= SEMANTIC_STRONG_SIMILARITY,
            literal("strong"),
        ),
        (
            meaningful_equal(candidate["fileId"], leader["fileId"]),
            literal("same_document"),
        ),
        (
            meaningful_equal(candidate_industry, leader_industry),
            literal("same_industry"),
        ),
        (
            ~meaningful(candidate_industry)
            & ~meaningful(leader_industry)
            & (
                leader_similarity - similarity
                <= SEMANTIC_NEAR_TOP_GAP
            )
            & (
                meaningful_equal(
                    candidate["proposalType"],
                    leader["proposalType"],
                )
                | meaningful_equal(
                    candidate["docCategory"],
                    leader["docCategory"],
                )
            ),
            literal("near_top_same_use"),
        ),
        else_=None,
    )


def keyword_rank_sql(parsed: ParsedQuery) -> tuple[str, dict]:
    """Return a parameterized Postgres relevance expression.

    Every matching field and every matching positive term contributes, so a
    title hit and multi-field evidence outrank a single incidental body hit.
    Exact and prefix title matches receive small additional bonuses.
    """
    parts: list[str] = []
    params: dict[str, str] = {}
    for index, term in enumerate(_rank_terms(parsed)):
        like_name = f"rank_{index}_like"
        params[like_name] = f"%{term}%"
        for field in SEARCH_FIELDS:
            parts.append(
                f"(CASE WHEN {field.sql_expr} ILIKE :{like_name} "
                f"THEN {field.weight} ELSE 0 END)"
            )
        exact_name = f"rank_{index}_exact"
        prefix_name = f"rank_{index}_prefix"
        params[exact_name] = term
        params[prefix_name] = f"{term}%"
        parts.extend(
            (
                "(CASE WHEN lower(coalesce(slide_title,'')) = "
                f"lower(:{exact_name}) THEN 80 ELSE 0 END)",
                "(CASE WHEN coalesce(slide_title,'') "
                f"ILIKE :{prefix_name} THEN 20 ELSE 0 END)",
            )
        )
    return (" + ".join(parts) if parts else "0"), params


def _field_text(slide: dict, field: SearchField) -> str:
    value = slide.get(field.dict_key)
    if field.dict_key == "tags":
        return "、".join(str(tag) for tag in (value or []) if tag)
    return str(value or "")


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    folded = text.casefold()
    return [
        term
        for term in terms
        if term.casefold() in folded
    ]


def _highlight_parts(text: str, terms: list[str]) -> list[dict]:
    usable = sorted(
        {term for term in terms if term},
        key=len,
        reverse=True,
    )
    if not usable:
        return [{"text": text, "matched": False}]
    pattern = re.compile(
        "(" + "|".join(re.escape(term) for term in usable) + ")",
        re.IGNORECASE,
    )
    return [
        {"text": part, "matched": bool(index % 2)}
        for index, part in enumerate(pattern.split(text))
        if part
    ]


def _snippet(text: str, terms: list[str]) -> list[dict]:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return []
    folded = compact.casefold()
    positions = [
        folded.find(term.casefold())
        for term in terms
        if term and folded.find(term.casefold()) >= 0
    ]
    if positions:
        first = min(positions)
        start = max(0, first - 55)
        end = min(len(compact), start + _SNIPPET_LENGTH)
    else:
        start = 0
        end = min(len(compact), _SNIPPET_LENGTH)
    excerpt = compact[start:end].strip()
    if start:
        excerpt = "…" + excerpt
    if end < len(compact):
        excerpt += "…"
    return _highlight_parts(excerpt, terms)


def _evidence_for(slide: dict, parsed: ParsedQuery) -> tuple[list[dict], int]:
    terms = _rank_terms(parsed)
    evidence: list[dict] = []
    score = 0
    title = str(slide.get("slideTitle") or "")
    title_folded = title.casefold()
    for field in SEARCH_FIELDS:
        text = _field_text(slide, field)
        matches = _matched_terms(text, terms)
        if not matches:
            continue
        score += field.weight * len(matches)
        evidence.append(
            {
                "field": field.field,
                "label": field.label,
                "terms": matches,
            }
        )
    for term in terms:
        term_folded = term.casefold()
        if title_folded == term_folded:
            score += 80
        if title_folded.startswith(term_folded):
            score += 20
    return evidence, score


def _best_keyword_snippet(
    slide: dict, parsed: ParsedQuery, evidence: list[dict]
) -> dict | None:
    by_field = {item["field"]: item for item in evidence}
    # A surrounding sentence is more informative than repeating the card title.
    for field_name in (
        "summary",
        "body",
        "title",
        "tags",
        "fileName",
        "client",
        "industry",
        "proposalType",
        "docCategory",
        "graphType",
        "layoutType",
    ):
        item = by_field.get(field_name)
        if not item:
            continue
        field = next(spec for spec in SEARCH_FIELDS if spec.field == field_name)
        parts = _snippet(_field_text(slide, field), item["terms"])
        if parts:
            return {"fieldLabel": field.label, "parts": parts}
    return None


def keyword_match_payload(slide: dict, parsed: ParsedQuery) -> dict:
    evidence, score = _evidence_for(slide, parsed)
    labels = [item["label"] for item in evidence]
    matched = []
    seen: set[str] = set()
    for item in evidence:
        for term in item["terms"]:
            key = term.casefold()
            if key not in seen:
                seen.add(key)
                matched.append(term)
    if labels:
        shown_labels = "・".join(labels[:3])
        if len(labels) > 3:
            shown_labels += "ほか"
        shown_terms = "」「".join(matched[:3])
        reason = f'{shown_labels}に「{shown_terms}」が一致'
    elif parsed.positive_terms:
        reason = "検索条件に一致"
    else:
        reason = "除外条件に一致する資料を除いて表示"
    return {
        "score": score,
        "matchReason": reason,
        "matchEvidence": evidence,
        "matchSnippet": _best_keyword_snippet(slide, parsed, evidence),
    }


def semantic_match_payload(
    slide: dict,
    parsed: ParsedQuery,
    similarity: float,
) -> dict:
    lexical = keyword_match_payload(slide, parsed)
    lexical_evidence = lexical["matchEvidence"]
    semantic_label = (
        "意味的に強く関連"
        if similarity >= SEMANTIC_STRONG_SIMILARITY
        else "意味的に関連"
    )
    evidence = [
        {"field": "semantic", "label": semantic_label, "terms": []},
        *lexical_evidence,
    ]
    snippet = lexical["matchSnippet"]
    if snippet is None:
        for key, label in (
            ("summary", "概要"),
            ("slideText", "本文"),
            ("slideTitle", "タイトル"),
        ):
            parts = _snippet(str(slide.get(key) or ""), [])
            if parts:
                snippet = {"fieldLabel": label, "parts": parts}
                break
    if lexical_evidence:
        labels = "・".join(item["label"] for item in lexical_evidence[:2])
        reason = f"検索文と意味が近く、{labels}にも一致"
    else:
        reason = "検索文と内容の意味が近い"
    return {
        "matchReason": reason,
        "matchEvidence": evidence,
        "matchSnippet": snippet,
        "matchStrength": (
            "strong"
            if similarity >= SEMANTIC_STRONG_SIMILARITY
            else "related"
        ),
    }