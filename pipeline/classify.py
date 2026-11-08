"""LLM 分类：方向判定 + 主题标签，带缓存、并发与降级。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..directions import DIRECTIONS, DIRECTION_BY_KEY, Direction
from ..llm import chat_text, extract_json, provider_name, resolve_provider
from .normalize import abstract_snippet

logger = logging.getLogger(__name__)

PROMPT_VERSION = "digest-classify-v1"
FALLBACK_MODEL = "fallback-keyword"


def _system_prompt(directions: tuple[Direction, ...]) -> str:
    lines = [
        "你是论文研究方向分类助手。研究方向定义如下：",
        "",
    ]
    for d in directions:
        lines.append(f"- {d.key}（{d.name}）：{d.description}")
    lines += [
        "",
        "任务：对给定论文列表逐篇判断研究方向，并给出主题标签。",
        "规则：",
        "1. primary 必须取上面某一个方向 key，表示论文最主要的方向。",
        "2. secondary 为 0-2 个其他相关方向 key，不能与 primary 重复。",
        "3. themes 给 1-3 个简短、具体的主题标签（中英文均可，例如 single-cell foundation model / 蛋白质结构预测）。",
        "4. relevance 是 0-5 的数字，表示论文与该 primary 方向的契合度与重要性。",
        "5. reason 用一句中文说明归类理由。",
        "只输出 JSON 数组，不要 markdown 代码块，不要其他文字。",
        "数组元素格式："
        '{"id":"p123","primary":"computer_science","secondary":[],"themes":["..."],"relevance":4.0,"reason":"..."}',
    ]
    return "\n".join(lines)


def _user_prompt(batch: list[dict[str, Any]]) -> str:
    items = []
    for row in batch:
        try:
            categories = json.loads(row.get("categories") or "[]")
        except Exception:  # noqa: BLE001
            categories = []
        items.append({
            "id": f"p{int(row['pk'])}",
            "title": row.get("title") or "",
            "abstract": abstract_snippet(row, 700),
            "source": row.get("source") or "",
            "categories": categories[:6],
        })
    return json.dumps({"papers": items}, ensure_ascii=False)


def _normalize_result(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        pid = str(item.get("id") or "")
        if not pid.startswith("p"):
            return None
        pk = int(pid[1:])
        primary = item.get("primary")
        if primary not in DIRECTION_BY_KEY:
            return None
        secondary = [s for s in (item.get("secondary") or [])
                     if s in DIRECTION_BY_KEY and s != primary][:2]
        themes = [str(t).strip() for t in (item.get("themes") or []) if str(t).strip()][:3]
        if not themes:
            themes = [DIRECTION_BY_KEY[primary].name]
        relevance = float(item.get("relevance") or 0)
        relevance = max(0.0, min(5.0, relevance))
        return {
            "paper_pk": pk,
            "primary": primary,
            "secondary": secondary,
            "themes": themes,
            "relevance": relevance,
            "reason": str(item.get("reason") or "")[:200],
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("classification item invalid: %s (%s)", item, exc)
        return None


async def classify_candidates(context, candidates: dict[int, dict[str, Any]],
                              config: dict[str, Any], store,
                              directions: tuple[Direction, ...] = DIRECTIONS
                              ) -> dict[int, dict[str, Any]]:
    """返回 pk -> classification。优先读缓存，只对缺失论文调用 LLM。"""
    pks = list(candidates.keys())
    cached = store.get_classifications(pks, PROMPT_VERSION)
    results: dict[int, dict[str, Any]] = dict(cached)
    missing = [pk for pk in pks if pk not in cached]
    if not missing:
        return results

    provider = resolve_provider(context, config.get("provider_classify"))
    if provider is None:
        logger.warning("no provider, using keyword fallback")
        return _fallback(candidates, results, store)

    model = provider_name(provider)
    batch_size = int(config.get("llm_batch_size") or 25)
    concurrency = int(config.get("llm_concurrency") or 4)
    batches = [missing[i:i + batch_size] for i in range(0, len(missing), batch_size)]
    system = _system_prompt(directions)
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    failed: list[int] = []

    async def run_batch(batch_pks: list[int]) -> None:
        async with sem:
            batch_rows = [candidates[pk] for pk in batch_pks]
            try:
                raw = await chat_text(provider, system_prompt=system,
                                      user_prompt=_user_prompt(batch_rows))
                parsed = extract_json(raw)
                if not isinstance(parsed, list):
                    raise ValueError("classification output is not a list")
                async with lock:
                    for item in parsed:
                        norm = _normalize_result(item)
                        if not norm:
                            continue
                        pk = norm["paper_pk"]
                        if pk not in candidates:
                            continue
                        store.save_classification(
                            pk, primary=norm["primary"], secondary=norm["secondary"],
                            themes=norm["themes"], relevance=norm["relevance"],
                            model=model, prompt_version=PROMPT_VERSION,
                        )
                        results[pk] = norm
            except Exception as exc:  # noqa: BLE001
                logger.warning("classification batch failed (%s papers): %s",
                               len(batch_pks), exc)
                failed.extend(batch_pks)

    await asyncio.gather(*(run_batch(b) for b in batches))
    if failed:
        logger.warning("falling back for %s unclassified papers", len(failed))
        _fallback({pk: candidates[pk] for pk in failed}, results, store)
    return results


def _fallback(candidates: dict[int, dict[str, Any]],
              results: dict[int, dict[str, Any]], store) -> dict[int, dict[str, Any]]:
    for pk, row in candidates.items():
        if pk in results:
            continue
        primary = row.get("prefilter_best")
        if primary not in DIRECTION_BY_KEY:
            continue
        score = float(row.get("prefilter_score") or 0)
        relevance = max(0.5, min(5.0, score / 3.0))
        norm = {
            "paper_pk": pk,
            "primary": primary,
            "secondary": [],
            "themes": [DIRECTION_BY_KEY[primary].name],
            "relevance": round(relevance, 2),
            "reason": "关键词粗筛降级结果",
        }
        store.save_classification(
            pk, primary=primary, secondary=[], themes=norm["themes"],
            relevance=norm["relevance"], model=FALLBACK_MODEL,
            prompt_version=PROMPT_VERSION,
        )
        results[pk] = norm
    return results
