"""
fmg_common.py

Shared FortiManager JSON-RPC client + scan/retrieve logic, used by both the
CLI (scripts/fmg_retrieve_oos.py) and the web UI (webui/app.py) so the two
front-ends can never drift out of sync on how devices are classified or
retrieved.

Standard library only.
"""

import glob
import json
import os
import ssl
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 30
DEFAULT_TASK_POLL_INTERVAL = 5
DEFAULT_TASK_TIMEOUT = 600

DEFAULT_LOG_DIR = "/var/log/fmg-retrieve-oos"
DEFAULT_LOG_RETENTION_DAYS = 30
LOG_FILENAME_PREFIX = "fmg-retrieve-oos"

# Only this exact conf_status triggers a retrieve. "unknown" (device never
# checked in / FMG can't tell) and any other value are deliberately left
# alone - retrieving them would be guessing, not reacting to a real desync.
OUT_OF_SYNC_STATUS = "outofsync"

STATUS_LABELS = {
    "outofsync": "DESYNC",
    "insync": "SYNC",
    "unknown": "INCONNU",
}

CONN_LABELS = {
    "up": "UP",
    "down": "DOWN",
}

REPORT_COLUMNS = [
    ("NAME", "name"),
    ("SN", "sn"),
    ("IP", "ip"),
    ("CONNEXION", "conn_label"),
    ("STATUT", "status_label"),
    ("ACTION", "action"),
    ("HEURE", "triggered_at"),
    ("RESULTAT", "result"),
    ("RAISON", "reason"),
]


class FmgApiError(Exception):
    pass


class FmgClient:
    def __init__(self, host, verify_ssl=True, timeout=DEFAULT_TIMEOUT, port=443, api_key=None):
        self.base_url = f"https://{host}:{port}/jsonrpc"
        self.timeout = timeout
        self.session = None
        self.api_key = api_key  # REST API Admin token auth, alternative to user/password
        self._id = 0
        self._ctx = ssl.create_default_context()
        if not verify_ssl:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def _next_id(self):
        self._id += 1
        return self._id

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, payload):
        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _call(self, method, url, data=None, extra=None, use_session=True):
        payload = {
            "id": self._next_id(),
            "method": method,
            "params": [
                {
                    "url": url,
                    **({"data": data} if data is not None else {}),
                    **(extra or {}),
                }
            ],
        }
        if method == "get":
            # Without "verbose", FortiManager returns enum fields like
            # conf_status/conn_status as raw integers instead of the
            # documented strings ("insync"/"outofsync"/"unknown"/"up"/"down").
            # Only relevant for reads - deliberately not sent on "exec"
            # (login/retrieve/logout) calls, which don't have enum fields to
            # translate and shouldn't have their behavior altered by it.
            payload["verbose"] = 1
        if use_session and self.session:
            payload["session"] = self.session

        try:
            body = self._post(payload)
        except urllib.error.URLError as exc:
            raise FmgApiError(f"connection error calling {url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise FmgApiError(f"invalid JSON response from {url}: {exc}") from exc

        result = body.get("result")
        if not result:
            raise FmgApiError(f"malformed response from {url}: {body}")
        first = result[0]
        status = first.get("status", {})
        if status.get("code", -1) != 0:
            raise FmgApiError(
                f"API error on {url}: code={status.get('code')} "
                f"message={status.get('message')}"
            )
        return first

    def login(self, user=None, password=None):
        if self.api_key:
            # REST API Admin token auth: no session to establish, the
            # Authorization header (set in _headers) carries it on every
            # call. Do one cheap call up front so a bad/expired token
            # surfaces immediately, like a password login failure would.
            self._call("get", "/sys/status", use_session=False)
            return

        # session token is returned at the top level of the response, not
        # inside "result", so this can't reuse the generic _call() helper.
        payload = {
            "id": self._next_id(),
            "method": "exec",
            "params": [{"url": "/sys/login/user", "data": {"user": user, "passwd": password}}],
        }
        try:
            body = self._post(payload)
        except urllib.error.URLError as exc:
            raise FmgApiError(f"connection error during login: {exc}") from exc

        result = body.get("result", [{}])[0]
        status = result.get("status", {})
        if status.get("code", -1) != 0:
            raise FmgApiError(
                f"login failed: code={status.get('code')} message={status.get('message')}"
            )
        session = body.get("session")
        if not session:
            raise FmgApiError("login succeeded but no session token was returned")
        self.session = session

    def logout(self):
        if self.api_key or not self.session:
            return
        try:
            self._call("exec", "/sys/logout")
        except FmgApiError:
            pass
        finally:
            self.session = None

    def get_devices(self, adom):
        result = self._call(
            "get",
            f"/dvmdb/adom/{adom}/device",
            extra={
                "fields": ["name", "sn", "conf_status", "db_status", "conn_status", "ip"],
                "option": ["no loadsub"],
            },
        )
        return result.get("data", [])

    def retrieve_config(self, adom, device_name):
        """Returns the raw "data" dict from /dvm/cmd/update/device (normally
        {"task": <id>, ...}). Returning the whole dict, not just the task id,
        lets the caller report exactly what FortiManager sent back if the
        expected "task" key is missing - instead of a bare "no task id"."""
        result = self._call(
            "exec",
            "/dvm/cmd/update/device",
            data={
                "adom": adom,
                "device": device_name,
                "flags": ["create_task", "nonblocking"],
            },
        )
        return result.get("data", {})

    def get_task(self, task_id):
        result = self._call("get", f"/task/task/{task_id}")
        return result.get("data", {})


def wait_for_task(client, task_id, poll_interval, timeout, on_poll=None):
    """Poll a task until it finishes. Returns (state, ok, reason)."""
    deadline = time.time() + timeout
    task = {}
    while time.time() < deadline:
        task = client.get_task(task_id)
        state = task.get("state")
        percent = task.get("percent", 0)
        if on_poll:
            on_poll(state, percent)
        if state in ("done", "error", "cancelled", "aborted"):
            break
        time.sleep(poll_interval)
    else:
        return "timeout", False, f"pas terminé après {timeout}s"

    lines = task.get("line") or []
    err = lines[0].get("err") if lines else task.get("num_err", 0)
    detail = lines[0].get("detail") if lines else task.get("detail")
    ok = task.get("state") == "done" and not err
    if ok:
        return task.get("state"), True, detail or "OK"
    reason = detail or f"état={task.get('state')} err={err}"
    return task.get("state"), False, reason


def make_run_log_path(log_dir, when=None, trigger=None):
    """One timestamped log file per run, e.g.
    fmg-retrieve-oos_manual_2026-07-03_14-05-00.log"""
    ts = time.strftime("%Y-%m-%d_%H-%M-%S", when or time.localtime())
    tag = f"_{trigger}" if trigger else ""
    return os.path.join(log_dir, f"{LOG_FILENAME_PREFIX}{tag}_{ts}.log")


def purge_old_logs(log_dir, retention_days):
    """Delete previous run log files older than retention_days. No-op if
    retention_days is falsy (0/None disables purging)."""
    if not retention_days or retention_days <= 0:
        return
    cutoff = time.time() - retention_days * 86400
    for path in glob.glob(os.path.join(log_dir, f"{LOG_FILENAME_PREFIX}_*.log")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


RESYNC_HISTORY_FILENAME = "resync-history.log"


def resync_history_path(log_dir):
    return os.path.join(log_dir, RESYNC_HISTORY_FILENAME)


def append_resync_history(log_dir, rows, trigger):
    """Append one line per device that was out-of-sync and got successfully
    resynced (result == SUCCESS) to a single cumulative audit file - the
    long-term answer to "which FGT were desync and got fixed, and when".
    Unlike the per-run log files, this one is never purged/rotated by the
    tool itself: it's meant to accumulate as history. No-op if log_dir is
    falsy. The file is created (even empty) on the very first run so
    `cat`/`tail -f` never fails with "no such file" just because nothing
    has needed a resync yet."""
    if not log_dir:
        return
    successes = [r for r in rows if r.get("result") == "SUCCESS"]
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(resync_history_path(log_dir), "a", encoding="utf-8") as fh:
            for r in successes:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                fh.write(
                    f"{ts} | {trigger} | {r.get('name')} | {r.get('sn', '-')} | "
                    f"{r.get('ip', '-')} | resync OK\n"
                )
    except OSError:
        pass


def format_report_table(rows):
    """Render `rows` as a plain-text aligned table, header + separator + rows."""
    widths = {}
    for header, key in REPORT_COLUMNS:
        widths[key] = max([len(header)] + [len(str(r.get(key, ""))) for r in rows])

    def fmt_row(values):
        return " | ".join(str(v).ljust(widths[key]) for v, (_, key) in zip(values, REPORT_COLUMNS))

    lines = [fmt_row([h for h, _ in REPORT_COLUMNS])]
    lines.append("-+-".join("-" * widths[key] for _, key in REPORT_COLUMNS))
    for r in rows:
        lines.append(fmt_row([r.get(key, "") for _, key in REPORT_COLUMNS]))
    return "\n".join(lines)


def classify_devices(devices, all_devices=False):
    """Build report rows from raw FMG device dicts and split out which ones
    should actually be retrieved (out-of-sync, reachable, and - unless
    all_devices - conf_status strictly "outofsync")."""
    rows = []
    for d in devices:
        conf_status = d.get("conf_status") or "unknown"
        conn_status = d.get("conn_status") or "unknown"
        # FortiManager's own GUI shows "Unknown" for a device it can't
        # currently reach, regardless of the last conf_status it had
        # cached before going unreachable - a stale reading can't be
        # trusted. Mirror that in the displayed STATUT (the raw
        # conf_status is kept below for the actual retrieve-targeting
        # logic, which already excludes DOWN devices separately).
        status_label = (
            "INCONNU"
            if conn_status != "up"
            else STATUS_LABELS.get(conf_status, f"AUTRE({conf_status})")
        )
        rows.append(
            {
                "name": d.get("name"),
                "sn": d.get("sn"),
                "ip": d.get("ip") or "-",
                "conf_status": conf_status,
                "status_label": status_label,
                "conn_status": conn_status,
                "conn_label": CONN_LABELS.get(conn_status, "INCONNU"),
                "action": "-",
                "triggered_at": "-",
                "result": "-",
                "reason": "-",
            }
        )

    if all_devices:
        candidate_rows = rows
    else:
        candidate_rows = [r for r in rows if r["conf_status"] == OUT_OF_SYNC_STATUS]
        for r in rows:
            if r["conf_status"] != OUT_OF_SYNC_STATUS:
                r["action"] = "skipped"
                r["reason"] = f"conf_status={r['conf_status']} (pas de retrieve)"

    # A device that is DOWN (no connectivity between the FGT and the FMG) is
    # never retrieved, even under all_devices: the call would just fail, and
    # it would falsely look like an FMG-side error.
    target_rows = []
    for r in candidate_rows:
        if r["conn_status"] != "up":
            r["action"] = "skipped"
            r["reason"] = f"conn_status={r['conn_status']} (FGT injoignable, retrieve non déclenché)"
            continue
        target_rows.append(r)
        if all_devices and r["conf_status"] != OUT_OF_SYNC_STATUS:
            r["reason"] = "forcé par --all"

    return rows, target_rows


def run_scan(
    host,
    adom,
    user=None,
    password=None,
    api_key=None,
    port=443,
    all_devices=False,
    dry_run=False,
    no_wait=False,
    poll_interval=DEFAULT_TASK_POLL_INTERVAL,
    task_timeout=DEFAULT_TASK_TIMEOUT,
    timeout=DEFAULT_TIMEOUT,
    verify_ssl=True,
    on_event=None,
):
    """Full scan + retrieve flow against one FortiManager ADOM.

    Authenticates either with a user/password admin account, or with a
    REST API Admin token (api_key) - pass exactly one of the two.

    Returns (rows, exit_code). Calls on_event(kind, **payload) at each
    meaningful step so a caller (CLI logger, web UI progress state, ...)
    can report progress without duplicating this logic.

    Exit codes: 0 = OK, 1 = at least one retrieve failed/timed out.
    """
    def emit(kind, **payload):
        if on_event:
            on_event(kind, **payload)

    client = FmgClient(host, verify_ssl=verify_ssl, timeout=timeout, port=port, api_key=api_key)
    client.login(user, password)  # raises FmgApiError - let the caller handle it
    emit("login_ok")

    try:
        devices = client.get_devices(adom)
        emit("devices_listed", count=len(devices))

        rows, target_rows = classify_devices(devices, all_devices=all_devices)
        emit("classified", rows=rows, target_rows=target_rows)

        if not target_rows:
            emit("done", rows=rows, exit_code=0)
            return rows, 0

        if dry_run:
            for r in target_rows:
                r["action"] = "would-retrieve (dry-run)"
            emit("done", rows=rows, exit_code=0)
            return rows, 0

        exit_code = 0
        for r in target_rows:
            name = r["name"]
            r["action"] = "retrieved"
            r["triggered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            emit("retrieve_start", row=r)
            try:
                retrieve_data = client.retrieve_config(adom, name)
            except FmgApiError as exc:
                r["result"] = "FAILED"
                r["reason"] = str(exc)
                exit_code = 1
                emit("retrieve_result", row=r)
                continue

            task_id = retrieve_data.get("task")

            if no_wait:
                r["result"] = "PENDING"
                r["reason"] = f"task {task_id} lancée, suivi non attendu"
                emit("retrieve_result", row=r)
                continue

            if not task_id:
                r["result"] = "FAILED"
                r["reason"] = f"aucun task_id retourné par l'API (réponse: {retrieve_data})"
                exit_code = 1
                emit("retrieve_result", row=r)
                continue

            def on_poll(state, percent, row=r):
                emit("task_poll", row=row, state=state, percent=percent)

            _, ok, reason = wait_for_task(client, task_id, poll_interval, task_timeout, on_poll=on_poll)
            r["result"] = "SUCCESS" if ok else "FAILED"
            r["reason"] = reason
            if not ok:
                exit_code = 1
            emit("retrieve_result", row=r)

        emit("done", rows=rows, exit_code=exit_code)
        return rows, exit_code
    finally:
        client.logout()
