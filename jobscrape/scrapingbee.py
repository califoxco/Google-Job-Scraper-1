"""ScrapingBee Google Search API backend.

Returns clean structured results (no HTML parsing, no CAPTCHAs) via
ScrapingBee's managed proxies. Docs: https://www.scrapingbee.com/documentation/google/
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .config import FRESHNESS_OPTIONS

ENDPOINT = "https://app.scrapingbee.com/api/v1/google"


def search(query: str, freshness: str, api_key: str, num: int = 30) -> list[dict]:
    """Run one Google search via ScrapingBee.

    Returns [{title, href, snippet}, ...] from organic_results.
    Raises RuntimeError on HTTP / quota errors so the caller can log it.
    """
    params = {"search": query, "country_code": "us", "nb_results": num}
    qdr = FRESHNESS_OPTIONS.get(freshness, {}).get("qdr")
    if qdr:
        # Pass Google's time filter through verbatim (tbs=qdr:w / d3 / d).
        params["extra_params"] = f"tbs=qdr:{qdr}"

    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})

    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"ScrapingBee HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"ScrapingBee network error: {e.reason}") from e

    results: list[dict] = []
    for it in data.get("organic_results", []) or []:
        results.append(
            {
                "title": it.get("title", "") or "",
                "href": it.get("url", "") or "",
                "snippet": it.get("description", "") or "",
            }
        )
    return results
