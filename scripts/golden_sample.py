#!/usr/bin/env python3
"""从数据库抽样生成标注黄金集 CSV（分类/主题评估用）。

示例：
python scripts/golden_sample.py --db paper_digest.db \
  --start 2026-11-01 --end 2026-11-09 --per-direction 20 --out golden.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
from pathlib import Path

DIRECTIONS = (
    "computer_science", "theoretical_physics", "astrophysics",
    "quantum_chemistry", "computational_biology", "medicine", "quantum_biology",
)

FIELDNAMES = [
    "sample_id", "pk", "llm_primary", "llm_secondary", "llm_themes",
    "llm_relevance", "direction_slot", "title", "abstract", "source",
    "categories", "published", "url",
    "human_primary", "human_secondary", "human_themes", "human_relevance", "notes",
]


def fetch_rows(con: sqlite3.Connection, start: str, end: str) -> list[dict]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT p.*, c.primary_direction, c.secondary, c.themes, c.relevance
           FROM papers p LEFT JOIN classifications c
             ON c.paper_pk = p.pk
            AND c.prompt_version = (
                SELECT prompt_version FROM classifications
                ORDER BY created_at DESC LIMIT 1
            )
           WHERE COALESCE(p.published, p.first_seen_at) >= ?
             AND COALESCE(p.published, p.first_seen_at) < ?""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/AstrBot/data/plugin_data/"
                                       "astrbot_plugin_paper_digest/paper_digest.db")
    parser.add_argument("--start", required=True, help="ISO date, e.g. 2026-11-01")
    parser.add_argument("--end", required=True, help="ISO date, exclusive, e.g. 2026-11-09")
    parser.add_argument("--per-direction", type=int, default=20)
    parser.add_argument("--hard-negatives", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261109)
    parser.add_argument("--out", default="golden_set.csv")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    con = sqlite3.connect(args.db)
    rows = fetch_rows(con, args.start, args.end)
    con.close()
    print(f"loaded {len(rows)} papers with classifications")

    out_rows: list[dict] = []
    seq = 0
    for direction in DIRECTIONS:
        pool = [r for r in rows if r.get("primary_direction") == direction]
        # 按 source 分层，每个 source 内随机抽样
        by_source: dict[str, list[dict]] = {}
        for r in pool:
            by_source.setdefault(r.get("source") or "unknown", []).append(r)
        picked: list[dict] = []
        per_source = max(1, args.per_direction // max(len(by_source), 1))
        for items in by_source.values():
            rng.shuffle(items)
            picked.extend(items[:per_source])
        rng.shuffle(picked)
        picked = picked[:args.per_direction]
        for r in picked:
            seq += 1
            out_rows.append(to_out(r, direction, seq))

        # 硬负例：次方向命中但主方向不是它的论文，最容易混淆
        negatives = []
        for r in rows:
            if r.get("primary_direction") == direction:
                continue
            try:
                secondary = json.loads(r.get("secondary") or "[]")
            except Exception:  # noqa: BLE001
                secondary = []
            if direction in secondary:
                negatives.append(r)
        rng.shuffle(negatives)
        for r in negatives[:args.hard_negatives]:
            seq += 1
            out_rows.append(to_out(r, direction, seq))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"wrote {len(out_rows)} samples -> {out_path}")
    return 0


def to_out(r: dict, direction_slot: str, seq: int) -> dict:
    return {
        "sample_id": f"S{seq:04d}",
        "pk": r.get("pk"),
        "llm_primary": r.get("primary_direction") or "",
        "llm_secondary": r.get("secondary") or "",
        "llm_themes": r.get("themes") or "",
        "llm_relevance": r.get("relevance") or "",
        "direction_slot": direction_slot,
        "title": r.get("title") or "",
        "abstract": (r.get("abstract") or "")[:1500],
        "source": r.get("source") or "",
        "categories": r.get("categories") or "",
        "published": r.get("published") or "",
        "url": r.get("url") or "",
        "human_primary": "",
        "human_secondary": "",
        "human_themes": "",
        "human_relevance": "",
        "notes": "",
    }


if __name__ == "__main__":
    raise SystemExit(main())
