"""Playwright-based Google scraper.

Uses a *persistent* browser profile (.browser-profile/) so cookies — and
any CAPTCHA you solve once in headed mode — carry over to later headless
cron runs. Includes light stealth, human-like delays, consent-page
handling, and block detection.
"""

from __future__ import annotations

import random
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from .config import PROJECT_ROOT, resolve_db_path
from .query import build_all_urls
from .storage import Storage

PROFILE_DIR = PROJECT_ROOT / ".browser-profile"

# Runs in the page: collect every result anchor that wraps an <h3>.
RESULT_JS = r"""
() => {
  const results = [];
  const seen = new Set();
  document.querySelectorAll('a:has(h3)').forEach(a => {
    const h3 = a.querySelector('h3');
    if (!h3) return;
    const href = a.href || '';
    if (!href || seen.has(href)) return;
    seen.add(href);
    const title = (h3.innerText || '').trim();
    let snippet = '';
    const c = a.closest('div.g, div.tF2Cxc, div.MjjYud') || a.parentElement;
    if (c) {
      const sn = c.querySelector('.VwiC3b, .lEBKkf, div[data-sncf]');
      if (sn) snippet = (sn.innerText || '').trim();
    }
    results.push({ title, href, snippet });
  });
  return results;
}
"""

CONSENT_SELECTORS = [
    "#L2AGLb",  # Google's "Accept all" consent button id
    'button:has-text("Accept all")',
    'button:has-text("I agree")',
    'button:has-text("Accept")',
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _unwrap(href: str) -> str:
    """Unwrap Google's /url?q=... redirect links to the real destination."""
    try:
        p = urlparse(href)
        if p.path == "/url":
            q = parse_qs(p.query).get("q")
            if q:
                return q[0]
    except Exception:
        pass
    return href


def _is_ats_link(href: str, ats: str) -> bool:
    """Keep only real result links on the target ATS host (drop google.com,
    accounts, support, etc.)."""
    if not href:
        return False
    try:
        host = urlparse(href).netloc.lower()
    except Exception:
        return False
    if not host or "google." in host:
        return False
    return ats.lower() in host if ats else True


def _maybe_consent(page) -> None:
    for sel in CONSENT_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click(timeout=2000)
                page.wait_for_timeout(800)
                return
        except Exception:
            continue


def _looks_blocked(page) -> bool:
    url = (page.url or "").lower()
    if "/sorry/" in url or "sorry/index" in url:
        return True
    try:
        if page.locator('form#captcha-form, iframe[src*="recaptcha"]').count() > 0:
            return True
    except Exception:
        pass
    return False


def run_scrape(cfg: dict, progress=None) -> dict:
    """Run every configured query, store results, return a summary dict.

    ``progress`` is an optional callback(str) for live UI logging; when
    omitted, messages print to stdout (good for cron logs).
    """

    def log(msg: str) -> None:
        (progress or print)(msg)

    db_path = resolve_db_path(cfg)
    store = Storage(db_path)
    run_id = store.start_run()

    targets = build_all_urls(cfg)
    headless = bool(cfg.get("headless", True))
    max_results = int(cfg.get("max_results_per_query", 30))
    min_delay = float(cfg.get("min_delay_seconds", 3.0))
    max_delay = float(cfg.get("max_delay_seconds", 8.0))

    total_found = total_new = blocked_count = 0
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent=USER_AGENT,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        for i, t in enumerate(targets):
            label = t["ats"] or "no-site"
            log(f"[{i + 1}/{len(targets)}] {label}")
            try:
                page.goto(t["url"], wait_until="domcontentloaded", timeout=30000)
                _maybe_consent(page)
                page.wait_for_timeout(1200)

                if _looks_blocked(page):
                    blocked_count += 1
                    log(
                        "  ! BLOCKED / CAPTCHA. Run once with headless OFF to "
                        "solve it; the profile is saved and reused after that."
                    )
                    time.sleep(random.uniform(min_delay, max_delay))
                    continue

                try:
                    page.wait_for_selector("a:has(h3)", timeout=8000)
                except Exception:
                    pass

                items = page.evaluate(RESULT_JS) or []
                kept = 0
                for it in items:
                    href = _unwrap(it.get("href", ""))
                    if not _is_ats_link(href, t["ats"]):
                        continue
                    if kept >= max_results:
                        break
                    kept += 1
                    total_found += 1
                    if store.upsert_job(
                        url=href,
                        title=it.get("title", ""),
                        snippet=it.get("snippet", ""),
                        ats=t["ats"],
                        query=t["url"],
                    ):
                        total_new += 1
                log(f"  -> {kept} job links ({total_new} new this run so far)")
            except Exception as e:  # noqa: BLE001 - report and move on
                log(f"  x error: {e}")

            if i < len(targets) - 1:
                time.sleep(random.uniform(min_delay, max_delay))

        context.close()

    note = "ok" if blocked_count == 0 else f"{blocked_count} queries blocked"
    store.finish_run(
        run_id,
        queries=len(targets),
        found=total_found,
        new_jobs=total_new,
        blocked=blocked_count,
        note=note,
    )
    store.close()

    return {
        "queries": len(targets),
        "found": total_found,
        "new": total_new,
        "blocked": blocked_count,
        "db": str(db_path),
    }
