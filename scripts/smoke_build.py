#!/usr/bin/env python3
"""离屏冒烟：不启动 AstrBot、不调用 LLM，跑通数据到图片的链路。

在容器内运行：
python scripts/smoke_build.py
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

directions_mod = importlib.import_module(f"{PKG}.directions")
pipeline_mod = importlib.import_module(f"{PKG}.pipeline")
skills_mod = importlib.import_module(f"{PKG}.skills")
render_mod = importlib.import_module(f"{PKG}.render")
store_mod = importlib.import_module(f"{PKG}.store")
sources_mod = importlib.import_module(f"{PKG}.sources")


class FakeContext:
    def get_provider_by_id(self, _):
        return None

    def get_using_provider(self):
        return None


class FakeStar:
    def __init__(self):
        from astrbot.core.utils.t2i.renderer import HtmlRenderer
        self.renderer = HtmlRenderer()

    async def html_render(self, tmpl, data, return_url=False, options=None):
        if not getattr(self.renderer, "endpoints", None):
            await self.renderer.initialize()
        return await self.renderer.render_custom_template(
            tmpl, data, return_url=return_url, options=options or {"quality": 90}
        )


def fake_rows() -> list[dict]:
    now = datetime.now(timezone.utc)
    specs = [
        ("computational_biology", "Single-cell foundation model for transcriptome integration", "q-bio.GN"),
        ("computational_biology", "A benchmark for protein structure prediction", "q-bio.BM"),
        ("computational_biology", "Phylogenetic inference with large language models", "q-bio.PE"),
        ("computer_science", "Retrieval-augmented agents for scientific discovery", "cs.AI"),
        ("computer_science", "Scaling laws for multimodal transformers", "cs.LG"),
        ("astrophysics", "Galaxy formation in the early universe", "astro-ph.GA"),
        ("astrophysics", "Exoplanet atmospheric retrieval with JWST", "astro-ph.EP"),
        ("theoretical_physics", "Entanglement entropy in holographic theories", "hep-th"),
        ("quantum_chemistry", "Coupled cluster calculations of excited states", "physics.chem-ph"),
        ("quantum_biology", "Quantum coherence in photosynthetic energy transfer", "q-bio.QM"),
        ("medicine", "Randomized controlled trial of a new vaccine", "medrxiv"),
        ("quantum_chemistry", "Machine learning potentials for reaction dynamics", "physics.chem-ph"),
    ]
    rows = []
    for i, (direction, title, category) in enumerate(specs, start=1):
        rows.append({
            "pk": i, "canonical_key": f"fake:{i}", "doi": None,
            "arxiv_id": f"2611.{i:05d}v1", "pmid": None,
            "source": "arxiv", "source_id": f"2611.{i:05d}v1",
            "title": title, "abstract": title + "。这是一段用于冒烟测试的摘要。",
            "authors": '["Alice","Bob","Carol"]', "url": f"https://arxiv.org/abs/2611.{i:05d}v1",
            "published": (now - timedelta(hours=6)).isoformat(),
            "categories": f'["{category}"]', "raw_json": "{}",
            "first_seen_at": now.isoformat(), "updated_at": now.isoformat(),
        })
    return rows


async def main() -> int:
    config = {
        "prefilter_per_direction": 50, "daily_top_per_direction": 5,
        "daily_cap_factor": 4, "max_themes_per_direction": 5,
        "provider_classify": "", "provider_summary": "", "provider_weekly": "",
        "llm_batch_size": 25, "llm_concurrency": 2,
    }
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        store = store_mod.Store(tmp_path / "smoke.db")
        archive = sources_mod.Archive(tmp_path / "archive")
        rows = fake_rows()
        for row in rows:
            # 直接写 sqlite，避免构造 Paper 对象
            with store.connect() as con:
                con.execute(
                    """INSERT OR REPLACE INTO papers
                       (pk, canonical_key, source, source_id, title, abstract, authors,
                        url, published, categories, first_seen_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["pk"], row["canonical_key"], row["source"], row["source_id"],
                     row["title"], row["abstract"], row["authors"], row["url"],
                     row["published"], row["categories"], row["first_seen_at"],
                     row["updated_at"]),
                )
        ctx = FakeContext()
        candidates, _ = pipeline_mod.prefilter(rows, per_direction=50)
        classifications = await pipeline_mod.classify_candidates(ctx, candidates, config, store)
        selected = {}
        for d in directions_mod.DIRECTIONS:
            selected[d.key] = pipeline_mod.select_direction(
                rows, classifications, {}, direction_key=d.key,
                top_n=5, max_themes=5,
            )
        selected, dropped = pipeline_mod.allocate_cap(selected, 28)
        totals = {d.key: sum(1 for c in classifications.values()
                             if c.get("primary") == d.key)
                  for d in directions_mod.DIRECTIONS}
        facts = pipeline_mod.build_daily_facts(
            report_date=datetime.now().strftime("%Y-%m-%d"),
            window_start=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            window_end=datetime.now(timezone.utc).isoformat(),
            selected=selected, totals=totals, source_stats={"arxiv": len(rows)},
            baseline={"rising_terms": [], "falling_terms": []},
        )
        copy = await skills_mod.run_skill(
            kind="daily", facts=facts, prompt_dir=ROOT / "prompts",
            context=ctx, config=config, store=store, archive=archive,
        )
        renderer = render_mod.Renderer(FakeStar(), archive, config)
        image = await renderer.render_daily(facts, copy)
        cards = await renderer.render_cards(facts)
        print("classifications:", len(classifications))
        print("selected:", facts["stats"]["selected_total"])
        print("copy headline:", copy.get("headline"))
        print("daily image:", image)
        if image:
            from PIL import Image
            print("daily image size:", Image.open(image).size)
        good_cards = [v for v in cards.values() if v]
        print("cards:", len(good_cards), "/", len(cards))
        if good_cards:
            from PIL import Image
            print("first card size:", Image.open(good_cards[0]).size)
        print("SMOKE OK")
        if os.environ.get("SMOKE_KEEP"):
            keep = Path("/tmp/paper_smoke_keep")
            shutil.copytree(tmp_path, keep, dirs_exist_ok=True)
            print("kept:", keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
