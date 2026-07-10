"""LLM job-title matching via a local Ollama model.

After each scrape (and via ``python -m jobscrape.matcher`` for a backfill),
unclassified leads are judged against the titles the user is hunting for —
the *first keyword of each search set* (e.g. "Software Engineer", "Full
Stack", "Frontend"). Non-matching jobs get ``title_match = 0`` and are hidden
by default in the tracker and assistant views (a toggle reveals them).

Runs fully locally against Ollama — no API key, no cost. If Ollama is down
or slow, jobs simply stay unclassified (``title_match`` NULL) and are retried
on the next scrape run.
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
    lines = "\n".join(f"{j['id']}. {j.get('title') or j.get('url') or ''}" for j in batch)
    return (
        "You are filtering job leads for a job seeker.\n"
        f"The seeker wants roles matching any of these titles: {', '.join(wanted)}.\n"
        "A job MATCHES if its title is that kind of role (including seniority "
        "variants, common abbreviations like SWE/SDE, and equivalent phrasings "
        "like 'Developer' for 'Engineer'). It does NOT match if it is a "
        "different profession (sales, accounting, mechanical, marketing, "
        "recruiting, etc).\n\n"
        f"Classify each job:\n{lines}\n\n"
        "Return JSON with a result for every id."
    )


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
    return {"checked": checked, "matched": matched, "failed": failed}


if __name__ == "__main__":
    _cfg = load_config()
    _store = Storage(resolve_db_path(_cfg))
    print(run_matching(_store, _cfg))
    _store.close()
