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
