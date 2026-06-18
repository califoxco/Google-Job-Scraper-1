"""Build Google search URLs from a config.

One URL is produced per ATS site so each ``site:`` filter gets its own
result page (Google gives poor coverage when you OR several site: filters
into a single query).
"""

from __future__ import annotations

import urllib.parse

from .config import FRESHNESS_OPTIONS

GOOGLE_SEARCH = "https://www.google.com/search"


def build_query_string(keywords: list[dict], ats_site: str) -> str:
    """Assemble the q= value.

    Ordering mirrors the reference URL: exact phrases first, then the
    site: filter, then loose terms -> e.g.
        "Software Engineer" "San Francisco" site:greenhouse.io remote
    """
    exact_parts: list[str] = []
    plain_parts: list[str] = []
    for kw in keywords:
        term = (kw.get("term") or "").strip()
        if not term:
            continue
        if kw.get("exact"):
            exact_parts.append(f'"{term}"')
        else:
            plain_parts.append(term)

    parts = list(exact_parts)
    if ats_site:
        parts.append(f"site:{ats_site}")
    parts.extend(plain_parts)
    return " ".join(parts)


def build_url(keywords: list[dict], ats_site: str, freshness: str) -> str:
    q = build_query_string(keywords, ats_site)
    params = {"q": q, "hl": "en", "gl": "us"}
    qdr = FRESHNESS_OPTIONS.get(freshness, {}).get("qdr")
    if qdr:
        params["tbs"] = f"qdr:{qdr}"
    # quote (not quote_plus) so spaces encode as %20, matching the example URL.
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{GOOGLE_SEARCH}?{query}"


def build_all_urls(cfg: dict) -> list[dict]:
    """Return [{ats, url}, ...] — one entry per configured ATS site."""
    urls: list[dict] = []
    sites = cfg.get("ats_sites") or [""]
    freshness = cfg.get("freshness", "week")
    for site in sites:
        urls.append({"ats": site, "url": build_url(cfg["keywords"], site, freshness)})
    return urls
