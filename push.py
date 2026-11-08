"""群推送：文本 + 趋势图 + 链接转发 + 各方向合并转发。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

from .directions import DIRECTIONS

logger = logging.getLogger(__name__)


def get_adapter(context) -> AiocqhttpAdapter | None:
    platforms = context.platform_manager.get_insts()
    return next((p for p in platforms if isinstance(p, AiocqhttpAdapter)), None)


def self_uin(adapter: AiocqhttpAdapter) -> str:
    return str(getattr(adapter, "client_self_id", None) or "10000")


async def send_chain(adapter: AiocqhttpAdapter, group: str,
                     chain: MessageChain) -> None:
    await AiocqhttpMessageEvent.send_message(
        bot=adapter.bot,
        message_chain=chain,
        is_group=True,
        session_id=str(group),
    )


def text_chain(text: str) -> MessageChain:
    return MessageChain([Plain(text)])


def image_chain(path: str) -> MessageChain:
    return MessageChain([Image.fromFileSystem(path)])


def link_forward(facts: dict[str, Any], uin: str) -> Nodes:
    nodes = []
    for block in facts.get("directions", []):
        for paper in block.get("papers", []):
            content = [
                Plain(f"{paper['title']}\n{paper.get('url') or ''}"),
            ]
            nodes.append(Node(name=block["name"], uin=uin, content=content))
    return Nodes(nodes)


def direction_forward(block: dict[str, Any], cards: dict[str, str | None],
                      uin: str, reasons: dict[str, str] | None = None,
                      with_cards: bool = True) -> Nodes:
    nodes = []
    for idx, paper in enumerate(block.get("papers", []), start=1):
        reason = (reasons or {}).get(paper["id"]) or paper.get("reason") or ""
        text = (
            f"{idx}. {paper['title']}\n"
            f"{paper.get('source') or ''} · {paper.get('published') or ''}"
            f" · 主题：{paper.get('theme') or '—'}\n"
            f"{reason}\n"
            f"{paper.get('url') or ''}"
        )
        content: list[Any] = [Plain(text)]
        card = cards.get(paper["id"]) if with_cards else None
        if card:
            content.append(Image.fromFileSystem(card))
        nodes.append(Node(name=block["name"], uin=uin, content=content))
    return Nodes(nodes)


async def _send_parts(adapter: AiocqhttpAdapter, groups: list[str],
                      parts: list[tuple[int, MessageChain]], store,
                      kind: str, report_date: str) -> None:
    sent = store.sent_parts(kind, report_date)
    for part_idx, chain in parts:
        if part_idx in sent:
            continue
        for group in groups:
            try:
                await send_chain(adapter, group, chain)
            except Exception as exc:  # noqa: BLE001
                logger.error("send part %s to %s failed: %s", part_idx, group, exc)
                raise
        store.mark_sent(kind, report_date, part_idx)
        await asyncio.sleep(2)


async def push_daily(*, adapter: AiocqhttpAdapter, facts: dict[str, Any],
                     copy: dict[str, Any], daily_image: str | None,
                     cards: dict[str, str | None], groups: list[str],
                     store, report_date: str) -> None:
    uin = self_uin(adapter)
    parts: list[tuple[int, MessageChain]] = [
        (0, text_chain("📚 下面播报论文")),
    ]
    if daily_image:
        parts.append((1, image_chain(daily_image)))
    parts.append((2, MessageChain([link_forward(facts, uin)])))
    parts.append((3, text_chain("下面按顺序发送各方向论文精选")))

    part_idx = 4
    for direction in DIRECTIONS:
        block = next((b for b in facts.get("directions", [])
                      if b.get("key") == direction.key), None)
        if not block or not block.get("papers"):
            continue
        parts.append((part_idx, MessageChain([
            direction_forward(block, cards, uin)
        ])))
        part_idx += 1

    parts.append((part_idx, text_chain("今日推送结束，有详细解读可以和我交流。")))
    await _send_parts(adapter, groups, parts, store, "daily", report_date)


async def push_weekly(*, adapter: AiocqhttpAdapter, facts: dict[str, Any],
                      copy: dict[str, Any], weekly_image: str | None,
                      cards: dict[str, str | None], groups: list[str],
                      store, report_date: str) -> None:
    uin = self_uin(adapter)
    reasons: dict[str, str] = {}
    for direction in copy.get("directions", []):
        for paper in direction.get("papers", []):
            if paper.get("paper_id"):
                reasons[paper["paper_id"]] = paper.get("reason") or ""

    parts: list[tuple[int, MessageChain]] = [
        (0, text_chain("📚 本周论文周报")),
    ]
    if weekly_image:
        parts.append((1, image_chain(weekly_image)))
    parts.append((2, MessageChain([link_forward(facts, uin)])))
    parts.append((3, text_chain("下面按顺序发送各方向周报精选")))

    part_idx = 4
    for direction in DIRECTIONS:
        block = next((b for b in facts.get("directions", [])
                      if b.get("key") == direction.key), None)
        if not block or not block.get("papers"):
            continue
        parts.append((part_idx, MessageChain([
            direction_forward(block, cards, uin, reasons=reasons)
        ])))
        part_idx += 1

    parts.append((part_idx, text_chain("本周周报结束，有想深入讨论的论文可以直接问我。")))
    await _send_parts(adapter, groups, parts, store, "weekly", report_date)
