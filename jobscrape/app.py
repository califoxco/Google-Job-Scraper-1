"""Flask config UI.

    python -m jobscrape.app   ->  http://127.0.0.1:5000

Lets you edit keywords, freshness, ATS sites, output DB, and run options;
trigger a scrape ("Run now"); and browse the leads collected so far.
"""

from __future__ import annotations

import os
import threading

from flask import Flask, jsonify, render_template, request

from .config import (
    FRESHNESS_OPTIONS,
    KNOWN_ATS,
    load_config,
    resolve_db_path,
    save_config,
)
from .query import GOOGLE_SEARCH
from .scraper import run_scrape
from .storage import Storage

app = Flask(__name__)

# In-memory state for the background "Run now" job (single-user local tool).
RUN_STATE: dict = {"running": False, "log": [], "summary": None, "error": None}


def _bg_run_capture() -> None:
    """Run a scrape on a background thread, capturing the summary."""
    RUN_STATE.update(running=True, log=[], summary=None, error=None)
    try:
        cfg = load_config()
        summary = run_scrape(cfg, progress=lambda m: RUN_STATE["log"].append(m))
        RUN_STATE["summary"] = summary
    except Exception as e:  # noqa: BLE001
        RUN_STATE["error"] = str(e)
    finally:
        RUN_STATE["running"] = False


@app.get("/")
def index():
    cfg = load_config()
    store = Storage(resolve_db_path(cfg))
    jobs = store.recent_jobs(150)
    runs = store.recent_runs(10)
    stats = store.stats()
    store.close()
    return render_template(
        "index.html",
        cfg=cfg,
        known_ats=KNOWN_ATS,
        freshness_options=FRESHNESS_OPTIONS,
        google_base=GOOGLE_SEARCH,
        jobs=jobs,
        runs=runs,
        stats=stats,
    )


@app.post("/save")
def save():
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    cfg["keywords"] = data.get("keywords", cfg["keywords"])
    cfg["freshness"] = data.get("freshness", cfg["freshness"])
    cfg["ats_sites"] = data.get("ats_sites", cfg["ats_sites"])
    cfg["results_db"] = data.get("results_db", cfg["results_db"])
    cfg["provider"] = data.get("provider", cfg.get("provider", "playwright"))
    cfg["scrapingbee_api_key"] = data.get(
        "scrapingbee_api_key", cfg.get("scrapingbee_api_key", "")
    )
    cfg["headless"] = bool(data.get("headless", cfg["headless"]))
    cfg["max_results_per_query"] = int(
        data.get("max_results_per_query", cfg["max_results_per_query"])
    )
    cfg["min_delay_seconds"] = float(data.get("min_delay_seconds", cfg["min_delay_seconds"]))
    cfg["max_delay_seconds"] = float(data.get("max_delay_seconds", cfg["max_delay_seconds"]))
    save_config(cfg)
    return jsonify(ok=True, config=cfg)


@app.post("/run")
def run():
    if RUN_STATE["running"]:
        return jsonify(ok=False, error="A scrape is already running."), 409
    threading.Thread(target=_bg_run_capture, daemon=True).start()
    return jsonify(ok=True)


@app.get("/run-status")
def run_status():
    return jsonify(RUN_STATE)


@app.post("/job/<int:job_id>/status")
def job_status(job_id: int):
    data = request.get_json(force=True, silent=True) or {}
    status = data.get("status", "new")
    store = Storage(resolve_db_path(load_config()))
    store.set_status(job_id, status)
    store.close()
    return jsonify(ok=True)


if __name__ == "__main__":
    # Default 5050 (5000 is often taken on Windows). Override with JGS_PORT.
    port = int(os.environ.get("JGS_PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
