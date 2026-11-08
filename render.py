"""图片渲染：复用 AstrBot 的 t2i 自定义 HTML 模板接口。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent


class Renderer:
    def __init__(self, star, archive, config: dict[str, Any]) -> None:
        self.star = star
        self.archive = archive
        self.config = config
        self.images_root = Path(archive.root) / "images"

    @staticmethod
    def _valid_image(path: str) -> bool:
        try:
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                return im.width >= 50 and im.height >= 50
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _peek(path: str) -> str:
        try:
            with open(path, "rb") as f:
                return f.read(200).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return ""

    async def _render_html(self, template_name: str, data: dict[str, Any],
                           subdir: str, filename: str) -> str | None:
        template = (PLUGIN_DIR / "templates" / template_name).read_text(encoding="utf-8")
        last_error: Exception | None = None
        for attempt in range(3):
            tmp_path = None
            try:
                tmp_path = await self.star.html_render(
                    template, data, return_url=False, options={"quality": 90}
                )
                if not tmp_path:
                    raise RuntimeError("empty render path")
                if not self._valid_image(tmp_path):
                    # t2i 端点会以 HTTP 200 返回 {"code":1,"message":"browser recycling..."}
                    raise RuntimeError(f"endpoint returned non-image: {self._peek(tmp_path)}")
                dest = self.images_root / subdir / filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tmp_path, dest)
                return str(dest)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("render %s attempt %s/3 failed: %s",
                               template_name, attempt + 1, exc)
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(2 * (attempt + 1))
        logger.error("render %s failed after retries: %s", template_name, last_error)
        return None

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
            if self._valid_image(str(dest)):
                return str(dest)
            try:
                dest.unlink()
            except Exception:  # noqa: BLE001
                pass
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
