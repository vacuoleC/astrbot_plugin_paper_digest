"""arXiv API adapter：按分类分页抓取，水位线 + 重叠窗口。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

import feedparser

from .base import HttpClient, Paper, RateLimiter, RateLimitError, SourceAdapter, parse_datetime

logger = logging.getLogger(__name__)

ARXIV_API = "https://export.arxiv.org/api/query"


class ArxivAdapter(SourceAdapter):
    name = "arxiv"

    def __init__(self, *, categories: list[str], request_interval: float = 3.0,
                 max_results: int = 500, max_pages: int = 5,
                 max_total_seconds: float = 240.0, fail_streak_limit: int = 2,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.categories = categories
        self.rate = RateLimiter(request_interval)
        self.max_results = max_results
        self.max_pages = max_pages
        self.max_total_seconds = max_total_seconds
        self.fail_streak_limit = fail_streak_limit

    async def fetch(self, client: HttpClient, window_start: datetime,
                    window_end: datetime) -> list[Paper]:
        papers: dict[str, Paper] = {}
        ok_pages = 0
        fail_streak = 0
        abort = False
        deadline = time.monotonic() + self.max_total_seconds

        for category in self.categories:
            if abort:
                break
            for page in range(self.max_pages):
                if time.monotonic() > deadline:
                    logger.warning("arXiv 抓取超过总时长 %.0fs，提前结束", self.max_total_seconds)
                    abort = True
                    break
                await self.rate.wait()
                params = {
                    "search_query": f"cat:{category}",
                    "start": page * self.max_results,
                    "max_results": self.max_results,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                }
                try:
                    text = await client.get_text(
                        ARXIV_API, params=params, retries=1, timeout=25
                    )
                except RateLimitError as exc:
                    logger.warning("arXiv 返回 429，停止本轮抓取: %s", exc)
                    abort = True
                    break
                except Exception as exc:  # noqa: BLE001
                    fail_streak += 1
                    logger.warning("arXiv fetch failed for %s page %s: %s",
                                   category, page, exc)
                    if fail_streak >= self.fail_streak_limit:
                        logger.warning("arXiv 连续失败 %s 次，停止本轮抓取", fail_streak)
                        abort = True
                    break
                ok_pages += 1
                fail_streak = 0
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
            if abort:
                break
        if ok_pages == 0:
            raise RuntimeError("arXiv API 不可用：本轮没有任何成功请求")
        logger.info("arXiv: %s papers (%s categories, %s pages)",
                    len(papers), len(self.categories), ok_pages)
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
