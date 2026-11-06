"""Batch ingestion: validate, commit samples + receipt + identity ledger atomically, then ACK.

Semantics (ADR 0002):

* The authenticated credential identifies the collector; body fields must agree with it.
* A receipt is unique per ``(collector_id, batch_id)``. Identical retransmission returns the
  recorded outcome; the same batch id with a different digest is a ``409 batch_conflict``.
* A record identity ``(collector_id, spool_epoch, seq)`` is accepted once. An identical record
  arriving in another batch counts as a duplicate; a different payload under a committed identity
  is an ``identity_conflict``, recorded in ``ingest_conflict`` and never overwritten.
* Any permanent per-record fault rejects the whole batch transactionally (explicit, 422) and lists
  the offending identities so the edge can quarantine them and re-batch the rest.
* The ACK is produced only after the transaction commits. Concurrent retransmissions are
  serialised by the unique constraints; deadlock/serialisation failures retry deterministically.
"""

from __future__ import annotations

import json
import math
import re
import time
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection, Engine, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError

from labmon.contracts.envelope import (
    ID_PATTERN,
    MAX_BATCH_DECODED_BYTES,
    MAX_BATCH_RECORDS,
    SUPPORTED_PROTOCOL_VERSIONS,
    AckOutcome,
    BatchAck,
    ClockConfidence,
    Envelope,
    RecordKind,
    RejectedRecord,
    SourceTimeMeaning,
    batch_digest,
    canonical_json,
    sha256_hex,
)
from labmon.server.models import (
    AcquisitionEvent,
    BatchReceipt,
    Channel,
    CollectorAssignment,
    IngestConflict,
    IngestIdentity,
    Instrument,
    ProfileRevision,
    Sample,
)

EARLIEST_ACCEPTED = datetime(2000, 1, 1, tzinfo=UTC)
RETRYABLE_SQLSTATES = {"40001", "40P01"}
_BATCH_ID_LEN = 32


class IngestError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --- transport decoding -------------------------------------------------------------------------


async def read_bounded(stream: AsyncIterator[bytes], limit: int = MAX_BATCH_DECODED_BYTES) -> bytes:
    buf = bytearray()
    async for chunk in stream:
        buf.extend(chunk)
        if len(buf) > limit:
            raise IngestError(413, "payload_too_large", f"request body exceeds {limit} bytes")
    return bytes(buf)


def decode_body(
    raw: bytes, content_encoding: str | None, limit: int = MAX_BATCH_DECODED_BYTES
) -> bytes:
    enc = (content_encoding or "identity").strip().lower()
    if enc == "identity":
        return raw
    if enc != "gzip":
        raise IngestError(415, "unsupported_encoding", "only gzip or identity encoding")
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(raw, limit + 1)
    except zlib.error as exc:
        raise IngestError(400, "malformed_batch", "invalid gzip stream") from exc
    if len(out) > limit or d.unconsumed_tail:
        raise IngestError(413, "payload_too_large", f"decoded batch exceeds {limit} bytes")
    if not d.eof:
        raise IngestError(400, "malformed_batch", "truncated gzip stream")
    return out


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name}")


@dataclass
class ParsedBatch:
    protocol_version: int
    batch_id: str
    collector_id: str
    records: list[Envelope | None]
    digests: list[str]
    identities: list[tuple[str, int, int]]
    schema_rejections: list[RejectedRecord] = field(default_factory=list)

    @property
    def digest(self) -> str:
        return batch_digest(self.batch_id, self.collector_id, self.digests)


def parse_batch(body: bytes) -> ParsedBatch:
    try:
        data = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError) as exc:
        raise IngestError(400, "malformed_batch", "body is not valid JSON") from exc
    if not isinstance(data, dict):
        raise IngestError(400, "malformed_batch", "batch must be an object")
    allowed = {"v", "batch_id", "collector_id", "records", "sent_at", "attempt"}
    if set(data) - allowed:
        raise IngestError(400, "malformed_batch", f"unknown fields {sorted(set(data) - allowed)}")
    version = data.get("v")
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise IngestError(
            400,
            "unsupported_protocol",
            f"supported protocol versions: {sorted(SUPPORTED_PROTOCOL_VERSIONS)}",
        )
    batch_id, collector_id = data.get("batch_id"), data.get("collector_id")
    if not (
        isinstance(batch_id, str)
        and len(batch_id) == _BATCH_ID_LEN
        and all(c in "0123456789abcdef" for c in batch_id)
    ):
        raise IngestError(400, "malformed_batch", "batch_id must be 32 lowercase hex characters")
    if not (isinstance(collector_id, str) and re.fullmatch(ID_PATTERN, collector_id)):
        raise IngestError(400, "malformed_batch", "invalid collector_id")
    records = data.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH_RECORDS:
        raise IngestError(400, "malformed_batch", f"records must hold 1..{MAX_BATCH_RECORDS} items")

    parsed = ParsedBatch(int(version), batch_id, collector_id, [], [], [])
    seen: set[tuple[str, int, int]] = set()
    for rec in records:
        if not isinstance(rec, dict):
            raise IngestError(400, "malformed_batch", "every record must be an object")
        epoch, seq = rec.get("spool_epoch"), rec.get("seq")
        if (
            not (isinstance(epoch, int) and isinstance(seq, int))
            or isinstance(epoch, bool)
            or isinstance(seq, bool)
        ):
            raise IngestError(400, "malformed_batch", "every record needs integer identity")
        rec_collector = rec.get("collector_id")
        identity = (collector_id, epoch, seq)
        if identity in seen:
            raise IngestError(400, "malformed_batch", f"duplicate identity {identity} in batch")
        seen.add(identity)
        parsed.identities.append(identity)
        try:
            env = Envelope.model_validate(rec)
        except ValidationError as exc:
            parsed.records.append(None)
            parsed.digests.append(sha256_hex(canonical_json(rec)))
            first = exc.errors()[0]
            loc = ".".join(str(p) for p in first.get("loc", ()))
            parsed.schema_rejections.append(
                RejectedRecord(
                    collector_id=collector_id,
                    spool_epoch=epoch,
                    seq=seq,
                    code="malformed_record",
                    message=f"{loc}: {first.get('msg', 'invalid')}"[:200],
                )
            )
            continue
        if rec_collector != collector_id:
            parsed.schema_rejections.append(
                RejectedRecord(
                    collector_id=collector_id,
                    spool_epoch=epoch,
                    seq=seq,
                    code="collector_mismatch",
                    message="record collector differs from batch",
                )
            )
        parsed.records.append(env)
        parsed.digests.append(env.digest())
    return parsed


# --- validation against the registry ------------------------------------------------------------


@dataclass
class IngestLimits:
    future_tolerance_s: float = 5.0
    max_future_s: float = 86_400.0


def _rej(env: Envelope, code: str, message: str) -> RejectedRecord:
    return RejectedRecord(
        collector_id=env.collector_id,
        spool_epoch=env.spool_epoch,
        seq=env.seq,
        code=code,
        message=message,
    )


def validate_against_registry(
    conn: Connection, collector_id: str, envs: list[Envelope], now: datetime, limits: IngestLimits
) -> list[RejectedRecord]:
    iids = {e.instrument_id for e in envs}
    instruments = {
        r.id: r
        for r in conn.execute(
            select(Instrument.id, Instrument.lifecycle, Instrument.simulated).where(
                Instrument.id.in_(iids)
            )
        )
    }
    channels = {
        r.id: r
        for r in conn.execute(
            select(Channel.id, Channel.instrument_id, Channel.data_type, Channel.enum_labels).where(
                Channel.instrument_id.in_(iids)
            )
        )
    }
    revisions = {
        r.id: r.instrument_id
        for r in conn.execute(
            select(ProfileRevision.id, ProfileRevision.instrument_id).where(
                ProfileRevision.instrument_id.in_(iids)
            )
        )
    }
    assignments: dict[str, list[Any]] = {}
    for a in conn.execute(
        select(CollectorAssignment).where(
            CollectorAssignment.collector_id == collector_id,
            CollectorAssignment.instrument_id.in_(iids),
        )
    ):
        assignments.setdefault(a.instrument_id, []).append(a)

    out: list[RejectedRecord] = []
    for e in envs:
        inst = instruments.get(e.instrument_id)
        if inst is None:
            out.append(_rej(e, "unknown_instrument", "instrument is not registered"))
            continue
        if inst.lifecycle not in ("active", "maintenance"):
            out.append(_rej(e, "instrument_not_active", f"instrument is {inst.lifecycle}"))
            continue
        if e.simulated != inst.simulated:
            out.append(_rej(e, "simulation_mismatch", "simulation flag differs from registry"))
            continue
        if revisions.get(e.profile_revision_id) != e.instrument_id:
            out.append(_rej(e, "unknown_profile_revision", "profile revision not of instrument"))
            continue
        if e.capture_time < EARLIEST_ACCEPTED or e.capture_time > now + timedelta(
            seconds=limits.max_future_s
        ):
            out.append(_rej(e, "time_out_of_bounds", "capture_time outside accepted bounds"))
            continue
        rows = assignments.get(e.instrument_id, [])
        if not rows:
            out.append(_rej(e, "not_assigned", "collector is not assigned to this instrument"))
            continue
        if not any(_within(a, e) for a in rows):
            out.append(
                _rej(
                    e,
                    "outside_assignment",
                    "record falls outside the collector's assignment (fenced or not yet valid)",
                )
            )
            continue
        if e.kind is RecordKind.SAMPLE:
            ch = channels.get(e.channel_id or "")
            if ch is None or ch.instrument_id != e.instrument_id:
                out.append(_rej(e, "unknown_channel", "channel is not part of the instrument"))
                continue
            problem = _type_problem(ch.data_type, ch.enum_labels, e.value)
            if problem:
                out.append(_rej(e, "type_mismatch", problem))
    return out


def _within(a: Any, e: Envelope) -> bool:
    if e.capture_time < a.valid_from:
        return False
    if a.valid_to is not None and e.capture_time >= a.valid_to:
        return False
    if (
        a.cutoff_epoch is not None
        and a.cutoff_seq is not None
        and (e.spool_epoch, e.seq) > (a.cutoff_epoch, a.cutoff_seq)
    ):
        return False
    return True


def _type_problem(data_type: str, enum_labels: dict[str, str] | None, value: object) -> str | None:
    if value is None:
        return None  # only allowed with bad quality (envelope rule)
    if data_type == "float":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return "float channel needs a number"
        if not math.isfinite(float(value)):
            return "value must be finite"
    elif data_type == "enum":
        if isinstance(value, bool) or not isinstance(value, int):
            return "enum channel needs an integer code"
        if enum_labels is not None and str(value) not in enum_labels:
            return f"enum code {value} is not mapped"
    elif data_type == "bool" and not isinstance(value, bool):
        return "boolean channel needs true/false"
    return None


# --- persistence --------------------------------------------------------------------------------


@dataclass
class IngestResult:
    status: int
    ack: BatchAck


class _Replay(Exception):
    pass


class _Conflict(Exception):
    def __init__(self, conflicts: list[tuple[tuple[str, int, int], str, str]]) -> None:
        super().__init__("identity conflict")
        self.conflicts = conflicts


def _ack_from_receipt(r: Any, now: datetime, replayed: bool) -> IngestResult:
    rejected = [RejectedRecord.model_validate(x) for x in (r.rejected or [])]
    outcome = AckOutcome(r.outcome)
    ack = BatchAck(
        batch_id=r.batch_id,
        outcome=outcome,
        receipt_id=r.id,
        payload_digest=r.payload_digest,
        accepted=r.accepted,
        duplicates=r.duplicates,
        rejected=rejected,
        replayed_receipt=replayed,
        server_time=now,
    )
    return IngestResult(200 if outcome is AckOutcome.COMMITTED else 422, ack)


def _existing_receipt(conn: Connection, collector_id: str, batch_id: str) -> Any:
    return conn.execute(
        select(BatchReceipt).where(
            BatchReceipt.collector_id == collector_id, BatchReceipt.batch_id == batch_id
        )
    ).first()


def _replay(r: Any, digest: str, now: datetime) -> IngestResult:
    if r.payload_digest != digest:
        raise IngestError(
            409, "batch_conflict", "a different payload was already recorded under this batch_id"
        )
    return _ack_from_receipt(r, now, replayed=True)


def _insert_receipt(
    conn: Connection,
    pb: ParsedBatch,
    outcome: AckOutcome,
    now: datetime,
    rejected: list[RejectedRecord],
) -> int:
    rid = conn.execute(
        pg_insert(BatchReceipt)
        .values(
            collector_id=pb.collector_id,
            batch_id=pb.batch_id,
            payload_digest=pb.digest,
            protocol_version=pb.protocol_version,
            outcome=outcome.value,
            record_count=len(pb.records),
            accepted=0,
            duplicates=0,
            rejected=[r.model_dump() for r in rejected],
            received_at=now,
        )
        .on_conflict_do_nothing(index_elements=["collector_id", "batch_id"])
        .returning(BatchReceipt.id)
    ).scalar()
    if rid is None:
        raise _Replay()
    return int(rid)


def _sample_row(
    e: Envelope, now: datetime, limits: IngestLimits, receipt_id: int, data_type: str
) -> dict[str, Any]:
    clock_conf, flag = e.clock_confidence, None
    if clock_conf is ClockConfidence.SUSPECT:
        flag = "collector_reported"
    elif e.capture_time > now + timedelta(seconds=limits.future_tolerance_s):
        clock_conf, flag = ClockConfidence.SUSPECT, "future_capture_time"
    if (
        e.source_time is not None
        and e.source_time_meaning is SourceTimeMeaning.SAMPLE_TIME
        and clock_conf is ClockConfidence.TRUSTED
    ):
        event_time, source = e.source_time, "source"
    else:
        event_time, source = e.capture_time, "capture"
    v = e.value
    return {
        "collector_id": e.collector_id,
        "spool_epoch": e.spool_epoch,
        "seq": e.seq,
        "channel_id": e.channel_id,
        "instrument_id": e.instrument_id,
        "profile_revision_id": e.profile_revision_id,
        "frame_id": e.frame_id,
        "value_num": float(v) if data_type == "float" and v is not None else None,
        "value_int": int(v) if data_type == "enum" and v is not None else None,
        "value_bool": bool(v) if data_type == "bool" and v is not None else None,
        "raw": e.raw,
        "source_status": e.source_status,
        "quality": e.quality.value if e.quality else None,
        "quality_reason": e.quality_reason.value if e.quality_reason else None,
        "source_time": e.source_time,
        "source_time_meaning": e.source_time_meaning.value,
        "capture_time": e.capture_time,
        "receive_time": now,
        "event_time": event_time,
        "event_time_source": source,
        "clock_confidence": clock_conf.value,
        "clock_flag": flag,
        "simulated": e.simulated,
        "receipt_id": receipt_id,
    }


def _ingest_tx(
    conn: Connection, pb: ParsedBatch, now: datetime, limits: IngestLimits
) -> IngestResult:
    existing = _existing_receipt(conn, pb.collector_id, pb.batch_id)
    if existing is not None:
        return _replay(existing, pb.digest, now)

    rejected = list(pb.schema_rejections)
    valid = [e for e in pb.records if e is not None]
    rejected_ids = {(r.spool_epoch, r.seq) for r in rejected}
    rejected += validate_against_registry(
        conn,
        pb.collector_id,
        [e for e in valid if (e.spool_epoch, e.seq) not in rejected_ids],
        now,
        limits,
    )
    if rejected:
        rid = _insert_receipt(conn, pb, AckOutcome.REJECTED, now, rejected)
        r = conn.execute(select(BatchReceipt).where(BatchReceipt.id == rid)).one()
        return _ack_from_receipt(r, now, replayed=False)

    receipt_id = _insert_receipt(conn, pb, AckOutcome.COMMITTED, now, [])
    ordered = sorted(
        zip(valid, (e.digest() for e in valid), strict=True), key=lambda x: x[0].identity
    )
    inserted = {
        (row[0], int(row[1]), int(row[2]))
        for row in conn.execute(
            pg_insert(IngestIdentity)
            .values(
                [
                    {
                        "collector_id": e.collector_id,
                        "spool_epoch": e.spool_epoch,
                        "seq": e.seq,
                        "kind": e.kind.value,
                        "record_digest": d,
                        "receipt_id": receipt_id,
                    }
                    for e, d in ordered
                ]
            )
            .on_conflict_do_nothing()
            .returning(IngestIdentity.collector_id, IngestIdentity.spool_epoch, IngestIdentity.seq)
        )
    }
    others = [(e, d) for e, d in ordered if e.identity not in inserted]
    duplicates = 0
    if others:
        existing_digests = {
            (r[0], int(r[1]), int(r[2])): r[3]
            for r in conn.execute(
                select(
                    IngestIdentity.collector_id,
                    IngestIdentity.spool_epoch,
                    IngestIdentity.seq,
                    IngestIdentity.record_digest,
                ).where(
                    tuple_(
                        IngestIdentity.collector_id, IngestIdentity.spool_epoch, IngestIdentity.seq
                    ).in_([e.identity for e, _ in others])
                )
            )
        }
        conflicts = [
            (e.identity, existing_digests[e.identity], d)
            for e, d in others
            if existing_digests.get(e.identity) != d
        ]
        if conflicts:
            raise _Conflict(conflicts)
        duplicates = len(others)

    fresh = [e for e, _ in ordered if e.identity in inserted]
    data_types = {
        r.id: r.data_type
        for r in conn.execute(
            select(Channel.id, Channel.data_type).where(
                Channel.id.in_({e.channel_id for e in fresh if e.channel_id})
            )
        )
    }
    samples = [
        _sample_row(e, now, limits, receipt_id, data_types[e.channel_id or ""])
        for e in fresh
        if e.kind is RecordKind.SAMPLE
    ]
    events = [
        {
            "collector_id": e.collector_id,
            "spool_epoch": e.spool_epoch,
            "seq": e.seq,
            "instrument_id": e.instrument_id,
            "profile_revision_id": e.profile_revision_id,
            "event_type": e.event_type.value if e.event_type else None,
            "detail": e.detail,
            "capture_time": e.capture_time,
            "receive_time": now,
            "clock_confidence": e.clock_confidence.value,
            "simulated": e.simulated,
            "receipt_id": receipt_id,
        }
        for e in fresh
        if e.kind is RecordKind.EVENT
    ]
    if samples:
        conn.execute(insert(Sample), samples)
    if events:
        conn.execute(insert(AcquisitionEvent), events)
    conn.execute(
        update(BatchReceipt)
        .where(BatchReceipt.id == receipt_id)
        .values(accepted=len(fresh), duplicates=duplicates)
    )
    ack = BatchAck(
        batch_id=pb.batch_id,
        outcome=AckOutcome.COMMITTED,
        receipt_id=receipt_id,
        payload_digest=pb.digest,
        accepted=len(fresh),
        duplicates=duplicates,
        server_time=now,
    )
    return IngestResult(200, ack)


def _record_conflicts(
    conn: Connection,
    pb: ParsedBatch,
    now: datetime,
    conflicts: list[tuple[tuple[str, int, int], str, str]],
) -> IngestResult:
    existing = _existing_receipt(conn, pb.collector_id, pb.batch_id)
    if existing is not None:
        return _replay(existing, pb.digest, now)
    conn.execute(
        insert(IngestConflict),
        [
            {
                "collector_id": c,
                "spool_epoch": ep,
                "seq": sq,
                "batch_id": pb.batch_id,
                "existing_digest": old,
                "offered_digest": new,
                "detected_at": now,
            }
            for (c, ep, sq), old, new in conflicts
        ],
    )
    rejected = [
        RejectedRecord(
            collector_id=c,
            spool_epoch=ep,
            seq=sq,
            code="identity_conflict",
            message="identity already committed with a different payload",
        )
        for (c, ep, sq), _, _ in conflicts
    ]
    rid = _insert_receipt(conn, pb, AckOutcome.REJECTED, now, rejected)
    r = conn.execute(select(BatchReceipt).where(BatchReceipt.id == rid)).one()
    return _ack_from_receipt(r, now, replayed=False)


def ingest(
    engine: Engine,
    authenticated_collector: str,
    pb: ParsedBatch,
    now: Callable[[], datetime],
    limits: IngestLimits | None = None,
    max_attempts: int = 5,
) -> IngestResult:
    if pb.collector_id != authenticated_collector:
        raise IngestError(
            403,
            "collector_mismatch",
            "batch collector_id does not match the authenticated credential",
        )
    limits = limits or IngestLimits()
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.begin() as conn:
                result = _ingest_tx(conn, pb, now(), limits)
            return result  # returned only after COMMIT succeeded
        except _Replay:
            with engine.connect() as conn:
                existing = _existing_receipt(conn, pb.collector_id, pb.batch_id)
            if existing is None:
                continue  # the competing transaction rolled back; try again
            return _replay(existing, pb.digest, now())
        except _Conflict as c:
            try:
                with engine.begin() as conn:
                    return _record_conflicts(conn, pb, now(), c.conflicts)
            except _Replay:
                continue
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            if sqlstate in RETRYABLE_SQLSTATES and attempt < max_attempts:
                time.sleep(0.01 * attempt)  # deterministic, bounded
                continue
            raise
    raise IngestError(503, "ingest_contention", "could not complete ingest; retry the same batch")
