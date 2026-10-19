"""Programmatic Simplify autofill over the Chrome DevTools Protocol.

Drives the dedicated automation profile (autoapply/launch_chrome.cmd, CDP on
127.0.0.1:9222) where Simplify Copilot is installed and logged in. For each
job URL it:

  1. opens the URL in a new tab,
  2. focuses the window + tab via Simplify's own chrome.windows/tabs APIs
     (Simplify only fills VISIBLE pages - this is load-bearing),
  3. waits for the page to load and Simplify to detect/score the job,
  4. dispatches Simplify's own 'trigger-autofill' keyboard-command handler
     inside its service worker (identical to pressing Alt+Shift+F),
  5. verifies the fill by reading form-field values across the tab's frames.

No synthetic mouse or keyboard anywhere. Keep the automation window visible
and don't type while a run is in progress (your keystrokes would land in the
forms); the run never clicks, so watching is safe.

Usage:
    python autoapply/cdp_apply.py probe <url>        # verbose single-URL test
    python autoapply/cdp_apply.py run <url> [...]
    python autoapply/cdp_apply.py run --from-db 10

If Chrome isn't running with the debug port, it is launched automatically.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import websocket

CDP_HTTP = "http://127.0.0.1:9222"
REPO = Path(__file__).resolve().parent.parent
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

SETTLE_SEC = 12   # post-load wait: Simplify job detection + resume scoring (~8s)
DWELL_SEC = 40    # keep the tab focused this long after Start Application -
                  # Simplify keeps working (uploads, questions) well past the
                  # first filled fields, and backgrounding the tab throttles it


class CDP:
    """Minimal flat-session CDP client (blocking, single-threaded)."""

    def __init__(self, ws_url: str):
        # suppress_origin: Chrome 111+ rejects CDP websockets with an Origin header
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self._id = 0

    def send(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        self._id += 1
        msg: dict = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        deadline = time.time() + 30
        while time.time() < deadline:
            raw = json.loads(self.ws.recv())
            if raw.get("id") == self._id:
                if "error" in raw:
                    raise RuntimeError(f"{method}: {raw['error']}")
                return raw.get("result", {})
        raise TimeoutError(f"no response to {method}")

    def targets(self) -> list[dict]:
        return self.send("Target.getTargets")["targetInfos"]

    def attach(self, target_id: str) -> str:
        return self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]

    def eval(self, session_id: str, expr: str, *, gesture: bool = False, await_promise: bool = True):
        r = self.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "userGesture": gesture,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
            session_id,
        )
        if "exceptionDetails" in r:
            desc = r["exceptionDetails"].get("exception", {}).get("description", str(r["exceptionDetails"]))
            raise RuntimeError(f"JS failed: {desc[:300]}")
        return r.get("result", {}).get("value")


def connect(auto_launch: bool = True) -> CDP:
    for attempt in range(2):
        try:
            with urllib.request.urlopen(f"{CDP_HTTP}/json/version", timeout=4) as r:
                ws_url = json.loads(r.read())["webSocketDebuggerUrl"]
            return CDP(ws_url)
        except (urllib.error.URLError, OSError):
            if not auto_launch or attempt:
                raise
            print("  automation Chrome not running - launching it...")
            subprocess.Popen([
                CHROME,
                f"--user-data-dir={REPO / '.autoapply-profile'}",
                "--remote-debugging-port=9222",
                "--no-first-run",
                "--no-default-browser-check",
            ])
            time.sleep(6)
    raise RuntimeError("could not reach CDP")


def find_simplify(cdp: CDP, retries: int = 5) -> tuple[str, str]:
    """Return (extension_id, sw_session_id); the attached session keeps the
    MV3 service worker alive for the rest of the run."""
    for _ in range(retries):
        for t in cdp.targets():
            if t["type"] != "service_worker" or not t["url"].startswith("chrome-extension://"):
                continue
            ext_id = t["url"].split("/")[2]
            try:
                sid = cdp.attach(t["targetId"])
                name = cdp.eval(sid, "chrome.runtime.getManifest().name", await_promise=False)
            except Exception:
                continue
            if name and "simplify" in str(name).lower():
                return ext_id, sid
        time.sleep(2)
    raise RuntimeError(
        "Simplify's service worker not found. Install + log in to Simplify in "
        "the automation profile (autoapply/launch_chrome.cmd), open any job "
        "page once, then retry."
    )


def open_tab(cdp: CDP, url: str) -> tuple[str, str]:
    tid = cdp.send("Target.createTarget", {"url": url})["targetId"]
    sid = cdp.attach(tid)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if cdp.eval(sid, "document.readyState", await_promise=False) == "complete":
                break
        except Exception:
            pass
        time.sleep(0.5)
    return tid, sid


def close_duplicates(cdp: CDP, url: str) -> None:
    """Close any already-open tab with this URL so lookups are unambiguous."""
    bare = url.split("://", 1)[-1].split("#")[0]
    for t in cdp.targets():
        if t["type"] == "page" and t["url"].split("://", 1)[-1].split("#")[0] == bare:
            try:
                cdp.send("Target.closeTarget", {"targetId": t["targetId"]})
            except Exception:
                pass


def focus_tab(cdp: CDP, sw_sid: str, target_id: str, url: str) -> int:
    """Make this exact tab active + its window focused (Simplify only fills
    visible pages). Returns the extension-API tab id, resolved by activating
    the CDP target and asking which tab is now active - immune to
    duplicate-URL tabs."""
    cdp.send("Target.activateTarget", {"targetId": target_id})
    time.sleep(0.5)
    bare = url.split("://", 1)[-1].split("#")[0][:80]
    js = (
        "(async () => { const bare = " + json.dumps(bare) + "; "
        "const strip = (u) => (u || '').replace(/^https?:\\/\\//, ''); "
        "const ts = await chrome.tabs.query({active: true}); "
        "const t = ts.find(t => strip(t.url).startsWith(bare)) || "
        "(ts.length === 1 ? ts[0] : null); "
        "if (!t) return null; "
        "await chrome.windows.update(t.windowId, {focused: true}); "
        "return t.id; })()"
    )
    tab_id = cdp.eval(sw_sid, js, gesture=True)
    if tab_id is None:
        raise RuntimeError("could not resolve the active tab in the automation profile")
    return int(tab_id)


def dispatch_autofill(cdp: CDP, sw_sid: str, tab_id: int) -> None:
    """Fire Simplify's own trigger-autofill command handler (= Alt+Shift+F)."""
    js = (
        "(async () => { const t = await chrome.tabs.get(" + str(tab_id) + "); "
        "chrome.commands.onCommand.dispatch('trigger-autofill', t); "
        "return 'dispatched'; })()"
    )
    cdp.eval(sw_sid, js, gesture=True)


# If the page shows only a job description, click its Apply button to reveal
# the form (job-boards.greenhouse.io, Ashby, company pages); no-op when a
# form is already present (Lever /apply, embeds).
ENSURE_FORM = """(() => {
  const hasInput = !!document.querySelector(
    'input[type=text], input[type=email], input[type=tel], input:not([type])');
  if (hasInput) return 'form-present';
  const el = [...document.querySelectorAll('button, a[role=button], a')].find(e => {
    const t = (e.innerText || '').trim().toLowerCase();
    return /^apply( now| for this job| for job)?$/.test(t) && e.offsetParent !== null;
  });
  if (el) { el.click(); return 'clicked-apply'; }
  return 'no-form-no-button';
})()"""


FIELD_PROBE = (
    "JSON.stringify([...document.querySelectorAll("
    "'input[type=text],input[type=email],input[type=tel],input:not([type])')]"
    ".filter(i => i.value && i.value.trim() && !i.value.startsWith('http'))"  # share-link widgets aren't fills
    ".slice(0, 6).map(i => ({n: (i.name || i.id || '?').slice(0, 30), v: i.value.slice(0, 30)})))"
)


def read_filled_fields(cdp: CDP, tab_sid: str, pre_iframes: set[str]) -> list[dict]:
    """Collect non-empty text inputs from the tab's main frame and any iframe
    targets that appeared after this tab was opened (its embeds)."""
    filled: list[dict] = []
    try:
        filled += json.loads(cdp.eval(tab_sid, FIELD_PROBE, await_promise=False) or "[]")
    except Exception:
        pass
    for t in cdp.targets():
        if t["type"] == "iframe" and t["targetId"] not in pre_iframes:
            try:
                fsid = cdp.attach(t["targetId"])
                filled += json.loads(cdp.eval(fsid, FIELD_PROBE, await_promise=False) or "[]")
            except Exception:
                continue
    return filled


def process(cdp: CDP, sw_sid: str, url: str, verbose: bool = False, dwell: int = DWELL_SEC) -> tuple[str, list[dict]]:
    def log(m):
        if verbose:
            print(f"    {m}")

    close_duplicates(cdp, url)
    pre_iframes = {t["targetId"] for t in cdp.targets() if t["type"] == "iframe"}
    tid, tab_sid = open_tab(cdp, url)
    log("tab open + loaded")
    tab_id = focus_tab(cdp, sw_sid, tid, url)
    log(f"window focused, tab {tab_id} active")
    time.sleep(SETTLE_SEC)
    log(f"settled {SETTLE_SEC}s (job detection)")
    try:
        form_state = cdp.eval(tab_sid, ENSURE_FORM, await_promise=False)
    except Exception:
        form_state = "probe-failed"
    log(f"form check: {form_state}")
    if form_state == "clicked-apply":
        time.sleep(5)  # form render + Simplify re-detection
    dispatch_autofill(cdp, sw_sid, tab_id)
    log(f"trigger-autofill dispatched; dwelling {dwell}s so Simplify can finish")

    # Fixed dwell: the tab must stay focused the whole time, even after the
    # first fields appear — moving on early throttles the still-filling page.
    fields: list[dict] = []
    deadline = time.time() + dwell
    while time.time() < deadline:
        time.sleep(min(5, max(0.5, deadline - time.time())))
        if not fields:
            fields = read_filled_fields(cdp, tab_sid, pre_iframes)
    if not fields:
        fields = read_filled_fields(cdp, tab_sid, pre_iframes)
    status = "filled" if fields else "no-fill"
    log(f"{status}: {json.dumps(fields)[:200]}")
    return status, fields


def urls_from_db(limit: int) -> list[str]:
    from pick_urls import pick  # shared picker (skips login-gated platforms)
    return pick(limit)


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("probe", "run"):
        print(__doc__)
        return 1
    mode, rest = args[0], args[1:]

    dwell = DWELL_SEC
    if "--dwell" in rest:
        i = rest.index("--dwell")
        dwell = int(rest[i + 1])
        rest = rest[:i] + rest[i + 2:]
    urls: list[str] = []
    if "--from-db" in rest:
        i = rest.index("--from-db")
        urls = urls_from_db(int(rest[i + 1]))
        rest = rest[:i] + rest[i + 2:]
    urls = [u for u in rest if u.startswith("http")] + urls
    if not urls:
        print("no URLs given")
        return 1

    cdp = connect()
    _, sw_sid = find_simplify(cdp)

    if mode == "probe":
        print(f"PROBE: {urls[0]}")
        status, fields = process(cdp, sw_sid, urls[0], verbose=True, dwell=dwell)
        print(f"PROBE RESULT: {status}")
        return 0 if status == "filled" else 2

    print(f"{len(urls)} job(s). Keep the automation window visible; don't type during the run.\n")
    ok = 0
    for n, url in enumerate(urls, 1):
        try:
            status, fields = process(cdp, sw_sid, url, dwell=dwell)
        except Exception as e:  # noqa: BLE001 - keep the batch going
            status, fields = f"error: {str(e)[:140]}", []
        sample = ", ".join(f"{f['n']}={f['v']}" for f in fields[:2])
        mark = "ok " if status == "filled" else "!! "
        print(f"[{n}/{len(urls)}] {mark}{status:<10} {sample[:60]:<60} {url[:70]}")
        ok += int(status == "filled")

    print(f"\ndone: {ok}/{len(urls)} autofilled; tabs left open - review and SUBMIT each one yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
