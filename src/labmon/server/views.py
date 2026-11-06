"""Saved chart views (HIST-01): personal views and explicitly shared lab templates.

Any signed-in user with access to the instrument may keep personal views. Sharing a view with
the lab (a template) needs custodian or administrator in that lab and is audited. Views hold
presentation settings only — never data or credentials — and are validated and bounded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, and_, insert, or_, select, update

from labmon.contracts.envelope import format_utc
from labmon.server.models import AppUser, Instrument, SavedView
from labmon.server.records import RecordError
from labmon.server.security import Principal, audit

TEMPLATE_ROLES = frozenset({"custodian", "administrator"})
MAX_VIEWS_PER_USER_AND_INSTRUMENT = 50


class AxisScale(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "log", "fixed"]
    min: float = Field(default=0.0, allow_inf_nan=False)
    max: float = Field(default=1.0, allow_inf_nan=False)


class ViewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: Literal["15m", "1h", "24h", "7d", "30d"] | None = None
    start: datetime | None = None
    end: datetime | None = None
    scales: dict[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")], AxisScale] = Field(
        default_factory=dict, max_length=16
    )
    zone: Literal["local", "UTC"] | None = None


class ViewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    scope: Literal["personal", "lab"] = "personal"
    config: ViewConfig


def _view(v: Any, owner: str | None) -> dict[str, Any]:
    return {
        "id": v.id,
        "instrument_id": v.instrument_id,
        "scope": v.scope,
        "name": v.name,
        "config": v.config,
        "owner": owner,
        "created_at": format_utc(v.created_at),
    }


def _instrument(conn: Connection, p: Principal, instrument_id: str) -> Any:
    inst = conn.execute(select(Instrument).where(Instrument.id == instrument_id)).first()
    if inst is None or not p.can_view(inst.lab_id):
        raise RecordError(404, "instrument not found")
    return inst


def list_views(conn: Connection, p: Principal, instrument_id: str) -> list[dict[str, Any]]:
    _instrument(conn, p, instrument_id)
    rows = conn.execute(
        select(SavedView, AppUser.username)
        .join(AppUser, AppUser.id == SavedView.owner_id)
        .where(
            SavedView.instrument_id == instrument_id,
            SavedView.deleted_at.is_(None),
            or_(
                SavedView.scope == "lab",
                and_(SavedView.scope == "personal", SavedView.owner_id == p.user_id),
            ),
        )
        .order_by(SavedView.scope, SavedView.name)
    ).all()
    return [_view(r, r.username) for r in rows]


def create_view(
    conn: Connection, p: Principal, instrument_id: str, body: ViewCreate, now: datetime
) -> dict[str, Any]:
    inst = _instrument(conn, p, instrument_id)
    cfg = body.config
    if (cfg.preset is None) == (cfg.start is None or cfg.end is None):
        raise RecordError(422, "a view needs either a preset or both start and end")
    if cfg.start is not None and cfg.end is not None and cfg.end <= cfg.start:
        raise RecordError(422, "end must be after start")
    if body.scope == "lab" and p.labs.get(inst.lab_id) not in TEMPLATE_ROLES:
        raise RecordError(403, "sharing a lab template needs the custodian or administrator role")
    count = conn.execute(
        select(SavedView.id).where(
            SavedView.instrument_id == instrument_id,
            SavedView.owner_id == p.user_id,
            SavedView.deleted_at.is_(None),
        )
    ).all()
    if len(count) >= MAX_VIEWS_PER_USER_AND_INSTRUMENT:
        raise RecordError(409, "too many saved views for this instrument; delete some first")
    vid = conn.execute(
        insert(SavedView)
        .values(
            instrument_id=instrument_id,
            owner_id=p.user_id,
            scope=body.scope,
            name=body.name,
            config=cfg.model_dump(mode="json", exclude_none=True),
            created_at=now,
        )
        .returning(SavedView.id)
    ).scalar_one()
    if body.scope == "lab":
        audit(
            conn,
            now,
            "user",
            str(p.user_id),
            "view_template_create",
            instrument_id,
            {"view_id": vid, "name": body.name},
        )
    row = conn.execute(select(SavedView).where(SavedView.id == vid)).one()
    return _view(row, p.username)


def delete_view(conn: Connection, p: Principal, view_id: int, now: datetime) -> dict[str, Any]:
    row = conn.execute(
        select(SavedView).where(SavedView.id == view_id, SavedView.deleted_at.is_(None))
    ).first()
    if row is None:
        raise RecordError(404, "view not found")
    inst = _instrument(conn, p, row.instrument_id)
    own = row.owner_id == p.user_id
    if row.scope == "personal" and not own:
        raise RecordError(404, "view not found")
    if row.scope == "lab" and p.labs.get(inst.lab_id) not in TEMPLATE_ROLES:
        raise RecordError(403, "removing a lab template needs the custodian or administrator role")
    conn.execute(update(SavedView).where(SavedView.id == view_id).values(deleted_at=now))
    if row.scope == "lab":
        audit(
            conn,
            now,
            "user",
            str(p.user_id),
            "view_template_delete",
            row.instrument_id,
            {"view_id": view_id, "name": row.name},
        )
    return {"deleted": view_id}
