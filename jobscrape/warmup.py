"""Open a real (headed) Chromium on the shared persistent profile so you can
solve a Google CAPTCHA / accept consent ONCE by hand.

    python -m jobscrape.warmup

A browser window opens on Google. Solve any challenge, then close the
window. The cookies are saved to .browser-profile/ and reused by later
headless runs (python -m jobscrape.run), which then stop getting blocked.
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright

from .scraper import PROFILE_DIR, USER_AGENT

WARMUP_URL = "https://www.google.com/search?q=site:greenhouse.io+software+engineer&hl=en&gl=us"


def main() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print("Opening a browser window. Solve any CAPTCHA / consent prompt,")
    print("then CLOSE the window to save the session.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent=USER_AGENT,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(WARMUP_URL, wait_until="domcontentloaded")
        # Wait until the user closes the window (up to 15 minutes).
        try:
            ctx.wait_for_event("close", timeout=15 * 60 * 1000)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    print("Session saved. Now run: python -m jobscrape.run")


if __name__ == "__main__":
    main()
