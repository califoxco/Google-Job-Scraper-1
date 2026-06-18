"""SQLite storage with dedupe-by-URL.

Re-seeing a job on a later run does NOT create a duplicate row; it just
bumps last_seen / seen_count. That keeps the leads table clean across the
every-few-hours cron.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT UNIQUE NOT NULL,
    title       TEXT,
    snippet     TEXT,
    ats         TEXT,
    query       TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    seen_count  INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen);
CREATE INDEX IF NOT EXISTS idx_jobs_ats        ON jobs(ats);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    queries     INTEGER DEFAULT 0,
    found       INTEGER DEFAULT 0,
    new_jobs    INTEGER DEFAULT 0,
    blocked     INTEGER DEFAULT 0,
    note        TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- jobs -------------------------------------------------------------

    def upsert_job(self, url: str, title: str, snippet: str, ats: str, query: str) -> bool:
        """Insert a new job or bump an existing one.

        Returns True if this URL was brand new (inserted), False if it had
        already been seen.
        """
        now = _now()
        row = self.conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO jobs (url, title, snippet, ats, query, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (url, title, snippet, ats, query, now, now),
            )
            self.conn.commit()
            return True

        # Already seen: refresh last_seen / count, keep best title+snippet.
        self.conn.execute(
            "UPDATE jobs SET last_seen = ?, seen_count = seen_count + 1, "
            "title = COALESCE(NULLIF(?, ''), title), "
            "snippet = COALESCE(NULLIF(?, ''), snippet) WHERE id = ?",
            (now, title, snippet, row["id"]),
        )
        self.conn.commit()
        return False

    def recent_jobs(self, limit: int = 100) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM jobs ORDER BY first_seen DESC, id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    def set_status(self, job_id: int, status: str) -> None:
        self.conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        self.conn.commit()

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        new = self.conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE status = 'new'"
        ).fetchone()["c"]
        return {"total": total, "new": new}

    # --- runs -------------------------------------------------------------

    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        queries: int,
        found: int,
        new_jobs: int,
        blocked: int,
        note: str = "",
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, queries = ?, found = ?, "
            "new_jobs = ?, blocked = ?, note = ? WHERE id = ?",
            (_now(), queries, found, new_jobs, blocked, note, run_id),
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 10) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()
