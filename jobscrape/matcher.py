"""LLM job-title matching via a local Ollama model.

After each scrape (and via ``python -m jobscrape.matcher`` for a backfill),
unclassified leads are judged against the titles the user is hunting for —
the *first keyword of each search set* (e.g. "Software Engineer", "Full
Stack", "Frontend"). Non-matching jobs get ``title_match = 0`` and are hidden
by default in the tracker and assistant views (a toggle reveals them).

Runs fully locally against Ollama — no API key, no cost. If Ollama is
unreachable, a deliberately lenient string-rule fallback classifies instead:
it only filters out staff/director/manager positions and keeps everything
else. Transient per-batch errors (Ollama up but a call failed) leave rows
unclassified (``title_match`` NULL) so the LLM retries them next run.
"""

from __future__ import annotations

import json
import urllib.request

from .config import load_config, resolve_db_path
from .storage import Storage

BATCH_SIZE = 20
TIMEOUT_S = 300  # a cold local model can take a while on the first batch

# Ollama structured-output schema: one verdict per job id.
SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "match": {"type": "boolean"},
                },
                "required": ["id", "match"],
            },
        }
    },
    "required": ["results"],
}


def wanted_titles(cfg: dict) -> list[str]:
    """The roles being hunted: first keyword of each search set, deduped."""
    titles: list[str] = []
    for kw_set in cfg.get("keyword_sets", []):
        if kw_set and kw_set[0].get("term"):
            term = kw_set[0]["term"].strip()
            if term and term.lower() not in {t.lower() for t in titles}:
                titles.append(term)
    return titles


def _prompt(batch: list[dict], wanted: list[str]) -> str:
    lines = "\n".join(
        f"{j['id']}. {j.get('title') or '(no title)'} || {(j.get('url') or '')[:100]}"
        for j in batch
    )
    return (
        "You are filtering job leads for a job seeker. Each lead is a page "
        "title followed by || and its URL.\n"
        f"The seeker wants individual-contributor roles matching any of these "
        f"titles: {', '.join(wanted)}.\n"
        "A lead MATCHES only if BOTH hold:\n"
        "1. It is ONE specific job posting / application page - judge from "
        "title and URL together. It is NOT a match if it looks like a job "
        "search or listing page, an aggregator, or a generic careers landing "
        "page (clues: titles like 'X jobs near me', '25 Software Engineer "
        "Jobs in San Francisco', 'Careers | Company', 'Jobs at Company', "
        "'Current Opportunities'; URLs with /search, /jobs-near-me, list-like "
        "paths).\n"
        "2. The role is that kind of individual-contributor position, "
        "including junior/mid/senior variants, abbreviations like SWE/SDE, "
        "and 'Developer' for 'Engineer' - but NOT a different profession "
        "(sales, accounting, mechanical, marketing, recruiting, design, "
        "etc), NOT a management-track role (Engineering Manager, Director, "
        "VP, Head of, Chief ...), and NOT a Staff/Principal-level or above "
        "position. ('Founding Engineer' is hands-on IC and MATCHES.)\n\n"
        f"Classify each lead:\n{lines}\n\n"
        "Return JSON with a result for every id."
    )


# Deliberately lenient fallback for when Ollama is unreachable: keep every
# title except obvious leadership postings.
FALLBACK_EXCLUDE = ("staff", "director", "manager")


def _fallback_classify(title: str) -> bool:
    t = (title or "").lower()
    return not any(word in t for word in FALLBACK_EXCLUDE)


def _ollama_up(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=5):
            return True
    except Exception:  # noqa: BLE001
        return False


def _ensure_ollama(base_url: str, log) -> bool:
    """If Ollama isn't answering, try to start it (scheduled overnight runs
    shouldn't depend on the app having been opened by hand)."""
    if _ollama_up(base_url):
        return True
    import os
    import shutil
    import subprocess

    exe = shutil.which("ollama") or os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
    )
    if not os.path.exists(exe):
        return False
    log("[match] Ollama not running - starting it...")
    try:
        subprocess.Popen(
            [exe, "serve"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        log(f"[match] could not start Ollama: {e}")
        return False
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(2)
        if _ollama_up(base_url):
            return True
    return False


def _classify_batch(batch: list[dict], wanted: list[str], base_url: str, model: str) -> dict[int, bool]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": _prompt(batch, wanted)}],
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        resp = json.loads(r.read())
    parsed = json.loads(resp["message"]["content"])
    ids = {j["id"] for j in batch}
    return {v["id"]: bool(v["match"]) for v in parsed.get("results", []) if v.get("id") in ids}


def run_matching(store: Storage, cfg: dict, progress=None) -> dict:
    """Classify every unclassified lead. Returns a small summary dict."""
    log = progress or print
    if not cfg.get("llm_match_enabled", True):
        return {"checked": 0, "matched": 0, "skipped": "disabled"}
    wanted = wanted_titles(cfg)
    if not wanted:
        return {"checked": 0, "matched": 0, "skipped": "no keyword sets"}

    jobs = store.jobs_needing_match()
    if not jobs:
        return {"checked": 0, "matched": 0}

    base_url = cfg.get("ollama_url", "http://127.0.0.1:11434")
    model = cfg.get("ollama_model", "qwen2.5:14b")

    if not _ensure_ollama(base_url, log):
        # LLM unavailable: lenient string rules, but PROVISIONAL - only the
        # obvious exclusions are written; everything else stays unclassified
        # so the LLM re-judges those rows on the next run. (Permanent lenient
        # verdicts previously let listing-page junk through for good.)
        log(
            f"[match] Ollama unreachable at {base_url} - fallback string "
            f"rules hide {'/'.join(FALLBACK_EXCLUDE)} titles; the rest wait "
            "for the LLM"
        )
        hidden = 0
        for j in jobs:
            if not _fallback_classify(j.get("title") or j.get("url") or ""):
                store.set_title_match(j["id"], False)
                hidden += 1
        log(f"[match] fallback: {hidden}/{len(jobs)} hidden, {len(jobs) - hidden} left unclassified")
        return {"checked": len(jobs), "matched": 0, "hidden": hidden, "engine": "fallback"}

    log(f"[match] {len(jobs)} titles -> {model} (wanted: {', '.join(wanted)})")

    checked = matched = failed = 0
    for i in range(0, len(jobs), BATCH_SIZE):
        batch = jobs[i:i + BATCH_SIZE]
        try:
            verdicts = _classify_batch(batch, wanted, base_url, model)
        except Exception as e:  # noqa: BLE001 — Ollama down/slow: retry next run
            failed += len(batch)
            log(f"[match]  x batch failed ({e}); leaving unclassified")
            continue
        for j in batch:
            verdict = verdicts.get(j["id"])
            if verdict is None:
                failed += 1
                continue
            store.set_title_match(j["id"], verdict)
            checked += 1
            matched += int(verdict)
        log(f"[match]  {min(i + BATCH_SIZE, len(jobs))}/{len(jobs)} classified")

    log(
        f"[match] done: {matched}/{checked} match your titles"
        + (f" ({failed} left unclassified)" if failed else "")
    )
    return {"checked": checked, "matched": matched, "failed": failed, "engine": "ollama"}


if __name__ == "__main__":
    _cfg = load_config()
    _store = Storage(resolve_db_path(_cfg))
    print(run_matching(_store, _cfg))
    _store.close()
