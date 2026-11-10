"""Commissioning wizard (SPECIFICATIONS §8, ONB-01) and collector provisioning for the web UI.

Six steps: Identify, Connect, Map, Preview, Approve, Verify. A draft is stored server-side, so an
interrupted wizard resumes where it stopped. A draft never changes a profile. Publishing needs a
passing preview of exactly the drafted profile and seed. It writes an immutable profile revision
and assigns the collector. The UI shows the instrument as *active* only after the collector
reports that revision in its heartbeat and its first samples are committed. Until then the
previously active revision stays in service.

v1 is simulator-only and read-only: the only mode is ``simulated``, the channel map is the
profile's documented map, and nothing here can send anything to an instrument.

Roles: custodian or administrator in the lab commission instruments. Only administrators
provision, package or revoke collectors. Out-of-scope ids answer 404.
"""

from __future__ import annotations

import io
import secrets
import zipfile
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, and_, func, insert, select, update

from labmon.contracts.envelope import ID_PATTERN, format_utc
from labmon.profiles import PROFILES
from labmon.server import provisioning
from labmon.server.bootstrap import InstrumentSeed, assign, ensure_collector, ensure_instrument
from labmon.server.models import (
    AppUser,
    AuditEvent,
    Channel,
    Collector,
    CollectorAssignment,
    CollectorCredential,
    CommissioningDraft,
    Instrument,
    LabGrant,
    ProfileRevision,
    Sample,
)
from labmon.server.records import RecordError
from labmon.server.security import Principal, audit
from labmon.sim.preview import preview as run_preview

COMMISSION_ROLES = frozenset({"custodian", "administrator"})
PROVISION_ROLES = frozenset({"administrator"})
COMMISSIONABLE = tuple(k for k in PROFILES if k != "sim-ambient")  # ambient is a smoke fixture
COLLECTOR_ONLINE_S = 30.0
STEP_NAMES = ("Identify", "Connect", "Map", "Preview", "Approve", "Verify")


class DraftData(BaseModel):
    """What the wizard collects. Every field is optional in a draft; publish checks completeness."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    location: str | None = Field(default=None, max_length=80)
    asset_id: str | None = Field(default=None, max_length=64)
    custodian: str | None = Field(default=None, max_length=64)
    collector_id: str | None = Field(default=None, pattern=ID_PATTERN)
    interface_ref: str | None = Field(default=None, max_length=200)
    mode: Literal["simulated"] = "simulated"
    profile: str | None = Field(default=None, max_length=64)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    notes: str | None = Field(default=None, max_length=1000)


class DraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lab_id: str = Field(pattern=ID_PATTERN)
    instrument_id: str = Field(pattern=ID_PATTERN)
    data: DraftData = Field(default_factory=DraftData)


class DraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: int | None = Field(default=None, ge=1, le=6)
    data: DraftData | None = None


class CollectorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collector_id: str = Field(pattern=ID_PATTERN)
    lab_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=80)


class PackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_url: str = Field(min_length=8, max_length=200)
    issue_token: bool = False  # True: a new credential, previous ones revoked


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=200)


# --- helpers ----------------------------------------------------------------------------------


def _actor(p: Principal) -> str:
    return f"user:{p.username}"


def _require(p: Principal, lab_id: str, roles: frozenset[str], what: str) -> None:
    if not p.can_view(lab_id):
        raise RecordError(404, "lab not found")
    if p.labs.get(lab_id) not in roles:
        raise RecordError(403, f"{what} needs {' or '.join(sorted(roles))} in this lab")


def _draft(conn: Connection, p: Principal, draft_id: str) -> Any:
    d = conn.execute(select(CommissioningDraft).where(CommissioningDraft.id == draft_id)).first()
    if d is None or not p.can_view(d.lab_id):
        raise RecordError(404, "draft not found")
    _require(p, d.lab_id, COMMISSION_ROLES, "commissioning")
    return d


def _username(conn: Connection, user_id: int) -> str | None:
    return conn.execute(select(AppUser.username).where(AppUser.id == user_id)).scalar()


def _collector_view(c: Any, now: datetime, assigned: list[str]) -> dict[str, Any]:
    hb = c.last_heartbeat or {}
    age = (now - c.last_heartbeat_at).total_seconds() if c.last_heartbeat_at else None
    return {
        "id": c.id,
        "lab_id": c.lab_id,
        "name": c.name,
        "revoked": c.revoked_at is not None,
        "last_heartbeat_at": format_utc(c.last_heartbeat_at) if c.last_heartbeat_at else None,
        "heartbeat_age_s": None if age is None else round(age, 1),
        "online": age is not None and age <= COLLECTOR_ONLINE_S and c.revoked_at is None,
        "software_version": hb.get("software_version"),
        "sampling": hb.get("sampling"),
        "upload_fault": hb.get("upload_fault"),
        "sources": hb.get("sources", []),
        "assigned_instruments": assigned,
    }


def _collectors(conn: Connection, lab_ids: list[str], now: datetime) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(Collector).where(Collector.lab_id.in_(lab_ids)).order_by(Collector.id)
    ).all()
    assigned: dict[str, list[str]] = {}
    for a in conn.execute(
        select(CollectorAssignment.collector_id, CollectorAssignment.instrument_id).where(
            CollectorAssignment.valid_to.is_(None)
        )
    ):
        assigned.setdefault(a.collector_id, []).append(a.instrument_id)
    active = {
        r.collector_id: r.n
        for r in conn.execute(
            select(CollectorCredential.collector_id, func.count().label("n"))
            .where(CollectorCredential.revoked_at.is_(None))
            .group_by(CollectorCredential.collector_id)
        )
    }
    out = []
    for c in rows:
        v = _collector_view(c, now, sorted(assigned.get(c.id, [])))
        v["active_credentials"] = int(active.get(c.id, 0))
        out.append(v)
    return out


def _profile_summary(key: str) -> dict[str, Any]:
    p = PROFILES[key]
    return {
        "key": p.key,
        "title": p.title,
        "adapter": p.adapter,
        "adapter_version": p.adapter_version,
        "opt_in": p.opt_in,
        "sign_convention": p.sign_convention,
        "cadence_note": p.cadence_note,
        "source_time_meaning": p.source_time_meaning.value,
        "channels": [
            {
                "key": c.key,
                "name": c.name,
                "quantity": c.quantity,
                "unit": c.unit,
                "data_type": c.data_type.value,
                "role": c.role,
                "expected_cadence_s": c.expected_cadence_s,
                "valid_min": c.valid_min,
                "valid_max": c.valid_max,
                "enum_labels": c.enum_labels,
                "note": c.note,
            }
            for c in p.channels
        ],
    }


def _active(conn: Connection, instrument_id: str) -> dict[str, Any] | None:
    inst = conn.execute(select(Instrument).where(Instrument.id == instrument_id)).first()
    if inst is None:
        return None
    rev = conn.execute(provisioning.latest_revision(instrument_id)).first()
    collector = conn.execute(
        select(CollectorAssignment.collector_id).where(
            CollectorAssignment.instrument_id == instrument_id,
            CollectorAssignment.valid_to.is_(None),
        )
    ).scalar()
    return {
        "lab_id": inst.lab_id,
        "name": inst.name,
        "location": inst.location,
        "asset_id": inst.asset_id,
        "profile": inst.profile_key,
        "seed": int((rev.definition or {}).get("seed", 0)) if rev else None,
        "revision": int(rev.revision) if rev else None,
        "revision_id": str(rev.id) if rev else None,
        "collector_id": collector,
        "simulated": inst.simulated,
    }


def _changes(active: dict[str, Any] | None, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-by-field comparison of the active revision with the draft (Approve step)."""
    fields = ("name", "location", "asset_id", "profile", "seed", "collector_id")
    before = active or {}
    return [
        {"field": f, "before": before.get(f), "after": data.get(f)}
        for f in fields
        if before.get(f) != data.get(f)
    ]


def _missing(data: dict[str, Any]) -> list[str]:
    need = ("name", "collector_id", "profile", "seed")
    return [k for k in need if data.get(k) in (None, "")]


def _draft_view(conn: Connection, d: Any, now: datetime) -> dict[str, Any]:
    active = _active(conn, d.instrument_id)
    preview = d.preview or {}
    stale = bool(preview) and (
        preview.get("profile") != d.data.get("profile") or preview.get("seed") != d.data.get("seed")
    )
    return {
        "id": d.id,
        "lab_id": d.lab_id,
        "instrument_id": d.instrument_id,
        "state": d.state,
        "step": d.step,
        "step_name": STEP_NAMES[d.step - 1],
        "data": d.data,
        "preview": {
            "ok": preview.get("ok"),
            "at": preview.get("at"),
            "profile": preview.get("profile"),
            "seed": preview.get("seed"),
            "checks": preview.get("checks", []),
            "stale": stale,
        }
        if preview
        else None,
        "missing": _missing(d.data),
        "existing": active,
        "changes": _changes(active, d.data),
        "created_by": _username(conn, d.created_by),
        "updated_by": _username(conn, d.updated_by),
        "created_at": format_utc(d.created_at),
        "updated_at": format_utc(d.updated_at),
        "published_revision_id": d.published_revision_id,
        "published_at": format_utc(d.published_at) if d.published_at else None,
    }


# --- options and drafts -----------------------------------------------------------------------


def options(conn: Connection, p: Principal, now: datetime) -> dict[str, Any]:
    labs = sorted(lab for lab, role in p.labs.items() if role in COMMISSION_ROLES)
    custodians: dict[str, list[dict[str, str]]] = {}
    for r in conn.execute(
        select(LabGrant.lab_id, AppUser.username, AppUser.display_name)
        .join(AppUser, AppUser.id == LabGrant.user_id)
        .where(
            LabGrant.lab_id.in_(labs),
            LabGrant.role.in_(sorted(COMMISSION_ROLES)),
            AppUser.disabled_at.is_(None),
        )
        .order_by(AppUser.username)
    ):
        custodians.setdefault(r.lab_id, []).append(
            {"username": r.username, "display_name": r.display_name}
        )
    instruments = [
        {"id": i.id, "lab_id": i.lab_id, "name": i.name, "profile": i.profile_key}
        for i in conn.execute(
            select(Instrument)
            .where(Instrument.lab_id.in_(labs), Instrument.lifecycle != "retired")
            .order_by(Instrument.id)
        )
    ]
    return {
        "labs": [{"id": lab, "role": p.labs[lab]} for lab in labs],
        "can_provision": sorted(lab for lab, r in p.labs.items() if r in PROVISION_ROLES),
        "profiles": [_profile_summary(k) for k in COMMISSIONABLE],
        "collectors": _collectors(conn, labs, now) if labs else [],
        "custodians": custodians,
        "instruments": instruments,
        "steps": list(STEP_NAMES),
        "modes": ["simulated"],
    }


def list_drafts(conn: Connection, p: Principal, now: datetime) -> list[dict[str, Any]]:
    labs = [lab for lab, role in p.labs.items() if role in COMMISSION_ROLES]
    rows = conn.execute(
        select(CommissioningDraft)
        .where(CommissioningDraft.lab_id.in_(labs), CommissioningDraft.state != "abandoned")
        .order_by(CommissioningDraft.updated_at.desc())
        .limit(100)
    ).all()
    return [_draft_view(conn, d, now) for d in rows]


def get_draft(conn: Connection, p: Principal, draft_id: str, now: datetime) -> dict[str, Any]:
    return _draft_view(conn, _draft(conn, p, draft_id), now)


def create_draft(
    conn: Connection, p: Principal, body: DraftCreate, now: datetime
) -> dict[str, Any]:
    _require(p, body.lab_id, COMMISSION_ROLES, "commissioning")
    existing = conn.execute(select(Instrument).where(Instrument.id == body.instrument_id)).first()
    data = body.data.model_dump(exclude_none=True)
    if existing is not None:
        if existing.lab_id != body.lab_id:
            # never reveal instruments of other labs: treat the id as taken
            raise RecordError(409, "instrument id is already in use")
        active = _active(conn, body.instrument_id) or {}
        # re-commissioning starts from the active revision
        base = {
            k: active[k]
            for k in ("name", "location", "asset_id", "profile", "seed", "collector_id")
            if active.get(k) is not None
        }
        data = {**base, **data}
    open_draft = conn.execute(
        select(CommissioningDraft.id).where(
            CommissioningDraft.instrument_id == body.instrument_id,
            CommissioningDraft.state == "draft",
        )
    ).scalar()
    if open_draft is not None:
        raise RecordError(409, f"an open draft exists for this instrument: {open_draft}")
    draft_id = secrets.token_hex(8)
    conn.execute(
        insert(CommissioningDraft).values(
            id=draft_id,
            lab_id=body.lab_id,
            instrument_id=body.instrument_id,
            state="draft",
            step=1,
            data=data,
            preview=None,
            created_by=p.user_id,
            updated_by=p.user_id,
            created_at=now,
            updated_at=now,
        )
    )
    audit(
        conn,
        now,
        "user",
        _actor(p),
        "commissioning_draft_create",
        body.instrument_id,
        {"draft": draft_id, "lab": body.lab_id, "recommission": existing is not None},
    )
    return get_draft(conn, p, draft_id, now)


def update_draft(
    conn: Connection, p: Principal, draft_id: str, body: DraftUpdate, now: datetime
) -> dict[str, Any]:
    d = _draft(conn, p, draft_id)
    if d.state != "draft":
        raise RecordError(409, f"draft is {d.state}")
    values: dict[str, Any] = {"updated_by": p.user_id, "updated_at": now}
    if body.data is not None:
        data = body.data.model_dump(exclude_none=True)
        if data.get("profile") and data["profile"] not in COMMISSIONABLE:
            raise RecordError(422, f"profile must be one of {', '.join(COMMISSIONABLE)}")
        values["data"] = data
    if body.step is not None:
        values["step"] = body.step
    conn.execute(
        update(CommissioningDraft).where(CommissioningDraft.id == draft_id).values(**values)
    )
    return get_draft(conn, p, draft_id, now)


def abandon_draft(conn: Connection, p: Principal, draft_id: str, now: datetime) -> dict[str, Any]:
    d = _draft(conn, p, draft_id)
    if d.state != "draft":
        raise RecordError(409, f"draft is {d.state}")
    conn.execute(
        update(CommissioningDraft)
        .where(CommissioningDraft.id == draft_id)
        .values(state="abandoned", updated_by=p.user_id, updated_at=now)
    )
    audit(
        conn,
        now,
        "user",
        _actor(p),
        "commissioning_draft_abandon",
        d.instrument_id,
        {"draft": draft_id},
    )
    return get_draft(conn, p, draft_id, now)


# --- preview, publish, verify -----------------------------------------------------------------


def preview_draft(conn: Connection, p: Principal, draft_id: str, now: datetime) -> dict[str, Any]:
    d = _draft(conn, p, draft_id)
    if d.state != "draft":
        raise RecordError(409, f"draft is {d.state}")
    profile, seed = d.data.get("profile"), d.data.get("seed")
    if profile not in COMMISSIONABLE or seed is None:
        raise RecordError(422, "choose a profile and seed (step 2) before the preview")
    start_ms = int(now.timestamp()) * 1000
    result = run_preview(profile, int(seed), start_ms)
    summary = {
        "ok": result["ok"],
        "at": format_utc(now),
        "profile": profile,
        "seed": seed,
        "checks": result["checks"],
        "window": result["window"],
        "drill": result["drill"],
    }
    conn.execute(
        update(CommissioningDraft)
        .where(CommissioningDraft.id == draft_id)
        .values(preview=summary, updated_by=p.user_id, updated_at=now)
    )
    return result


def publish_draft(conn: Connection, p: Principal, draft_id: str, now: datetime) -> dict[str, Any]:
    d = _draft(conn, p, draft_id)
    if d.state != "draft":
        raise RecordError(409, f"draft is {d.state}")
    data = dict(d.data)
    missing = _missing(data)
    if missing:
        raise RecordError(422, f"complete the draft first: {', '.join(missing)}")
    pv = d.preview or {}
    if not pv.get("ok") or pv.get("profile") != data["profile"] or pv.get("seed") != data["seed"]:
        raise RecordError(422, "run a passing preview of this profile and seed before publishing")
    collector = conn.execute(select(Collector).where(Collector.id == data["collector_id"])).first()
    if collector is None or collector.lab_id != d.lab_id:
        raise RecordError(422, "the collector must exist in the same lab")
    if collector.revoked_at is not None:
        raise RecordError(422, "the collector is revoked")
    active = _active(conn, d.instrument_id)
    if active is None:
        rev_id = ensure_instrument(
            conn,
            d.lab_id,
            InstrumentSeed(
                d.instrument_id,
                data["name"],
                data["profile"],
                int(data["seed"]),
                asset_id=data.get("asset_id"),
                location=data.get("location"),
            ),
            actor=_actor(p),
            simulated=True,
        )
        assign(conn, data["collector_id"], d.instrument_id, now, f"commissioned (draft {d.id})")
    else:
        if active["profile"] != data["profile"]:
            raise RecordError(
                409,
                "a different profile changes the channel set; commission a new instrument id "
                "instead and retire this one",
            )
        if active["collector_id"] not in (None, data["collector_id"]):
            raise RecordError(
                409,
                "moving an instrument to another collector is not supported by the wizard; "
                "an administrator must end the current assignment first",
            )
        if not _changes(active, data):
            raise RecordError(409, "nothing changed against the active revision")
        profile = PROFILES[data["profile"]]
        n = int(active["revision"] or 0) + 1
        rev_id = f"{d.instrument_id}-r{n}"
        conn.execute(
            insert(ProfileRevision).values(
                id=rev_id,
                instrument_id=d.instrument_id,
                revision=n,
                profile_key=profile.key,
                adapter=profile.adapter,
                adapter_version=profile.adapter_version,
                definition={**profile.model_dump(mode="json"), "seed": int(data["seed"])},
                definition_digest=profile.definition_digest(),
                created_by=_actor(p),
            )
        )
        conn.execute(
            update(Instrument)
            .where(Instrument.id == d.instrument_id)
            .values(name=data["name"], location=data.get("location"), asset_id=data.get("asset_id"))
        )
        if active["collector_id"] is None:
            assign(conn, data["collector_id"], d.instrument_id, now, f"commissioned ({d.id})")
    conn.execute(
        update(CommissioningDraft)
        .where(CommissioningDraft.id == draft_id)
        .values(
            state="published",
            step=6,
            published_revision_id=rev_id,
            published_at=now,
            updated_by=p.user_id,
            updated_at=now,
        )
    )
    audit(
        conn,
        now,
        "user",
        _actor(p),
        "instrument_commission",
        d.instrument_id,
        {
            "draft": d.id,
            "revision": rev_id,
            "simulated": True,
            "changes": _changes(active, data),
            "preview_at": pv.get("at"),
        },
    )
    return get_draft(conn, p, draft_id, now)


def verify_draft(conn: Connection, p: Principal, draft_id: str, now: datetime) -> dict[str, Any]:
    """Activation evidence: the collector applied the revision and committed samples with it."""
    d = _draft(conn, p, draft_id)
    if d.state != "published" or d.published_revision_id is None:
        raise RecordError(409, "publish the draft first")
    rev = d.published_revision_id
    collector_id = d.data.get("collector_id")
    c = conn.execute(select(Collector).where(Collector.id == collector_id)).first()
    applied = None
    if c is not None and c.last_heartbeat:
        for s in c.last_heartbeat.get("sources", []):
            if s.get("instrument_id") == d.instrument_id:
                applied = s
    hb_age = (now - c.last_heartbeat_at).total_seconds() if c and c.last_heartbeat_at else None
    channels = [
        r.id
        for r in conn.execute(
            select(Channel.id)
            .where(Channel.instrument_id == d.instrument_id)
            .order_by(Channel.sort_order)
        )
    ]
    since = d.published_at - timedelta(minutes=5)
    stats = conn.execute(
        select(
            func.count().label("n"),
            func.min(Sample.receive_time).label("first"),
            func.max(Sample.receive_time).label("last"),
        ).where(
            Sample.channel_id.in_(channels),
            Sample.event_time >= since,
            Sample.profile_revision_id == rev,
        )
    ).one()
    latest = []
    for ch in channels:
        r = conn.execute(
            select(
                Sample.value_num,
                Sample.value_int,
                Sample.value_bool,
                Sample.quality,
                Sample.event_time,
            )
            .where(
                and_(
                    Sample.channel_id == ch,
                    Sample.event_time >= since,
                    Sample.profile_revision_id == rev,
                )
            )
            .order_by(Sample.event_time.desc())
            .limit(1)
        ).first()
        if r is not None:
            value = (
                r.value_num
                if r.value_num is not None
                else (r.value_int if r.value_int is not None else r.value_bool)
            )
            latest.append(
                {
                    "channel_id": ch,
                    "value": value,
                    "quality": r.quality,
                    "event_time": format_utc(r.event_time),
                }
            )
    applied_rev = applied.get("profile_revision_id") if applied else None
    if applied_rev != rev:
        state = "awaiting_collector"
    elif not stats.n:
        state = "awaiting_samples"
    else:
        state = "receiving"
    return {
        "state": state,
        "revision_id": rev,
        "collector": {
            "id": collector_id,
            "heartbeat_age_s": None if hb_age is None else round(hb_age, 1),
            "online": hb_age is not None and hb_age <= COLLECTOR_ONLINE_S,
            "applied_revision_id": applied_rev,
            "source_state": applied.get("state") if applied else None,
        },
        "samples": {
            "count": int(stats.n),
            "first_received": format_utc(stats.first) if stats.first else None,
            "last_received": format_utc(stats.last) if stats.last else None,
        },
        "latest": latest,
        "checked_at": format_utc(now),
    }


def commissioning_record(
    conn: Connection, p: Principal, draft_id: str, now: datetime
) -> dict[str, Any]:
    """The printable/exportable commissioning record (step 6)."""
    d = _draft(conn, p, draft_id)
    view = _draft_view(conn, d, now)
    rev = None
    if d.published_revision_id:
        rev = conn.execute(
            select(ProfileRevision).where(ProfileRevision.id == d.published_revision_id)
        ).first()
    events = [
        {"at": format_utc(e.at), "actor": e.actor_id, "action": e.action, "detail": e.detail}
        for e in conn.execute(
            select(AuditEvent)
            .where(
                AuditEvent.target == d.instrument_id,
                AuditEvent.action.in_(
                    [
                        "commissioning_draft_create",
                        "instrument_commission",
                        "commissioning_draft_abandon",
                    ]
                ),
            )
            .order_by(AuditEvent.at)
        )
        if (e.detail or {}).get("draft") == d.id
    ]
    return {
        "kind": "labmon-commissioning-record",
        "evidence": "simulated — not field acceptance",
        "generated_at": format_utc(now),
        "generated_by": p.username,
        "instrument": {
            "id": d.instrument_id,
            "lab_id": d.lab_id,
            "name": d.data.get("name"),
            "location": d.data.get("location"),
            "asset_id": d.data.get("asset_id"),
            "custodian": d.data.get("custodian"),
            "interface_reference": d.data.get("interface_ref"),
            "mode": "simulated",
        },
        "source": {
            "profile": d.data.get("profile"),
            "seed": d.data.get("seed"),
            "collector_id": d.data.get("collector_id"),
        },
        "revision": {
            "id": rev.id,
            "number": rev.revision,
            "adapter": rev.adapter,
            "adapter_version": rev.adapter_version,
            "definition_digest": rev.definition_digest,
            "created_by": rev.created_by,
        }
        if rev
        else None,
        "channel_map": _profile_summary(d.data["profile"])["channels"]
        if d.data.get("profile") in PROFILES
        else [],
        "preview": view["preview"],
        "verification": verify_draft(conn, p, draft_id, now) if d.state == "published" else None,
        "calibration": "not applicable: simulated source with documented known answers",
        "changes": view["changes"] if d.state == "draft" else None,
        "notes": d.data.get("notes"),
        "audit": events,
        "state": d.state,
    }


# --- collectors (administrator) ---------------------------------------------------------------


def list_collectors(conn: Connection, p: Principal, now: datetime) -> list[dict[str, Any]]:
    labs = [lab for lab, role in p.labs.items() if role in COMMISSION_ROLES]
    return _collectors(conn, labs, now) if labs else []


def create_collector(
    conn: Connection, p: Principal, body: CollectorCreate, now: datetime
) -> dict[str, Any]:
    _require(p, body.lab_id, PROVISION_ROLES, "provisioning collectors")
    if conn.execute(select(Collector.id).where(Collector.id == body.collector_id)).first():
        raise RecordError(409, "collector id is already in use")
    ensure_collector(conn, body.collector_id, body.lab_id, body.name)
    audit(conn, now, "user", _actor(p), "collector_add", body.collector_id, {"lab": body.lab_id})
    return next(c for c in _collectors(conn, [body.lab_id], now) if c["id"] == body.collector_id)


def _collector_for_admin(conn: Connection, p: Principal, collector_id: str) -> Any:
    c = conn.execute(select(Collector).where(Collector.id == collector_id)).first()
    if c is None or not p.can_view(c.lab_id):
        raise RecordError(404, "collector not found")
    _require(p, c.lab_id, PROVISION_ROLES, "provisioning collectors")
    return c


def collector_package(
    conn: Connection,
    p: Principal,
    collector_id: str,
    body: PackageRequest,
    now: datetime,
    allow_http: bool,
) -> tuple[bytes, str]:
    """ZIP with ``collector.toml`` and, when asked, a new ``collector.token`` (old ones revoked).

    The token exists only in this response; it is stored as a digest."""
    c = _collector_for_admin(conn, p, collector_id)
    url = urlsplit(body.server_url)
    if url.scheme not in ("https", "http") or not url.hostname or url.path not in ("", "/"):
        raise RecordError(422, "server URL must be scheme://host[:port] without a path")
    if url.scheme == "http" and not allow_http:
        raise RecordError(422, "production collectors must use HTTPS")
    if url.username or url.password or url.query or url.fragment:
        raise RecordError(422, "server URL must not carry credentials, a query or a fragment")
    server_url = f"{url.scheme}://{url.netloc}"
    try:
        toml = provisioning.config_text(
            conn, c.id, server_url, f"the commissioning UI by {p.username} at {format_utc(now)}"
        )
        token, revoked = (
            provisioning.issue_credential(conn, c.id, now) if body.issue_token else (None, 0)
        )
    except provisioning.ProvisioningError as exc:
        raise RecordError(exc.status, exc.message) from exc
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("collector.toml", toml)
        if token is not None:
            zf.writestr("collector.token", token + "\n")
        zf.writestr(
            "README.txt",
            "Copy collector.toml"
            + (" and collector.token" if token else "")
            + " into the collector's configuration directory through the approved channel.\n"
            "Restrict collector.token to the service account (Linux: chmod 0600; Windows: see\n"
            "docs/operations/deployment.md), restart the collector, then delete this archive.\n",
        )
    audit(
        conn,
        now,
        "user",
        _actor(p),
        "collector_package",
        c.id,
        {"server_url": server_url, "token_issued": token is not None, "previous_revoked": revoked},
    )
    suffix = "-with-credential" if token else ""
    return buf.getvalue(), f"{c.id}{suffix}.zip"


def revoke_collector(
    conn: Connection, p: Principal, collector_id: str, body: RevokeRequest, now: datetime
) -> dict[str, Any]:
    c = _collector_for_admin(conn, p, collector_id)
    n = provisioning.revoke_collector(conn, c.id, now)
    audit(
        conn,
        now,
        "user",
        _actor(p),
        "collector_revoke",
        c.id,
        {"credentials": n, "reason": body.reason},
    )
    return next(x for x in _collectors(conn, [c.lab_id], now) if x["id"] == c.id)
