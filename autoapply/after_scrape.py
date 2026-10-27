"""Daily auto-apply, run by run.bat right after the scheduled scrape.

Submits the top N jobs of the tracker's default view (config key
`auto_apply_after_scrape`, default 50; 0 disables), one at a time, exactly
like the tracker's Start button. Needs an unlocked screen: if the pages
aren't visible the run stops without flagging anything.
"""
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

from cdp_apply import run_submit  # noqa: E402
from jobscrape.config import load_config  # noqa: E402
from pick_urls import pick  # noqa: E402


def main() -> int:
    n = int(load_config().get("auto_apply_after_scrape", 50) or 0)
    print(f"\n=== daily auto-apply {datetime.now():%Y-%m-%d %H:%M} ===", flush=True)
    if n <= 0:
        print("disabled (auto_apply_after_scrape = 0)")
        return 0
    urls = pick(n)
    if not urls:
        print("no eligible jobs to apply to")
        return 0
    return run_submit(urls, dry_run=False)


if __name__ == "__main__":
    sys.exit(main())
