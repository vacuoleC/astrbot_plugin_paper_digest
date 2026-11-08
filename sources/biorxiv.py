"""bioRxiv / medRxiv API adapter。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .base import HttpClient, Paper, SourceAdapter, parse_datetime

logger = logging.getLogger(__name__)

API = "https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}"


class BiorxivAdapter(SourceAdapter):
    name = "biorxiv"

    def __init__(self, *, servers: tuple[str, ...] = ("biorxiv", "medrxiv"),
                 max_pages: int = 30, **kwargs) -> None:
        super().__init__(**kwargs)
        self.servers = servers
        self.max_pages = max_pages

    async def fetch(self, client: HttpClient, window_start: datetime,
                    window_end: datetime) -> list[Paper]:
        papers: dict[str, Paper] = {}
        start = window_start.strftime("%Y-%m-%d")
        end = window_end.strftime("%Y-%m-%d")
        for server in self.servers:
            cursor = 0
            for _ in range(self.max_pages):
                url = API.format(server=server, start=start, end=end, cursor=cursor)
                try:
                    data = await client.get_json(url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("%s fetch failed at cursor %s: %s", server, cursor, exc)
                    break
                self.archive.write_json(self.name, f"{server}_{cursor}", data)
                collection = data.get("collection") or []
                if not collection:
                    break
                for item in collection:
                    paper = self._parse(item, server)
                    if paper is None:
                        continue
                    dt = parse_datetime(paper.published)
                    if dt is not None:
                        # API 偶尔返回窗口前的旧条目，过滤掉（允许 1 天时区余量）
                        if dt.date() < window_start.date() - timedelta(days=1):
                            continue
                        if dt.date() > window_end.date() + timedelta(days=1):
                            continue
                    papers.setdefault(paper.canonical_key(), paper)
                messages = data.get("messages") or [{}]
                total = int(messages[0].get("total") or 0)
                count = int(messages[0].get("count") or len(collection))
                cursor += max(count, 1)
                if total and cursor >= total:
                    break
                if not total and len(collection) < 100:
                    break
        logger.info("bioRxiv/medRxiv: %s papers", len(papers))
        return list(papers.values())

    def _parse(self, item: dict, server: str) -> Paper | None:
        try:
            doi = (item.get("doi") or "").strip()
            title = (item.get("title") or "").strip()
            if not doi or not title:
                return None
            authors_raw = item.get("authors") or ""
            authors = [a.strip() for a in authors_raw.replace(";", ",").split(",") if a.strip()]
            published = item.get("date") or item.get("published")
            if published and len(str(published)) == 10:
                published = str(published) + "T00:00:00Z"
            category = (item.get("category") or "").strip()
            version = item.get("version") or "1"
            url = f"https://www.{server}.org/content/{doi}v{version}"
            return Paper(
                source=self.name,
                source_id=f"{server}:{doi}",
                doi=doi,
                title=title,
                abstract=(item.get("abstract") or "").strip(),
                authors=authors,
                url=url,
                published=published,
                categories=[category] if category else [],
                raw={"server": server, "version": version},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("bioRxiv parse error: %s", exc)
            return None
