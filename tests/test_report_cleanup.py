from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paper_digest.pipeline.facts import compute_baseline
from paper_digest.pipeline.normalize import dedupe_rows, filter_backfill_rows
from paper_digest.pipeline.select import impact_score, is_review, select_weekly
from paper_digest.directions import DIRECTION_BY_KEY


def row(pk: int, title: str, *, doi=None, source="arxiv", published="2026-11-08T00:00:00Z",
        first_seen="2026-11-09T00:00:00Z", abstract="", backfill=0,
        categories="[]", authors='["A. Author"]') -> dict:
    return {
        "pk": pk, "title": title, "abstract": abstract, "source": source,
        "doi": doi, "arxiv_id": None, "pmid": None, "categories": categories,
        "authors": authors, "published": published, "first_seen_at": first_seen,
        "url": f"https://example.org/{pk}", "backfill": backfill,
    }


class CleanupTest(unittest.TestCase):
    def test_dedupe_doi_versions(self):
        v1 = row(1, "Same paper", doi="10.26434/chemrxiv.1/v1",
                 published="2026-11-02T00:00:00Z", abstract="short")
        v2 = row(2, "Same paper", doi="10.26434/chemrxiv.1/v2",
                 published="2026-11-05T00:00:00Z", abstract="longer abstract")
        kept = dedupe_rows([v1, v2])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["pk"], 2)

    def test_dedupe_title_without_doi(self):
        a = row(3, "A title only paper", published="2026-11-02T00:00:00Z")
        b = row(4, "A title only paper", published="2026-11-06T00:00:00Z")
        kept = dedupe_rows([a, b])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["pk"], 4)

    def test_backfill_filter(self):
        old = row(5, "old", published="2026-10-20T00:00:00Z", backfill=1)
        recent = row(6, "recent", published="2026-11-08T00:00:00Z", backfill=1)
        normal_old = row(7, "normal old", published="2026-10-20T00:00:00Z", backfill=0)
        from datetime import datetime, timezone
        kept = filter_backfill_rows([old, recent, normal_old],
                                    datetime(2026, 11, 8, tzinfo=timezone.utc))
        self.assertEqual([r["pk"] for r in kept], [6, 7])

    def test_review_penalty_and_weekly_order(self):
        review = row(8, "A Comprehensive Review of Object Detection")
        paper = row(9, "EvoCodeAgent: Project-Level Code Generation")
        self.assertTrue(is_review(review))
        self.assertGreater(impact_score(paper, {"relevance": 5.0}),
                           impact_score(review, {"relevance": 5.0}))
        classifications = {
            8: {"primary": "computer_science", "themes": ["review"], "relevance": 5.0},
            9: {"primary": "computer_science", "themes": ["agents"], "relevance": 5.0},
        }
        selected = select_weekly([review, paper], classifications, {}, per_direction=1)
        self.assertEqual(selected["computer_science"][0]["pk"], 9)

    def test_baseline_available(self):
        empty = compute_baseline([row(10, "single-cell model")], [])
        self.assertFalse(empty["available"])
        prev = [row(11, "protein structure prediction") for _ in range(3)]
        cur = [row(12, "single-cell model") for _ in range(3)]
        full = compute_baseline(cur, prev)
        self.assertTrue(full["available"])


if __name__ == "__main__":
    unittest.main()
