"""Operator diagnostics (OPS-01): fault classification with a next step, and system health.

``instrument_faults`` is a pure function over the status snapshot so every rule is unit-tested
without a database. Severities: ``critical`` (data is or will be lost), ``warning`` (data is
late, suspect or at risk), ``info``. Each fault names one concrete next step for the operator.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from labmon.contracts.envelope import format_utc
from labmon.server.backup import backup_status
from labmon.server.settings import Settings
from labmon.server.tls import cached_check

CLOCK_OFFSET_WARN_S = 5.0
BACKLOG_WARN_S = 60.0
DISK_FREE_WARN_FRACTION = 0.10
DISK_FREE_CRIT_BYTES = 512 * 1024 * 1024


def _f(severity: str, code: str, message: str, next_step: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message, "next_step": next_step}


def instrument_faults(status: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    coll = status.get("collector") or {}
    acq = status.get("acquisition") or {}
    conn = coll.get("connectivity")
    if acq.get("reason_code") == "no_assignment":
        out.append(
            _f(
                "info",
                "no_collector",
                "No collector is assigned to this instrument.",
                "An administrator assigns a collector during commissioning.",
            )
        )
    elif conn == "revoked":
        out.append(
            _f(
                "critical",
                "collector_revoked",
                "The collector credential is revoked.",
                "Issue a new credential to the collector and restart its service.",
            )
        )
    elif conn == "never_seen":
        out.append(
            _f(
                "warning",
                "collector_never_seen",
                "The collector has never connected.",
                "Start the labmon-collector service on the instrument PC and check its "
                "server URL and credential file.",
            )
        )
    elif conn == "offline":
        out.append(
            _f(
                "warning",
                "collector_offline",
                "No heartbeat from the collector for more than 30 s.",
                "Check that the instrument PC is on and the labmon-collector service is "
                "running; it uploads its backlog when it reconnects.",
            )
        )
    fault = coll.get("upload_fault")
    if fault:
        if "authentication" in fault:
            out.append(
                _f(
                    "critical",
                    "upload_auth",
                    f"Uploads stopped: {fault}.",
                    "Re-issue the collector credential and restart the collector; "
                    "readings are kept in its spool meanwhile.",
                )
            )
        else:
            out.append(
                _f(
                    "critical",
                    "upload_fault",
                    f"Uploads stopped: {fault}.",
                    "Run `labmon-collector status` on the instrument PC and contact the "
                    "administrator; readings are kept in the spool.",
                )
            )
    if conn == "online":
        offset = coll.get("clock_offset_s")
        if isinstance(offset, int | float) and abs(offset) > CLOCK_OFFSET_WARN_S:
            out.append(
                _f(
                    "warning",
                    "clock_offset",
                    f"Collector clock differs from the server by {offset:+.1f} s.",
                    "Enable time synchronisation (NTP) on the instrument PC.",
                )
            )
        age = coll.get("backlog_age_s") or 0
        if age > BACKLOG_WARN_S:
            out.append(
                _f(
                    "warning",
                    "delivery_delayed",
                    f"Oldest unsent reading is {age:.0f} s old "
                    f"({coll.get('unacknowledged')} pending).",
                    "Check the API health and the network path from the instrument PC.",
                )
            )
        free, total = coll.get("spool_disk_free_bytes"), coll.get("spool_disk_total_bytes")
        if isinstance(free, int) and isinstance(total, int) and total > 0:
            if free < DISK_FREE_CRIT_BYTES:
                out.append(
                    _f(
                        "critical",
                        "spool_disk_low",
                        f"Only {free / 2**20:.0f} MiB free for the collector spool.",
                        "Free disk space on the instrument PC now; acquisition stops "
                        "when the spool cannot grow.",
                    )
                )
            elif free / total < DISK_FREE_WARN_FRACTION:
                out.append(
                    _f(
                        "warning",
                        "spool_disk_low",
                        f"{free / total:.0%} disk free for the collector spool.",
                        "Free disk space on the instrument PC.",
                    )
                )
        for src in coll.get("sources") or []:
            if src.get("state") == "spool_full":
                out.append(
                    _f(
                        "critical",
                        "spool_full",
                        "The collector spool is full; new readings are not being stored.",
                        "Restore the upload path or free disk space; the lost interval "
                        "is recorded.",
                    )
                )
        if coll.get("quarantined"):
            out.append(
                _f(
                    "warning",
                    "quarantined",
                    f"{coll['quarantined']} records were rejected by the server.",
                    "Run `labmon-collector quarantine` and send the output to the administrator.",
                )
            )
    if acq.get("state") == "source_unavailable":
        out.append(
            _f(
                "warning",
                "source_unavailable",
                "The collector is online but the instrument does not answer.",
                "Check the instrument link (cable, power, port); the collector retries on its own.",
            )
        )
    if acq.get("reason_code") == "bad_quality":
        names = ", ".join(c.get("key", "?") for c in acq.get("channels", []))
        out.append(
            _f(
                "warning",
                "bad_quality",
                f"Bad readings on {names}.",
                "Inspect the sensor and its wiring (open thermocouple, gauge error).",
            )
        )
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["severity"]])


def system_health(conn: Connection, settings: Settings, now: datetime) -> dict[str, Any]:
    q = conn.execute(text("SELECT count(*), min(marked_at) FROM rollup_dirty")).one()
    jobs = {
        str(k): int(v)
        for k, v in conn.execute(text("SELECT status, count(*) FROM export_job GROUP BY status"))
    }
    db_bytes = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
    try:
        settings.export_dir.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(settings.export_dir)
        export_disk = {"free_bytes": disk.free, "total_bytes": disk.total}
    except OSError:
        export_disk = None
    faults: list[dict[str, str]] = []
    oldest = q[1]
    if oldest is not None and (now - oldest).total_seconds() > 300:
        faults.append(
            _f(
                "warning",
                "rollups_behind",
                "Minute rollups are more than 5 min behind.",
                "Check that the `labmon-server worker` service is running.",
            )
        )
    if jobs.get("failed"):
        faults.append(
            _f(
                "warning",
                "exports_failed",
                f"{jobs['failed']} export job(s) failed.",
                "Open the failed export to see its error; retry with a smaller range.",
            )
        )
    backup = backup_status(settings.backup_status_file, now)
    if backup is None:
        faults.append(
            _f(
                "warning",
                "backups_not_monitored",
                "Backup freshness is not monitored (LABMON_BACKUP_STATUS_FILE is not set).",
                "Schedule `labmon-server backup` and point LABMON_BACKUP_STATUS_FILE at "
                "its status file.",
            )
        )
    elif not backup.get("ok"):
        faults.append(
            _f(
                "critical",
                "backup_failed",
                "The last backup failed.",
                "Read the backup log, fix the cause and run `labmon-server backup`.",
            )
        )
    elif backup.get("age_hours", 0) > 26:
        faults.append(
            _f(
                "warning",
                "backup_stale",
                f"The last good backup is {backup['age_hours']:.0f} h old.",
                "Check the backup timer and run `labmon-server backup` now.",
            )
        )
    tls = None
    if settings.tls_check_url:
        tls = cached_check(settings.tls_check_url, settings.tls_ca_file, now)
        if tls["status"] != "ok":
            faults.append(
                _f(
                    "critical" if tls["status"] == "fail" else "warning",
                    "tls_certificate",
                    f"HTTPS certificate: {tls['detail']}.",
                    "Renew or reinstall the site certificate (docs/operations/certificates.md). "
                    "Collectors keep spooling until uploads succeed again.",
                )
            )
    elif settings.environment == "production":
        faults.append(
            _f(
                "warning",
                "tls_not_monitored",
                "Certificate expiry is not monitored (LABMON_TLS_CHECK_URL is not set).",
                "Set LABMON_TLS_CHECK_URL to the URL clients use, e.g. https://monitor.lab.example.",
            )
        )
    return {
        "server_time": format_utc(now),
        "tls": tls,
        "rollup_queue": {
            "minutes": int(q[0]),
            "oldest_marked_at": None if oldest is None else format_utc(oldest),
        },
        "export_jobs": {k: int(v) for k, v in jobs.items()},
        "database_bytes": int(db_bytes),
        "export_disk": export_disk,
        "backup": backup,
        "faults": faults,
    }
