from __future__ import annotations

import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paper_digest.directions import DIRECTION_BY_KEY
from paper_digest.pipeline.weekdigest import (
    assign_direction, build_week_digests, merge_batch_digests,
)
from paper_digest.store import Store


def row(pk: int, title: str, categories: str = "[]") -> dict:
    return {
        "pk": pk, "title": title, "abstract": title, "source": "arxiv",
        "categories": categories, "authors": "[]",
        "published": "2026-11-09T00:00:00Z",
        "first_seen_at": "2026-11-09T01:00:00+00:00",
        "url": f"https://example.org/{pk}",
    }


class WeekDigestTest(unittest.TestCase):
    def test_assign_direction_prefers_classification(self):
        classifications = {1: {"primary": "medicine", "themes": ["clinical"]}}
        self.assertEqual(assign_direction(row(1, "Single-cell atlas"), classifications),
                         "medicine")

    def test_assign_direction_falls_back_to_keywords(self):
        self.assertEqual(
            assign_direction(row(2, "Single-cell foundation model for genomics",
                                 '["q-bio.GN"]'), {}),
            "computational_biology",
        )

    def test_assign_direction_skips_irrelevant(self):
        self.assertIsNone(assign_direction(row(3, "Unrelated cooking notes"), {}))

    def test_merge_batch_digests(self):
        direction = DIRECTION_BY_KEY["computer_science"]
        merged = merge_batch_digests(direction, 10, [
            {"themes": [{"label": "LLM agents", "count": 3, "summary": "a",
                         "paper_ids": ["p1"]}],
             "trend": "trend-a", "notable_paper_ids": ["p1"]},
            {"themes": [{"label": "llm agents", "count": 2, "summary": "b",
                         "paper_ids": ["p2"]}],
             "trend": "trend-b", "notable_paper_ids": ["p2"]},
        ])
        self.assertEqual(merged["total"], 10)
        self.assertEqual(merged["batches"], 2)
        self.assertEqual(merged["themes"][0]["count"], 5)
        self.assertEqual(merged["themes"][0]["paper_ids"], ["p1", "p2"])
        self.assertIn("trend-a", merged["trend"])
        self.assertIn("trend-b", merged["trend"])


class FakeProvider:
    provider_config = {"id": "fake/model"}

    async def text_chat(self, *, prompt=None, system_prompt=None, **kwargs):
        return SimpleNamespace(completion_text=(
            '{"themes":[{"label":"agents","count":1,"summary":"s",'
            '"paper_ids":[]}],"trend":"weekly trend","notable_paper_ids":[]}'
        ))


class FakeContext:
    def get_provider_by_id(self, _):
        return FakeProvider()

    def get_using_provider(self):
        return FakeProvider()


class WeekDigestPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_week_digests_with_provider(self):
        classifications = {
            i: {"primary": "computer_science", "themes": ["agents"], "relevance": 4.0}
            for i in (1, 2, 3)
        }
        rows = [row(i, f"LLM agent paper {i}") for i in (1, 2, 3)]
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            digests = await build_week_digests(
                context=FakeContext(), rows=rows, classifications=classifications,
                config={"provider_classify": "fake/model",
                        "weekly_digest_batch_size": 2,
                        "weekly_digest_concurrency": 1},
                store=store, week_start="2026-11-01",
            )
        digest = digests["computer_science"]
        self.assertEqual(digest["total"], 3)
        self.assertEqual(digest["model"], "fake/model")
        self.assertEqual(digest["themes"][0]["label"], "agents")
        self.assertIn("weekly trend", digest["trend"])

if __name__ == "__main__":
    unittest.main()
