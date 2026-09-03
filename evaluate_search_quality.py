"""Compare the previous and current search algorithms on a judged mini-set.

Semantic cases call the configured embedding service. The baseline is executed
live against the same corpus: keyword results use the former creation-time
ordering, while semantic results use the former no-threshold ordering/count.

Run with:

    uv run python evaluate_search_quality.py
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

from sqlalchemy import func, select, text

import main
from db import SEARCH_EXPR, SessionLocal, Slide
from gemini_embed import embed_text
from search_query import parse_search_query


CASES_PATH = Path(__file__).parent / "tests" / "search_quality_cases.json"
_TOP_K = 12
_METRIC_K = 5


def _metrics(ids: list[str], judgments: dict[str, int]) -> dict:
    grades = [int(judgments.get(slide_id, 0)) for slide_id in ids[:_METRIC_K]]
    noise = sum(grade == 0 for grade in grades)
    relevant = sum(grade > 0 for grade in grades)
    return {
        "firstGrade": grades[0] if grades else 0,
        "relevantAt5": relevant,
        "noiseAt5": noise,
        "gainAt5": sum(grades),
        "dcgAt5": round(
            sum(
                (2**grade - 1) / math.log2(index + 2)
                for index, grade in enumerate(grades)
            ),
            4,
        ),
    }


async def _baseline_keyword(case: dict, session) -> dict:
    parsed = parse_search_query(case["query"])
    where_sql, params = main._build_keyword_where(parsed)
    result_stmt = select(Slide.slide_id)
    count_stmt = select(func.count()).select_from(Slide)
    if where_sql:
        predicate = text(where_sql).bindparams(**params)
        result_stmt = result_stmt.where(predicate)
        count_stmt = count_stmt.where(
            text(where_sql).bindparams(**params)
        )
    ids = list(
        (
            await session.execute(
                result_stmt.order_by(Slide.created_at.desc()).limit(_TOP_K)
            )
        ).scalars()
    )
    total = int((await session.execute(count_stmt)).scalar() or 0)
    return {"first": ids[0] if ids else None, "total": total, "top": ids}


async def _baseline_semantic(case: dict, session) -> dict:
    parsed = parse_search_query(case["query"])
    embed_query = " ".join(parsed.positive_terms) or case["query"]
    qvec = await embed_text(embed_query, task_type="RETRIEVAL_QUERY")
    distance = Slide.embedding.cosine_distance(qvec).label("distance")
    result_stmt = select(Slide.slide_id, distance).where(
        Slide.embedding.is_not(None)
    )
    count_stmt = select(func.count()).select_from(Slide).where(
        Slide.embedding.is_not(None)
    )
    for index, term in enumerate(parsed.excludes):
        name = f"baseline_excl_{index}"
        result_stmt = result_stmt.where(
            text(f"{SEARCH_EXPR} NOT ILIKE :{name}").bindparams(
                **{name: f"%{term}%"}
            )
        )
        count_stmt = count_stmt.where(
            text(f"{SEARCH_EXPR} NOT ILIKE :{name}").bindparams(
                **{name: f"%{term}%"}
            )
        )
    rows = (
        await session.execute(
            result_stmt.order_by(distance.asc()).limit(_TOP_K)
        )
    ).all()
    ids = [row[0] for row in rows]
    total = int((await session.execute(count_stmt)).scalar() or 0)
    return {"first": ids[0] if ids else None, "total": total, "top": ids}


async def _baseline(case: dict, session) -> dict:
    if case["mode"] == "semantic":
        return await _baseline_semantic(case, session)
    return await _baseline_keyword(case, session)


def _expectation_checks(result: dict, expected: dict) -> dict[str, bool]:
    ids = result["top"]
    checks: dict[str, bool] = {}
    if expected.get("first"):
        checks["first"] = bool(ids) and ids[0] == expected["first"]
    for slide_id in expected.get("topIncludes", []):
        checks[f"includes:{slide_id}"] = slide_id in ids
    for slide_id in expected.get("topExcludes", []):
        checks[f"excludes:{slide_id}"] = slide_id not in ids
    for left, right in expected.get("orderedBefore", []):
        checks[f"order:{left}<{right}"] = (
            left in ids and right in ids and ids.index(left) < ids.index(right)
        )
    if "maxTotal" in expected:
        checks["maxTotal"] = result["total"] <= expected["maxTotal"]
    return checks


async def evaluate() -> list[dict]:
    fixture = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    reports: list[dict] = []
    async with SessionLocal() as session:
        for case in fixture["cases"]:
            baseline = await _baseline(case, session)
            current_raw = await main.search_slides(
                q=case["query"],
                mode=case["mode"],
                industry=None,
                client=None,
                proposalType=None,
                graphType=None,
                layoutType=None,
                docCategory=None,
                tag=None,
                source=None,
                limit=_TOP_K,
                offset=0,
                session=session,
            )
            current_ids = [
                item["slideId"] for item in current_raw["items"]
            ]
            current = {
                "first": current_ids[0] if current_ids else None,
                "total": current_raw["total"],
                "top": current_ids,
                "fitTiers": [
                    item.get("semanticFitTier")
                    for item in current_raw["items"]
                    if item.get("semanticFitTier")
                ],
            }
            baseline_metrics = _metrics(baseline["top"], case["judgments"])
            current_metrics = _metrics(current["top"], case["judgments"])
            snapshot_checks = {
                "first": baseline["first"] == case["baseline"]["first"],
                "total": baseline["total"] == case["baseline"]["total"],
            }
            quality_checks = {
                "firstGradeNotWorse": (
                    current_metrics["firstGrade"]
                    >= baseline_metrics["firstGrade"]
                ),
                "noiseAt5NotWorse": (
                    current_metrics["noiseAt5"]
                    <= baseline_metrics["noiseAt5"]
                ),
            }
            expectation_checks = _expectation_checks(
                current,
                case["expect"],
            )
            improved = (
                current_metrics["firstGrade"] > baseline_metrics["firstGrade"]
                or current_metrics["noiseAt5"] < baseline_metrics["noiseAt5"]
                or current_metrics["gainAt5"] > baseline_metrics["gainAt5"]
                or current_metrics["dcgAt5"] > baseline_metrics["dcgAt5"]
            )
            improvement_required = case["role"] == "calibration"
            passed = (
                all(snapshot_checks.values())
                and all(quality_checks.values())
                and all(expectation_checks.values())
                and (improved or not improvement_required)
            )
            reports.append(
                {
                    "id": case["id"],
                    "role": case["role"],
                    "query": case["query"],
                    "mode": case["mode"],
                    "baseline": {
                        **baseline,
                        "metrics": baseline_metrics,
                    },
                    "current": {
                        **current,
                        "metrics": current_metrics,
                    },
                    "checks": {
                        "baselineSnapshot": snapshot_checks,
                        "quality": quality_checks,
                        "expected": expectation_checks,
                        "strictImprovement": improved,
                        "improvementRequired": improvement_required,
                    },
                    "passed": passed,
                }
            )
    return reports


def main_cli() -> None:
    reports = asyncio.run(evaluate())
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    if not all(report["passed"] for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main_cli()