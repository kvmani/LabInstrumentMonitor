"""Authoritative instrument status snapshot.

Independent fields: lifecycle, collector connectivity, acquisition state and instrument-reported
process state. Rules from SPECIFICATIONS.md §4 "Status vocabulary":

* Collector connectivity uses only server receive time of authenticated heartbeats
  (offline after 30 s). Heartbeats never refresh a sample's freshness.
* Freshness uses the *event time* of centrally committed samples from the *current* acquisition
  assignment with trusted clocks. A replayed historical reading has an old event time and cannot
  make a channel fresh; equal values arriving on time keep it fresh.
* Process state comes only from the profile's validated state channel and is Unknown when stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, and_, desc, select

from labmon.contracts.envelope import format_utc
from labmon.server.diagnostics import instrument_faults
from labmon.server.models import (
    AcquisitionEvent,
    Channel,
    Collector,
    CollectorAssignment,
    Instrument,
    Lab,
    ProfileRevision,
    Sample,
)
from labmon.server.runs import active_run

FUTURE_SLACK = timedelta(seconds=5)


@dataclass(frozen=True)
class StatusConfig:
    heartbeat_offline_s: float = 30.0


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else format_utc(dt)


def _age(now: datetime, dt: datetime | None) -> float | None:
    return None if dt is None else round((now - dt).total_seconds(), 3)


def _value(row: Any, data_type: str) -> float | int | bool | None:
    if data_type == "float":
        return row.value_num  # type: ignore[no-any-return]
    if data_type == "enum":
        return row.value_int  # type: ignore[no-any-return]
    return row.value_bool  # type: ignore[no-any-return]


def _sample_view(row: Any, ch: Any, now: datetime) -> dict[str, Any] | None:
    if row is None:
        return None
    value = _value(row, ch.data_type)
    label = None
    if ch.data_type == "enum" and value is not None and ch.enum_labels:
        label = ch.enum_labels.get(str(value))
    return {
        "value": value,
        "label": label,
        "quality": row.quality,
        "quality_reason": row.quality_reason,
        "event_time": _iso(row.event_time),
        "age_s": _age(now, row.event_time),
        "receive_time": _iso(row.receive_time),
        "receive_lag_s": round((row.receive_time - row.event_time).total_seconds(), 3),
        "clock_confidence": row.clock_confidence,
    }


def _latest(
    conn: Connection,
    channel_id: str,
    collector_id: str,
    since: datetime,
    now: datetime,
    good_only: bool,
) -> Any:
    q = (
        select(
            Sample.value_num,
            Sample.value_int,
            Sample.value_bool,
            Sample.quality,
            Sample.quality_reason,
            Sample.event_time,
            Sample.receive_time,
            Sample.clock_confidence,
        )
        .where(
            Sample.channel_id == channel_id,
            Sample.collector_id == collector_id,
            Sample.event_time >= since,
            Sample.event_time <= now + FUTURE_SLACK,
            Sample.clock_confidence == "trusted",
        )
        .order_by(desc(Sample.event_time), desc(Sample.spool_epoch), desc(Sample.seq))
        .limit(1)
    )
    if good_only:
        q = q.where(Sample.quality == "good")
    return conn.execute(q).first()


def _backlog_age(hb: dict[str, Any]) -> float:
    try:
        oldest = datetime.fromisoformat(
            str(hb["oldest_pending_capture_time"]).replace("Z", "+00:00")
        )
        taken = datetime.fromisoformat(str(hb["collector_time"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return 0.0
    return max(0.0, (taken - oldest).total_seconds())


def collector_view(c: Any, now: datetime, cfg: StatusConfig) -> dict[str, Any]:
    if c is None:
        return {"id": None, "connectivity": "never_seen", "last_heartbeat_at": None}
    if c.revoked_at is not None:
        state = "revoked"
    elif c.last_heartbeat_at is None:
        state = "never_seen"
    elif (now - c.last_heartbeat_at).total_seconds() <= cfg.heartbeat_offline_s:
        state = "online"
    else:
        state = "offline"
    hb = c.last_heartbeat or {}
    offset = None
    if c.last_heartbeat_collector_time is not None and c.last_heartbeat_at is not None:
        offset = round((c.last_heartbeat_collector_time - c.last_heartbeat_at).total_seconds(), 3)
    return {
        "id": c.id,
        "name": c.name,
        "connectivity": state,
        "last_heartbeat_at": _iso(c.last_heartbeat_at),
        "heartbeat_age_s": _age(now, c.last_heartbeat_at),
        "clock_offset_s": offset,
        "unacknowledged": hb.get("unacknowledged"),
        "oldest_pending_capture_time": hb.get("oldest_pending_capture_time"),
        # Age of the oldest unacknowledged record when the heartbeat was taken (collector clock
        # on both sides). Records in flight are normal; only an old backlog proves a delay.
        "backlog_age_s": _backlog_age(hb),
        "upload_fault": hb.get("upload_fault"),
        "sampling": hb.get("sampling"),
        "software_version": hb.get("software_version"),
        "sources": hb.get("sources", []),
        "quarantined": hb.get("quarantined"),
        "spool_disk_free_bytes": hb.get("spool_disk_free_bytes"),
        "spool_disk_total_bytes": hb.get("spool_disk_total_bytes"),
    }


def instrument_status(
    conn: Connection, instrument_id: str, now: datetime, cfg: StatusConfig | None = None
) -> dict[str, Any]:
    cfg = cfg or StatusConfig()
    inst = conn.execute(
        select(Instrument, Lab.name.label("lab_name"))
        .join(Lab, Lab.id == Instrument.lab_id)
        .where(Instrument.id == instrument_id)
    ).one()
    assignment = conn.execute(
        select(CollectorAssignment)
        .where(
            CollectorAssignment.instrument_id == instrument_id,
            CollectorAssignment.valid_to.is_(None),
        )
        .order_by(desc(CollectorAssignment.valid_from))
        .limit(1)
    ).first()
    collector = None
    if assignment is not None:
        collector = conn.execute(
            select(Collector).where(Collector.id == assignment.collector_id)
        ).first()
    coll = collector_view(collector, now, cfg)
    channels = conn.execute(
        select(Channel)
        .where(Channel.instrument_id == instrument_id)
        .order_by(Channel.sort_order, Channel.key)
    ).all()

    ch_views: list[dict[str, Any]] = []
    for ch in channels:
        window = max(3.0 * ch.expected_cadence_s, 5.0)
        latest = good = None
        if assignment is not None:
            latest = _latest(
                conn, ch.id, assignment.collector_id, assignment.valid_from, now, good_only=False
            )
            good = (
                latest
                if latest is not None and latest.quality == "good"
                else _latest(
                    conn, ch.id, assignment.collector_id, assignment.valid_from, now, good_only=True
                )
            )
        fresh = latest is not None and (now - latest.event_time).total_seconds() <= window
        ch_views.append(
            {
                "id": ch.id,
                "key": ch.key,
                "name": ch.name,
                "quantity": ch.quantity,
                "unit": ch.unit,
                "data_type": ch.data_type,
                "role": ch.role,
                "display_precision": ch.display_precision,
                "enum_labels": ch.enum_labels,
                "expected_cadence_s": ch.expected_cadence_s,
                "display_format": ch.display_format,
                "note": ch.note,
                "freshness_window_s": window,
                "fresh": fresh,
                "latest": _sample_view(latest, ch, now),
                "last_good": _sample_view(good, ch, now),
            }
        )

    revision = conn.execute(
        select(ProfileRevision.id, ProfileRevision.revision, ProfileRevision.definition)
        .where(ProfileRevision.instrument_id == instrument_id)
        .order_by(desc(ProfileRevision.revision))
        .limit(1)
    ).first()
    acquisition = _acquisition_state(conn, inst, assignment, coll, ch_views, now)
    process = _process_state(inst.profile_key, ch_views)
    out: dict[str, Any] = {
        "server_time": format_utc(now),
        "instrument": {
            "id": inst.id,
            "name": inst.name,
            "lab_id": inst.lab_id,
            "lab_name": inst.lab_name,
            "asset_id": inst.asset_id,
            "location": inst.location,
            "lifecycle": inst.lifecycle,
            "simulated": inst.simulated,
            "profile_key": inst.profile_key,
        },
        "collector": coll,
        "acquisition": acquisition,
        "process_state": process,
        "channels": ch_views,
        "display": display_of(revision),
        "active_run": active_run(conn, instrument_id, now),
    }
    out["faults"] = instrument_faults(out)
    return out


def latest_display(conn: Connection, instrument_id: str) -> dict[str, Any]:
    revision = conn.execute(
        select(ProfileRevision.id, ProfileRevision.revision, ProfileRevision.definition)
        .where(ProfileRevision.instrument_id == instrument_id)
        .order_by(desc(ProfileRevision.revision))
        .limit(1)
    ).first()
    return display_of(revision)


def display_of(revision: Any) -> dict[str, Any]:
    """Presentation hints from the immutable profile revision (data only, no code)."""
    d = (revision.definition if revision is not None else None) or {}
    return {
        "profile_revision_id": None if revision is None else revision.id,
        "profile_revision": None if revision is None else revision.revision,
        "title": d.get("title"),
        "panels": d.get("panels") or [],
        "analysis": d.get("analysis"),
        "sign_convention": d.get("sign_convention"),
        "cadence_note": d.get("cadence_note"),
        "tolerance": d.get("tolerance_c"),
        "state_channel": d.get("state_channel"),
    }


def _source_state(coll: dict[str, Any], instrument_id: str) -> str | None:
    for s in coll.get("sources") or []:
        if s.get("instrument_id") == instrument_id:
            return str(s.get("state"))
    return None


def _acquisition_state(
    conn: Connection,
    inst: Any,
    assignment: Any,
    coll: dict[str, Any],
    chs: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    measured = [c for c in chs if c["role"] != "state"] or chs
    last_good_times = [c["last_good"]["event_time"] for c in measured if c["last_good"]]
    last_good = max(last_good_times) if last_good_times else None
    base: dict[str, Any] = {"last_good_at": last_good}
    online = coll["connectivity"] == "online"
    source = _source_state(coll, inst.id) if online else None
    if assignment is None:
        return {**base, "state": "waiting", "reason_code": "no_assignment"}
    if not any(c["latest"] for c in chs):
        if source == "unavailable":
            return {**base, "state": "source_unavailable", "reason_code": "no_response"}
        return {
            **base,
            "state": "waiting",
            "reason_code": "collector_online_no_samples" if online else "no_samples_yet",
        }
    fresh = [c for c in measured if c["fresh"]]
    if len(fresh) == len(measured):
        bad = [c for c in measured if c["latest"]["quality"] == "bad"]
        if bad:
            return {
                **base,
                "state": "degraded",
                "reason_code": "bad_quality",
                "channels": [
                    {"key": c["key"], "quality_reason": c["latest"]["quality_reason"]} for c in bad
                ],
            }
        return {**base, "state": "receiving", "reason_code": "fresh"}
    if fresh:
        return {
            **base,
            "state": "degraded",
            "reason_code": "partial",
            "channels": [{"key": c["key"]} for c in measured if not c["fresh"]],
        }
    if online and source == "unavailable":
        return {
            **base,
            "state": "source_unavailable",
            "reason_code": "collector_online_no_response",
        }
    if online:
        unacked = coll.get("unacknowledged") or 0
        if coll.get("sampling") and unacked > 0 and coll.get("backlog_age_s", 0) > 5.0:
            return {
                **base,
                "state": "stale",
                "reason_code": "collector_sampling_delivery_delayed",
                "unacknowledged": unacked,
            }
        return {**base, "state": "stale", "reason_code": "collector_online_no_new_readings"}
    last_event = conn.execute(
        select(AcquisitionEvent.event_type, AcquisitionEvent.capture_time)
        .where(and_(AcquisitionEvent.instrument_id == inst.id))
        .order_by(desc(AcquisitionEvent.capture_time))
        .limit(1)
    ).first()
    reason = (
        "collector_offline"
        if coll["connectivity"] == "offline"
        else ("collector_revoked" if coll["connectivity"] == "revoked" else "collector_never_seen")
    )
    return {
        **base,
        "state": "stale",
        "reason_code": reason,
        "last_event": None
        if last_event is None
        else {"type": last_event.event_type, "at": _iso(last_event.capture_time)},
    }


def _process_state(profile_key: str, chs: list[dict[str, Any]]) -> dict[str, Any]:
    state_chs = [c for c in chs if c["role"] == "state"]
    if not state_chs:
        return {"state": "unknown", "source": None}
    c = state_chs[0]
    latest = c["latest"]
    if not c["fresh"] or latest is None or latest["quality"] == "bad":
        return {
            "state": "unknown",
            "source": f"channel:{c['key']}",
            "last_known": None if latest is None else latest.get("label"),
            "last_known_at": None if latest is None else latest.get("event_time"),
        }
    return {
        "state": latest.get("label") or str(latest["value"]),
        "code": latest["value"],
        "source": f"channel:{c['key']}",
        "since_check": latest["event_time"],
    }
