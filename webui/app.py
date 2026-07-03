#!/usr/bin/env python3
"""
webui/app.py

Small web control panel for fmg_retrieve_oos: configure the FortiManager
connection + log file + scan frequency, launch a scan on demand with a live
progress bar, and optionally let it run continuously on a schedule.

Meant for a headless Debian server (no desktop/X11): run this as a systemd
service and reach it through an SSH tunnel (see systemd/fmg-webui.service
and the README) rather than exposing it directly on the network.

Requires Flask (apt install python3-flask on Debian). Everything else is
standard library, shared with the CLI via lib/fmg_common.py.
"""

import json
import os
import secrets
import sys
import threading
import time

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from fmg_common import (  # noqa: E402
    DEFAULT_LOG_DIR,
    DEFAULT_LOG_RETENTION_DAYS,
    FmgApiError,
    REPORT_COLUMNS,
    append_resync_history,
    format_report_table,
    make_run_log_path,
    purge_old_logs,
    run_scan,
)

CONFIG_DIR = os.environ.get(
    "FMG_WEBUI_CONFIG_DIR", os.path.expanduser("~/.config/fmg-retrieve-oos")
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
PASSWORD_FILE = os.path.join(CONFIG_DIR, "fmg.passwd")
API_KEY_FILE = os.path.join(CONFIG_DIR, "fmg.apikey")

DEFAULT_CONFIG = {
    "host": "",
    "port": 443,
    "adom": "",
    "auth_mode": "password",  # "password" (classic admin) or "apikey" (REST API Admin token)
    "user": "",
    "log_dir": DEFAULT_LOG_DIR,
    "log_retention_days": DEFAULT_LOG_RETENTION_DAYS,
    "insecure": False,
    "all_devices": False,
    "poll_interval": 5,
    "task_timeout": 600,
    "timeout": 30,
    "frequency_minutes": 15,
    "scheduler_enabled": False,
}

WEBUI_USERNAME = os.environ.get("WEBUI_USERNAME")
WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD")
if not WEBUI_USERNAME or not WEBUI_PASSWORD:
    sys.exit(
        "WEBUI_USERNAME and WEBUI_PASSWORD env vars must be set before starting "
        "the web UI (it can trigger changes on your FortiManager - it must not "
        "be reachable without a login)."
    )

app = Flask(__name__)

lock = threading.Lock()
state = {
    "phase": "idle",  # idle | running | done | error
    "trigger": None,  # manual | scheduler
    "dry_run": False,
    "rows": [],
    "target_total": 0,
    "target_done": 0,
    "current_device": None,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "error": None,
    "log_lines": [],
    "log_path": None,
    "next_run_at": None,
}


# ---- config / secret storage -------------------------------------------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True, mode=0o700)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_FILE)


def load_password():
    if os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    return None


def save_password(password):
    os.makedirs(CONFIG_DIR, exist_ok=True, mode=0o700)
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(password)


def load_api_key():
    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    return None


def save_api_key(api_key):
    os.makedirs(CONFIG_DIR, exist_ok=True, mode=0o700)
    fd = os.open(API_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(api_key)


# ---- auth ---------------------------------------------------------------

@app.before_request
def require_auth():
    auth = request.authorization
    valid = (
        auth
        and secrets.compare_digest(auth.username, WEBUI_USERNAME)
        and secrets.compare_digest(auth.password, WEBUI_PASSWORD)
    )
    if not valid:
        return Response(
            "Authentication required", 401, {"WWW-Authenticate": 'Basic realm="fmg-retrieve-oos"'}
        )


# ---- scan execution -------------------------------------------------------

def _open_run_log(cfg):
    """Create this run's timestamped log file, purging old ones first.
    Returns (path, file_object) or (None, None) if disabled/unwritable."""
    log_dir = cfg.get("log_dir")
    if not log_dir:
        return None, None
    try:
        os.makedirs(log_dir, exist_ok=True)
        purge_old_logs(log_dir, cfg.get("log_retention_days"))
        path = make_run_log_path(log_dir)
        return path, open(path, "w", encoding="utf-8")
    except OSError:
        return None, None


def _run_worker(cfg, password, api_key, trigger, dry_run):
    log_path, log_fh = _open_run_log(cfg)

    def _log(message):
        ts = time.strftime("%H:%M:%S")
        line = f"{ts} {message}"
        state["log_lines"].append(line)
        state["log_lines"] = state["log_lines"][-300:]
        if log_fh:
            log_fh.write(line + "\n")
            log_fh.flush()

    with lock:
        state.update(
            phase="running",
            trigger=trigger,
            dry_run=dry_run,
            rows=[],
            target_total=0,
            target_done=0,
            current_device=None,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at=None,
            exit_code=None,
            error=None,
            log_lines=[],
            log_path=log_path,
        )
        _log(f"Démarrage du scan ({trigger}) sur {cfg['host']} / ADOM {cfg['adom']}")
        if log_path:
            _log(f"Log de ce run: {log_path}")
        elif cfg.get("log_dir"):
            _log(f"Impossible d'écrire dans '{cfg['log_dir']}', log affiché ici uniquement.")

    def on_event(kind, **payload):
        with lock:
            if kind == "login_ok":
                _log("Connecté au FortiManager.")
            elif kind == "devices_listed":
                _log(f"{payload['count']} device(s) trouvés dans l'ADOM.")
            elif kind == "classified":
                state["rows"] = payload["rows"]
                state["target_total"] = len(payload["target_rows"])
                _log(f"{state['target_total']} device(s) à traiter.")
            elif kind == "retrieve_start":
                r = payload["row"]
                state["current_device"] = r["name"]
                _log(f"Retrieve lancé pour {r['name']} à {r['triggered_at']}")
            elif kind == "task_poll":
                r, s, p = payload["row"], payload["state"], payload["percent"]
                _log(f"  {r['name']}: {s} ({p}%)")
            elif kind == "retrieve_result":
                r = payload["row"]
                state["target_done"] += 1
                _log(f"{r['name']}: {r['result']} - {r['reason']}")

    try:
        rows, exit_code = run_scan(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"] or None,
            password=password,
            api_key=api_key,
            adom=cfg["adom"],
            all_devices=cfg["all_devices"],
            dry_run=dry_run,
            no_wait=False,
            poll_interval=cfg["poll_interval"],
            task_timeout=cfg["task_timeout"],
            timeout=cfg["timeout"],
            verify_ssl=not cfg["insecure"],
            on_event=on_event,
        )
        with lock:
            state["rows"] = rows
            state["exit_code"] = exit_code
            state["phase"] = "done"
            state["current_device"] = None
            state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _log(f"Scan terminé (code={exit_code}).")
            if log_fh:
                log_fh.write("\n" + format_report_table(rows) + "\n")
                log_fh.flush()
        if not dry_run:
            append_resync_history(cfg.get("log_dir"), rows, trigger)
    except FmgApiError as exc:
        with lock:
            state["phase"] = "error"
            state["error"] = str(exc)
            state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _log(f"ERREUR API: {exc}")
    except Exception as exc:  # noqa: BLE001 - a background thread must never
        # die silently: an uncaught exception here would otherwise leave the
        # UI stuck showing "running" forever with no explanation.
        with lock:
            state["phase"] = "error"
            state["error"] = f"erreur inattendue: {exc}"
            state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _log(f"ERREUR inattendue: {exc}")
    finally:
        if log_fh:
            log_fh.close()


def try_start_run(trigger, dry_run=False):
    cfg = load_config()
    if not cfg.get("host") or not cfg.get("adom"):
        return False, "Configuration incomplète (host / ADOM)."

    password = None
    api_key = None
    if cfg.get("auth_mode") == "apikey":
        api_key = load_api_key()
        if not api_key:
            return False, "Clé API FortiManager non configurée."
    else:
        if not cfg.get("user"):
            return False, "Configuration incomplète (utilisateur API)."
        password = load_password()
        if not password:
            return False, "Mot de passe FortiManager non configuré."

    with lock:
        if state["phase"] == "running":
            return False, "Un scan est déjà en cours."
        state["phase"] = "running"

    threading.Thread(
        target=_run_worker, args=(cfg, password, api_key, trigger, dry_run), daemon=True
    ).start()
    return True, None


# ---- scheduler --------------------------------------------------------------

def scheduler_loop():
    while True:
        cfg = load_config()
        if cfg.get("scheduler_enabled"):
            with lock:
                next_run = state["next_run_at"]
                running = state["phase"] == "running"
            if next_run is None:
                with lock:
                    state["next_run_at"] = time.time() + cfg["frequency_minutes"] * 60
            elif time.time() >= next_run and not running:
                try_start_run("scheduler")
                with lock:
                    state["next_run_at"] = time.time() + cfg["frequency_minutes"] * 60
        else:
            with lock:
                state["next_run_at"] = None
        time.sleep(2)


# ---- routes -------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    cfg = load_config()
    return render_template(
        "index.html",
        cfg=cfg,
        has_password=load_password() is not None,
        has_api_key=load_api_key() is not None,
        report_columns=REPORT_COLUMNS,
        error=request.args.get("error"),
    )


@app.route("/config", methods=["POST"])
def save_config_route():
    cfg = load_config()
    cfg["host"] = request.form.get("host", "").strip()
    cfg["port"] = int(request.form.get("port") or 443)
    cfg["adom"] = request.form.get("adom", "").strip()
    cfg["auth_mode"] = "apikey" if request.form.get("auth_mode") == "apikey" else "password"
    cfg["user"] = request.form.get("user", "").strip()
    cfg["log_dir"] = request.form.get("log_dir", "").strip()
    cfg["log_retention_days"] = max(
        0, int(request.form.get("log_retention_days") or DEFAULT_LOG_RETENTION_DAYS)
    )
    cfg["insecure"] = request.form.get("insecure") == "on"
    cfg["all_devices"] = request.form.get("all_devices") == "on"
    cfg["poll_interval"] = max(1, int(request.form.get("poll_interval") or 5))
    cfg["task_timeout"] = max(10, int(request.form.get("task_timeout") or 600))
    cfg["timeout"] = max(5, int(request.form.get("timeout") or 30))
    cfg["frequency_minutes"] = max(1, int(request.form.get("frequency_minutes") or 15))
    cfg["scheduler_enabled"] = request.form.get("scheduler_enabled") == "on"
    save_config(cfg)

    new_password = request.form.get("password", "")
    if new_password:
        save_password(new_password)

    new_api_key = request.form.get("api_key", "")
    if new_api_key:
        save_api_key(new_api_key)

    with lock:
        state["next_run_at"] = None  # reschedule cleanly from now

    return redirect(url_for("index"))


@app.route("/run", methods=["POST"])
def run_now():
    dry_run = request.form.get("dry_run") == "1"
    ok, err = try_start_run("manual", dry_run=dry_run)
    if not ok:
        return redirect(url_for("index", error=err))
    return redirect(url_for("index"))


@app.route("/api/status")
def api_status():
    with lock:
        snapshot = dict(state)
    cfg = load_config()
    snapshot["scheduler_enabled"] = cfg["scheduler_enabled"]
    snapshot["frequency_minutes"] = cfg["frequency_minutes"]
    return jsonify(snapshot)


if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    host = os.environ.get("FMG_WEBUI_HOST", "127.0.0.1")
    port = int(os.environ.get("FMG_WEBUI_PORT", "8877"))
    app.run(host=host, port=port, threaded=True, use_reloader=False)
