#!/usr/bin/env python3
"""
fmg_retrieve_oos.py

Runs FROM a Linux server (cron / systemd timer) against the FortiManager
JSON-RPC API. Scans the FortiGates registered in a given (backup) ADOM and,
for every device whose config is out-of-sync (conf_status == "outofsync"
*exactly* - "unknown" and any other status are left untouched) AND reachable
(conn_status == "up"), triggers a "Retrieve Config" (device -> FortiManager),
i.e. the API equivalent of "diagnose test deploymanager reloadconf <oid>",
but done properly over HTTPS/JSON-RPC instead of an SSH CLI scrape.

Every run writes a clear report to stdout and, unless disabled, to its own
timestamped file under --log-dir (one file per run, e.g.
fmg-retrieve-oos_2026-07-03_14-05-00.log): a table listing every device in
the ADOM with its sync/connectivity status, whether a retrieve was
triggered (and at what time), and the outcome (success/failed + reason).

No third-party dependencies: only the Python standard library is used, so
it can be dropped on any Linux host with Python 3.6+.

Usage:
    fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve
    fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --dry-run
    fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --all
    fmg_retrieve_oos.py --host fmg.example.com --adom BACKUP --user api-retrieve --no-wait

Credentials - two mutually exclusive modes:
  - Classic admin account: --user + a password, NEVER passed on the command
    line. Provide the password via env var FMG_PASSWORD, or
    FMG_PASSWORD_FILE=/path/to/secret (file mode should be 600).
  - REST API Admin (token auth, recommended by Fortinet): --api-key-file
    pointing at a file containing the token, or env var FMG_API_KEY_FILE /
    FMG_API_KEY. --user is not needed in this mode.

Exit codes (useful for cron/monitoring):
    0  OK - nothing out of sync, or all retrieves succeeded
    1  one or more retrieve tasks failed or ended in error/timeout
    2  API/authentication/connection error
    3  invalid arguments / configuration
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from fmg_common import (  # noqa: E402
    DEFAULT_LOG_DIR,
    DEFAULT_LOG_RETENTION_DAYS,
    FmgApiError,
    append_resync_history,
    format_report_table,
    make_run_log_path,
    purge_old_logs,
    run_scan,
)


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


def read_api_key(args):
    if args.api_key_file:
        with open(args.api_key_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    if os.environ.get("FMG_API_KEY_FILE"):
        with open(os.environ["FMG_API_KEY_FILE"], "r", encoding="utf-8") as fh:
            return fh.read().strip()
    if os.environ.get("FMG_API_KEY"):
        return os.environ["FMG_API_KEY"]
    return None


def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Scan a FortiManager ADOM over the JSON-RPC API and retrieve "
        "config from any out-of-sync, reachable FortiGate."
    )
    p.add_argument("--host", required=True, help="FortiManager hostname or IP")
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--adom", required=True, help="ADOM to scan (e.g. the backup ADOM)")
    p.add_argument(
        "--user", help="API user (dedicated, least-privilege). Required unless --api-key-file is used."
    )
    p.add_argument(
        "--password-file",
        help="Path to a file containing the API user's password (mode 600 recommended). "
        "Falls back to FMG_PASSWORD_FILE or FMG_PASSWORD env vars.",
    )
    p.add_argument(
        "--api-key-file",
        help="Path to a file containing a FortiManager REST API Admin token (mode 600 "
        "recommended), used instead of --user/password. Falls back to FMG_API_KEY_FILE "
        "or FMG_API_KEY env vars.",
    )
    p.add_argument(
        "--all", action="store_true", help="Consider every device in the ADOM, not just out-of-sync ones "
        "(still skips unreachable/DOWN devices)"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="List out-of-sync devices only, trigger nothing"
    )
    p.add_argument(
        "--no-wait",
        action="store_true",
        help="Do not poll retrieve tasks until completion (fire-and-forget). "
        "By default the script waits so the report can show success/failed + reason.",
    )
    p.add_argument("--poll-interval", type=int, default=5)
    p.add_argument("--task-timeout", type=int, default=600)
    p.add_argument("--timeout", type=int, default=30, help="HTTP request timeout (s)")
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (lab use only, avoid in production)",
    )
    p.add_argument(
        "--log-dir",
        default=DEFAULT_LOG_DIR,
        help=f"Directory for the per-run log file, in addition to stdout "
        f"(default: {DEFAULT_LOG_DIR}). One timestamped file is created per "
        "run (fmg-retrieve-oos_YYYY-MM-DD_HH-MM-SS.log). Pass an empty "
        "string to disable file logging.",
    )
    p.add_argument(
        "--log-retention-days",
        type=int,
        default=DEFAULT_LOG_RETENTION_DAYS,
        help=f"Delete previous run log files older than this many days at the "
        f"start of each run (default: {DEFAULT_LOG_RETENTION_DAYS}). 0 disables purging.",
    )
    p.add_argument(
        "--trigger",
        choices=["manual", "scheduler"],
        default="manual",
        help="Purely informational: tags the log line and per-run log filename so "
        "'last manual run' and 'last scheduled run' can be told apart (e.g. when "
        "manual and timer-triggered runs use separate systemd units).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def setup_logging(args):
    log = logging.getLogger("fmg_retrieve_oos")
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    if args.log_dir:
        try:
            os.makedirs(args.log_dir, exist_ok=True)
            purge_old_logs(args.log_dir, args.log_retention_days)
            log_path = make_run_log_path(args.log_dir, trigger=args.trigger)
            fh = logging.FileHandler(log_path)
            fh.setFormatter(fmt)
            log.addHandler(fh)
            log.info("Log de ce run (%s): %s", args.trigger, log_path)
        except OSError as exc:
            log.warning(
                "cannot write to log dir '%s' (%s); continuing with stdout only",
                args.log_dir, exc,
            )

    return log


def make_on_event(log, adom):
    def on_event(kind, **payload):
        if kind == "devices_listed":
            log.info("ADOM '%s': %d device(s) found", adom, payload["count"])
        elif kind == "classified" and not payload["target_rows"]:
            log.info("No device to retrieve in ADOM '%s' (none out-of-sync and reachable).", adom)
        elif kind == "retrieve_start":
            r = payload["row"]
            log.info(">> retrieve triggered for %s at %s", r["name"], r["triggered_at"])
        elif kind == "task_poll":
            r, state, percent = payload["row"], payload["state"], payload["percent"]
            log.info("  %s: state=%s percent=%s%%", r["name"], state, percent)
        elif kind == "retrieve_result":
            r = payload["row"]
            if r["result"] == "SUCCESS":
                log.info("retrieve for %s completed successfully", r["name"])
            elif r["result"] == "FAILED":
                log.error("retrieve for %s failed: %s", r["name"], r["reason"])
        elif kind == "done":
            log.info("\n" + format_report_table(payload["rows"]))

    return on_event


def main():
    args = build_arg_parser().parse_args()
    log = setup_logging(args)

    api_key = read_api_key(args)
    password = None
    if not api_key:
        if not args.user:
            log.error(
                "no credentials: use --user + --password-file (or FMG_PASSWORD_FILE/"
                "FMG_PASSWORD), or --api-key-file (or FMG_API_KEY_FILE/FMG_API_KEY) "
                "for a REST API Admin token"
            )
            return 3
        password = read_password(args)
        if not password:
            log.error(
                "no password provided: use --password-file, or set FMG_PASSWORD_FILE / FMG_PASSWORD"
            )
            return 3

    log.info("Démarrage du scan (%s) sur %s / ADOM %s", args.trigger, args.host, args.adom)

    try:
        rows, exit_code = run_scan(
            host=args.host,
            port=args.port,
            user=args.user,
            password=password,
            api_key=api_key,
            adom=args.adom,
            all_devices=args.all,
            dry_run=args.dry_run,
            no_wait=args.no_wait,
            poll_interval=args.poll_interval,
            task_timeout=args.task_timeout,
            timeout=args.timeout,
            verify_ssl=not args.insecure,
            on_event=make_on_event(log, args.adom),
        )
    except FmgApiError as exc:
        log.error("FortiManager API error: %s", exc)
        return 2

    if args.dry_run:
        log.info("Dry-run: no retrieve triggered.")
    elif args.log_dir:
        append_resync_history(args.log_dir, rows, args.trigger)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
