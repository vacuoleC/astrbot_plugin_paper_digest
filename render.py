"""图片渲染：复用 AstrBot 的 t2i 自定义 HTML 模板接口。"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent


class Renderer:
    def __init__(self, star, archive, config: dict[str, Any]) -> None:
        self.star = star
        self.archive = archive
        self.config = config
        self.images_root = Path(archive.root) / "images"

    async def _render_html(self, template_name: str, data: dict[str, Any],
                           subdir: str, filename: str) -> str | None:
        template = (PLUGIN_DIR / "templates" / template_name).read_text(encoding="utf-8")
        try:
            tmp_path = await self.star.html_render(
                template, data, return_url=False, options={"quality": 90}
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("render %s failed: %s", template_name, exc)
            return None
        if not tmp_path:
            logger.error("render %s returned empty path", template_name)
            return None
        dest = self.images_root / subdir / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(tmp_path, dest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("copy rendered image failed (%s -> %s): %s", tmp_path, dest, exc)
            return str(tmp_path)
        return str(dest)

    @staticmethod
    def _generated_at() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    async def render_daily(self, facts: dict[str, Any], copy: dict[str, Any]) -> str | None:
        return await self._render_html(
            "daily.html",
            {"facts": facts, "copy": copy, "generated_at": self._generated_at()},
            "reports",
            f"daily_{facts.get('date', 'unknown')}.jpg",
        )

    async def render_weekly(self, facts: dict[str, Any], copy: dict[str, Any]) -> str | None:
        return await self._render_html(
            "weekly.html",
            {"facts": facts, "copy": copy, "generated_at": self._generated_at()},
            "reports",
            f"weekly_{facts.get('date', 'unknown')}.jpg",
        )

    async def render_card(self, paper: dict[str, Any], direction_name: str) -> str | None:
        paper_id = str(paper.get("id") or "")
        if not paper_id:
            return None
        dest = self.images_root / "cards" / f"{paper_id}.jpg"
        if dest.exists():
            return str(dest)
        return await self._render_html(
            "card.html",
            {"paper": paper, "direction_name": direction_name},
            "cards",
            f"{paper_id}.jpg",
        )

    async def render_cards(self, facts: dict[str, Any], concurrency: int = 4
                           ) -> dict[str, str | None]:
        """渲染所有入选论文的摘要卡，返回 paper_id -> path。"""
        by_id = facts.get("papers") or {}
        direction_of: dict[str, str] = {}
        for block in facts.get("directions", []):
            for paper in block.get("papers", []):
                direction_of[paper["id"]] = block["name"]

        sem = asyncio.Semaphore(concurrency)
        results: dict[str, str | None] = {}

        async def one(pid: str) -> None:
            async with sem:
                results[pid] = await self.render_card(
                    by_id[pid], direction_of.get(pid, "")
                )

        await asyncio.gather(*(one(pid) for pid in by_id))
        return results
