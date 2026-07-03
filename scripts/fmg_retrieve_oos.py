#!/usr/bin/env python3
"""
fmg_retrieve_oos.py

Runs FROM a Linux server (cron / systemd timer) against the FortiManager
JSON-RPC API. Scans the FortiGates registered in a given (backup) ADOM and,
for every device whose config is out-of-sync (conf_status == "outofsync"),
triggers a "Retrieve Config" (device -> FortiManager), i.e. the API
equivalent of "diagnose test deploymanager reloadconf <oid>", but done
properly over HTTPS/JSON-RPC instead of an SSH CLI scrape.

No third-party dependencies: only the Python standard library is used, so
it can be dropped on any Linux host with Python 3.6+.

Usage:
    fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve
    fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --dry-run
    fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --all
    fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --wait

Credentials:
    The password is NEVER passed on the command line. Provide it via:
      - env var FMG_PASSWORD, or
      - env var FMG_PASSWORD_FILE=/path/to/secret (file mode should be 600)

Exit codes (useful for cron/monitoring):
    0  OK - nothing out of sync, or all retrieves succeeded
    1  one or more retrieve tasks failed or ended in error/timeout
    2  API/authentication/connection error
    3  invalid arguments / configuration
"""

import argparse
import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

DEFAULT_TIMEOUT = 30
DEFAULT_TASK_POLL_INTERVAL = 5
DEFAULT_TASK_TIMEOUT = 600

OUT_OF_SYNC_STATUSES = {"outofsync"}


class FmgApiError(Exception):
    pass


class FmgClient:
    def __init__(self, host, verify_ssl=True, timeout=DEFAULT_TIMEOUT, port=443):
        self.base_url = f"https://{host}:{port}/jsonrpc"
        self.timeout = timeout
        self.session = None
        self._id = 0
        self._ctx = ssl.create_default_context()
        if not verify_ssl:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def _next_id(self):
        self._id += 1
        return self._id

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
        if use_session and self.session:
            payload["session"] = self.session

        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                body = json.loads(resp.read().decode("utf-8"))
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

    def login(self, user, password):
        # session token is returned at the top level of the response, not
        # inside "result", so this can't reuse the generic _call() helper.
        payload = {
            "id": self._next_id(),
            "method": "exec",
            "params": [{"url": "/sys/login/user", "data": {"user": user, "passwd": password}}],
        }
        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                body = json.loads(resp.read().decode("utf-8"))
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
        if not self.session:
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
        result = self._call(
            "exec",
            "/dvm/cmd/update/device",
            data={
                "adom": adom,
                "device": device_name,
                "flags": ["create_task", "nonblocking"],
            },
        )
        data = result.get("data", {})
        return data.get("task")

    def get_task(self, task_id):
        result = self._call("get", f"/task/task/{task_id}")
        return result.get("data", {})


def wait_for_task(client, task_id, poll_interval, timeout, log):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get_task(task_id)
        state = task.get("state")
        percent = task.get("percent", 0)
        log.info("  task %s: state=%s percent=%s%%", task_id, state, percent)
        if state in ("done", "error", "cancelled", "aborted"):
            return state
        time.sleep(poll_interval)
    log.warning("  task %s: timed out after %ss", task_id, timeout)
    return "timeout"


def read_password(args):
    if args.password_file:
        with open(args.password_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    if os.environ.get("FMG_PASSWORD_FILE"):
        with open(os.environ["FMG_PASSWORD_FILE"], "r", encoding="utf-8") as fh:
            return fh.read().strip()
    if os.environ.get("FMG_PASSWORD"):
        return os.environ["FMG_PASSWORD"]
    return None


def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Scan a FortiManager ADOM over the JSON-RPC API and retrieve "
        "config from any out-of-sync FortiGate."
    )
    p.add_argument("--host", required=True, help="FortiManager hostname or IP")
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--adom", required=True, help="ADOM to scan (e.g. the backup ADOM)")
    p.add_argument("--user", required=True, help="API user (dedicated, least-privilege)")
    p.add_argument(
        "--password-file",
        help="Path to a file containing the API user's password (mode 600 recommended). "
        "Falls back to FMG_PASSWORD_FILE or FMG_PASSWORD env vars.",
    )
    p.add_argument(
        "--all", action="store_true", help="Retrieve every device in the ADOM, not just out-of-sync ones"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="List out-of-sync devices only, trigger nothing"
    )
    p.add_argument(
        "--wait", action="store_true", help="Poll each retrieve task until it finishes instead of firing-and-forgetting"
    )
    p.add_argument("--poll-interval", type=int, default=DEFAULT_TASK_POLL_INTERVAL)
    p.add_argument("--task-timeout", type=int, default=DEFAULT_TASK_TIMEOUT)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP request timeout (s)")
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (lab use only, avoid in production)",
    )
    p.add_argument("--log-file", help="Optional log file path (in addition to stdout)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def setup_logging(args):
    log = logging.getLogger("fmg_retrieve_oos")
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    if args.log_file:
        fh = logging.FileHandler(args.log_file)
        fh.setFormatter(fmt)
        log.addHandler(fh)

    return log


def main():
    args = build_arg_parser().parse_args()
    log = setup_logging(args)

    password = read_password(args)
    if not password:
        log.error(
            "no password provided: use --password-file, or set FMG_PASSWORD_FILE / FMG_PASSWORD"
        )
        return 3

    client = FmgClient(args.host, verify_ssl=not args.insecure, timeout=args.timeout, port=args.port)

    try:
        client.login(args.user, password)
    except FmgApiError as exc:
        log.error("login failed: %s", exc)
        return 2

    exit_code = 0
    try:
        try:
            devices = client.get_devices(args.adom)
        except FmgApiError as exc:
            log.error("failed to list devices in ADOM '%s': %s", args.adom, exc)
            return 2

        log.info("ADOM '%s': %d device(s) found", args.adom, len(devices))

        if args.all:
            targets = devices
        else:
            targets = [d for d in devices if d.get("conf_status") in OUT_OF_SYNC_STATUSES]

        if not targets:
            log.info("No out-of-sync devices detected in ADOM '%s'.", args.adom)
            return 0

        log.info("Devices selected for retrieve (%d):", len(targets))
        for d in targets:
            log.info(
                "  name=%s sn=%s conf_status=%s db_status=%s conn_status=%s ip=%s",
                d.get("name"), d.get("sn"), d.get("conf_status"),
                d.get("db_status"), d.get("conn_status"), d.get("ip"),
            )

        if args.dry_run:
            log.info("Dry-run: no retrieve triggered.")
            return 0

        for d in targets:
            name = d.get("name")
            try:
                task_id = client.retrieve_config(args.adom, name)
            except FmgApiError as exc:
                log.error("retrieve failed to start for %s: %s", name, exc)
                exit_code = 1
                continue

            log.info(">> retrieve triggered for %s (task=%s)", name, task_id)

            if args.wait and task_id:
                state = wait_for_task(client, task_id, args.poll_interval, args.task_timeout, log)
                if state != "done":
                    log.error("retrieve for %s ended with state=%s", name, state)
                    exit_code = 1
                else:
                    log.info("retrieve for %s completed successfully", name)

        return exit_code
    finally:
        client.logout()


if __name__ == "__main__":
    sys.exit(main())
