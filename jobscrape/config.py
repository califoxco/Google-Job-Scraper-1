"""Configuration: defaults, load/save, and shared constants.

The config lives in ``config.json`` at the project root. The UI writes it;
the scraper reads it. If the file is missing, DEFAULT_CONFIG is used (so a
fresh checkout runs without any setup).
"""

from __future__ import annotations

import json
from pathlib import Path

# Project root = parent of the jobscrape package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

# Freshness presets -> Google's tbs=qdr: token.
#   week -> qdr:w  (past week)
#   72h  -> qdr:d3 (past 3 days; Google accepts a numeric suffix)
#   24h  -> qdr:d  (past day)
FRESHNESS_OPTIONS = {
    "week": {"label": "Past week", "qdr": "w"},
    "72h": {"label": "Past 72 hours", "qdr": "d3"},
    "24h": {"label": "Past 24 hours", "qdr": "d"},
}

# Common ATS domains, seeded as toggles in the UI. site:<domain> targets
# the public job boards these vendors host.
KNOWN_ATS = [
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "jobvite.com",
    "workable.com",
    "breezy.hr",
    "recruitee.com",
    "icims.com",
    "bamboohr.com",
]

DEFAULT_CONFIG = {
    # Each keyword is {term, exact}. exact=True wraps it in quotes for an
    # exact-phrase match; exact=False appends it as a loose term.
    "keywords": [
        {"term": "Software Engineer", "exact": True},
        {"term": "San Francisco", "exact": True},
        {"term": "remote", "exact": False},
    ],
    "freshness": "week",
    "ats_sites": ["greenhouse.io"],
    "results_db": "results/jobs.db",
    "headless": True,
    "max_results_per_query": 30,
    "min_delay_seconds": 3.0,
    "max_delay_seconds": 8.0,
}


def load_config() -> dict:
    """Load config.json, filling in any missing keys from DEFAULT_CONFIG."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**DEFAULT_CONFIG, **data}
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def resolve_db_path(cfg: dict) -> Path:
    """Resolve results_db to an absolute path (relative paths are anchored
    to the project root so cron / Task Scheduler work regardless of CWD)."""
    p = Path(cfg.get("results_db", "results/jobs.db"))
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p
