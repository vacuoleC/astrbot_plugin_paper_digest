from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paper_digest.directions import DIRECTIONS
from paper_digest.pipeline.select import allocate_cap, select_direction, select_weekly


def row(pk: int, title: str, source: str = "arxiv") -> dict:
    return {
        "pk": pk, "title": title, "abstract": "", "source": source,
        "categories": "[]", "authors": "[]",
        "published": "2026-11-09T00:00:00Z",
        "first_seen_at": "2026-11-09T01:00:00+00:00",
        "url": f"https://example.org/{pk}", "prefilter_score": 5.0,
    }


def cls(primary: str, themes: list[str], relevance: float = 4.0) -> dict:
    return {"primary": primary, "secondary": [], "themes": themes,
            "relevance": relevance, "reason": "test"}


class SelectTest(unittest.TestCase):
    def test_theme_coverage_and_primary_only(self):
        rows = [row(i, f"paper {i}") for i in range(1, 7)]
        classifications = {
            1: cls("computational_biology", ["single-cell"], 5.0),
            2: cls("computational_biology", ["single-cell"], 4.0),
            3: cls("computational_biology", ["genomics"], 3.0),
            4: cls("computational_biology", ["protein"], 2.0),
            5: cls("medicine", ["clinical"], 5.0),
            6: cls("computational_biology", ["other"], 1.0),
        }
        chosen = select_direction(
            rows, classifications, overrides={},
            direction_key="computational_biology", top_n=4, max_themes=3,
        )
        ids = [c["pk"] for c in chosen]
        self.assertEqual(len(chosen), 4)
        self.assertIn(1, ids)  # single-cell 代表
        self.assertIn(3, ids)  # genomics 代表
        self.assertIn(4, ids)  # protein 代表
        self.assertNotIn(5, ids)  # 主方向不是计算生物学

    def test_skip_and_pin(self):
        rows = [row(1, "a"), row(2, "b"), row(3, "c")]
        classifications = {
            1: cls("computer_science", ["t1"], 5.0),
            2: cls("computer_science", ["t2"], 4.0),
            3: cls("computer_science", ["t3"], 3.0),
        }
        overrides = {2: {"action": "skip"}, 3: {"action": "pin"}}
        chosen = select_direction(
            rows, classifications, overrides,
            direction_key="computer_science", top_n=2,
        )
        ids = [c["pk"] for c in chosen]
        self.assertNotIn(2, ids)
        self.assertIn(3, ids)
        self.assertTrue(chosen[0]["pinned"])

    def test_allocate_cap_trims_from_largest_direction(self):
        selected = {
            d.key: [{"pk": i, "relevance": 5 - i, "row": {"prefilter_score": 1}}
                    for i in range(5)]
            for d in DIRECTIONS
        }
        capped, dropped = allocate_cap(selected, 28)
        self.assertEqual(sum(len(v) for v in capped.values()), 28)
        self.assertEqual(len(dropped), len(DIRECTIONS) * 5 - 28)

    def test_weekly_returns_at_most_per_direction(self):
        rows = [row(i, f"p{i}", source="crossref" if i % 2 else "arxiv")
                for i in range(1, 9)]
        classifications = {
            i: cls("computer_science", ["t"], relevance=float(i))
            for i in range(1, 9)
        }
        selected = select_weekly(rows, classifications, overrides={},
                                 per_direction=3)
        self.assertEqual(len(selected["computer_science"]), 3)


if __name__ == "__main__":
    unittest.main()
