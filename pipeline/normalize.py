"""论文行数据的归一化与辅助函数。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..textutils import normalize_title, parse_datetime, title_fingerprint  # noqa: F401


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
