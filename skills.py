"""Skill 加载与执行：SKILL.md 提示词 + facts 输入 + copy 输出校验。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template

from .llm import chat_text, extract_json, provider_name, resolve_provider

logger = logging.getLogger(__name__)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def load_skill(prompt_dir: str | Path, kind: str) -> tuple[dict[str, Any], str]:
    path = Path(prompt_dir) / kind / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    frontmatter = yaml.safe_load(match.group(1)) or {}
    return frontmatter, match.group(2)


def render_prompt(body: str, facts: dict[str, Any]) -> str:
    week = facts.get("week") or {}
    week_range = ""
    if week:
        week_range = f"{week.get('start', '')} ~ {week.get('end', '')}"
    template = Template(body)
    return template.render(
        facts_json=json.dumps(facts, ensure_ascii=False, indent=2),
        date=facts.get("date", ""),
        week_range=week_range,
        language="中文",
    )


def validate_copy(copy: dict[str, Any], schema_path: str | Path) -> None:
    from jsonschema import validate

    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    validate(instance=copy, schema=schema)


def _sanitize_ids(copy: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """过滤模型编造的 paper_id，只保留 facts 中存在的论文。"""
    valid = set((facts.get("papers") or {}).keys())
    for direction in copy.get("directions", []) or []:
        cited = [i for i in (direction.get("cited_paper_ids") or []) if i in valid]
        if "cited_paper_ids" in direction:
            direction["cited_paper_ids"] = cited
        papers = direction.get("papers")
        if isinstance(papers, list):
            direction["papers"] = [p for p in papers if p.get("paper_id") in valid]
    if "cited_paper_ids" in copy:
        copy["cited_paper_ids"] = [i for i in (copy.get("cited_paper_ids") or [])
                                   if i in valid]
    return copy


def _fallback_daily(facts: dict[str, Any]) -> dict[str, Any]:
    directions = []
    cited: list[str] = []
    for block in facts.get("directions", []):
        papers = block.get("papers") or []
        if not papers:
            continue
        ids = [p["id"] for p in papers[:3]]
        cited.extend(ids[:2])
        directions.append({
            "key": block["key"],
            "name": block["name"],
            "trend": f"今日共 {block.get('total', len(papers))} 篇，精选 {len(papers)} 篇。",
            "bullets": [p.get("title", "")[:60] for p in papers[:3]],
            "cited_paper_ids": ids,
        })
    stats = facts.get("stats", {})
    return {
        "schema_version": 1,
        "headline": f"今日收录 {stats.get('total_new', 0)} 篇新论文，精选 {stats.get('selected_total', 0)} 篇",
        "summary": "（降级文案）LLM 输出不可用，以下为按收录量生成的简版趋势。",
        "directions": directions,
        "cross_observations": [],
        "cited_paper_ids": cited,
    }


def _fallback_weekly(facts: dict[str, Any]) -> dict[str, Any]:
    directions = []
    for block in facts.get("directions", []):
        papers = block.get("papers") or []
        if not papers:
            continue
        directions.append({
            "key": block["key"],
            "name": block["name"],
            "analysis": f"本周共 {block.get('total', len(papers))} 篇，精选 {len(papers)} 篇。",
            "papers": [
                {"paper_id": p["id"],
                 "reason": f"方向相关度 {p.get('relevance', 0)}，影响力评分 {p.get('impact_score', 0)}。"}
                for p in papers
            ],
        })
    stats = facts.get("stats", {})
    return {
        "schema_version": 1,
        "headline": f"本周收录 {stats.get('total_new', 0)} 篇，精选 {stats.get('selected_total', 0)} 篇",
        "overview": "（降级文案）LLM 输出不可用。",
        "directions": directions,
        "cross_field": "",
        "next_week": [],
    }


async def run_skill(*, kind: str, facts: dict[str, Any], prompt_dir: str | Path,
                    context, config: dict[str, Any], store, archive) -> dict[str, Any]:
    frontmatter, body = load_skill(prompt_dir, kind)
    schema_path = Path(prompt_dir) / kind / "copy.schema.json"
    provider_key = "provider_weekly" if kind == "weekly" else "provider_summary"
    provider = resolve_provider(context, config.get(provider_key))
    fallback = _fallback_weekly(facts) if kind == "weekly" else _fallback_daily(facts)

    if provider is None:
        logger.warning("no provider for skill %s, using fallback copy", kind)
        store.save_skill_run(kind=kind, report_date=facts.get("date", ""), facts=facts,
                             raw_output=None, copy=fallback, valid=False,
                             error="no provider", model="fallback")
        return fallback

    system_prompt = frontmatter.get("system_prompt") or (
        "你是一个严谨的科研论文趋势分析助手。只依据用户提供的 facts 数据写作，"
        "不得编造论文、链接或数字。严格按用户要求输出 JSON。"
    )
    user_prompt = render_prompt(body, facts)
    model = provider_name(provider)
    last_error = None
    raw_output = None

    for attempt in range(2):
        try:
            raw_output = await chat_text(provider, system_prompt=system_prompt,
                                         user_prompt=user_prompt)
            copy = extract_json(raw_output)
            validate_copy(copy, schema_path)
            copy = _sanitize_ids(copy, facts)
            store.save_skill_run(kind=kind, report_date=facts.get("date", ""),
                                 facts=facts, raw_output=raw_output, copy=copy,
                                 valid=True, error=None, model=model)
            return copy
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("skill %s attempt %s failed: %s", kind, attempt + 1, exc)
            user_prompt = (
                render_prompt(body, facts)
                + "\n\n上一次输出未通过校验，错误如下：\n"
                + str(exc)
                + "\n上一次输出：\n"
                + (raw_output or "")
                + "\n请只输出修正后的 JSON。"
            )

    archive.write_text("skill_runs", f"{kind}_{facts.get('date', 'unknown')}_failed.txt",
                       (raw_output or "") + "\n\nERROR: " + str(last_error))
    store.save_skill_run(kind=kind, report_date=facts.get("date", ""), facts=facts,
                         raw_output=raw_output, copy=fallback, valid=False,
                         error=str(last_error), model=model)
    return fallback
