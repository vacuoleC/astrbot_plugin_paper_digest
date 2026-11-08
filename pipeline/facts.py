"""把选片结果整理成 skill 的输入事实包（facts.json）。"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable

from ..directions import DIRECTIONS, DIRECTION_BY_KEY
from .normalize import abstract_snippet, paper_id, row_authors, row_published

TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]{2,}")
STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "have", "has", "had", "not", "but", "can", "could", "would", "should",
    "using", "used", "use", "based", "into", "than", "then", "also", "more",
    "most", "such", "these", "those", "their", "they", "our", "we", "you",
    "its", "it", "is", "of", "in", "on", "to", "by", "as", "at", "or", "an",
    "a", "be", "via", "under", "over", "between", "within", "without",
    "study", "paper", "results", "propose", "proposed", "method", "methods",
    "show", "shows", "shown", "new", "novel", "approach", "approaches",
    "data", "model", "models", "however", "here", "we", "our",
}


def _tokens(rows: Iterable[dict[str, Any]]) -> Counter:
    counter: Counter = Counter()
    for row in rows:
        text = ((row.get("title") or "") + " " + (row.get("abstract") or "")).lower()
        for token in TOKEN_RE.findall(text):
            if token in STOPWORDS:
                continue
            counter[token] += 1
    return counter


def compute_baseline(current_rows: list[dict[str, Any]],
                     previous_rows: list[dict[str, Any]],
                     top_n: int = 10) -> dict[str, Any]:
    cur = _tokens(current_rows)
    prev = _tokens(previous_rows)
    rising = []
    falling = []
    for term, count in cur.items():
        if count < 3:
            continue
        ratio = (count + 1) / (prev.get(term, 0) + 2)
        rising.append({
            "term": term,
            "current": count,
            "previous": prev.get(term, 0),
            "ratio": round(ratio, 2),
        })
    rising.sort(key=lambda x: x["ratio"] * math.log(x["current"] + 1), reverse=True)
    for term, count in prev.items():
        if count < 3:
            continue
        ratio = (cur.get(term, 0) + 1) / (count + 2)
        if ratio < 0.6:
            falling.append({
                "term": term,
                "current": cur.get(term, 0),
                "previous": count,
                "ratio": round(ratio, 2),
            })
    falling.sort(key=lambda x: x["ratio"])
    return {"rising_terms": rising[:top_n], "falling_terms": falling[:top_n]}


def paper_facts(item: dict[str, Any]) -> dict[str, Any]:
    row = item["row"]
    return {
        "id": paper_id(row["pk"]),
        "title": row.get("title") or "",
        "authors": row_authors(row)[:3],
        "source": row.get("source") or "",
        "url": row.get("url") or "",
        "published": (row_published(row).date().isoformat()
                      if row_published(row) else ""),
        "abstract_snippet": abstract_snippet(row, 400),
        "theme": item.get("theme") or "",
        "reason": item.get("reason") or "",
        "relevance": item.get("relevance", 0),
        "impact_score": item.get("impact_score", 0),
        "pinned": bool(item.get("pinned")),
    }


def _direction_block(direction_key: str,
                     items: list[dict[str, Any]],
                     total: int,
                     digest: dict[str, Any] | None = None,
                     daily_highlights: list[dict[str, Any]] | None = None
                     ) -> dict[str, Any]:
    direction = DIRECTION_BY_KEY[direction_key]
    papers = [paper_facts(i) for i in items]
    themes: dict[str, list[str]] = {}
    for p in papers:
        themes.setdefault(p.get("theme") or "其他", []).append(p["id"])
    block = {
        "key": direction.key,
        "name": direction.name,
        "total": int(total),
        "selected_count": len(papers),
        "themes": [{"label": label, "paper_ids": ids}
                   for label, ids in themes.items()],
        "papers": papers,
    }
    if digest is not None:
        block["digest"] = digest
    if daily_highlights is not None:
        block["daily_highlights"] = daily_highlights
    return block


def build_daily_facts(*, report_date: str, window_start: str, window_end: str,
                      selected: dict[str, list[dict[str, Any]]],
                      totals: dict[str, int],
                      source_stats: dict[str, int],
                      baseline: dict[str, Any],
                      notes: list[str] | None = None) -> dict[str, Any]:
    directions = []
    flat: dict[str, dict[str, Any]] = {}
    for d in DIRECTIONS:
        items = selected.get(d.key, [])
        block = _direction_block(d.key, items, totals.get(d.key, len(items)))
        directions.append(block)
        for paper in block["papers"]:
            flat[paper["id"]] = paper
    return {
        "schema_version": 1,
        "kind": "daily",
        "date": report_date,
        "window": {"start": window_start, "end": window_end},
        "stats": {
            "sources": source_stats,
            "total_new": sum(source_stats.values()),
            "selected_total": sum(len(v) for v in selected.values()),
        },
        "baseline": baseline,
        "directions": directions,
        "papers": flat,
        "notes": notes or [],
    }


def build_weekly_facts(*, report_date: str, week_start: str, week_end: str,
                       selected: dict[str, list[dict[str, Any]]],
                       totals: dict[str, int],
                       source_stats: dict[str, int],
                       daily_summaries: list[dict[str, Any]],
                       baseline: dict[str, Any] | None = None,
                       notes: list[str] | None = None,
                       digests: dict[str, dict[str, Any]] | None = None,
                       daily_highlights: dict[str, list[dict[str, Any]]] | None = None
                       ) -> dict[str, Any]:
    directions = []
    flat: dict[str, dict[str, Any]] = {}
    for d in DIRECTIONS:
        items = selected.get(d.key, [])
        digest = (digests or {}).get(d.key)
        total = digest.get("total", totals.get(d.key, len(items))) if digest else totals.get(
            d.key, len(items))
        block = _direction_block(
            d.key, items, total, digest=digest,
            daily_highlights=(daily_highlights or {}).get(d.key, []),
        )
        directions.append(block)
        for paper in block["papers"]:
            flat[paper["id"]] = paper
    return {
        "schema_version": 1,
        "kind": "weekly",
        "date": report_date,
        "week": {"start": week_start, "end": week_end},
        "stats": {
            "sources": source_stats,
            "total_new": sum(source_stats.values()),
            "selected_total": sum(len(v) for v in selected.values()),
        },
        "daily_summaries": daily_summaries,
        "baseline": baseline or {},
        "directions": directions,
        "papers": flat,
        "notes": notes or [],
    }
