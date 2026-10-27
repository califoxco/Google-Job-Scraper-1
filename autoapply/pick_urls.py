"""Pick the next N application URLs from jobs.db, top of the tracker first.

Mirrors the tracker's default view, in its order: newest first, excluding
rows that are applied, dismissed/hidden, flagged "needs human attention", or
hidden as a poor title match. Unattended runs (the daily auto-apply after the
scrape) therefore work through exactly what you'd see at the top of /admin.
"""
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def pick(limit: int) -> list[str]:
    conn = sqlite3.connect(REPO / "results" / "jobs.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    # Rows the auto-applier already failed on wait for a human; never retry.
    skip_flagged = "AND attention IS NULL " if "attention" in cols else ""
    rows = conn.execute(
        "SELECT url FROM jobs "
        "WHERE status = 'new' AND applied_at IS NULL "
        "AND (title_match IS NULL OR title_match = 1) "
        f"{skip_flagged}"
        "ORDER BY first_seen DESC, id DESC LIMIT ?",
        (limit,),
    )
    return [url for (url,) in rows]


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    for url in pick(limit):
        print(url)
