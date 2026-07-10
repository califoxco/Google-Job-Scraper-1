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
    status      TEXT NOT NULL DEFAULT 'new',
    applied_at  TEXT,
    applied_by  TEXT,
    notes       TEXT,
    title_match INTEGER
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


def _now_local() -> str:
    """Local wall-clock time (what 'when I applied' should read as)."""
    return datetime.now().isoformat(timespec="minutes")


# Statuses that imply you've applied (so applied_at gets stamped).
APPLIED_STATES = {"applied", "interviewing", "offer", "rejected"}


class Storage:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for col, ctype in (
            ("applied_at", "TEXT"),
            ("applied_by", "TEXT"),
            ("notes", "TEXT"),
            ("title_match", "INTEGER"),
        ):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ctype}")
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

    def _state(self, job_id: int) -> dict:
        r = self.conn.execute(
            "SELECT status, applied_at, applied_by FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not r:
            return {}
        return {
            "status": r["status"],
            "applied_at": r["applied_at"],
            "applied_by": r["applied_by"],
        }

    def set_status(self, job_id: int, status: str, who: str = "admin") -> dict:
        """Set status; stamp/clear applied_at to match. Returns new state."""
        if status in ("new", "dismissed"):
            # "dismissed" = not a real link / not applicable: definitely unapplied.
            self.conn.execute(
                "UPDATE jobs SET status = ?, applied_at = NULL, applied_by = NULL "
                "WHERE id = ?",
                (status, job_id),
            )
        elif status in APPLIED_STATES:
            # Stamp only if not already applied (preserve original time + person).
            self.conn.execute(
                "UPDATE jobs SET status = ?, applied_at = COALESCE(applied_at, ?), "
                "applied_by = COALESCE(applied_by, ?) WHERE id = ?",
                (status, _now_local(), who, job_id),
            )
        else:  # e.g. "hidden" — leave applied_at as-is
            self.conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?", (status, job_id)
            )
        self.conn.commit()
        return self._state(job_id)

    def set_applied(self, job_id: int, applied: bool, who: str = "admin") -> dict:
        """Quick toggle: check = applied + timestamp + who, uncheck = back to new.

        ``who`` is 'admin' or 'va' — VA-submitted applications are billed, so
        whoever first marks a job applied is recorded and preserved.
        """
        if applied:
            self.conn.execute(
                "UPDATE jobs SET status = 'applied', applied_at = COALESCE(applied_at, ?), "
                "applied_by = COALESCE(applied_by, ?) WHERE id = ?",
                (_now_local(), who, job_id),
            )
        else:
            self.conn.execute(
                "UPDATE jobs SET status = 'new', applied_at = NULL, applied_by = NULL "
                "WHERE id = ?",
                (job_id,),
            )
        self.conn.commit()
        return self._state(job_id)

    def set_dismissed(self, job_id: int, dismissed: bool) -> dict:
        """Quick toggle: mark a result as not-a-real-link / not-applicable."""
        if dismissed:
            self.conn.execute(
                "UPDATE jobs SET status = 'dismissed', applied_at = NULL, "
                "applied_by = NULL WHERE id = ?",
                (job_id,),
            )
        else:
            self.conn.execute(
                "UPDATE jobs SET status = 'new' WHERE id = ?", (job_id,)
            )
        self.conn.commit()
        return self._state(job_id)

    def set_notes(self, job_id: int, notes: str) -> None:
        self.conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, job_id))
        self.conn.commit()

    def jobs_needing_match(self, limit: int = 2000) -> list[dict]:
        """Unclassified leads for the LLM title matcher (newest first).

        Skips applied/dismissed rows — the user already triaged those.
        """
        cur = self.conn.execute(
            "SELECT id, title, url FROM jobs "
            "WHERE title_match IS NULL AND applied_at IS NULL AND status != 'dismissed' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    def set_title_match(self, job_id: int, match: bool | None) -> None:
        """Record the LLM's verdict: 1 match, 0 no-match, NULL unclassified."""
        self.conn.execute(
            "UPDATE jobs SET title_match = ? WHERE id = ?",
            (None if match is None else int(match), job_id),
        )
        self.conn.commit()

    def applied_jobs(self) -> list[dict]:
        """Everything with an applied timestamp, newest first (Excel export)."""
        cur = self.conn.execute(
            "SELECT * FROM jobs WHERE applied_at IS NOT NULL "
            "ORDER BY applied_at DESC, id DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        new = self.conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE status = 'new'"
        ).fetchone()["c"]
        applied = self.conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE applied_at IS NOT NULL"
        ).fetchone()["c"]
        applied_va = self.conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE applied_at IS NOT NULL AND applied_by = 'va'"
        ).fetchone()["c"]
        dismissed = self.conn.execute(
            "SELECT COUNT(*) c FROM jobs WHERE status = 'dismissed'"
        ).fetchone()["c"]
        return {
            "total": total,
            "new": new,
            "applied": applied,
            "applied_va": applied_va,
            "dismissed": dismissed,
        }

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
