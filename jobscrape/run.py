"""CLI entry point — this is what the cron / Task Scheduler job runs.

    python -m jobscrape.run

Exit codes: 0 = ok, 2 = every query was blocked (so cron logs flag it).
"""

from __future__ import annotations

import sys

from .config import load_config
from .scraper import run_scrape


def main() -> None:
    cfg = load_config()
    print("job-google-scrape")
    print(f"  provider  : {cfg.get('provider')}")
    print(f"  freshness : {cfg.get('freshness')}")
    print(f"  ats sites : {', '.join(cfg.get('ats_sites', [])) or '(none)'}")
    print("-" * 40)

    summary = run_scrape(cfg)

    print("-" * 40)
    print("Summary")
    print(f"  queries : {summary['queries']}")
    print(f"  found   : {summary['found']}")
    print(f"  new     : {summary['new']}")
    print(f"  blocked : {summary['blocked']}")
    print(f"  db      : {summary['db']}")

    if summary["queries"] and summary["blocked"] == summary["queries"]:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
