from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paper_digest.pipeline.facts import build_daily_facts, compute_baseline


def row(pk: int, title: str, abstract: str = "") -> dict:
    return {
        "pk": pk, "title": title, "abstract": abstract, "source": "arxiv",
        "categories": "[]", "authors": '["A. Author"]',
        "published": "2026-11-09T00:00:00Z",
        "first_seen_at": "2026-11-09T01:00:00+00:00",
        "url": f"https://example.org/{pk}",
    }


class FactsTest(unittest.TestCase):
    def test_baseline_rising_term(self):
        current = [row(i, "single-cell foundation model") for i in range(5)]
        previous = [row(i, "protein structure prediction") for i in range(5)]
        baseline = compute_baseline(current, previous)
        terms = [t["term"] for t in baseline["rising_terms"]]
        self.assertIn("single-cell", terms)

    def test_daily_facts_structure(self):
        selected = {
            "computational_biology": [{
                "pk": 7, "row": row(7, "A single-cell study", "abstract text"),
                "theme": "single-cell", "reason": "important",
                "relevance": 4.5, "impact_score": 9.0, "pinned": False,
            }],
        }
        facts = build_daily_facts(
            report_date="2026-11-09",
            window_start="2026-11-08T00:00:00+00:00",
            window_end="2026-11-09T00:00:00+00:00",
            selected=selected, totals={"computational_biology": 10},
            source_stats={"arxiv": 10}, baseline={"rising_terms": []},
        )
        self.assertEqual(facts["kind"], "daily")
        block = next(b for b in facts["directions"] if b["key"] == "computational_biology")
        self.assertEqual(block["selected_count"], 1)
        self.assertEqual(block["papers"][0]["id"], "p7")
        self.assertIn("p7", facts["papers"])
        self.assertEqual(facts["stats"]["selected_total"], 1)


if __name__ == "__main__":
    unittest.main()
