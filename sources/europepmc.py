"""Europe PMC REST adapter：按方向关键词查询，覆盖 PubMed/PMC/预印本。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .base import HttpClient, Paper, RateLimiter, SourceAdapter

logger = logging.getLogger(__name__)

API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


class EuropePmcAdapter(SourceAdapter):
    name = "europepmc"
    # 容器内 Python 默认 TLS 握手访问 EBI 会 ConnectionReset（2026-11-09 实测），
    # curl 和 ssl=False 正常，因此对 EBI 关闭证书校验。
    ssl_verify = False
    # 查询本身会向前看 lookback_days 天，抓取窗口只需 1 天
    fetch_lookback_days = 1

    def __init__(self, *, direction_terms: dict[str, tuple[str, ...]],
                 page_size: int = 1000, max_pages: int = 5,
                 lookback_days: int = 7, **kwargs) -> None:
        super().__init__(**kwargs)
        self.direction_terms = direction_terms
        self.page_size = page_size
        self.max_pages = max_pages
        self.lookback_days = lookback_days
        self.rate = RateLimiter(1.0)

    async def fetch(self, client: HttpClient, window_start: datetime,
                    window_end: datetime) -> list[Paper]:
        papers: dict[str, Paper] = {}
        ok_requests = 0
        # Europe PMC 索引有 3-7 天延迟，日期窗口向前放宽，靠 first_seen 去重
        start = (window_start - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        end = window_end.strftime("%Y-%m-%d")
        for direction_key, terms in self.direction_terms.items():
            if not terms:
                continue
            term_query = " OR ".join(
                f'"{t}"' if " " in t else t for t in terms
            )
            query = (
                f"({term_query}) AND (FIRST_PDATE:[{start} TO {end}]) "
                f"AND (SRC:MED OR SRC:PPR)"
            )
            cursor = "*"
            for _ in range(self.max_pages):
                await self.rate.wait()
                params = {
                    "query": query,
                    "format": "json",
                    "pageSize": self.page_size,
                    "cursorMark": cursor,
                    "resultType": "core",
                }
                try:
                    data = await client.get_json(API, params=params)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("EuropePMC fetch failed for %s: %s", direction_key, exc)
                    break
                ok_requests += 1
                self.archive.write_json(self.name, f"{direction_key}_{cursor[:20]}", data)
                results = (data.get("resultList") or {}).get("result") or []
                if not results:
                    break
                for item in results:
                    paper = self._parse(item)
                    if paper is not None:
                        papers.setdefault(paper.canonical_key(), paper)
                next_cursor = data.get("nextCursorMark")
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
        if ok_requests == 0:
            raise RuntimeError("Europe PMC API 不可用：本轮没有任何成功请求")
        logger.info("EuropePMC: %s papers", len(papers))
        return list(papers.values())

    def _parse(self, item: dict) -> Paper | None:
        try:
            source = item.get("source") or ""
            source_id = str(item.get("id") or "")
            title = (item.get("title") or "").strip()
            if not source_id or not title:
                return None
            doi = item.get("doi")
            authors_raw = item.get("authorString") or ""
            authors = [a.strip() for a in authors_raw.replace(";", ",").split(",") if a.strip()]
            published = item.get("firstPublicationDate") or item.get("firstIndexDate")
            if published and len(str(published)) == 10:
                published = str(published) + "T00:00:00Z"
            url = f"https://europepmc.org/article/{source}/{source_id}"
            return Paper(
                source=self.name,
                source_id=f"{source}:{source_id}",
                doi=doi,
                pmid=str(item.get("pmid")) if item.get("pmid") else None,
                title=title,
                abstract=(item.get("abstractText") or "").strip(),
                authors=authors,
                url=url,
                published=published,
                categories=[c for c in (item.get("pubTypeList") or {}).get("pubType", []) if c],
                raw={"journal": item.get("journalTitle"), "source": source},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("EuropePMC parse error: %s", exc)
            return None
