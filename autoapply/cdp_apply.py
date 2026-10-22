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
    python autoapply/cdp_apply.py submit --from-db 50 [--dry-run]
        one job at a time: autofill, click Simplify's Submit Application,
        close the tab, mark the row applied - or flag it 'needs human
        attention' (never retried) if anything is uncertain

If Chrome isn't running with the debug port, it is launched automatically.
"""

from __future__ import annotations

import json
import re
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


# --- submit mode ----------------------------------------------------------
# Simplify renders its in-page panel inside a shadow root, invisible to
# document.querySelector; CDP's piercing DOM search reaches it.

AUTOFILL_WAIT_SEC = 75     # max wait for the panel to report "Autofill complete!"
POST_FILL_BUFFER_SEC = 5   # let uploads/late fields settle before submitting
CONFIRM_WAIT_SEC = 25      # max wait for a confirmation after clicking Submit
CLOSE_DELAY_SEC = 3        # keep the tab visible briefly before closing it

REQUIRED_RE = re.compile(r"Required \((\d+) of (\d+) filled\)")
CONFIRM_RE = re.compile(
    r"application (?:was |has been )?(?:successfully )?(?:submitted|received|sent)"
    r"|thank(?:s| you)[^.\n]{0,40}(?:apply|application)"
    r"|successfully (?:submitted|applied)"
    r"|(?:we(?:'|’)ve|we have) received your application"
    r"|you(?:'|’)ve (?:successfully )?applied",
    re.I,
)
CONFIRM_URL_RE = re.compile(r"confirm|thank|success|submitted|complete", re.I)

ROOT_TEXT_FN = (
    "function () { return (this.getRootNode().textContent || '')"
    ".replace(/\\s+/g, ' ').trim(); }"
)


def _find_node(cdp: CDP, sid: str, selector: str) -> int | None:
    """Piercing DOM search (reaches Simplify's shadow-root panel)."""
    try:
        cdp.send("DOM.getDocument", {"depth": -1, "pierce": True}, sid)
        r = cdp.send(
            "DOM.performSearch",
            {"query": selector, "includeUserAgentShadowDOM": True},
            sid,
        )
        try:
            if not r["resultCount"]:
                return None
            ids = cdp.send(
                "DOM.getSearchResults",
                {"searchId": r["searchId"], "fromIndex": 0, "toIndex": 1},
                sid,
            )["nodeIds"]
            return ids[0] if ids else None
        finally:
            cdp.send("DOM.discardSearchResults", {"searchId": r["searchId"]}, sid)
    except Exception:
        return None


def _call_on_node(cdp: CDP, sid: str, node_id: int, fn: str):
    obj = cdp.send("DOM.resolveNode", {"nodeId": node_id}, sid)["object"]["objectId"]
    r = cdp.send(
        "Runtime.callFunctionOn",
        {"objectId": obj, "functionDeclaration": fn, "returnByValue": True},
        sid,
    )
    return r.get("result", {}).get("value")


def _panel_text(cdp: CDP, sids: list[str]) -> tuple[str | None, str]:
    """Locate Simplify's panel (by its buttons) in the tab or its frames.
    Returns (session_id_holding_it, panel_text)."""
    for sid in sids:
        for sel in ("#proxy-submit-button", "#fill-button"):
            node = _find_node(cdp, sid, sel)
            if node:
                try:
                    return sid, _call_on_node(cdp, sid, node, ROOT_TEXT_FN) or ""
                except Exception:
                    continue
    return None, ""


def _page_texts(cdp: CDP, tab_sid: str, pre_iframes: set[str]) -> tuple[str, str]:
    """(current tab URL, visible text of the tab + its new frames)."""
    url, chunks = "", []
    try:
        url = cdp.eval(tab_sid, "location.href", await_promise=False) or ""
        chunks.append(cdp.eval(tab_sid, "document.body ? document.body.innerText : ''", await_promise=False) or "")
    except Exception:
        pass
    for t in cdp.targets():
        if t["type"] == "iframe" and t["targetId"] not in pre_iframes:
            try:
                fsid = cdp.attach(t["targetId"])
                chunks.append(cdp.eval(fsid, "document.body ? document.body.innerText : ''", await_promise=False) or "")
            except Exception:
                continue
    return url, "\n".join(chunks)


def _frame_sessions(cdp: CDP, tab_sid: str, pre_iframes: set[str]) -> list[str]:
    sids = [tab_sid]
    for t in cdp.targets():
        if t["type"] == "iframe" and t["targetId"] not in pre_iframes:
            try:
                sids.append(cdp.attach(t["targetId"]))
            except Exception:
                continue
    return sids


def _trusted_click(cdp: CDP, sid: str, node_id: int) -> None:
    """Real mouse events at the element's center (isTrusted=true); falls back
    to element.click() if geometry is unavailable."""
    try:
        cdp.send("DOM.scrollIntoViewIfNeeded", {"nodeId": node_id}, sid)
        q = cdp.send("DOM.getBoxModel", {"nodeId": node_id}, sid)["model"]["content"]
        x = (q[0] + q[2] + q[4] + q[6]) / 4
        y = (q[1] + q[3] + q[5] + q[7]) / 4
        for ev in ("mouseMoved", "mousePressed", "mouseReleased"):
            cdp.send(
                "Input.dispatchMouseEvent",
                {"type": ev, "x": x, "y": y, "button": "left", "clickCount": 1},
                sid,
            )
    except Exception:
        _call_on_node(cdp, sid, node_id, "function () { this.click(); }")


def submit_one(cdp: CDP, sw_sid: str, url: str, dry_run: bool = False) -> tuple[str, str]:
    """Autofill + submit one application. Returns (outcome, detail) where
    outcome is 'applied', 'attention', or 'dry-ok'. Always closes the tab."""
    tid = None
    try:
        close_duplicates(cdp, url)
        pre_iframes = {t["targetId"] for t in cdp.targets() if t["type"] == "iframe"}
        tid, tab_sid = open_tab(cdp, url)
        tab_id = focus_tab(cdp, sw_sid, tid, url)
        time.sleep(SETTLE_SEC)
        try:
            if cdp.eval(tab_sid, ENSURE_FORM, await_promise=False) == "clicked-apply":
                time.sleep(5)
        except Exception:
            pass
        dispatch_autofill(cdp, sw_sid, tab_id)

        # 1. wait for Simplify's own "Autofill complete!" signal
        panel_sid, text = None, ""
        deadline = time.time() + AUTOFILL_WAIT_SEC
        while time.time() < deadline:
            time.sleep(3)
            panel_sid, text = _panel_text(cdp, _frame_sessions(cdp, tab_sid, pre_iframes))
            if "Autofill complete" in text:
                break
        if not panel_sid:
            return "attention", "Simplify did not recognize an application form on this page"
        if "Autofill complete" not in text:
            return "attention", f"autofill did not complete within {AUTOFILL_WAIT_SEC}s"
        time.sleep(POST_FILL_BUFFER_SEC)
        _, text = _panel_text(cdp, [panel_sid])

        # 2. safety gate: never submit with required answers missing
        m = REQUIRED_RE.search(text)
        if m and int(m.group(1)) < int(m.group(2)):
            got, need = int(m.group(1)), int(m.group(2))
            return "attention", f"{need - got} required field(s) unfilled ({got} of {need})"
        req = f"{m.group(1)}/{m.group(2)} required" if m else "required count n/a"

        btn = _find_node(cdp, panel_sid, "#proxy-submit-button")
        if not btn:
            return "attention", "no Submit Application button in Simplify's panel"
        if _call_on_node(cdp, panel_sid, btn, "function () { return !!this.disabled; }"):
            return "attention", "Submit Application button is disabled"
        if dry_run:
            return "dry-ok", f"ready to submit ({req}) - not clicked (dry run)"

        # 3. click, then require a NEW confirmation (text present before the
        # click - e.g. 'thank you for your interest' boilerplate - doesn't count)
        url0, page0 = _page_texts(cdp, tab_sid, pre_iframes)
        before = len(CONFIRM_RE.findall(page0))
        _trusted_click(cdp, panel_sid, btn)
        deadline = time.time() + CONFIRM_WAIT_SEC
        while time.time() < deadline:
            time.sleep(2.5)
            url1, page1 = _page_texts(cdp, tab_sid, pre_iframes)
            if url1 != url0 and CONFIRM_URL_RE.search(url1):
                return "applied", f"confirmation page ({req})"
            if len(CONFIRM_RE.findall(page1)) > before:
                return "applied", f"confirmation shown ({req})"
            _, ptext = _panel_text(cdp, [panel_sid])
            if CONFIRM_RE.search(ptext):
                return "applied", f"Simplify reported submitted ({req})"
        return "attention", "clicked Submit but saw no confirmation - verify manually (captcha/validation?)"
    except Exception as e:  # noqa: BLE001
        return "attention", f"error: {str(e)[:140]}"
    finally:
        if tid:
            time.sleep(CLOSE_DELAY_SEC)
            try:
                cdp.send("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass


def _ensure_keeper_tab(cdp: CDP) -> None:
    """Closing job tabs must never close the window's last tab (which would
    quit the automation Chrome mid-run)."""
    pages = [t for t in cdp.targets() if t["type"] == "page"]
    if not any(t["url"] in ("about:blank", "chrome://newtab/") for t in pages):
        cdp.send("Target.createTarget", {"url": "about:blank"})


def run_submit(cdp: CDP, sw_sid: str, urls: list[str], dry_run: bool) -> int:
    sys.path.insert(0, str(REPO))
    from jobscrape.config import load_config, resolve_db_path
    from jobscrape.storage import Storage

    store = Storage(resolve_db_path(load_config()))
    _ensure_keeper_tab(cdp)
    tag = " (DRY RUN - nothing will be submitted)" if dry_run else ""
    print(f"{len(urls)} job(s), one at a time{tag}. Don't type in the automation window.\n", flush=True)
    applied = attention = 0
    for n, url in enumerate(urls, 1):
        outcome, detail = submit_one(cdp, sw_sid, url, dry_run=dry_run)
        job_id = store.id_for_url(url)
        if outcome == "applied":
            applied += 1
            if job_id:
                store.mark_auto_applied(job_id)
        elif outcome == "attention":
            attention += 1
            if job_id and not dry_run:
                store.set_attention(job_id, detail)
        mark = {"applied": "ok ", "dry-ok": "-- "}.get(outcome, "!! ")
        print(f"[{n}/{len(urls)}] {mark}{outcome:<9} {detail[:70]:<70} {url[:60]}", flush=True)
    store.close()
    if dry_run:
        print(f"\ndry run done: {len(urls) - attention} ready, {attention} would need attention.")
    else:
        print(f"\ndone: {applied} submitted + marked applied, {attention} flagged 'needs human attention'.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("probe", "run", "submit"):
        print(__doc__)
        return 1
    mode, rest = args[0], args[1:]

    dry_run = "--dry-run" in rest
    rest = [a for a in rest if a != "--dry-run"]
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

    if mode == "submit":
        return run_submit(cdp, sw_sid, urls, dry_run)

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
