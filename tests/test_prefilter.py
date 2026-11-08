from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paper_digest.directions import DIRECTION_BY_KEY
from paper_digest.pipeline.prefilter import prefilter, score_paper


def make_row(pk: int, title: str, abstract: str = "", categories: str = '["q-bio.GN"]',
             source: str = "arxiv") -> dict:
    return {
        "pk": pk,
        "title": title,
        "abstract": abstract,
        "source": source,
        "categories": categories,
        "authors": "[]",
        "published": "2026-11-09T00:00:00Z",
        "first_seen_at": "2026-11-09T01:00:00+00:00",
        "url": f"https://example.org/{pk}",
    }


class PrefilterTest(unittest.TestCase):
    def test_qbio_scores_higher_for_computational_biology(self):
        row = make_row(1, "Single-cell foundation models for transcriptome analysis")
        cb = score_paper(row, DIRECTION_BY_KEY["computational_biology"])
        cs = score_paper(row, DIRECTION_BY_KEY["computer_science"])
        self.assertGreater(cb, cs)

    def test_exclude_keyword_penalizes(self):
        row = make_row(2, "A clinical trial of computational biology software")
        score = score_paper(row, DIRECTION_BY_KEY["computational_biology"])
        self.assertLess(score, 4.0)

    def test_prefilter_returns_candidates_and_direction_lists(self):
        rows = [
            make_row(1, "Single-cell foundation model for genomics"),
            make_row(2, "A study of galaxy formation and dark matter"),
            make_row(3, "Unrelated paper about cooking", categories="[]"),
        ]
        candidates, by_direction = prefilter(rows, per_direction=10)
        self.assertIn(1, candidates)
        self.assertIn(2, candidates)
        self.assertNotIn(3, candidates)
        self.assertIn(1, by_direction["computational_biology"])
        self.assertIn(2, by_direction["astrophysics"])


if __name__ == "__main__":
    unittest.main()
