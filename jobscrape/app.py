"""Flask UI: admin console (/admin) + VA assistant view (/assistant).

    python -m jobscrape.app   ->  http://127.0.0.1:5050

Auth is deliberately lightweight (shared passwords in config.json):
  - direct localhost access (no proxy headers) is treated as admin, so the
    owner never sees a login screen at his own desk
  - through a tunnel (ngrok adds X-Forwarded-For) you log in with either the
    VA password (assistant view only) or the admin password (everything)
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import date, timedelta
from functools import wraps

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .config import (
    ATS_PRESETS,
    FRESHNESS_OPTIONS,
    PROJECT_ROOT,
    load_config,
    resolve_db_path,
    save_config,
)
from .export import applied_workbook_bytes
from .query import GOOGLE_SEARCH
from .scraper import run_scrape
from .storage import Storage

app = Flask(__name__)
app.secret_key = load_config()["flask_secret"]
app.permanent_session_lifetime = timedelta(days=30)

# In-memory state for the background "Run now" job (single-user local tool).
RUN_STATE: dict = {"running": False, "log": [], "summary": None, "error": None}

# State for the background autoapply batch (autoapply/cdp_apply.py).
# "urls" records exactly which leads the batch processed, so the tracker can
# outline those rows until their status is updated.
AUTOAPPLY_STATE: dict = {"running": False, "log": [], "exit": None, "urls": [], "mode": "fill"}


def _pick_autoapply_urls(count: int) -> list[str]:
    from importlib import util as _ilu

    spec = _ilu.spec_from_file_location(
        "pick_urls", PROJECT_ROOT / "autoapply" / "pick_urls.py"
    )
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.pick(count)


def _bg_autoapply(count: int = 5, mode: str = "fill") -> None:
    """Run the CDP autoapply batch as a subprocess, streaming its output.

    mode 'fill': open `count` tabs (default 5 - ten simultaneous ATS tabs plus
    Simplify and a resident LLM OOM Chrome's oldest renderers) and leave them
    filled for review. mode 'submit': one tab at a time, autofill + submit +
    close, DB updated per job (cdp_apply.py submit).
    """
    AUTOAPPLY_STATE.update(running=True, log=[], exit=None, urls=[], mode=mode)
    try:
        urls = _pick_autoapply_urls(count)
        AUTOAPPLY_STATE["urls"] = urls
        if not urls:
            AUTOAPPLY_STATE["log"].append("no eligible leads to apply to")
            AUTOAPPLY_STATE["exit"] = 0
            return
        proc = subprocess.Popen(
            [
                sys.executable, "-u",
                str(PROJECT_ROOT / "autoapply" / "cdp_apply.py"),
                "submit" if mode == "submit" else "run", *urls,
            ],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            AUTOAPPLY_STATE["log"].append(line.rstrip())
        AUTOAPPLY_STATE["exit"] = proc.wait()
    except Exception as e:  # noqa: BLE001
        AUTOAPPLY_STATE["log"].append(f"error: {e}")
        AUTOAPPLY_STATE["exit"] = -1
    finally:
        AUTOAPPLY_STATE["running"] = False


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


# --- auth -------------------------------------------------------------------


def _current_role() -> str | None:
    """'admin', 'va', or None (not authenticated)."""
    role = session.get("role")
    if role in ("admin", "va"):
        return role
    # A plain localhost hit with no proxy headers is the owner at his desk;
    # anything through ngrok carries X-Forwarded-For and must log in.
    if request.remote_addr == "127.0.0.1" and "X-Forwarded-For" not in request.headers:
        return "admin"
    return None


def require_role(*roles: str):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            role = _current_role()
            if role is None:
                if request.method == "GET":
                    return redirect(url_for("login"))
                return jsonify(ok=False, error="auth required"), 401
            if role not in roles:
                if request.method == "GET":
                    return redirect(url_for("assistant"))
                return jsonify(ok=False, error="forbidden"), 403
            return fn(*args, **kwargs)

        return wrapper

    return deco


@app.route("/login", methods=["GET", "POST"])
def login():
    role = _current_role()
    if role:
        return redirect("/admin" if role == "admin" else "/assistant")
    error = None
    if request.method == "POST":
        cfg = load_config()
        pw = (request.form.get("password") or "").strip()
        if pw and pw == cfg.get("admin_password"):
            session.permanent = True
            session["role"] = "admin"
            return redirect("/admin")
        if pw and pw == cfg.get("va_password"):
            session.permanent = True
            session["role"] = "va"
            return redirect("/assistant")
        error = "Wrong password, try again."
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def root():
    role = _current_role()
    if role == "admin":
        return redirect("/admin")
    if role == "va":
        return redirect("/assistant")
    return redirect(url_for("login"))


# --- pages --------------------------------------------------------------------


@app.get("/admin")
@require_role("admin")
def admin():
    cfg = load_config()
    store = Storage(resolve_db_path(cfg))
    jobs = store.recent_jobs(2000)
    runs = store.recent_runs(10)
    stats = store.stats()
    store.close()
    return render_template(
        "index.html",
        cfg=cfg,
        ats_presets=ATS_PRESETS,
        freshness_options=FRESHNESS_OPTIONS,
        google_base=GOOGLE_SEARCH,
        jobs=jobs,
        runs=runs,
        stats=stats,
    )


@app.get("/assistant")
@require_role("admin", "va")
def assistant():
    cfg = load_config()
    store = Storage(resolve_db_path(cfg))
    jobs = store.recent_jobs(2000)
    stats = store.stats()
    store.close()
    return render_template("assistant.html", jobs=jobs, stats=stats)


@app.get("/export/applied.xlsx")
@require_role("admin", "va")
def export_applied():
    store = Storage(resolve_db_path(load_config()))
    jobs = store.applied_jobs()
    store.close()
    fname = f"applied-{date.today().isoformat()}.xlsx"
    return Response(
        applied_workbook_bytes(jobs),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# --- config + scrape (admin only) -----------------------------------------------


@app.post("/save")
@require_role("admin")
def save():
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    cfg["keyword_sets"] = data.get("keyword_sets", cfg["keyword_sets"])
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
    cfg["llm_match_enabled"] = bool(
        data.get("llm_match_enabled", cfg.get("llm_match_enabled", True))
    )
    cfg["ollama_url"] = data.get("ollama_url", cfg.get("ollama_url", "")).strip() or cfg.get(
        "ollama_url", "http://127.0.0.1:11434"
    )
    cfg["ollama_model"] = data.get("ollama_model", cfg.get("ollama_model", "")).strip() or cfg.get(
        "ollama_model", "qwen2.5:14b"
    )
    cfg["simplify_search_enabled"] = bool(
        data.get("simplify_search_enabled", cfg.get("simplify_search_enabled", True))
    )
    cfg["simplify_url"] = data.get("simplify_url", cfg.get("simplify_url", "")).strip()
    save_config(cfg)
    return jsonify(ok=True, config=cfg)


@app.post("/run")
@require_role("admin")
def run():
    if RUN_STATE["running"]:
        return jsonify(ok=False, error="A scrape is already running."), 409
    threading.Thread(target=_bg_run_capture, daemon=True).start()
    return jsonify(ok=True)


@app.get("/run-status")
@require_role("admin")
def run_status():
    return jsonify(RUN_STATE)


@app.post("/autoapply")
@require_role("admin")
def autoapply():
    if AUTOAPPLY_STATE["running"]:
        return jsonify(ok=False, error="An autoapply batch is already running."), 409
    data = request.get_json(force=True, silent=True) or {}
    mode = "submit" if data.get("mode") == "submit" else "fill"
    cap = 50 if mode == "submit" else 10  # submit runs are one tab at a time
    count = max(1, min(cap, int(data.get("count", 5))))
    threading.Thread(target=_bg_autoapply, args=(count, mode), daemon=True).start()
    return jsonify(ok=True, count=count, mode=mode)


@app.get("/autoapply-status")
@require_role("admin")
def autoapply_status():
    return jsonify(AUTOAPPLY_STATE)


# --- tracker edits (admin + VA; who applied is recorded for billing) ------------


@app.post("/job/<int:job_id>/status")
@require_role("admin")
def job_status(job_id: int):
    data = request.get_json(force=True, silent=True) or {}
    store = Storage(resolve_db_path(load_config()))
    state = store.set_status(job_id, data.get("status", "new"), who="admin")
    store.close()
    return jsonify(ok=True, **state)


@app.post("/job/<int:job_id>/applied")
@require_role("admin", "va")
def job_applied(job_id: int):
    data = request.get_json(force=True, silent=True) or {}
    store = Storage(resolve_db_path(load_config()))
    state = store.set_applied(job_id, bool(data.get("applied")), who=_current_role())
    store.close()
    return jsonify(ok=True, **state)


@app.post("/job/<int:job_id>/dismissed")
@require_role("admin", "va")
def job_dismissed(job_id: int):
    data = request.get_json(force=True, silent=True) or {}
    store = Storage(resolve_db_path(load_config()))
    state = store.set_dismissed(job_id, bool(data.get("dismissed")))
    store.close()
    return jsonify(ok=True, **state)


@app.post("/job/<int:job_id>/match")
@require_role("admin")
def job_match(job_id: int):
    """Manual override of the LLM title-match verdict."""
    data = request.get_json(force=True, silent=True) or {}
    match = bool(data.get("match"))
    store = Storage(resolve_db_path(load_config()))
    store.set_title_match(job_id, match)
    store.close()
    return jsonify(ok=True, title_match=int(match))


@app.post("/job/<int:job_id>/notes")
@require_role("admin", "va")
def job_notes(job_id: int):
    data = request.get_json(force=True, silent=True) or {}
    store = Storage(resolve_db_path(load_config()))
    store.set_notes(job_id, data.get("notes", ""))
    store.close()
    return jsonify(ok=True)


if __name__ == "__main__":
    # Default 5050 (5000 is often taken on Windows). Override with JGS_PORT.
    port = int(os.environ.get("JGS_PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
