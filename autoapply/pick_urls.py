"""Print the next N application URLs from jobs.db (new + matched + unapplied)."""
import sqlite3
import sys
from pathlib import Path

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
db = Path(__file__).resolve().parent.parent / "results" / "jobs.db"
conn = sqlite3.connect(db)
rows = conn.execute(
    "SELECT url FROM jobs "
    "WHERE status = 'new' AND applied_at IS NULL AND title_match = 1 "
    "ORDER BY first_seen DESC LIMIT ?",
    (limit,),
)
for (url,) in rows:
    print(url)
