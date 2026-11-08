"""arXiv API adapter：按分类分页抓取，水位线 + 重叠窗口。"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import feedparser

from .base import HttpClient, Paper, RateLimiter, SourceAdapter, parse_datetime

logger = logging.getLogger(__name__)

ARXIV_API = "https://export.arxiv.org/api/query"


class ArxivAdapter(SourceAdapter):
    name = "arxiv"

    def __init__(self, *, categories: list[str], request_interval: float = 3.0,
                 max_results: int = 500, max_pages: int = 5, **kwargs) -> None:
        super().__init__(**kwargs)
        self.categories = categories
        self.rate = RateLimiter(request_interval)
        self.max_results = max_results
        self.max_pages = max_pages

    async def fetch(self, client: HttpClient, window_start: datetime,
                    window_end: datetime) -> list[Paper]:
        papers: dict[str, Paper] = {}
        for category in self.categories:
            for page in range(self.max_pages):
                await self.rate.wait()
                params = {
                    "search_query": f"cat:{category}",
                    "start": page * self.max_results,
                    "max_results": self.max_results,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                }
                try:
                    text = await client.get_text(ARXIV_API, params=params)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("arXiv fetch failed for %s page %s: %s", category, page, exc)
                    break
                self.archive.write_json(self.name, f"{category}_{page}",
                                        {"params": params, "text": text})
                feed = await asyncio.to_thread(feedparser.parse, text)
                entries = feed.entries
                if not entries:
                    break
                stop = False
                for entry in entries:
                    paper = self._parse(entry)
                    if paper is None:
                        continue
                    dt = parse_datetime(paper.published)
                    if dt is not None and dt < window_start:
                        stop = True
                        continue
                    papers.setdefault(paper.canonical_key(), paper)
                if stop or len(entries) < self.max_results:
                    break
        logger.info("arXiv: %s papers (%s categories)", len(papers), len(self.categories))
        return list(papers.values())

    def _parse(self, entry) -> Paper | None:
        try:
            arxiv_url = entry.get("id", "")
            arxiv_id = arxiv_url.rsplit("/", 1)[-1] if arxiv_url else ""
            if not arxiv_id:
                return None
            title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
            abstract = re.sub(r"\s+", " ", entry.get("summary", "")).strip()
            authors = [a.get("name", "") for a in entry.get("authors", []) if a.get("name")]
            published = entry.get("published") or entry.get("updated")
            tags = [t.get("term") for t in entry.get("tags", []) if t.get("term")]
            primary = entry.get("arxiv_primary_category") or {}
            primary_term = primary.get("term") if isinstance(primary, dict) else None
            link = entry.get("link") or f"https://arxiv.org/abs/{arxiv_id}"
            doi = entry.get("arxiv_doi")
            return Paper(
                source=self.name,
                source_id=arxiv_id,
                arxiv_id=arxiv_id,
                doi=doi,
                title=title,
                abstract=abstract,
                authors=authors,
                url=link,
                published=published,
                categories=tags + ([primary_term] if primary_term and primary_term not in tags else []),
                raw={"id": arxiv_url, "published": published, "updated": entry.get("updated")},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("arXiv parse error: %s", exc)
            return None
