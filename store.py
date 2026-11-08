"""SQLite 持久化：论文、分类、抓取水位线、选片、报告、发送记录、人工干预。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.init_schema()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def init_schema(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS papers (
                    pk INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_key TEXT NOT NULL UNIQUE,
                    doi TEXT,
                    arxiv_id TEXT,
                    pmid TEXT,
                    source TEXT,
                    source_id TEXT,
                    title TEXT NOT NULL,
                    abstract TEXT DEFAULT '',
                    authors TEXT DEFAULT '[]',
                    url TEXT DEFAULT '',
                    published TEXT,
                    categories TEXT DEFAULT '[]',
                    raw_json TEXT,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published);
                CREATE INDEX IF NOT EXISTS idx_papers_first_seen ON papers(first_seen_at);
                CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);

                CREATE TABLE IF NOT EXISTS paper_aliases (
                    paper_pk INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    doi TEXT,
                    arxiv_id TEXT,
                    pmid TEXT,
                    PRIMARY KEY (source, source_id)
                );

                CREATE TABLE IF NOT EXISTS classifications (
                    paper_pk INTEGER NOT NULL,
                    primary_direction TEXT,
                    secondary TEXT DEFAULT '[]',
                    themes TEXT DEFAULT '[]',
                    relevance REAL DEFAULT 0,
                    model TEXT,
                    prompt_version TEXT,
                    created_at TEXT,
                    PRIMARY KEY (paper_pk, prompt_version)
                );
                CREATE INDEX IF NOT EXISTS idx_class_direction
                    ON classifications(primary_direction);

                CREATE TABLE IF NOT EXISTS fetch_state (
                    source TEXT NOT NULL,
                    key TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT,
                    last_cursor TEXT,
                    meta TEXT,
                    PRIMARY KEY (source, key)
                );

                CREATE TABLE IF NOT EXISTS daily_selections (
                    report_date TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    paper_pks TEXT NOT NULL,
                    themes TEXT,
                    created_at TEXT,
                    PRIMARY KEY (report_date, direction)
                );

                CREATE TABLE IF NOT EXISTS reports (
                    kind TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    facts_json TEXT,
                    copy_json TEXT,
                    status TEXT DEFAULT 'built',
                    created_at TEXT,
                    PRIMARY KEY (kind, report_date)
                );

                CREATE TABLE IF NOT EXISTS sends (
                    kind TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    part INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    sent_at TEXT,
                    PRIMARY KEY (kind, report_date, part)
                );

                CREATE TABLE IF NOT EXISTS skill_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT,
                    report_date TEXT,
                    facts_json TEXT,
                    raw_output TEXT,
                    copy_json TEXT,
                    valid INTEGER,
                    error TEXT,
                    model TEXT,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS weekly_digests (
                    week_start TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    digest_json TEXT,
                    model TEXT,
                    created_at TEXT,
                    PRIMARY KEY (week_start, direction)
                );

                CREATE TABLE IF NOT EXISTS overrides (
                    paper_pk INTEGER PRIMARY KEY,
                    action TEXT NOT NULL,
                    direction TEXT,
                    note TEXT,
                    created_at TEXT
                );
                """
            )

    # ---------- papers ----------

    def upsert_paper(self, paper) -> int:
        key = paper.canonical_key()
        now = _now()
        with self._lock, self.connect() as con:
            row = con.execute("SELECT * FROM papers WHERE canonical_key = ?", (key,)).fetchone()
            if row is None:
                cur = con.execute(
                    """INSERT INTO papers
                       (canonical_key, doi, arxiv_id, pmid, source, source_id, title,
                        abstract, authors, url, published, categories, raw_json,
                        first_seen_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key, paper.doi, paper.arxiv_id, paper.pmid,
                        paper.source, paper.source_id, paper.title,
                        paper.abstract or "", json.dumps(paper.authors, ensure_ascii=False),
                        paper.url or "", paper.published,
                        json.dumps(paper.categories, ensure_ascii=False),
                        json.dumps(paper.raw or {}, ensure_ascii=False), now, now,
                    ),
                )
                pk = int(cur.lastrowid)
            else:
                pk = int(row["pk"])
                # 合并：摘要取更长、作者取更多、缺字段补全
                new_abstract = row["abstract"] or ""
                if len(paper.abstract or "") > len(new_abstract):
                    new_abstract = paper.abstract
                new_authors = json.loads(row["authors"] or "[]")
                if len(paper.authors or []) > len(new_authors):
                    new_authors = paper.authors
                con.execute(
                    """UPDATE papers SET
                        doi=COALESCE(doi, ?), arxiv_id=COALESCE(arxiv_id, ?),
                        pmid=COALESCE(pmid, ?), abstract=?, authors=?,
                        url=CASE WHEN url='' THEN ? ELSE url END,
                        published=COALESCE(published, ?), categories=?,
                        raw_json=?, updated_at=?
                       WHERE pk=?""",
                    (
                        paper.doi, paper.arxiv_id, paper.pmid,
                        new_abstract, json.dumps(new_authors, ensure_ascii=False),
                        paper.url or "", paper.published,
                        json.dumps(paper.categories or [], ensure_ascii=False),
                        json.dumps(paper.raw or {}, ensure_ascii=False), now, pk,
                    ),
                )
            con.execute(
                """INSERT OR REPLACE INTO paper_aliases
                   (paper_pk, source, source_id, doi, arxiv_id, pmid)
                   VALUES (?,?,?,?,?,?)""",
                (pk, paper.source, paper.source_id, paper.doi, paper.arxiv_id, paper.pmid),
            )
            return pk

    def get_paper(self, pk: int) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM papers WHERE pk = ?", (pk,)).fetchone()
            return dict(row) if row else None

    def get_papers(self, pks: Iterable[int]) -> list[dict[str, Any]]:
        pks = list(pks)
        if not pks:
            return []
        placeholders = ",".join("?" for _ in pks)
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM papers WHERE pk IN ({placeholders})", pks
            ).fetchall()
            by_pk = {int(r["pk"]): dict(r) for r in rows}
            return [by_pk[pk] for pk in pks if pk in by_pk]

    def get_paper_by_key(self, key: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM papers WHERE canonical_key = ?", (key,)).fetchone()
            return dict(row) if row else None

    def papers_since(self, since_iso: str) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM papers WHERE COALESCE(published, first_seen_at) >= ? "
                "ORDER BY COALESCE(published, first_seen_at) DESC",
                (since_iso,),
            ).fetchall()
            return [dict(r) for r in rows]

    def papers_between(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM papers "
                "WHERE COALESCE(published, first_seen_at) >= ? "
                "AND COALESCE(published, first_seen_at) < ? "
                "ORDER BY COALESCE(published, first_seen_at) DESC",
                (start_iso, end_iso),
            ).fetchall()
            return [dict(r) for r in rows]

    def papers_first_seen_between(self, start_iso: str, end_iso: str
                                  ) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM papers WHERE first_seen_at >= ? AND first_seen_at < ? "
                "ORDER BY first_seen_at DESC",
                (start_iso, end_iso),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_papers_between(self, start_iso: str, end_iso: str,
                             source: str | None = None) -> int:
        q = ("SELECT COUNT(*) FROM papers WHERE COALESCE(published, first_seen_at) >= ? "
             "AND COALESCE(published, first_seen_at) < ?")
        args: list[Any] = [start_iso, end_iso]
        if source:
            q += " AND source = ?"
            args.append(source)
        with self.connect() as con:
            return int(con.execute(q, args).fetchone()[0])

    # ---------- classification ----------

    def save_classification(self, paper_pk: int, *, primary: str | None,
                            secondary: list[str], themes: list[str],
                            relevance: float, model: str, prompt_version: str) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO classifications
                   (paper_pk, primary_direction, secondary, themes, relevance,
                    model, prompt_version, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (paper_pk, primary, json.dumps(secondary, ensure_ascii=False),
                 json.dumps(themes, ensure_ascii=False), relevance, model,
                 prompt_version, _now()),
            )

    def get_classifications(self, pks: Iterable[int],
                            prompt_version: str) -> dict[int, dict[str, Any]]:
        pks = list(pks)
        if not pks:
            return {}
        placeholders = ",".join("?" for _ in pks)
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM classifications WHERE prompt_version = ? "
                f"AND paper_pk IN ({placeholders})",
                [prompt_version, *pks],
            ).fetchall()
            out: dict[int, dict[str, Any]] = {}
            for r in rows:
                d = dict(r)
                d["secondary"] = json.loads(d.get("secondary") or "[]")
                d["themes"] = json.loads(d.get("themes") or "[]")
                d["primary"] = d.get("primary_direction")
                out[int(d["paper_pk"])] = d
            return out

    # ---------- fetch state ----------

    def get_fetch_state(self, source: str, key: str = "") -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM fetch_state WHERE source=? AND key=?", (source, key)
            ).fetchone()
            return dict(row) if row else None

    def save_fetch_state(self, source: str, key: str, last_success_at: str,
                         cursor: str | None = None, meta: dict | None = None) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO fetch_state
                   (source, key, last_success_at, last_cursor, meta)
                   VALUES (?,?,?,?,?)""",
                (source, key, last_success_at, cursor,
                 json.dumps(meta or {}, ensure_ascii=False)),
            )

    # ---------- selections / reports / sends ----------

    def save_selection(self, report_date: str, direction: str,
                       paper_pks: list[int], themes: list[str]) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO daily_selections
                   (report_date, direction, paper_pks, themes, created_at)
                   VALUES (?,?,?,?,?)""",
                (report_date, direction, json.dumps(paper_pks),
                 json.dumps(themes, ensure_ascii=False), _now()),
            )

    def get_selection(self, report_date: str, direction: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM daily_selections WHERE report_date=? AND direction=?",
                (report_date, direction),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["paper_pks"] = json.loads(d.get("paper_pks") or "[]")
            d["themes"] = json.loads(d.get("themes") or "[]")
            return d

    def save_report(self, kind: str, report_date: str, facts: dict,
                    copy: dict | None = None, status: str = "built") -> None:
        with self._lock, self.connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO reports
                   (kind, report_date, facts_json, copy_json, status, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (kind, report_date, json.dumps(facts, ensure_ascii=False),
                 json.dumps(copy, ensure_ascii=False) if copy is not None else None,
                 status, _now()),
            )

    def get_report(self, kind: str, report_date: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM reports WHERE kind=? AND report_date=?",
                (kind, report_date),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["facts"] = json.loads(d.get("facts_json") or "{}")
            d["copy"] = json.loads(d["copy_json"]) if d.get("copy_json") else None
            return d

    def latest_report(self, kind: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM reports WHERE kind=? ORDER BY report_date DESC LIMIT 1",
                (kind,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["facts"] = json.loads(d.get("facts_json") or "{}")
            d["copy"] = json.loads(d["copy_json"]) if d.get("copy_json") else None
            return d

    def mark_sent(self, kind: str, report_date: str, part: int, status: str = "ok") -> None:
        with self._lock, self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO sends (kind, report_date, part, status, sent_at) "
                "VALUES (?,?,?,?,?)",
                (kind, report_date, part, status, _now()),
            )

    def sent_parts(self, kind: str, report_date: str) -> set[int]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT part FROM sends WHERE kind=? AND report_date=? AND status='ok'",
                (kind, report_date),
            ).fetchall()
            return {int(r["part"]) for r in rows}

    # ---------- skill runs ----------

    def save_skill_run(self, *, kind: str, report_date: str, facts: dict,
                       raw_output: str | None, copy: dict | None, valid: bool,
                       error: str | None, model: str) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                """INSERT INTO skill_runs
                   (kind, report_date, facts_json, raw_output, copy_json,
                    valid, error, model, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (kind, report_date, json.dumps(facts, ensure_ascii=False),
                 raw_output, json.dumps(copy, ensure_ascii=False) if copy else None,
                 1 if valid else 0, error, model, _now()),
            )

    # ---------- weekly digests ----------

    def save_weekly_digest(self, week_start: str, direction: str, digest: dict,
                           model: str) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO weekly_digests
                   (week_start, direction, digest_json, model, created_at)
                   VALUES (?,?,?,?,?)""",
                (week_start, direction, json.dumps(digest, ensure_ascii=False),
                 model, _now()),
            )

    def get_weekly_digest(self, week_start: str, direction: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM weekly_digests WHERE week_start=? AND direction=?",
                (week_start, direction),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["digest"] = json.loads(d.get("digest_json") or "{}")
            return d

    # ---------- overrides ----------

    def set_override(self, paper_pk: int, action: str, direction: str | None = None,
                     note: str | None = None) -> None:
        with self._lock, self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO overrides (paper_pk, action, direction, note, created_at) "
                "VALUES (?,?,?,?,?)",
                (paper_pk, action, direction, note, _now()),
            )

    def clear_override(self, paper_pk: int) -> None:
        with self._lock, self.connect() as con:
            con.execute("DELETE FROM overrides WHERE paper_pk=?", (paper_pk,))

    def get_overrides(self) -> dict[int, dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM overrides").fetchall()
            return {int(r["paper_pk"]): dict(r) for r in rows}

    # ---------- stats ----------

    def stats(self, since_iso: str) -> dict[str, Any]:
        with self.connect() as con:
            total = con.execute(
                "SELECT COUNT(*) FROM papers WHERE first_seen_at >= ?", (since_iso,)
            ).fetchone()[0]
            by_source = {
                r["source"]: r["n"]
                for r in con.execute(
                    "SELECT source, COUNT(*) AS n FROM papers "
                    "WHERE first_seen_at >= ? GROUP BY source", (since_iso,)
                )
            }
            classified = con.execute(
                "SELECT COUNT(*) FROM classifications WHERE created_at >= ?", (since_iso,)
            ).fetchone()[0]
            return {"total": int(total), "by_source": by_source,
                    "classified": int(classified)}
