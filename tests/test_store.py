from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paper_digest.store import Store


@dataclass
class FakePaper:
    source: str = "arxiv"
    source_id: str = "2611.00001v1"
    title: str = "A paper"
    abstract: str = "abstract"
    authors: list | None = None
    url: str = "https://arxiv.org/abs/2611.00001v1"
    published: str | None = "2026-11-09T00:00:00Z"
    doi: str | None = None
    arxiv_id: str | None = "2611.00001v1"
    pmid: str | None = None
    categories: list | None = None
    raw: dict | None = None

    def __post_init__(self):
        self.authors = self.authors or ["A"]
        self.categories = self.categories or ["cs.AI"]
        self.raw = self.raw or {}

    def canonical_key(self) -> str:
        if self.doi:
            return "doi:" + self.doi.lower()
        return "arxiv:" + self.arxiv_id.lower()


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_dedup(self):
        p1 = FakePaper()
        p2 = FakePaper(title="A paper updated", abstract="longer abstract text")
        pk1 = self.store.upsert_paper(p1)
        pk2 = self.store.upsert_paper(p2)
        self.assertEqual(pk1, pk2)
        self.assertEqual(len(self.store.papers_since("2000-01-01T00:00:00+00:00")), 1)
        row = self.store.get_paper(pk1)
        self.assertEqual(row["abstract"], "longer abstract text")

    def test_classification_roundtrip(self):
        pk = self.store.upsert_paper(FakePaper())
        self.store.save_classification(pk, primary="computer_science",
                                       secondary=["theoretical_physics"],
                                       themes=["llm"], relevance=4.0,
                                       model="m", prompt_version="v1")
        got = self.store.get_classifications([pk], "v1")
        self.assertEqual(got[pk]["primary"], "computer_science")
        self.assertEqual(got[pk]["themes"], ["llm"])

    def test_report_and_sends(self):
        self.store.save_report("daily", "2026-11-09", {"a": 1}, {"b": 2})
        report = self.store.get_report("daily", "2026-11-09")
        self.assertEqual(report["facts"], {"a": 1})
        self.assertEqual(report["copy"], {"b": 2})
        self.store.mark_sent("daily", "2026-11-09", 0)
        self.store.mark_sent("daily", "2026-11-09", 2)
        self.assertEqual(self.store.sent_parts("daily", "2026-11-09"), {0, 2})

    def test_fetch_state_and_overrides(self):
        self.store.save_fetch_state("arxiv", "", "2026-11-09T00:00:00+00:00")
        state = self.store.get_fetch_state("arxiv")
        self.assertEqual(state["last_success_at"], "2026-11-09T00:00:00+00:00")
        self.store.set_override(3, "skip")
        self.assertEqual(self.store.get_overrides()[3]["action"], "skip")
        self.store.clear_override(3)
        self.assertEqual(self.store.get_overrides(), {})


if __name__ == "__main__":
    unittest.main()
