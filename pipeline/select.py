"""选片：主题覆盖优先 + 每日 4n 上限；周报按影响力启发式排序。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..directions import DIRECTIONS, DIRECTION_BY_KEY, Direction

logger = logging.getLogger(__name__)

CODE_SIGNALS = ("github", "code is available", "we release", "open-source",
                "dataset is available", "benchmark")
SOTA_SIGNALS = ("state-of-the-art", "sota", "outperform", "breakthrough")
REVIEW_RE = re.compile(r"\b(review|survey|overview|tutorial)\b", re.IGNORECASE)


def _relevance(cls: dict[str, Any] | None) -> float:
    return float((cls or {}).get("relevance") or 0.0)


def _themes(cls: dict[str, Any] | None) -> list[str]:
    return list((cls or {}).get("themes") or [])


def _row_text(row: dict[str, Any]) -> str:
    return ((row.get("title") or "") + " " + (row.get("abstract") or "")).lower()


def is_review(row: dict[str, Any]) -> bool:
    text = (row.get("title") or "") + " " + (row.get("abstract") or "")
    if REVIEW_RE.search(text):
        return True
    try:
        categories = " ".join(json.loads(row.get("categories") or "[]"))
    except Exception:  # noqa: BLE001
        categories = ""
    return "review" in categories.lower()


def impact_score(row: dict[str, Any], cls: dict[str, Any] | None) -> float:
    """周报用的启发式影响力分；后续可用历史引用回放校准权重。"""
    score = _relevance(cls) * 1.0
    if row.get("source") == "crossref":
        score += 1.0  # 已正式发表
    elif row.get("source") == "europepmc":
        score += 0.5
    text = _row_text(row)
    if any(sig in text for sig in CODE_SIGNALS):
        score += 1.0
    if any(sig in text for sig in SOTA_SIGNALS):
        score += 0.5
    if is_review(row):
        score -= 3.0  # 综述/教程不应该靠体量和来源刷到方向第一
    return score


def select_direction(rows: list[dict[str, Any]],
                     classifications: dict[int, dict[str, Any]],
                     overrides: dict[int, dict[str, Any]],
                     *, direction_key: str, top_n: int = 5,
                     max_themes: int = 5) -> list[dict[str, Any]]:
    """在一个方向内先按主题取代表，再按分数补足。"""
    eligible: list[dict[str, Any]] = []
    for row in rows:
        pk = int(row["pk"])
        ov = overrides.get(pk)
        if ov and ov.get("action") == "skip":
            continue
        cls = classifications.get(pk)
        if not cls or cls.get("primary") != direction_key:
            continue
        eligible.append({
            "pk": pk,
            "row": row,
            "cls": cls,
            "score": _relevance(cls) * 10 + float(row.get("prefilter_score") or 0),
            "pinned": bool(ov and ov.get("action") == "pin"),
        })

    # 置顶论文优先
    pinned = [e for e in eligible if e["pinned"]]
    rest = [e for e in eligible if not e["pinned"]]
    rest.sort(key=lambda e: e["score"], reverse=True)

    chosen: list[dict[str, Any]] = []
    seen: set[int] = set()
    for e in pinned:
        if e["pk"] not in seen:
            chosen.append(e)
            seen.add(e["pk"])

    # 主题覆盖：每个主题先选最高分代表
    theme_rep: dict[str, dict[str, Any]] = {}
    for e in rest:
        themes = _themes(e["cls"])
        theme = themes[0] if themes else "其他"
        if theme not in theme_rep:
            theme_rep[theme] = e
        if len(theme_rep) >= max_themes:
            break
    for e in theme_rep.values():
        if e["pk"] not in seen and len(chosen) < top_n:
            chosen.append(e)
            seen.add(e["pk"])

    # 补足到 top_n
    for e in rest:
        if len(chosen) >= top_n:
            break
        if e["pk"] not in seen:
            chosen.append(e)
            seen.add(e["pk"])

    result = []
    for e in chosen[:top_n]:
        themes = _themes(e["cls"])
        result.append({
            "pk": e["pk"],
            "row": e["row"],
            "theme": themes[0] if themes else "其他",
            "themes": themes,
            "relevance": _relevance(e["cls"]),
            "reason": (e["cls"] or {}).get("reason") or "",
            "pinned": e["pinned"],
            "impact_score": impact_score(e["row"], e["cls"]),
            "is_review": is_review(e["row"]),
        })
    return result


def allocate_cap(selected: dict[str, list[dict[str, Any]]], cap: int
                 ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """多退少补：总数超过 cap 时，从篇数最多的方向删最低分论文。"""
    selected = {k: list(v) for k, v in selected.items()}
    dropped: list[dict[str, Any]] = []
    while sum(len(v) for v in selected.values()) > cap:
        candidates = [(k, v) for k, v in selected.items() if v]
        if not candidates:
            break
        key, items = max(candidates, key=lambda kv: (len(kv[1]), -sum(
            x.get("relevance", 0) for x in kv[1]) / max(len(kv[1]), 1)))
        non_pinned = [x for x in items if not x.get("pinned")]
        victim = min(non_pinned or items, key=lambda x: (
            x.get("relevance", 0),
            x.get("row", {}).get("prefilter_score", 0),
        ))
        selected[key].remove(victim)
        dropped.append(victim)
    return selected, dropped


def select_weekly(rows: list[dict[str, Any]],
                  classifications: dict[int, dict[str, Any]],
                  overrides: dict[int, dict[str, Any]],
                  *, per_direction: int = 3) -> dict[str, list[dict[str, Any]]]:
    selected: dict[str, list[dict[str, Any]]] = {d.key: [] for d in DIRECTIONS}
    for d in DIRECTIONS:
        pool = []
        for row in rows:
            pk = int(row["pk"])
            ov = overrides.get(pk)
            if ov and ov.get("action") == "skip":
                continue
            cls = classifications.get(pk) or {}
            if cls.get("primary") != d.key:
                continue
            pool.append({
                "pk": pk,
                "row": row,
                "cls": cls,
                "theme": (_themes(cls) or [d.name])[0],
                "themes": _themes(cls),
                "relevance": _relevance(cls),
                "reason": cls.get("reason") or "",
                "pinned": bool(ov and ov.get("action") == "pin"),
                "impact_score": impact_score(row, cls),
                "is_review": is_review(row),
            })
        pool.sort(key=lambda x: (x["pinned"], not x.get("is_review"), x["impact_score"]), reverse=True)
        selected[d.key] = pool[:per_direction]
    return selected
