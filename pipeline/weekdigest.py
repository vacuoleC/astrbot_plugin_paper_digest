"""周报的全量论文主题汇总（map-reduce），让周报能点评整个方向而不只是 3 篇精选。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter, defaultdict
from typing import Any

from ..directions import DIRECTIONS, Direction
from ..llm import chat_text, extract_json, provider_name, resolve_provider
from .normalize import abstract_snippet, paper_id, row_published
from .prefilter import score_paper

logger = logging.getLogger(__name__)

DIGEST_PROMPT_VERSION = "weekly-digest-v2"


def assign_direction(row: dict[str, Any],
                     classifications: dict[int, dict[str, Any]],
                     directions: tuple[Direction, ...] = DIRECTIONS) -> str | None:
    """优先用 LLM 主方向；未分类论文用关键词粗筛最高分方向。"""
    pk = int(row["pk"])
    cls = classifications.get(pk)
    if cls and cls.get("primary"):
        return str(cls["primary"])
    scores = {d.key: score_paper(row, d) for d in directions}
    best_key, best_score = max(scores.items(), key=lambda kv: kv[1])
    return best_key if best_score > 0 else None


def count_by_direction(rows: list[dict[str, Any]],
                       classifications: dict[int, dict[str, Any]],
                       directions: tuple[Direction, ...] = DIRECTIONS
                       ) -> dict[str, int]:
    counts: Counter = Counter()
    for row in rows:
        key = assign_direction(row, classifications, directions)
        if key:
            counts[key] += 1
    return {d.key: int(counts.get(d.key, 0)) for d in directions}


def _paper_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": paper_id(row["pk"]),
        "title": row.get("title") or "",
        "abstract": abstract_snippet(row, 150),
        "source": row.get("source") or "",
        "published": row_published(row).date().isoformat() if row_published(row) else "",
    }


def _system_prompt() -> str:
    return (
        "你是科研论文周度趋势分析助手。用户会给某个方向的一批论文（标题+摘要片段），"
        "请总结这一批论文的主题分布、主要进展和代表工作。\n"
        "只输出 JSON，不要 markdown：\n"
        '{"themes":[{"label":"简短具体的中文主题","count":3,"summary":"1-2句",'
        '"paper_ids":["p123"]}],"trend":"2-4句本批趋势","notable_paper_ids":["p123"]}\n'
        "themes 给 2-6 个；count 是该主题在本批中的论文数；paper_ids 只能从输入中选。"
    )


def _user_prompt(direction: Direction, batch: list[dict[str, Any]]) -> str:
    return json.dumps({
        "direction": direction.name,
        "description": direction.description,
        "total": len(batch),
        "papers": [_paper_item(r) for r in batch],
    }, ensure_ascii=False)


def _normalize_batch(data: Any, valid_ids: set[str]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    themes = []
    for item in data.get("themes") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        ids = [i for i in (item.get("paper_ids") or []) if i in valid_ids][:8]
        themes.append({
            "label": label[:40],
            "count": int(item.get("count") or len(ids)),
            "summary": str(item.get("summary") or "")[:200],
            "paper_ids": ids,
        })
    notable = [i for i in (data.get("notable_paper_ids") or []) if i in valid_ids][:10]
    if not themes and not notable:
        return None
    return {
        "themes": themes,
        "trend": str(data.get("trend") or "")[:400],
        "notable_paper_ids": notable,
    }


def merge_batch_digests(direction: Direction, total: int,
                        batch_digests: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    trends: list[str] = []
    notable: list[str] = []
    for digest in batch_digests:
        for theme in digest.get("themes") or []:
            key = theme["label"].lower()
            if key not in merged:
                merged[key] = {
                    "label": theme["label"],
                    "count": 0,
                    "summaries": [],
                    "paper_ids": [],
                }
            merged[key]["count"] += int(theme.get("count") or 0)
            if theme.get("summary") and theme["summary"] not in merged[key]["summaries"]:
                merged[key]["summaries"].append(theme["summary"])
            for pid in theme.get("paper_ids") or []:
                if pid not in merged[key]["paper_ids"]:
                    merged[key]["paper_ids"].append(pid)
        if digest.get("trend") and digest["trend"] not in trends:
            trends.append(digest["trend"])
        for pid in digest.get("notable_paper_ids") or []:
            if pid not in notable:
                notable.append(pid)
    themes = []
    for item in sorted(merged.values(), key=lambda x: x["count"], reverse=True)[:8]:
        themes.append({
            "label": item["label"],
            "count": item["count"],
            "summary": "；".join(item["summaries"][:2])[:300],
            "paper_ids": item["paper_ids"][:10],
        })
    return {
        "direction": direction.key,
        "name": direction.name,
        "total": total,
        "themes": themes,
        "trend": " ".join(trends[:3])[:800],
        "notable_paper_ids": notable[:10],
        "batches": len(batch_digests),
    }


def _fallback_digest(direction: Direction, rows: list[dict[str, Any]],
                     classifications: dict[int, dict[str, Any]]) -> dict[str, Any]:
    themes: dict[str, list[str]] = defaultdict(list)
    notable: list[tuple[float, str]] = []
    for row in rows:
        pk = int(row["pk"])
        cls = classifications.get(pk) or {}
        label = (cls.get("themes") or [direction.name])[0]
        themes[label].append(paper_id(pk))
        notable.append((float(cls.get("relevance") or 0), paper_id(pk)))
    notable.sort(reverse=True)
    return {
        "direction": direction.key,
        "name": direction.name,
        "total": len(rows),
        "themes": [
            {"label": label, "count": len(ids), "summary": "", "paper_ids": ids[:10]}
            for label, ids in sorted(themes.items(), key=lambda kv: len(kv[1]), reverse=True)[:8]
        ],
        "trend": f"本周共 {len(rows)} 篇；LLM 汇总不可用，以下按分类主题统计。",
        "notable_paper_ids": [pid for _, pid in notable[:10]],
        "batches": 0,
        "model": "fallback",
        "prompt_version": DIGEST_PROMPT_VERSION,
    }


async def build_direction_digest(*, context, direction: Direction,
                                 rows: list[dict[str, Any]],
                                 classifications: dict[int, dict[str, Any]],
                                 config: dict[str, Any], store,
                                 week_key: str) -> dict[str, Any]:
    if not rows:
        return {"direction": direction.key, "name": direction.name, "total": 0,
                "themes": [], "trend": "", "notable_paper_ids": [], "batches": 0}
    cached = store.get_weekly_digest(week_key, direction.key)
    cached_digest = (cached or {}).get("digest") or {}
    if (cached_digest.get("prompt_version") == DIGEST_PROMPT_VERSION
            and cached_digest.get("model") != "fallback"):
        return cached_digest

    provider = resolve_provider(context, config.get("provider_classify"))
    if provider is None:
        digest = _fallback_digest(direction, rows, classifications)
        store.save_weekly_digest(week_key, direction.key, digest, "fallback")
        return digest

    batch_size = max(20, int(config.get("weekly_digest_batch_size") or 120))
    concurrency = max(1, int(config.get("weekly_digest_concurrency") or 4))
    batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    sem = asyncio.Semaphore(concurrency)
    batch_digests: list[dict[str, Any]] = []
    lock = asyncio.Lock()
    system = _system_prompt()
    model = provider_name(provider)

    async def run_batch(batch: list[dict[str, Any]]) -> None:
        valid_ids = {paper_id(r["pk"]) for r in batch}
        async with sem:
            try:
                raw = await chat_text(provider, system_prompt=system,
                                      user_prompt=_user_prompt(direction, batch))
                parsed = _normalize_batch(extract_json(raw), valid_ids)
                if parsed is None:
                    raise ValueError("empty digest")
            except Exception as exc:  # noqa: BLE001
                logger.warning("weekly digest batch failed for %s: %s", direction.key, exc)
                parsed = None
            if parsed is None:
                # 单批失败时用该批论文的分类主题兜底
                parsed = _fallback_digest(direction, batch, classifications)
                parsed.pop("model", None)
                parsed.pop("prompt_version", None)
            async with lock:
                batch_digests.append(parsed)

    await asyncio.gather(*(run_batch(b) for b in batches))
    if not batch_digests:
        digest = _fallback_digest(direction, rows, classifications)
    else:
        digest = merge_batch_digests(direction, len(rows), batch_digests)
        digest["model"] = model
        digest["prompt_version"] = DIGEST_PROMPT_VERSION
    store.save_weekly_digest(week_key, direction.key, digest, model)
    logger.info("weekly digest %s: %s papers, %s themes, %s batches",
                direction.key, len(rows), len(digest.get("themes") or []), len(batches))
    return digest


async def build_week_digests(*, context, rows: list[dict[str, Any]],
                             classifications: dict[int, dict[str, Any]],
                             config: dict[str, Any], store,
                             week_start: str) -> dict[str, dict[str, Any]]:
    """按方向汇总本周所有论文（标题+摘要片段），供周报 SKILL 使用。"""
    groups: dict[str, list[dict[str, Any]]] = {d.key: [] for d in DIRECTIONS}
    for row in rows:
        key = assign_direction(row, classifications)
        if key in groups:
            groups[key].append(row)
    result: dict[str, dict[str, Any]] = {}
    for direction in DIRECTIONS:
        result[direction.key] = await build_direction_digest(
            context=context, direction=direction, rows=groups[direction.key],
            classifications=classifications, config=config, store=store,
            week_key=week_start,
        )
    return result
