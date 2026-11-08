"""Crossref adapter：按 ISSN / 前缀抓取期刊与 chemRxiv 元数据。"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from .base import HttpClient, Paper, RateLimiter, SourceAdapter

logger = logging.getLogger(__name__)

API = "https://api.crossref.org/works"
TAG_RE = re.compile(r"<[^>]+>")


class CrossrefAdapter(SourceAdapter):
    name = "crossref"

    def __init__(self, *, direction_filters: dict[str, tuple[str, ...]],
                 rows: int = 1000, max_pages: int = 5, **kwargs) -> None:
        super().__init__(**kwargs)
        self.direction_filters = direction_filters
        self.rows = rows
        self.max_pages = max_pages
        self.rate = RateLimiter(1.0)

    async def fetch(self, client: HttpClient, window_start: datetime,
                    window_end: datetime) -> list[Paper]:
        papers: dict[str, Paper] = {}
        ok_requests = 0
        start = window_start.strftime("%Y-%m-%d")
        end = window_end.strftime("%Y-%m-%d")
        for direction_key, filters in self.direction_filters.items():
            if not filters:
                continue
            filter_str = ",".join(
                list(filters)
                + [f"from-created-date:{start}", f"until-created-date:{end}"]
            )
            cursor = "*"
            for _ in range(self.max_pages):
                await self.rate.wait()
                params = {
                    "filter": filter_str,
                    "rows": self.rows,
                    "cursor": cursor,
                    "select": "DOI,title,abstract,author,issued,published-online,"
                              "container-title,URL,type,created",
                }
                try:
                    data = await client.get_json(API, params=params)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Crossref fetch failed for %s: %s", direction_key, exc)
                    break
                ok_requests += 1
                self.archive.write_json(self.name, f"{direction_key}_{cursor[:16]}", data)
                message = data.get("message") or {}
                items = message.get("items") or []
                if not items:
                    break
                for item in items:
                    paper = self._parse(item)
                    if paper is not None:
                        papers.setdefault(paper.canonical_key(), paper)
                next_cursor = message.get("next-cursor")
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
        if ok_requests == 0:
            raise RuntimeError("Crossref API 不可用：本轮没有任何成功请求")
        logger.info("Crossref: %s papers", len(papers))
        return list(papers.values())

    def _parse(self, item: dict) -> Paper | None:
        try:
            doi = (item.get("DOI") or "").strip()
            titles = item.get("title") or []
            title = re.sub(r"\s+", " ", titles[0]).strip() if titles else ""
            if not doi or not title:
                return None
            abstract = TAG_RE.sub(" ", item.get("abstract") or "")
            abstract = re.sub(r"\s+", " ", abstract).strip()
            authors = []
            for a in item.get("author") or []:
                name = " ".join(x for x in [a.get("given"), a.get("family")] if x)
                if name:
                    authors.append(name)
            published = self._date(item)
            containers = item.get("container-title") or []
            url = item.get("URL") or f"https://doi.org/{doi}"
            return Paper(
                source=self.name,
                source_id=doi,
                doi=doi,
                title=title,
                abstract=abstract,
                authors=authors,
                url=url,
                published=published,
                categories=[containers[0]] if containers else [],
                raw={"type": item.get("type"), "publisher": item.get("publisher")},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Crossref parse error: %s", exc)
            return None

    @staticmethod
    def _date(item: dict) -> str | None:
        for key in ("published-online", "issued", "created"):
            parts = (item.get(key) or {}).get("date-parts") or []
            if parts and parts[0]:
                try:
                    y, m, d = (list(parts[0]) + [1, 1])[:3]
                    return datetime(int(y), int(m), int(d)).strftime("%Y-%m-%dT00:00:00Z")
                except Exception:  # noqa: BLE001
                    continue
        return None
