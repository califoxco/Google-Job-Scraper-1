"""Configuration: defaults, load/save, and shared constants.

The config lives in ``config.json`` at the project root. The UI writes it;
the scraper reads it. If the file is missing, DEFAULT_CONFIG is used (so a
fresh checkout runs without any setup).
"""

from __future__ import annotations

import json
import os
import secrets
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

# Search "sources" seeded as toggles in the UI. Each is a crafted Google query
# *fragment* (the part between the keywords and "remote"), not just a domain —
# modeled on briansjobsearch.com. A source resolves to:
#   key    selected value stored in config.ats_sites
#   label  shown in the UI
#   query  the q= fragment, e.g. "site:greenhouse.io" or an OR/wildcard group
#   hosts  result-host filter (list of substrings); None = accept any company
#          host (used by the wildcard "careers/job pages" presets, whose
#          results land on arbitrary company domains).

# Simple per-domain ATS: key == domain, query == "site:<domain>".
_SIMPLE_ATS = [
    ("greenhouse.io", "Greenhouse"),
    ("lever.co", "Lever"),
    ("ashbyhq.com", "Ashby"),
    ("myworkdayjobs.com", "Workday"),
    ("smartrecruiters.com", "SmartRecruiters"),
    ("jobvite.com", "Jobvite"),
    ("workable.com", "Workable"),
    ("breezy.hr", "Breezy"),
    ("recruitee.com", "Recruitee"),
    ("icims.com", "iCIMS"),
    ("bamboohr.com", "BambooHR"),
    ("applytojob.com", "JazzHR"),
    ("gem.com", "Gem"),
    ("dover.io", "Dover"),
    ("teamtailor.com", "Teamtailor"),
    ("pinpointhq.com", "Pinpoint"),
    ("careerpuck.com", "CareerPuck"),
    ("catsone.com", "CATS"),
    ("trakstar.com", "Trakstar"),
    ("homerun.co", "Homerun"),
    ("factorialhr.com", "Factorial"),
    ("keka.com", "Keka"),
    ("trinethire.com", "TriNet Hire"),
    ("oraclecloud.com", "Oracle Recruiting"),
    ("jobs.gusto.com", "Gusto"),
    ("wellfound.com", "Wellfound"),
    ("workatastartup.com", "Y Combinator"),
    ("builtin.com", "Built In"),
    ("remotive.com", "Remotive"),
    ("remoterocketship.com", "Remote Rocketship"),
]

# Composite presets: multi-domain OR groups and wildcard site: operators.
_COMPOSITE_ATS = [
    {
        "key": "rippling",
        "label": "Rippling",
        "query": "(site:rippling.com OR site:rippling-ats.com)",
        "hosts": ["rippling.com", "rippling-ats.com"],
    },
    {
        "key": "adp",
        "label": "ADP",
        "query": "(site:workforcenow.adp.com OR site:myjobs.adp.com)",
        "hosts": ["adp.com"],
    },
    {
        "key": "linkedin-jobs",
        "label": "LinkedIn Jobs",
        "query": "site:linkedin.com/jobs/view",
        "hosts": ["linkedin.com"],
    },
    {
        "key": "careers-pages",
        "label": "Careers pages (any company)",
        "query": "(site:careers.* OR site:*/careers/* OR site:*/career/*)",
        "hosts": None,
    },
    {
        "key": "job-pages",
        "label": "Job / opening pages (any company)",
        "query": (
            "(site:*/employment/* OR site:*/opportunities/* OR site:*/openings/* "
            "OR site:*/join-us/* OR site:*/work-with-us/*)"
        ),
        "hosts": None,
    },
]

ATS_PRESETS = [
    {"key": d, "label": lbl, "query": f"site:{d}", "hosts": [d]}
    for d, lbl in _SIMPLE_ATS
] + _COMPOSITE_ATS

ATS_BY_KEY = {p["key"]: p for p in ATS_PRESETS}


def get_source(key: str) -> dict:
    """Resolve a selected source key to its preset. Unknown keys (custom
    domains the user typed) fall back to a bare ``site:<key>`` search."""
    if key in ATS_BY_KEY:
        return ATS_BY_KEY[key]
    return {"key": key, "label": key, "query": f"site:{key}", "hosts": [key]}

DEFAULT_CONFIG = {
    # A list of keyword *sets*. Each set is a list of {term, exact} and becomes
    # its own search; every set is run against every selected source, so the
    # total queries per run = len(keyword_sets) x len(ats_sites). exact=True
    # wraps a term in quotes (exact-phrase match); exact=False is a loose term.
    "keyword_sets": [
        [
            {"term": "Software Engineer", "exact": True},
            {"term": "San Francisco", "exact": True},
            {"term": "remote", "exact": False},
        ],
    ],
    "freshness": "week",
    "ats_sites": ["greenhouse.io"],
    "results_db": "results/jobs.db",
    # Fetch backend: "scrapingbee" (API, never blocked) or "playwright"
    # (local headless Chromium).
    "provider": "playwright",
    "scrapingbee_api_key": "",
    # Playwright-only knobs:
    "headless": True,
    "max_results_per_query": 30,
    "min_delay_seconds": 3.0,
    "max_delay_seconds": 8.0,
    # Lightweight auth for the hosted UI (see app.py). Deliberately simple:
    # va_password unlocks the assistant view only; admin_password unlocks the
    # full console when accessed through a tunnel (direct localhost access is
    # always treated as admin). CHANGE THESE if you expose the app publicly.
    "va_password": "jobapply",
    "admin_password": "jobadmin",
    # Optional ngrok settings used by start_job_scrape.ps1 (not by the app):
    # a reserved domain gives you a permanent public link for your assistant.
    "ngrok_domain": "",
    "ngrok_authtoken": "",
    # LLM title matching via a local Ollama model (see matcher.py). After each
    # scrape, new leads are classified against the first keyword of every
    # search set; non-matching jobs are hidden by default in the UI.
    "llm_match_enabled": True,
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5:14b",
    # Simplify job-search scraping (see simplify_search.py). The filter is a
    # Typesense filter_by string copied from simplify.jobs' own search
    # requests; __SINCE__ becomes the epoch N days ago. The API key is the
    # public search-only key Simplify ships in its frontend.
    "simplify_search_enabled": True,
    # Paste your simplify.jobs/jobs?... search URL (with all filters set) here
    # or in the UI; it is parsed into the Typesense filter automatically and
    # takes precedence over simplify_filter below.
    "simplify_url": (
        "https://simplify.jobs/jobs?type=Full-Time&experience_level=Mid+Level"
        "&experience_level=Senior&functions=Backend+Engineering"
        "&functions=Frontend+Engineering&functions=Full-Stack+Engineering"
        "&functions=Software+Engineering&min_salary=180000&posted_within_days=7"
        "&location=bbox%7E38.045138%2C-122.521992%2C37.177517%2C-121.767631"
        "%7ESan+Francisco+Bay+Area&location=remote%7ERemote+in+USA"
    ),
    "simplify_posted_within_days": 7,
    "simplify_max_pages": 20,
    "simplify_filter": (
        "type:=[`Full-Time`] && experience_level:=[`Mid Level`, `Senior`] && "
        "functions:=[`Backend Engineering`, `Frontend Engineering`, "
        "`Full-Stack Engineering`, `Software Engineering`] && "
        "(min_salary:>=180000 && salary_period:=4) && "
        "updated_date:>=__SINCE__ && "
        "(geolocations:(38.045138, -122.521992, 38.045138, -121.767631, "
        "37.177517, -121.767631, 37.177517, -122.521992) || "
        "locations:=[`Remote in USA`])"
    ),
    "simplify_typesense_key": (
        "SWF1ODFZbzBkcVlVdnVwT2FqUE5EZ3JpSk5hVmdpUHg1SklXWEdGbHZVRT1POHJieyJl"
        "eGNsdWRlX2ZpZWxkcyI6ImNvbXBhbnlfdXJsLGNhdGVnb3JpZXMsYWRkaXRpb25hbF9y"
        "ZXF1aXJlbWVudHMsY291bnRyaWVzLGRlZ3JlZXMsZ2VvbG9jYXRpb25zLGluZHVzdHJp"
        "ZXMsaXNfc2ltcGxlX2FwcGxpY2F0aW9uLGpvYl9saXN0cyxsZWFkZXJzaGlwX3R5cGUs"
        "c2VjdXJpdHlfY2xlYXJhbmNlLHNraWxscyx1cmwifQ=="
    ),
}


def get_scrapingbee_key(cfg: dict) -> str:
    """API key from env (preferred, keeps secrets out of files) or config."""
    return os.environ.get("SCRAPINGBEE_API_KEY") or cfg.get("scrapingbee_api_key", "")


def load_config() -> dict:
    """Load config.json, filling in any missing keys from DEFAULT_CONFIG.

    Migrates the legacy single ``keywords`` list into ``keyword_sets``.
    """
    data: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    cfg = {**DEFAULT_CONFIG, **data}
    if "keyword_sets" not in data and data.get("keywords"):
        cfg["keyword_sets"] = [data["keywords"]]
    cfg.pop("keywords", None)  # normalize away the legacy field
    if not cfg.get("flask_secret"):
        # Session-signing key; persisted so logins survive app restarts.
        cfg["flask_secret"] = secrets.token_hex(32)
        save_config(cfg)
    return cfg


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
