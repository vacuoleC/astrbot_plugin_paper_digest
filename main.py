"""AstrBot 论文速递插件：每日趋势日报 + 每周影响力周报。"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import StarTools

from .directions import DIRECTIONS, DIRECTION_BY_KEY
from .llm import provider_name, resolve_provider
from .pipeline import (
    allocate_cap,
    build_daily_facts,
    build_weekly_facts,
    classify_candidates,
    compute_baseline,
    prefilter,
    select_direction,
    select_weekly,
)
from .push import get_adapter, push_daily, push_weekly
from .render import Renderer
from .skills import run_skill
from .sources import (
    Archive,
    ArxivAdapter,
    BiorxivAdapter,
    CrossrefAdapter,
    EuropePmcAdapter,
    HttpClient,
    utcnow,
)
from .store import Store
from .textutils import parse_datetime

DEFAULTS: dict[str, Any] = {
    "target_groups": [],
    "test_groups": [],
    "test_group": "",
    "test_mode": True,
    "enable_push": False,
    "dry_run": True,
    "push_time": "09:30",
    "weekly_push_time": "09:30",
    "hard_wall_time": "10:00",
    "timezone": "Asia/Shanghai",
    "contact_email": "",
    "provider_classify": "",
    "provider_summary": "",
    "provider_weekly": "",
    "daily_top_per_direction": 5,
    "daily_cap_factor": 4,
    "weekly_per_direction": 3,
    "max_themes_per_direction": 5,
    "prefilter_per_direction": 200,
    "llm_batch_size": 25,
    "llm_concurrency": 4,
    "arxiv_request_interval": 3,
    "http_timeout": 60,
    "overlap_hours": 24,
    "archive_dir": "/AstrBot/paperdaily",
    "retain_days": 180,
    "debug": False,
}


class Main(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        raw = config or {}
        try:
            raw = dict(raw)
        except Exception:  # noqa: BLE001
            raw = {}
        self.config = {**DEFAULTS, **raw}
        self.tz = ZoneInfo(str(self.config.get("timezone") or "Asia/Shanghai"))

        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_paper_digest"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        archive_dir = Path(str(self.config.get("archive_dir") or "/AstrBot/paperdaily"))
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            archive_dir = self.data_dir / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
        self.archive = Archive(archive_dir)
        self.store = Store(self.data_dir / "paper_digest.db")
        self.prompt_dir = Path(__file__).resolve().parent / "prompts"
        self.renderer = Renderer(self, self.archive, self.config)

        self.scheduler = AsyncIOScheduler(timezone=self.tz)
        self._started = False

    # ---------- lifecycle ----------

    async def initialize(self) -> None:
        self._setup_jobs()
        if not self.scheduler.running:
            self.scheduler.start()
        self._started = True
        logger.info("论文速递插件已启动：push_time=%s weekly=%s archive=%s",
                    self.config.get("push_time"), self.config.get("weekly_push_time"),
                    self.archive.root)
        self._maybe_catch_up()

    async def terminate(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self._started = False

    def _setup_jobs(self) -> None:
        early_hour, early_minute = 7, 30
        daily_hour, daily_minute = self._lead_time(self.config.get("push_time"), 25)
        weekly_hour, weekly_minute = self._lead_time(self.config.get("weekly_push_time"), 25)
        self.scheduler.add_job(
            self._job_ingest_early, CronTrigger(hour=early_hour, minute=early_minute,
                                                timezone=self.tz),
            id="paper_ingest_early", replace_existing=True,
        )
        self.scheduler.add_job(
            self._job_daily, CronTrigger(day_of_week="mon-fri", hour=daily_hour,
                                         minute=daily_minute, timezone=self.tz),
            id="paper_daily", replace_existing=True,
        )
        self.scheduler.add_job(
            self._job_weekly, CronTrigger(day_of_week="sat", hour=weekly_hour,
                                          minute=weekly_minute, timezone=self.tz),
            id="paper_weekly", replace_existing=True,
        )

    @staticmethod
    def _lead_time(hhmm: str | None, lead_minutes: int) -> tuple[int, int]:
        try:
            hour, minute = map(int, str(hhmm or "09:30").split(":"))
        except Exception:  # noqa: BLE001
            hour, minute = 9, 30
        dt = datetime(2000, 1, 1, hour, minute) - timedelta(minutes=lead_minutes)
        return dt.hour, dt.minute

    def _maybe_catch_up(self) -> None:
        now = datetime.now(self.tz)
        kind = None
        if now.weekday() < 5:
            kind = "daily"
            push_hour, push_minute = self._parse_hhmm(self.config.get("push_time"))
        elif now.weekday() == 5:
            kind = "weekly"
            push_hour, push_minute = self._parse_hhmm(self.config.get("weekly_push_time"))
        if kind is None:
            return
        push_at = now.replace(hour=push_hour, minute=push_minute, second=0, microsecond=0)
        wall_hour, wall_minute = self._parse_hhmm(self.config.get("hard_wall_time"))
        wall_at = now.replace(hour=wall_hour, minute=wall_minute, second=0, microsecond=0)
        if not (push_at <= now <= wall_at + timedelta(hours=2)):
            return
        report_date = now.strftime("%Y-%m-%d")
        report = self.store.get_report(kind, report_date)
        if report and report.get("status") == "sent":
            return
        logger.info("检测到错过 %s 推送，启动补跑", kind)
        asyncio.create_task(self._job_daily() if kind == "daily" else self._job_weekly())

    @staticmethod
    def _parse_hhmm(value: str | None) -> tuple[int, int]:
        try:
            hour, minute = map(int, str(value or "09:30").split(":"))
            return hour, minute
        except Exception:  # noqa: BLE001
            return 9, 30

    async def _job_ingest_early(self) -> None:
        try:
            await self.ingest(include_arxiv=False)
        except Exception as exc:  # noqa: BLE001
            logger.error("早间抓取失败: %s", exc, exc_info=True)

    async def _job_daily(self) -> None:
        try:
            await self.run_daily()
        except Exception as exc:  # noqa: BLE001
            logger.error("日报任务失败: %s", exc, exc_info=True)

    async def _job_weekly(self) -> None:
        try:
            await self.run_weekly()
        except Exception as exc:  # noqa: BLE001
            logger.error("周报任务失败: %s", exc, exc_info=True)

    # ---------- ingest ----------

    def _adapters(self, include_arxiv: bool) -> list:
        common = dict(archive=self.archive,
                      contact_email=str(self.config.get("contact_email") or ""),
                      timeout=float(self.config.get("http_timeout") or 60),
                      debug=bool(self.config.get("debug")))
        adapters: list = []
        if include_arxiv:
            categories: list[str] = []
            for d in DIRECTIONS:
                for cat in d.arxiv_categories:
                    if cat not in categories:
                        categories.append(cat)
            adapters.append(ArxivAdapter(
                categories=categories,
                request_interval=float(self.config.get("arxiv_request_interval") or 3),
                **common,
            ))
        adapters.append(BiorxivAdapter(servers=("biorxiv", "medrxiv"), **common))
        terms = {d.key: d.europepmc_terms for d in DIRECTIONS if d.europepmc_terms}
        if terms:
            adapters.append(EuropePmcAdapter(direction_terms=terms, **common))
        filters = {d.key: d.crossref_filters for d in DIRECTIONS if d.crossref_filters}
        if filters:
            adapters.append(CrossrefAdapter(direction_filters=filters, **common))
        return adapters

    async def ingest(self, *, include_arxiv: bool = True,
                     skip_recent_others: bool = True) -> dict[str, int]:
        now = utcnow()
        overlap = timedelta(hours=int(self.config.get("overlap_hours") or 24))
        counts: dict[str, int] = {}
        for adapter in self._adapters(include_arxiv):
            state = self.store.get_fetch_state(adapter.name)
            last = parse_datetime(state["last_success_at"]) if state else None
            if adapter.name != "arxiv" and skip_recent_others and last:
                if now - last < timedelta(hours=12):
                    logger.info("%s 最近 12h 已抓取，跳过", adapter.name)
                    continue
            start = (last - overlap) if last else (now - timedelta(days=7))
            start = max(start, now - timedelta(days=7))
            try:
                async with HttpClient(
                    timeout=float(self.config.get("http_timeout") or 60),
                    contact_email=str(self.config.get("contact_email") or ""),
                    ssl_verify=getattr(adapter, "ssl_verify", True),
                ) as client:
                    papers = await adapter.fetch(client, start, now)
            except Exception as exc:  # noqa: BLE001
                logger.error("%s 抓取失败: %s", adapter.name, exc)
                continue
            inserted = 0
            for paper in papers:
                try:
                    self.store.upsert_paper(paper)
                    inserted += 1
                except Exception as exc:  # noqa: BLE001
                    logger.debug("upsert paper failed: %s", exc)
            counts[adapter.name] = inserted
            self.store.save_fetch_state(adapter.name, "", now.isoformat())
            logger.info("%s 抓取完成：%s 篇", adapter.name, inserted)
        return counts

    # ---------- daily ----------

    async def run_daily(self, *, force: bool = False, push: bool | None = None) -> dict[str, Any]:
        now = datetime.now(self.tz)
        if now.weekday() >= 5 and not force:
            await self.ingest(include_arxiv=True)
            return {"status": "weekend-ingest-only"}
        report_date = now.strftime("%Y-%m-%d")
        existing = self.store.get_report("daily", report_date)
        if not force and existing and existing.get("status") == "sent":
            return {"status": "already-sent", "date": report_date}

        await self.ingest(include_arxiv=True)
        hours = 72 if now.weekday() == 0 else 26
        window_start = now - timedelta(hours=hours)
        rows = self.store.papers_first_seen_between(
            window_start.astimezone(timezone.utc).isoformat(),
            now.astimezone(timezone.utc).isoformat(),
        )
        if not rows:
            logger.warning("日报窗口内没有新论文")
            return {"status": "no-papers", "date": report_date}

        facts, copy, image, cards = await self.build_daily(
            report_date, rows, window_start, now
        )
        result: dict[str, Any] = {
            "status": "built", "date": report_date,
            "selected": facts["stats"]["selected_total"],
            "image": image,
        }
        targets = self._push_targets(push)
        if not targets:
            result["status"] = "built-dry-run"
            return result

        await self._wait_until_push_time()
        if datetime.now(self.tz) > self._time_today(self.config.get("hard_wall_time")):
            logger.warning("超过硬截止时间，发送降级版")
            image, cards = None, {}

        adapter = get_adapter(self.context)
        if adapter is None:
            logger.error("未找到 aiocqhttp 适配器，无法推送")
            result["status"] = "no-adapter"
            return result
        await push_daily(adapter=adapter, facts=facts, copy=copy, daily_image=image,
                         cards=cards, groups=targets, store=self.store,
                         report_date=report_date)
        self.store.save_report("daily", report_date, facts, copy=copy, status="sent")
        result.update({"status": "sent", "groups": targets})
        return result

    async def build_daily(self, report_date: str, rows: list[dict[str, Any]],
                          window_start: datetime, window_end: datetime
                          ) -> tuple[dict[str, Any], dict[str, Any], str | None,
                                     dict[str, str | None]]:
        candidates, _ = prefilter(
            rows, per_direction=int(self.config.get("prefilter_per_direction") or 200)
        )
        classifications = await classify_candidates(
            self.context, candidates, self.config, self.store
        )
        overrides = self.store.get_overrides()
        selected: dict[str, list[dict[str, Any]]] = {}
        for d in DIRECTIONS:
            selected[d.key] = select_direction(
                rows, classifications, overrides, direction_key=d.key,
                top_n=int(self.config.get("daily_top_per_direction") or 5),
                max_themes=int(self.config.get("max_themes_per_direction") or 5),
            )
        cap = int(self.config.get("daily_cap_factor") or 4) * len(DIRECTIONS)
        selected, dropped = allocate_cap(selected, cap)
        if dropped:
            logger.info("超过 4n 上限，退掉 %s 篇", len(dropped))

        totals = Counter(cls.get("primary") for cls in classifications.values())
        source_stats = Counter(row.get("source") or "unknown" for row in rows)
        previous_rows = self.store.papers_first_seen_between(
            (window_start - timedelta(days=7)).astimezone(timezone.utc).isoformat(),
            window_start.astimezone(timezone.utc).isoformat(),
        )
        baseline = compute_baseline(rows, previous_rows)
        facts = build_daily_facts(
            report_date=report_date,
            window_start=window_start.astimezone(timezone.utc).isoformat(),
            window_end=window_end.astimezone(timezone.utc).isoformat(),
            selected=selected, totals=dict(totals),
            source_stats=dict(source_stats), baseline=baseline,
        )
        for d in DIRECTIONS:
            self.store.save_selection(
                report_date, d.key,
                [item["pk"] for item in selected.get(d.key, [])],
                [item.get("theme") for item in selected.get(d.key, [])],
            )
        self.store.save_report("daily", report_date, facts, copy=None, status="built")
        copy = await run_skill(
            kind="daily", facts=facts, prompt_dir=self.prompt_dir,
            context=self.context, config=self.config, store=self.store,
            archive=self.archive,
        )
        self.store.save_report("daily", report_date, facts, copy=copy, status="built")
        self.archive.write_json_plain(
            "reports", f"daily_{report_date}.json", {"facts": facts, "copy": copy}
        )
        image = await self.renderer.render_daily(facts, copy)
        cards = await self.renderer.render_cards(facts)
        return facts, copy, image, cards

    # ---------- weekly ----------

    async def run_weekly(self, *, force: bool = False, push: bool | None = None
                         ) -> dict[str, Any]:
        now = datetime.now(self.tz)
        report_date = now.strftime("%Y-%m-%d")
        existing = self.store.get_report("weekly", report_date)
        if not force and existing and existing.get("status") == "sent":
            return {"status": "already-sent", "date": report_date}
        await self.ingest(include_arxiv=True)
        window_start = now - timedelta(days=7)
        rows = self.store.papers_first_seen_between(
            window_start.astimezone(timezone.utc).isoformat(),
            now.astimezone(timezone.utc).isoformat(),
        )
        if not rows:
            return {"status": "no-papers", "date": report_date}
        facts, copy, image, cards = await self.build_weekly(
            report_date, rows, window_start, now
        )
        result: dict[str, Any] = {
            "status": "built", "date": report_date,
            "selected": facts["stats"]["selected_total"], "image": image,
        }
        targets = self._push_targets(push)
        if not targets:
            result["status"] = "built-dry-run"
            return result
        await self._wait_until_push_time(weekly=True)
        if datetime.now(self.tz) > self._time_today(self.config.get("hard_wall_time")):
            image, cards = None, {}
        adapter = get_adapter(self.context)
        if adapter is None:
            result["status"] = "no-adapter"
            return result
        await push_weekly(adapter=adapter, facts=facts, copy=copy, weekly_image=image,
                          cards=cards, groups=targets, store=self.store,
                          report_date=report_date)
        self.store.save_report("weekly", report_date, facts, copy=copy, status="sent")
        result.update({"status": "sent", "groups": targets})
        return result

    async def build_weekly(self, report_date: str, rows: list[dict[str, Any]],
                           window_start: datetime, window_end: datetime
                           ) -> tuple[dict[str, Any], dict[str, Any], str | None,
                                      dict[str, str | None]]:
        candidates, _ = prefilter(
            rows, per_direction=int(self.config.get("prefilter_per_direction") or 200)
        )
        classifications = await classify_candidates(
            self.context, candidates, self.config, self.store
        )
        overrides = self.store.get_overrides()
        selected = select_weekly(
            rows, classifications, overrides,
            per_direction=int(self.config.get("weekly_per_direction") or 3),
        )
        totals = Counter(cls.get("primary") for cls in classifications.values())
        source_stats = Counter(row.get("source") or "unknown" for row in rows)

        daily_summaries = []
        for offset in range(7):
            day = (window_end - timedelta(days=offset)).strftime("%Y-%m-%d")
            report = self.store.get_report("daily", day)
            if report and report.get("copy"):
                copy_day = report["copy"]
                daily_summaries.append({
                    "date": day,
                    "headline": copy_day.get("headline", ""),
                    "summary": copy_day.get("summary", ""),
                    "directions": copy_day.get("directions", []),
                })

        facts = build_weekly_facts(
            report_date=report_date,
            week_start=window_start.astimezone(timezone.utc).isoformat(),
            week_end=window_end.astimezone(timezone.utc).isoformat(),
            selected=selected, totals=dict(totals),
            source_stats=dict(source_stats), daily_summaries=daily_summaries,
            baseline={},
        )
        self.store.save_report("weekly", report_date, facts, copy=None, status="built")
        copy = await run_skill(
            kind="weekly", facts=facts, prompt_dir=self.prompt_dir,
            context=self.context, config=self.config, store=self.store,
            archive=self.archive,
        )
        self.store.save_report("weekly", report_date, facts, copy=copy, status="built")
        self.archive.write_json_plain(
            "reports", f"weekly_{report_date}.json", {"facts": facts, "copy": copy}
        )
        image = await self.renderer.render_weekly(facts, copy)
        cards = await self.renderer.render_cards(facts)
        return facts, copy, image, cards

    # ---------- push helpers ----------

    def _push_targets(self, override: bool | None) -> list[str]:
        if override is not None:
            enabled = override
        else:
            enabled = bool(self.config.get("enable_push")) and not bool(
                self.config.get("dry_run")
            )
        if not enabled:
            return []
        if bool(self.config.get("test_mode")):
            groups = self._parse_groups(self.config.get("test_groups"))
            if not groups:
                groups = self._parse_groups(self.config.get("test_group"))
        else:
            groups = self._parse_groups(self.config.get("target_groups"))
        return groups

    @staticmethod
    def _parse_groups(value) -> list[str]:
        """兼容 WebUI 的 list 类型和旧的逗号分隔字符串。"""
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            items = [str(v).strip() for v in value]
        else:
            items = [g.strip() for g in re.split(r"[,，]", str(value))]
        return [g for g in items if g]

    async def _wait_until_push_time(self, weekly: bool = False) -> None:
        key = "weekly_push_time" if weekly else "push_time"
        hour, minute = self._parse_hhmm(self.config.get(key))
        now = datetime.now(self.tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < target:
            logger.info("等待推送时间 %02d:%02d", hour, minute)
            await asyncio.sleep((target - now).total_seconds())

    def _time_today(self, hhmm: str | None) -> datetime:
        hour, minute = self._parse_hhmm(hhmm)
        return datetime.now(self.tz).replace(hour=hour, minute=minute,
                                             second=0, microsecond=0)

    # ---------- commands ----------

    @filter.command("paper_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看论文速递运行状态：24 小时入库量、各数据源最后成功时间、日报/周报状态与默认模型。"""
        now = datetime.now(self.tz)
        since = (now - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
        stats = self.store.stats(since)
        lines = [f"论文速递状态 · {now:%Y-%m-%d %H:%M}",
                 f"24h 入库：{stats['total']} 篇（{stats['by_source']}）"]
        for source in ("arxiv", "biorxiv", "europepmc", "crossref"):
            state = self.store.get_fetch_state(source)
            lines.append(f"{source} 最后成功：{state['last_success_at'] if state else '从未'}")
        report = self.store.get_report("daily", now.strftime("%Y-%m-%d"))
        lines.append("今日日报：" + ("已生成" if report else "未生成"))
        lines.append("正式群：" + (", ".join(self._parse_groups(self.config.get("target_groups"))) or "未配置"))
        lines.append("测试群：" + (", ".join(self._parse_groups(self.config.get("test_groups"))) or "未配置"))
        for kind in ("daily", "weekly"):
            latest = self.store.latest_report(kind)
            if latest:
                lines.append(f"最近 {kind}：{latest['report_date']}")
        default = resolve_provider(self.context, "")
        lines.append(f"默认模型：{provider_name(default)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("paper_models")
    async def cmd_models(self, event: AstrMessageEvent):
        """列出 AstrBot 已配置的 provider，便于在插件配置里选择分类/日报/周报模型。"""
        lines = ["可用 provider："]
        try:
            providers = self.context.get_all_providers()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"获取 provider 失败：{exc}")
            return
        default = resolve_provider(self.context, "")
        default_name = provider_name(default)
        for provider in providers:
            name = provider_name(provider)
            mark = " (默认)" if name == default_name else ""
            lines.append(f"- {name}{mark}")
        lines.append("在 WebUI 插件配置里填写 provider_classify / provider_summary / provider_weekly。")
        yield event.plain_result("\n".join(lines))

    @filter.command("paper_run_ingest")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_ingest(self, event: AstrMessageEvent):
        """立即抓取所有论文源（arXiv、bioRxiv/medRxiv、Europe PMC、Crossref），用于手动补数据。"""
        counts = await self.ingest(include_arxiv=True, skip_recent_others=False)
        yield event.plain_result(f"抓取完成：{json.dumps(counts, ensure_ascii=False)}")

    @filter.command("paper_build_daily")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_build_daily(self, event: AstrMessageEvent):
        """立即生成当日日报（分类、选片、文案、趋势图和摘要卡），不推送，用于预览效果。"""
        now = datetime.now(self.tz)
        window_start = now - timedelta(hours=26)
        rows = self.store.papers_first_seen_between(
            window_start.astimezone(timezone.utc).isoformat(),
            now.astimezone(timezone.utc).isoformat(),
        )
        if not rows:
            yield event.plain_result("窗口内没有论文，先运行 /paper_run_ingest。")
            return
        report_date = now.strftime("%Y-%m-%d")
        facts, copy, image, cards = await self.build_daily(
            report_date, rows, window_start, now
        )
        yield event.plain_result(
            f"日报已生成（未推送）：入选 {facts['stats']['selected_total']} 篇，"
            f"图片 {image or '渲染失败'}，卡片 {sum(1 for v in cards.values() if v)} 张。"
        )

    @filter.command("paper_skill_test")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_skill_test(self, event: AstrMessageEvent, kind: str = "daily"):
        """用最近一次 facts 试跑 daily/weekly 的 SKILL.md，返回模型原始 JSON 输出，便于调文案。"""
        kind = (kind or "daily").strip()
        if kind not in ("daily", "weekly"):
            yield event.plain_result("用法：/paper_skill_test daily|weekly")
            return
        report = self.store.latest_report(kind)
        if not report:
            yield event.plain_result(f"还没有 {kind} 报告，先运行 /paper_build_daily。")
            return
        facts = report["facts"]
        copy = await run_skill(
            kind=kind, facts=facts, prompt_dir=self.prompt_dir,
            context=self.context, config=self.config, store=self.store,
            archive=self.archive,
        )
        yield event.plain_result(
            json.dumps(copy, ensure_ascii=False)[:1800]
        )

    @filter.command("paper_pin")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_pin(self, event: AstrMessageEvent, paper: str = ""):
        """置顶某篇论文（参数为 p123 或 123），选片时优先保留。"""
        pk = self._parse_paper_pk(paper)
        if pk is None:
            yield event.plain_result("用法：/paper_pin p123")
            return
        self.store.set_override(pk, "pin")
        yield event.plain_result(f"已置顶 p{pk}")

    @filter.command("paper_skip")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_skip(self, event: AstrMessageEvent, paper: str = ""):
        """从选片中排除某篇论文（参数为 p123 或 123）。"""
        pk = self._parse_paper_pk(paper)
        if pk is None:
            yield event.plain_result("用法：/paper_skip p123")
            return
        self.store.set_override(pk, "skip")
        yield event.plain_result(f"已排除 p{pk}")

    @staticmethod
    def _parse_paper_pk(value: str) -> int | None:
        match = re.search(r"(\d+)", value or "")
        return int(match.group(1)) if match else None
