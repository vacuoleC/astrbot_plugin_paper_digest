"""论文行数据的归一化与辅助函数。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from ..textutils import (  # noqa: F401
    normalize_doi,
    normalize_title,
    parse_datetime,
    title_fingerprint,
)


def row_authors(row: dict[str, Any]) -> list[str]:
    try:
        value = json.loads(row.get("authors") or "[]")
        return [str(v) for v in value if v]
    except Exception:  # noqa: BLE001
        return []


def row_categories(row: dict[str, Any]) -> list[str]:
    try:
        value = json.loads(row.get("categories") or "[]")
        return [str(v) for v in value if v]
    except Exception:  # noqa: BLE001
        return []


def row_published(row: dict[str, Any]) -> datetime:
    dt = parse_datetime(row.get("published")) or parse_datetime(row.get("first_seen_at"))
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def abstract_snippet(row: dict[str, Any], limit: int = 320) -> str:
    text = (row.get("abstract") or "").strip()
    if not text:
        return ""
    return text[:limit].rsplit(" ", 1)[0] + ("…" if len(text) > limit else "")


def paper_id(pk: int) -> str:
    return f"p{int(pk)}"


def report_dedup_key(row: dict[str, Any]) -> str:
    """报告层去重键：优先 DOI（去版本后缀），其次 arXiv ID，再次标题指纹。"""
    doi = row.get("doi")
    if doi:
        return "doi:" + normalize_doi(str(doi))
    arxiv_id = row.get("arxiv_id")
    if arxiv_id:
        return "arxiv:" + re.sub(r"v\d+$", "", str(arxiv_id), flags=re.IGNORECASE).lower()
    pmid = row.get("pmid")
    if pmid:
        return "pmid:" + str(pmid)
    return "title:" + title_fingerprint(row.get("title") or "",
                                        (row_authors(row) or [""])[0])


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一论文的 v1/v2、跨源副本只保留信息最全/最新的一条。"""
    keepers: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = report_dedup_key(row)
        current = keepers.get(key)
        if current is None:
            keepers[key] = row
            continue
        def score(r: dict[str, Any]):
            return (
                row_published(r),
                len(r.get("abstract") or ""),
                parse_datetime(r.get("first_seen_at")) or datetime.now(timezone.utc),
            )
        if score(row) > score(current):
            keepers[key] = row
    return list(keepers.values())


def filter_backfill_rows(rows: list[dict[str, Any]],
                         window_start: datetime) -> list[dict[str, Any]]:
    """历史回填论文只保留 published 落在本次窗口内的，避免旧文污染日报/周报。"""
    start = window_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    kept = []
    for row in rows:
        if not row.get("backfill"):
            kept.append(row)
            continue
        published = parse_datetime(row.get("published") or row.get("first_seen_at"))
        if published is None:
            kept.append(row)
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published >= start:
            kept.append(row)
    return kept
