"""Build Google search URLs from a config.

One URL is produced per ATS site so each ``site:`` filter gets its own
result page (Google gives poor coverage when you OR several site: filters
into a single query).
"""

from __future__ import annotations

import urllib.parse

from .config import FRESHNESS_OPTIONS, get_source

GOOGLE_SEARCH = "https://www.google.com/search"


def build_query_string(keywords: list[dict], source_query: str) -> str:
    """Assemble the q= value.

    Ordering mirrors the reference URL: exact phrases first, then the source
    fragment (e.g. ``site:greenhouse.io`` or an OR/wildcard group), then loose
    terms -> e.g.
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
    if source_query:
        parts.append(source_query)
    parts.extend(plain_parts)
    return " ".join(parts)


def build_url(keywords: list[dict], source_query: str, freshness: str) -> str:
    q = build_query_string(keywords, source_query)
    params = {"q": q, "hl": "en", "gl": "us"}
    qdr = FRESHNESS_OPTIONS.get(freshness, {}).get("qdr")
    if qdr:
        params["tbs"] = f"qdr:{qdr}"
    # quote (not quote_plus) so spaces encode as %20, matching the example URL.
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{GOOGLE_SEARCH}?{query}"


def set_label(keywords: list[dict]) -> str:
    """Short label for a keyword set (its first term), for logs/preview."""
    for kw in keywords:
        term = (kw.get("term") or "").strip()
        if term:
            return term
    return "(empty)"


def keyword_sets(cfg: dict) -> list[list[dict]]:
    """Normalize config to a list of keyword sets (handles the legacy key)."""
    sets = cfg.get("keyword_sets")
    if not sets:
        sets = [cfg.get("keywords") or []]
    return sets


def build_all_urls(cfg: dict) -> list[dict]:
    """Return [{ats, label, hosts, q, url}, ...] — one entry per
    keyword-set x source combination.

    ``ats`` is the source key (stored on each job), ``hosts`` is its result
    filter, ``q`` is the raw search string (ScrapingBee), and ``url`` is the
    full Google URL (Playwright + UI preview).
    """
    urls: list[dict] = []
    keys = cfg.get("ats_sites") or [""]
    freshness = cfg.get("freshness", "week")
    for kw in keyword_sets(cfg):
        slabel = set_label(kw)
        for key in keys:
            src = get_source(key) if key else {"key": "", "label": "", "query": "", "hosts": None}
            urls.append(
                {
                    "ats": src["key"] or key,
                    "label": f"{slabel} · {src['label']}" if src["label"] else slabel,
                    "hosts": src["hosts"],
                    "q": build_query_string(kw, src["query"]),
                    "url": build_url(kw, src["query"], freshness),
                }
            )
    return urls
