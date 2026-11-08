"""Source adapter 公共设施：Paper 模型、限速、HTTP、原始响应归档。"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

from ..textutils import (  # noqa: F401  (re-exported for adapters)
    iso,
    normalize_title,
    parse_datetime,
    title_fingerprint,
    utcnow,
)

logger = logging.getLogger(__name__)

PLUGIN_UA = "astrbot_plugin_paper_digest/0.1 (+https://github.com/vacuoleC/astrbot_plugin_paper_digest)"


@dataclass
class Paper:
    source: str
    source_id: str
    title: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    url: str = ""
    published: str | None = None  # ISO datetime/date string
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    categories: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def first_author(self) -> str:
        return self.authors[0] if self.authors else ""

    def canonical_key(self) -> str:
        if self.doi:
            return "doi:" + self.doi.strip().lower()
        if self.arxiv_id:
            base = re.sub(r"v\d+$", "", self.arxiv_id.strip(), flags=re.IGNORECASE)
            return "arxiv:" + base.lower()
        if self.pmid:
            return "pmid:" + str(self.pmid).strip()
        return "title:" + title_fingerprint(self.title, self.first_author())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_id": self.source_id,
            "title": self.title,
            "abstract": self.abstract,
            "authors": self.authors,
            "url": self.url,
            "published": self.published,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "pmid": self.pmid,
            "categories": self.categories,
        }


class RateLimiter:
    """串行限速器；同一个 host 的请求按最小间隔排队。"""

    def __init__(self, interval_seconds: float) -> None:
        self.interval = float(interval_seconds)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at = time.monotonic() + self.interval


class RateLimitError(RuntimeError):
    """上游明确返回 429，调用方应停止该源的连续请求。"""


class HttpClient:
    def __init__(self, *, timeout: float = 60, contact_email: str = "",
                 ssl_verify: bool = True) -> None:
        self.timeout = aiohttp.ClientTimeout(total=timeout, connect=30, sock_read=timeout)
        self.ssl_verify = ssl_verify
        self.session: aiohttp.ClientSession | None = None
        self.headers = {"User-Agent": PLUGIN_UA}
        if contact_email:
            self.headers["From"] = contact_email
            self.headers["User-Agent"] = f"{PLUGIN_UA} mailto:{contact_email}"

    async def __aenter__(self) -> "HttpClient":
        self.session = aiohttp.ClientSession(
            timeout=self.timeout,
            headers=self.headers,
            trust_env=True,
            connector=aiohttp.TCPConnector(ssl=self.ssl_verify),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def get_text(self, url: str, *, params: dict | None = None,
                       headers: dict | None = None, retries: int = 3,
                       timeout: float | None = None) -> str:
        assert self.session is not None
        last: Exception | None = None
        for attempt in range(retries):
            try:
                async with self.session.get(url, params=params, headers=headers,
                                            timeout=timeout) as resp:
                    if resp.status == 429:
                        raise RateLimitError(f"HTTP 429: {url}")
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        raise RuntimeError(f"HTTP {resp.status}: {body}")
                    return await resp.text()
            except RateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"GET {url} failed after {retries} attempts: {last}")

    async def get_json(self, url: str, *, params: dict | None = None,
                       headers: dict | None = None, retries: int = 3,
                       timeout: float | None = None) -> Any:
        text = await self.get_text(url, params=params, headers=headers,
                                   retries=retries, timeout=timeout)
        return json.loads(text)


class Archive:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_json(self, source: str, tag: str, obj: Any) -> Path:
        stamp = utcnow().strftime("%Y%m%d_%H%M%S_%f")
        day = utcnow().strftime("%Y%m%d")
        path = self._path("raw", source, day, f"{stamp}_{tag}.json.gz")
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return path

    def write_json_plain(self, subdir: str, name: str, obj: Any) -> Path:
        path = self._path(subdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return path

    def write_text(self, subdir: str, name: str, text: str) -> Path:
        path = self._path(subdir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path


class SourceAdapter:
    name = "base"
    ssl_verify = True
    # 每次抓取固定回看的自然日数；不依赖上次成功时间，避免源日期滞后导致漏抓
    fetch_lookback_days = 7

    def __init__(self, *, archive: Archive, contact_email: str = "",
                 timeout: float = 60, debug: bool = False) -> None:
        self.archive = archive
        self.contact_email = contact_email
        self.timeout = timeout
        self.debug = debug

    async def fetch(self, client: HttpClient, window_start: datetime,
                    window_end: datetime) -> list[Paper]:
        raise NotImplementedError

    async def backfill(self, client: HttpClient, start_date: date,
                       end_date: date) -> list[Paper]:
        start = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        return await self.fetch(client, start, end)
