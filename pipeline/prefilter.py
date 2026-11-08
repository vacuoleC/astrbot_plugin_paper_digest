"""规则/关键词粗筛：把每天上千篇论文压到每方向数百个候选。"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..directions import DIRECTIONS, Direction


def score_paper(row: dict[str, Any], direction: Direction) -> float:
    title = (row.get("title") or "").lower()
    abstract = (row.get("abstract") or "").lower()
    try:
        categories = [str(c).lower() for c in json.loads(row.get("categories") or "[]")]
    except Exception:  # noqa: BLE001
        categories = []
    category_set = set(categories)

    score = 0.0
    for cat in direction.arxiv_categories:
        if cat.lower() in category_set:
            score += 3.0
    for cat in direction.biorxiv_categories:
        if cat.lower() in category_set:
            score += 3.0

    for kw in direction.include_keywords:
        k = kw.lower()
        if k in title:
            score += 4.0
        elif k in abstract:
            score += 1.0

    excluded = False
    for kw in direction.exclude_keywords:
        k = kw.lower()
        if k in title:
            score -= 6.0
            excluded = True
        elif k in abstract:
            score -= 2.0

    # 标题命中包含词时给一个下限；但排除词命中时不再兜底
    if not excluded and any(kw.lower() in title for kw in direction.include_keywords):
        score = max(score, 4.0)
    return score


def prefilter(rows: Iterable[dict[str, Any]], per_direction: int = 200,
              directions: tuple[Direction, ...] = DIRECTIONS
              ) -> tuple[dict[int, dict[str, Any]], dict[str, list[int]]]:
    """返回 (候选论文表, 每方向候选 pk 列表)。

    候选论文表里包含每个方向上的粗筛分与最高分方向。
    """
    candidates: dict[int, dict[str, Any]] = {}
    entries: dict[int, dict[str, Any]] = {}
    by_direction: dict[str, list[tuple[float, int]]] = {d.key: [] for d in directions}

    for row in rows:
        pk = int(row["pk"])
        scores = {d.key: score_paper(row, d) for d in directions}
        positive = {k: v for k, v in scores.items() if v > 0}
        if not positive:
            continue
        best_key, best_score = max(positive.items(), key=lambda kv: kv[1])
        entry = dict(row)
        entry["prefilter_scores"] = scores
        entry["prefilter_best"] = best_key
        entry["prefilter_score"] = best_score
        entries[pk] = entry
        for key, value in positive.items():
            by_direction[key].append((value, pk))

    result: dict[str, list[int]] = {}
    wanted: set[int] = set()
    for key, values in by_direction.items():
        values.sort(key=lambda x: x[0], reverse=True)
        top = [pk for _, pk in values[:per_direction]]
        result[key] = top
        wanted.update(top)
    # 只把各方向 top-N 的并集送给 LLM，避免把所有正分论文都送去分类
    candidates = {pk: entries[pk] for pk in wanted}
    return candidates, result
