"""Operator records: user-created runs and annotations (RUN-01).

These are records about experiments, never commands to instruments. Every create, correction
and retraction is audited with actor, reason and a before/after snapshot. Corrections keep the
same identity; annotations carry a revision number; retraction is soft so history survives.
Windows may be backdated; they may not start in the future.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, and_, insert, or_, select, update

from labmon.contracts.envelope import format_utc
from labmon.server.models import Annotation, AppUser, Instrument, Run
from labmon.server.runs import run_view
from labmon.server.security import Principal, audit

FUTURE_SLACK = timedelta(minutes=1)


class RecordError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamps need an explicit UTC offset")
    return v


Aware = Annotated[datetime, AfterValidator(_aware)]
Tag = Annotated[str, Field(pattern=r"^[\w .+-]{1,32}$")]
Reason = Annotated[str, Field(min_length=3, max_length=500)]


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    started_at: Aware
    ended_at: Aware | None = None
    notes: str | None = Field(default=None, max_length=4000)
    tags: list[Tag] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def _window(self) -> RunCreate:
        if self.ended_at is not None and self.ended_at <= self.started_at:
            raise ValueError("ended_at must be after started_at")
        return self


class RunUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Reason
    name: str | None = Field(default=None, min_length=1, max_length=120)
    started_at: Aware | None = None
    ended_at: Aware | None = None
    notes: str | None = Field(default=None, max_length=4000)
    tags: list[Tag] | None = Field(default=None, max_length=12)


class AnnotationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: Aware
    end_at: Aware | None = None
    text: str = Field(min_length=1, max_length=1000)
    run_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _window(self) -> AnnotationCreate:
        if self.end_at is not None and self.end_at <= self.at:
            raise ValueError("end_at must be after at")
        return self


class AnnotationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Reason
    at: Aware | None = None
    end_at: Aware | None = None
    text: str | None = Field(default=None, min_length=1, max_length=1000)


class Retract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Reason


def _not_future(t: datetime | None, now: datetime) -> None:
    if t is not None and t > now + FUTURE_SLACK:
        raise RecordError(422, "records cannot start or end in the future")


def _instrument_for_write(conn: Connection, p: Principal, instrument_id: str) -> Any:
    inst = conn.execute(select(Instrument).where(Instrument.id == instrument_id)).first()
    if inst is None or not p.can_view(inst.lab_id):
        raise RecordError(404, "instrument not found")
    if not p.can_record(inst.lab_id):
        raise RecordError(403, "your role in this lab is read-only")
    return inst


def create_run(
    conn: Connection, p: Principal, instrument_id: str, body: RunCreate, now: datetime
) -> dict[str, Any]:
    inst = _instrument_for_write(conn, p, instrument_id)
    _not_future(body.started_at, now)
    _not_future(body.ended_at, now)
    key = f"u{uuid.uuid4().hex[:10]}"
    rid = f"{instrument_id}.{key}"
    conn.execute(
        insert(Run).values(
            id=rid,
            instrument_id=instrument_id,
            run_key=key,
            name=body.name,
            kind="user_run",
            source="user",
            simulated=inst.simulated,
            started_at=body.started_at,
            planned_end_at=None,
            ended_at=body.ended_at,
            end_reason="completed" if body.ended_at else None,
            run_metadata={},
            first_seen_at=now,
            notes=body.notes,
            tags=body.tags,
            created_by=p.username,
        )
    )
    row = conn.execute(select(Run).where(Run.id == rid)).one()
    view = run_view(row, now)
    audit(conn, now, "user", str(p.user_id), "run_create", rid, {"after": view})
    return view


def update_run(
    conn: Connection, p: Principal, run_id: str, body: RunUpdate, now: datetime
) -> dict[str, Any]:
    row = conn.execute(select(Run).where(Run.id == run_id).with_for_update()).first()
    if row is None:
        raise RecordError(404, "run not found")
    _instrument_for_write(conn, p, row.instrument_id)
    changes: dict[str, Any] = {}
    if body.notes is not None:
        changes["notes"] = body.notes
    if body.tags is not None:
        changes["tags"] = body.tags
    window_fields = {"name": body.name, "started_at": body.started_at, "ended_at": body.ended_at}
    if any(v is not None for v in window_fields.values()):
        if row.source != "user":
            raise RecordError(
                422,
                "name and window of instrument-reported runs come from the "
                "instrument; add notes, tags or an annotation instead",
            )
        for k, v in window_fields.items():
            if v is not None:
                changes[k] = v
        start = changes.get("started_at", row.started_at)
        end = changes.get("ended_at", row.ended_at)
        _not_future(start, now)
        _not_future(end, now)
        if end is not None and end <= start:
            raise RecordError(422, "ended_at must be after started_at")
        if "ended_at" in changes:
            changes["end_reason"] = "completed"
    if not changes:
        raise RecordError(422, "nothing to change")
    before = run_view(row, now)
    conn.execute(update(Run).where(Run.id == run_id).values(**changes, updated_at=now))
    after = run_view(conn.execute(select(Run).where(Run.id == run_id)).one(), now)
    audit(
        conn,
        now,
        "user",
        str(p.user_id),
        "run_update",
        run_id,
        {"reason": body.reason, "before": before, "after": after},
    )
    return after


def annotation_view(a: Any, author: str | None) -> dict[str, Any]:
    return {
        "id": a.id,
        "instrument_id": a.instrument_id,
        "run_id": a.run_id,
        "at": format_utc(a.at),
        "end_at": None if a.end_at is None else format_utc(a.end_at),
        "text": a.text,
        "author": author,
        "revision": a.revision,
        "created_at": format_utc(a.created_at),
        "updated_at": None if a.updated_at is None else format_utc(a.updated_at),
        "retracted": a.retracted_at is not None,
        "retract_reason": a.retract_reason,
    }


def list_annotations(
    conn: Connection,
    instrument_id: str,
    start: datetime,
    end: datetime,
    include_retracted: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    q = (
        select(Annotation, AppUser.username)
        .join(AppUser, AppUser.id == Annotation.author_id)
        .where(
            Annotation.instrument_id == instrument_id,
            Annotation.at < end,
            or_(
                and_(Annotation.end_at.is_(None), Annotation.at >= start), Annotation.end_at > start
            ),
        )
        .order_by(Annotation.at, Annotation.id)
        .limit(limit)
    )
    if not include_retracted:
        q = q.where(Annotation.retracted_at.is_(None))
    return [annotation_view(r, r.username) for r in conn.execute(q)]


def create_annotation(
    conn: Connection, p: Principal, instrument_id: str, body: AnnotationCreate, now: datetime
) -> dict[str, Any]:
    _instrument_for_write(conn, p, instrument_id)
    _not_future(body.at, now)
    _not_future(body.end_at, now)
    if body.run_id is not None:
        run = conn.execute(select(Run.instrument_id).where(Run.id == body.run_id)).first()
        if run is None or run.instrument_id != instrument_id:
            raise RecordError(422, "run does not belong to this instrument")
    aid = conn.execute(
        insert(Annotation)
        .values(
            instrument_id=instrument_id,
            run_id=body.run_id,
            at=body.at,
            end_at=body.end_at,
            text=body.text,
            author_id=p.user_id,
            created_at=now,
        )
        .returning(Annotation.id)
    ).scalar_one()
    view = annotation_view(
        conn.execute(select(Annotation).where(Annotation.id == aid)).one(), p.username
    )
    audit(
        conn, now, "user", str(p.user_id), "annotation_create", f"annotation:{aid}", {"after": view}
    )
    return view


def _annotation_for_write(conn: Connection, p: Principal, annotation_id: int) -> Any:
    row = conn.execute(
        select(Annotation).where(Annotation.id == annotation_id).with_for_update()
    ).first()
    if row is None:
        raise RecordError(404, "annotation not found")
    _instrument_for_write(conn, p, row.instrument_id)
    if row.retracted_at is not None:
        raise RecordError(409, "annotation is retracted")
    return row


def update_annotation(
    conn: Connection, p: Principal, annotation_id: int, body: AnnotationUpdate, now: datetime
) -> dict[str, Any]:
    a = _annotation_for_write(conn, p, annotation_id)
    changes = {
        k: v
        for k, v in {"at": body.at, "end_at": body.end_at, "text": body.text}.items()
        if v is not None
    }
    if not changes:
        raise RecordError(422, "nothing to change")
    at, end_at = changes.get("at", a.at), changes.get("end_at", a.end_at)
    _not_future(at, now)
    _not_future(end_at, now)
    if end_at is not None and end_at <= at:
        raise RecordError(422, "end_at must be after at")
    author = conn.execute(select(AppUser.username).where(AppUser.id == a.author_id)).scalar()
    before = annotation_view(a, author)
    conn.execute(
        update(Annotation)
        .where(Annotation.id == annotation_id)
        .values(**changes, revision=Annotation.revision + 1, updated_at=now)
    )
    after = annotation_view(
        conn.execute(select(Annotation).where(Annotation.id == annotation_id)).one(), author
    )
    audit(
        conn,
        now,
        "user",
        str(p.user_id),
        "annotation_update",
        f"annotation:{annotation_id}",
        {"reason": body.reason, "before": before, "after": after},
    )
    return after


def retract_annotation(
    conn: Connection, p: Principal, annotation_id: int, body: Retract, now: datetime
) -> dict[str, Any]:
    a = _annotation_for_write(conn, p, annotation_id)
    author = conn.execute(select(AppUser.username).where(AppUser.id == a.author_id)).scalar()
    before = annotation_view(a, author)
    conn.execute(
        update(Annotation)
        .where(Annotation.id == annotation_id)
        .values(
            retracted_at=now, retracted_by=p.user_id, retract_reason=body.reason, updated_at=now
        )
    )
    after = annotation_view(
        conn.execute(select(Annotation).where(Annotation.id == annotation_id)).one(), author
    )
    audit(
        conn,
        now,
        "user",
        str(p.user_id),
        "annotation_retract",
        f"annotation:{annotation_id}",
        {"reason": body.reason, "before": before},
    )
    return after
