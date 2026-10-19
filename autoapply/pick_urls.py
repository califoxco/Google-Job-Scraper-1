"""Pick the next N application URLs from jobs.db (new + matched + unapplied).

Excludes platforms where applying happens in-platform behind a login
(Wellfound, Work at a Startup, LinkedIn, aggregators) - Simplify can't
autofill those, so they only waste batch slots.
"""
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Hosts that are never direct, fillable application forms.
EXCLUDE_HOSTS = (
    "wellfound.com",
    "workatastartup.com",
    "linkedin.com",
    "builtin.com",
    "remotive.com",
    "remoterocketship.com",
)


def pick(limit: int) -> list[str]:
    conn = sqlite3.connect(REPO / "results" / "jobs.db")
    not_like = " ".join(f"AND url NOT LIKE '%{h}%'" for h in EXCLUDE_HOSTS)
    rows = conn.execute(
        "SELECT url FROM jobs "
        "WHERE status = 'new' AND applied_at IS NULL AND title_match = 1 "
        f"{not_like} "
        "ORDER BY first_seen DESC, id DESC LIMIT ?",
        (limit,),
    )
    return [url for (url,) in rows]


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    for url in pick(limit):
        print(url)
