"""纯文本/时间工具（不依赖 aiohttp，方便单测和脚本复用）。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y-%m",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title or "").lower()
    title = re.sub(r"<[^>]+>", " ", title)
    title = re.sub(r"[^a-z0-9一-鿿]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def title_fingerprint(title: str, first_author: str = "") -> str:
    raw = normalize_title(title) + "|" + (first_author or "").strip().lower()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
