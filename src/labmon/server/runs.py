"""Run records derived from instrument-reported run events.

``acquisition_event`` rows remain the source of truth; the ``run`` table is a queryable
projection maintained inside the ingest transaction:

* ``run_started`` inserts the run (``ON CONFLICT DO NOTHING``), so a collector restart that
  re-announces the current run is idempotent.
* ``run_ended`` sets ``ended_at`` once; later duplicates do not move it.

Run IDs are ``<instrument_id>.<run_key>``; the simulator uses one key per program cycle, so a
repeated recipe always produces a new run and earlier history is preserved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection, and_, desc, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from labmon.contracts.envelope import Envelope, EventType, format_utc
from labmon.server.models import Run

RUN_EVENTS = (EventType.RUN_STARTED, EventType.RUN_ENDED)
RunKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")]


class RunStartedDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_key: RunKey
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    program_start: datetime
    program_end: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunEndedDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_key: RunKey
    end_reason: Literal["completed", "aborted"]
    program_end: datetime


def validate_run_event(e: Envelope) -> str | None:
    """Return a problem description, or None when the run event detail is well formed."""
    model = RunStartedDetail if e.event_type is EventType.RUN_STARTED else RunEndedDetail
    try:
        d = model.model_validate(e.detail or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        return f"{'.'.join(str(p) for p in first.get('loc', ()))}: {first.get('msg')}"[:200]
    for ts in (getattr(d, "program_start", None), d.program_end):
        if ts is not None and ts.tzinfo is None:
            return "run timestamps need an offset"
    return None


def run_id(instrument_id: str, run_key: str) -> str:
    return f"{instrument_id}.{run_key}"


def apply_run_events(conn: Connection, events: list[Envelope], now: datetime) -> None:
    for e in sorted(events, key=lambda x: x.identity):
        if e.event_type is EventType.RUN_STARTED:
            s = RunStartedDetail.model_validate(e.detail or {})
            # A collector restart can miss a run's end. A newer run on the same instrument
            # proves the older one is over: close it at its planned end (or this start) and
            # say so, instead of leaving it "running" forever.
            conn.execute(
                update(Run)
                .where(
                    and_(
                        Run.instrument_id == e.instrument_id,
                        Run.ended_at.is_(None),
                        Run.started_at < s.program_start,
                        Run.run_key != s.run_key,
                    )
                )
                .values(
                    ended_at=func.least(
                        func.coalesce(Run.planned_end_at, s.program_start), s.program_start
                    ),
                    end_reason="end_not_reported",
                )
            )
            conn.execute(
                pg_insert(Run)
                .values(
                    id=run_id(e.instrument_id, s.run_key),
                    instrument_id=e.instrument_id,
                    run_key=s.run_key,
                    name=s.name,
                    kind=s.kind,
                    source="instrument",
                    simulated=e.simulated,
                    started_at=s.program_start,
                    planned_end_at=s.program_end,
                    run_metadata=s.metadata,
                    first_seen_at=now,
                )
                .on_conflict_do_nothing(index_elements=["instrument_id", "run_key"])
            )
        elif e.event_type is EventType.RUN_ENDED:
            d = RunEndedDetail.model_validate(e.detail or {})
            conn.execute(
                update(Run)
                .where(
                    and_(
                        Run.instrument_id == e.instrument_id,
                        Run.run_key == d.run_key,
                        Run.ended_at.is_(None),
                    )
                )
                .values(ended_at=d.program_end, end_reason=d.end_reason)
            )


def run_view(r: Any, now: datetime) -> dict[str, Any]:
    end = r.ended_at
    return {
        "id": r.id,
        "instrument_id": r.instrument_id,
        "run_key": r.run_key,
        "name": r.name,
        "kind": r.kind,
        "source": r.source,
        "simulated": r.simulated,
        "started_at": format_utc(r.started_at),
        "planned_end_at": None if r.planned_end_at is None else format_utc(r.planned_end_at),
        "ended_at": None if end is None else format_utc(end),
        "end_reason": r.end_reason,
        "active": run_is_active(r, now),
        "metadata": r.run_metadata,
        "notes": r.notes,
        "tags": r.tags or [],
        "created_by": r.created_by,
        "updated_at": None if r.updated_at is None else format_utc(r.updated_at),
    }


def run_is_active(r: Any, now: datetime) -> bool:
    """Open and not past its planned end (a missed end report must not look like a live run)."""
    if r.ended_at is not None or r.started_at > now:
        return False
    return r.planned_end_at is None or r.planned_end_at > now


def effective_end(r: Any, now: datetime) -> tuple[datetime, bool]:
    """(end of the data window, whether the run is over) for analysis."""
    if r.ended_at is not None:
        return r.ended_at, True
    if r.planned_end_at is not None and r.planned_end_at <= now:
        return r.planned_end_at, True
    return now, False


def list_runs(
    conn: Connection, instrument_id: str, now: datetime, limit: int = 50
) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(Run)
        .where(Run.instrument_id == instrument_id)
        .order_by(desc(Run.started_at))
        .limit(limit)
    ).all()
    return [run_view(r, now) for r in rows]


def active_run(conn: Connection, instrument_id: str, now: datetime) -> dict[str, Any] | None:
    r = conn.execute(
        select(Run)
        .where(
            Run.instrument_id == instrument_id,
            Run.ended_at.is_(None),
            Run.started_at <= now,
            or_(Run.planned_end_at.is_(None), Run.planned_end_at > now),
        )
        .order_by(desc(Run.started_at))
        .limit(1)
    ).first()
    return None if r is None else run_view(r, now)
