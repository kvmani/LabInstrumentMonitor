"""Durable exports (EXP-01): raw rows as CSV plus a manifest, in one ZIP per job.

* A request creates a ``queued`` job after scope and size checks; nothing heavy runs in the API.
* A worker claims jobs with a time-limited lease (``FOR UPDATE SKIP LOCKED``). A killed worker's
  lease expires and another worker retries the job from scratch; after ``max_attempts`` it fails.
* Rows come from one ``REPEATABLE READ`` read-only snapshot. The manifest records the snapshot
  (``pg_snapshot_xmin`` and database time) as the export cutoff — a wall-clock cutoff alone could
  not exclude concurrent commits.
* The ZIP is written to a hidden partial file, fsynced and atomically renamed; only then is the
  job marked ``succeeded`` with the file checksum. Partial exports are never visible.
* Downloads re-check the user's current lab access and the expiry; files are deleted on expiry.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import uuid
import zipfile
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, Engine, func, insert, select, text, update

from labmon import __version__
from labmon.contracts.envelope import format_utc
from labmon.server.models import Channel, ExportJob, Instrument, Lab, ProfileRevision, Sample
from labmon.server.security import Principal, audit
from labmon.server.series import CSV_COLUMNS, iter_sample_records
from labmon.server.settings import Settings

log = logging.getLogger(__name__)
MANIFEST_FORMAT = "labmon-export/1"


class ExportError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamps need an explicit UTC offset")
    return v


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: Annotated[datetime, AfterValidator(_aware)]
    end: Annotated[datetime, AfterValidator(_aware)]
    channels: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]] | None = Field(
        default=None, max_length=32
    )

    @model_validator(mode="after")
    def _range(self) -> ExportRequest:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end - self.start > timedelta(days=31):
            raise ValueError("exports are limited to 31 days per job")
        return self


def job_view(j: Any) -> dict[str, Any]:
    return {
        "id": j.id,
        "instrument_id": j.instrument_id,
        "status": j.status,
        "params": j.params,
        "created_at": format_utc(j.created_at),
        "finished_at": None if j.finished_at is None else format_utc(j.finished_at),
        "attempts": j.attempts,
        "row_count": j.row_count,
        "byte_size": j.byte_size,
        "sha256": j.sha256,
        "file_name": j.file_name,
        "expires_at": None if j.expires_at is None else format_utc(j.expires_at),
        "error": j.error,
    }


def _channel_ids(conn: Connection, instrument_id: str, keys: list[str] | None) -> list[str]:
    rows = conn.execute(
        select(Channel.id, Channel.key).where(Channel.instrument_id == instrument_id)
    ).all()
    if keys is None:
        return [r.id for r in rows]
    known = {r.key: r.id for r in rows}
    unknown = sorted(set(keys) - set(known))
    if unknown:
        raise ExportError(422, f"unknown channels {unknown}")
    return [known[k] for k in keys]


def create_job(
    conn: Connection,
    p: Principal,
    instrument_id: str,
    body: ExportRequest,
    now: datetime,
    settings: Settings,
) -> dict[str, Any]:
    inst = conn.execute(select(Instrument).where(Instrument.id == instrument_id)).first()
    if inst is None or not p.can_view(inst.lab_id):
        raise ExportError(404, "instrument not found")
    channel_ids = _channel_ids(conn, instrument_id, body.channels)
    n = conn.execute(
        select(func.count())
        .select_from(Sample)
        .where(
            Sample.instrument_id == instrument_id,
            Sample.channel_id.in_(channel_ids),
            Sample.event_time >= body.start,
            Sample.event_time < body.end,
        )
    ).scalar_one()
    if n > settings.export_row_limit:
        raise ExportError(
            413,
            f"{n} rows exceed the {settings.export_row_limit}-row export limit; split the range",
        )
    job_id = uuid.uuid4().hex
    params = {
        "start": format_utc(body.start),
        "end": format_utc(body.end),
        "channels": body.channels,
        "estimated_rows": int(n),
    }
    conn.execute(
        insert(ExportJob).values(
            id=job_id,
            user_id=p.user_id,
            instrument_id=instrument_id,
            params=params,
            status="queued",
            created_at=now,
            attempts=0,
        )
    )
    audit(
        conn,
        now,
        "user",
        str(p.user_id),
        "export_requested",
        instrument_id,
        {"export_id": job_id, **params},
    )
    return job_view(conn.execute(select(ExportJob).where(ExportJob.id == job_id)).one())


def get_job_for(conn: Connection, p: Principal, job_id: str) -> Any:
    """Access is re-checked on every read: the requester must still see the instrument's lab."""
    j = conn.execute(select(ExportJob).where(ExportJob.id == job_id)).first()
    if j is None or j.user_id != p.user_id:
        raise ExportError(404, "export not found")
    lab = conn.execute(select(Instrument.lab_id).where(Instrument.id == j.instrument_id)).scalar()
    if lab is None or not p.can_view(lab):
        raise ExportError(404, "export not found")
    return j


def list_jobs(
    conn: Connection, p: Principal, instrument_id: str | None, limit: int = 20
) -> list[dict[str, Any]]:
    q = select(ExportJob).where(ExportJob.user_id == p.user_id)
    if instrument_id:
        q = q.where(ExportJob.instrument_id == instrument_id)
    rows = conn.execute(q.order_by(ExportJob.created_at.desc()).limit(limit)).all()
    labs = {str(i): str(lab) for i, lab in conn.execute(select(Instrument.id, Instrument.lab_id))}
    return [job_view(r) for r in rows if p.can_view(labs.get(r.instrument_id, ""))]


def export_path(settings: Settings, job_id: str) -> Path:
    return settings.export_dir / f"{job_id}.zip"


# --- worker side -----------------------------------------------------------------------------


def claim_job(engine: Engine, worker: str, now: datetime, lease_s: float) -> Any:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
            UPDATE export_job SET status = 'running', lease_owner = :w,
              lease_expires_at = :exp, attempts = attempts + 1, started_at = :now, error = NULL
            WHERE id = (
              SELECT id FROM export_job
              WHERE status = 'queued' OR (status = 'running' AND lease_expires_at < :now)
              ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *
            """
            ),
            {"w": worker, "now": now, "exp": now + timedelta(seconds=lease_s)},
        ).first()
    return row


def _write_zip(engine: Engine, job: Any, settings: Settings, now: datetime) -> dict[str, Any]:
    params = job.params
    start = datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    final = export_path(settings, job.id)
    partial = settings.export_dir / f".{job.id}.zip.partial"
    with engine.connect().execution_options(
        isolation_level="REPEATABLE READ", postgresql_readonly=True
    ) as conn:
        snap = conn.execute(
            text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text AS xmin, now() AS db_time")
        ).one()
        inst = conn.execute(
            select(Instrument, Lab.name.label("lab_name"))
            .join(Lab, Lab.id == Instrument.lab_id)
            .where(Instrument.id == job.instrument_id)
        ).one()
        channel_ids = _channel_ids(conn, job.instrument_id, params.get("channels"))
        channels = conn.execute(
            select(Channel).where(Channel.id.in_(channel_ids)).order_by(Channel.sort_order)
        ).all()
        by_channel: Counter[str] = Counter()
        by_quality: Counter[str] = Counter()
        revisions: set[str] = set()
        digest = hashlib.sha256()
        rows = 0
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            with zf.open("data.csv", "w", force_zip64=True) as raw:
                text_out = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                hashing = _HashingWriter(text_out, digest)
                writer = csv.writer(hashing, lineterminator="\n")
                writer.writerow(CSV_COLUMNS)
                for row, s in iter_sample_records(conn, job.instrument_id, start, end, channel_ids):
                    writer.writerow(row)
                    rows += 1
                    by_channel[s.channel_id] += 1
                    by_quality[s.quality] += 1
                    revisions.add(s.profile_revision_id)
                text_out.flush()
                text_out.detach()
            rev_rows = conn.execute(
                select(
                    ProfileRevision.id,
                    ProfileRevision.revision,
                    ProfileRevision.definition_digest,
                    ProfileRevision.adapter_version,
                ).where(ProfileRevision.id.in_(sorted(revisions)))
            ).all()
            manifest = {
                "format": MANIFEST_FORMAT,
                "export_id": job.id,
                "app_version": __version__,
                "generated_at": format_utc(now),
                "instrument": {
                    "id": inst.id,
                    "name": inst.name,
                    "lab": inst.lab_name,
                    "simulated": inst.simulated,
                    "profile": inst.profile_key,
                },
                "simulated": bool(inst.simulated),
                "range": {
                    "start": params["start"],
                    "end": params["end"],
                    "interval": "[start,end)",
                    "time_zone": "UTC (RFC 3339, Z)",
                    "display_time_zone": settings.display_timezone,
                },
                "filters": {"channels": params.get("channels") or "all"},
                "cutoff": {
                    "snapshot_xmin": snap.xmin,
                    "database_time": format_utc(snap.db_time),
                    "consistency": "single REPEATABLE READ read-only snapshot",
                },
                "channels": [
                    {
                        "id": c.id,
                        "key": c.key,
                        "name": c.name,
                        "quantity": c.quantity,
                        "unit": c.unit,
                        "data_type": c.data_type,
                        "enum_labels": c.enum_labels,
                    }
                    for c in channels
                ],
                "profile_revisions": [
                    {
                        "id": r.id,
                        "revision": r.revision,
                        "definition_digest": r.definition_digest,
                        "adapter_version": r.adapter_version,
                    }
                    for r in rev_rows
                ],
                "counts": {
                    "rows": rows,
                    "by_channel": dict(by_channel),
                    "by_quality": dict(by_quality),
                },
                "files": {
                    "data.csv": {"sha256": digest.hexdigest(), "rows": rows + 1, "header": True}
                },
                "notes": "Raw committed samples, not chart aggregates. Bad-quality rows keep "
                "their diagnostic fields with an empty value. Text fields that start "
                "with = + - @ are prefixed with ' (spreadsheet formula safety).",
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    with partial.open("rb") as fh:
        os.fsync(fh.fileno())
        zip_digest = hashlib.file_digest(fh, "sha256").hexdigest()
    os.replace(partial, final)  # atomic publication within the export directory
    if hasattr(os, "O_DIRECTORY"):  # make the rename itself durable (POSIX)
        dfd = os.open(settings.export_dir, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return {"rows": rows, "sha256": zip_digest, "bytes": final.stat().st_size, "manifest": manifest}


class _HashingWriter:
    """Text sink that hashes exactly the UTF-8 bytes written to the CSV member."""

    def __init__(self, inner: io.TextIOWrapper, digest: Any) -> None:
        self.inner, self.digest = inner, digest

    def write(self, s: str) -> int:
        self.digest.update(s.encode("utf-8"))
        return self.inner.write(s)


def run_claimed(
    engine: Engine,
    job: Any,
    settings: Settings,
    now: Callable[[], datetime],
    fault: Callable[[str], None] | None = None,
) -> str:
    """Execute one claimed job. ``fault`` is a test hook called before publication."""
    try:
        result = _write_zip(engine, job, settings, now())
        if fault is not None:
            fault(job.id)
    except Exception as exc:  # noqa: BLE001 - recorded on the job; the worker keeps running
        log.exception("export %s failed", job.id)
        status = "failed" if job.attempts >= settings.export_max_attempts else "queued"
        with engine.begin() as conn:
            conn.execute(
                update(ExportJob)
                .where(ExportJob.id == job.id)
                .values(
                    status=status,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
        return status
    finished = now()
    with engine.begin() as conn:
        conn.execute(
            update(ExportJob)
            .where(ExportJob.id == job.id)
            .values(
                status="succeeded",
                finished_at=finished,
                row_count=result["rows"],
                byte_size=result["bytes"],
                sha256=result["sha256"],
                manifest=result["manifest"],
                file_name=f"{'SIMULATED_' if result['manifest']['simulated'] else ''}"
                f"{job.instrument_id}_{job.id[:8]}.zip",
                expires_at=finished + timedelta(days=settings.export_retention_days),
                lease_owner=None,
                lease_expires_at=None,
            )
        )
    return "succeeded"


def expire(engine: Engine, settings: Settings, now: datetime) -> int:
    with engine.begin() as conn:
        rows = conn.execute(
            update(ExportJob)
            .where(ExportJob.status == "succeeded", ExportJob.expires_at < now)
            .values(status="expired")
            .returning(ExportJob.id)
        ).all()
    for (job_id,) in rows:
        export_path(settings, job_id).unlink(missing_ok=True)
    return len(rows)


def process_exports(
    engine: Engine, settings: Settings, worker: str, now: Callable[[], datetime]
) -> int:
    done = 0
    while (job := claim_job(engine, worker, now(), settings.export_lease_s)) is not None:
        run_claimed(engine, job, settings, now)
        done += 1
    expire(engine, settings, now())
    return done
