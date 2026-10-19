"""Scrape job leads from Simplify's job search (no login required).

Simplify's search UI is backed by a public Typesense cluster
(js-ha.simplify.jobs). The frontend ships a scoped search-only API key, so we
can page through the same filtered results the website shows. The search
response deliberately excludes the external application URL, but every
posting has an unauthenticated tracked redirect:

    GET https://simplify.jobs/jobs/click/<posting_id>   ->  307 Location: <ATS url>

We resolve that per NEW posting, clean the URL (drop embed=true, swap
utm_source), and upsert into the same jobs table the Google scraper uses —
so dedupe, LLM title matching, the tracker, and autoapply all apply as-is.

Configure via config.json:
    simplify_search_enabled   bool
    simplify_filter           Typesense filter_by; __SINCE__ is replaced with
                              the epoch for simplify_posted_within_days ago
    simplify_posted_within_days, simplify_max_pages, simplify_typesense_key

Standalone run:  python -m jobscrape.simplify_search
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import load_config, resolve_db_path
from .storage import Storage

SEARCH_URL = "https://js-ha.simplify.jobs/multi_search?use_cache=true&cache_ttl=120"
CLICK_URL = "https://simplify.jobs/jobs/click/{pid}"
PER_PAGE = 21
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"

# Map final-URL hosts to the same ats tokens the Google scraper uses.
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "workable.com", "breezy.hr", "recruitee.com", "icims.com",
    "smartrecruiters.com", "jobvite.com", "bamboohr.com", "rippling.com",
    "teamtailor.com", "wellfound.com",
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D102
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _search_page(cfg: dict, filter_by: str, page: int) -> dict:
    body = json.dumps({
        "searches": [{
            "collection": "jobs",
            "q": "*",
            "query_by": "title,company_name,functions,locations",
            "sort_by": "_text_match:desc,shuffle_key:asc,posting_id:desc",
            "page": page,
            "per_page": PER_PAGE,
            "filter_by": filter_by,
        }]
    }).encode()
    req = urllib.request.Request(
        SEARCH_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Origin": "https://simplify.jobs",
            "Referer": "https://simplify.jobs/",
            "User-Agent": UA,
            "x-typesense-api-key": cfg["simplify_typesense_key"],
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["results"][0]


def _resolve_click(pid: str) -> str | None:
    req = urllib.request.Request(
        CLICK_URL.format(pid=pid), headers={"User-Agent": UA}
    )
    try:
        _opener.open(req, timeout=20)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.headers.get("Location")
        return None
    except Exception:
        return None
    return None  # 200 = no external link


def _clean_url(url: str) -> str:
    """Drop embed=true (iframe rendering) and de-brand tracking params."""
    p = urllib.parse.urlsplit(url)
    cleaned = []
    for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
        if k == "embed":
            continue
        if v.lower() == "simplify":  # utm_source, gh_src, src, ref, ...
            v = "google"
        cleaned.append((k, v))
    return urllib.parse.urlunsplit(
        (p.scheme, p.netloc, p.path, urllib.parse.urlencode(cleaned), "")
    )


def _ats_token(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for known in ATS_HOSTS:
        if host.endswith(known):
            return known
    return host.removeprefix("www.") or "simplify"


def _snippet(doc: dict) -> str:
    bits = [doc.get("company_name") or ""]
    if doc.get("locations"):
        bits.append(" / ".join(doc["locations"][:3]))
    lo, hi = doc.get("min_salary"), doc.get("max_salary")
    if lo and hi:
        bits.append(f"${lo:,.0f}-${hi:,.0f}")
    if doc.get("experience_level"):
        bits.append(", ".join(doc["experience_level"]))
    return " | ".join(b for b in bits if b)


def run_simplify_search(store: Storage, cfg: dict, progress=None) -> dict:
    """Page through the configured Simplify search and upsert new leads."""
    log = progress or print
    if not cfg.get("simplify_search_enabled", False):
        return {"found": 0, "new": 0, "skipped": "disabled"}

    since = int(time.time()) - int(cfg.get("simplify_posted_within_days", 7)) * 86400
    filter_by = cfg["simplify_filter"].replace("__SINCE__", str(since))
    max_pages = int(cfg.get("simplify_max_pages", 20))

    seen_ids: set[str] = set()
    found = new = resolved = no_link = 0
    page = 1
    while page <= max_pages:
        try:
            result = _search_page(cfg, filter_by, page)
        except Exception as e:  # noqa: BLE001
            log(f"[simplify] search page {page} failed: {e}")
            break
        hits = result.get("hits", [])
        total = result.get("found", 0)
        if page == 1:
            log(f"[simplify] {total} postings match the filter")
        if not hits:
            break

        for h in hits:
            doc = h["document"]
            pid = doc.get("posting_id") or doc.get("id")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            found += 1

            # Already resolved on an earlier run? (query field stores the pid)
            row = store.conn.execute(
                "SELECT 1 FROM jobs WHERE query = ?", (f"simplify:{pid}",)
            ).fetchone()
            if row:
                continue

            ext = _resolve_click(pid)
            time.sleep(0.25)  # politeness
            if not ext:
                no_link += 1
                continue
            resolved += 1
            url = _clean_url(ext)
            if store.upsert_job(
                url=url,
                title=doc.get("title", ""),
                snippet=_snippet(doc),
                ats=_ats_token(url),
                query=f"simplify:{pid}",
            ):
                new += 1

        log(f"[simplify] page {page}: {found} seen, {new} new so far")
        if page * PER_PAGE >= total:
            break
        page += 1

    log(f"[simplify] done: {new} new leads ({resolved} resolved, {no_link} without external link)")
    return {"found": found, "new": new, "resolved": resolved, "no_link": no_link}


if __name__ == "__main__":
    _cfg = load_config()
    _store = Storage(resolve_db_path(_cfg))
    print(run_simplify_search(_store, _cfg))
    _store.close()
