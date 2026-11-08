#!/usr/bin/env python3
"""按日期区间回填论文数据（在服务器容器内运行）。

示例：
python scripts/backfill.py --start 2026-11-01 --end 2026-11-09
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name
mod_sources = importlib.import_module(f"{PKG}.sources")
mod_store = importlib.import_module(f"{PKG}.store")
mod_directions = importlib.import_module(f"{PKG}.directions")

Archive = mod_sources.Archive
HttpClient = mod_sources.HttpClient
ArxivAdapter = mod_sources.ArxivAdapter
BiorxivAdapter = mod_sources.BiorxivAdapter
EuropePmcAdapter = mod_sources.EuropePmcAdapter
CrossrefAdapter = mod_sources.CrossrefAdapter
Store = mod_store.Store
DIRECTIONS = mod_directions.DIRECTIONS


def daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


async def run(args) -> None:
    archive = Archive(args.archive)
    store = Store(args.db)
    common = dict(archive=archive, contact_email=args.email,
                  timeout=args.timeout, debug=False)
    categories: list[str] = []
    for d in DIRECTIONS:
        for cat in d.arxiv_categories:
            if cat not in categories:
                categories.append(cat)
    adapters = []
    if "arxiv" in args.sources:
        adapters.append(ArxivAdapter(categories=categories, request_interval=3, **common))
    if "biorxiv" in args.sources:
        adapters.append(BiorxivAdapter(servers=("biorxiv", "medrxiv"), **common))
    if "europepmc" in args.sources:
        terms = {d.key: d.europepmc_terms for d in DIRECTIONS if d.europepmc_terms}
        adapters.append(EuropePmcAdapter(direction_terms=terms, **common))
    if "crossref" in args.sources:
        filters = {d.key: d.crossref_filters for d in DIRECTIONS if d.crossref_filters}
        adapters.append(CrossrefAdapter(direction_filters=filters, **common))

    for day in daterange(args.start, args.end):
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        print(f"== {day} ==")
        for adapter in adapters:
            try:
                async with HttpClient(timeout=args.timeout, contact_email=args.email,
                                      ssl_verify=getattr(adapter, "ssl_verify", True)) as client:
                    papers = await adapter.fetch(client, start, end)
            except Exception as exc:  # noqa: BLE001
                print(f"  {adapter.name}: FAILED {exc}")
                continue
            n = 0
            for paper in papers:
                store.upsert_paper(paper)
                n += 1
            print(f"  {adapter.name}: {n} papers")
        store.save_fetch_state("backfill", "", end.isoformat())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--sources", default="arxiv,biorxiv,europepmc,crossref")
    parser.add_argument("--db", default="/AstrBot/data/plugin_data/"
                                       "astrbot_plugin_paper_digest/paper_digest.db")
    parser.add_argument("--archive", default="/AstrBot/paperdaily")
    parser.add_argument("--email", default="")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    args.start = date.fromisoformat(args.start)
    args.end = date.fromisoformat(args.end)
    args.sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
