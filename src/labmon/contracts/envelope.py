"""Immutable record envelopes and upload batches shared by the collector and the central API.

Identity: ``(collector_id, spool_epoch, seq)`` is the global identity of every record (sample or
diagnostic event). The collector allocates it atomically when it persists the envelope, before any
transmission. The canonical digest of a record covers every field of the envelope; the digest of a
batch covers its identity and its exact membership, never mutable transport metadata such as
``sent_at`` or ``attempt``.

Protocol version 1. See docs/adr/0002-ingest-identity.md.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

PROTOCOL_VERSION: Final = 1
SUPPORTED_PROTOCOL_VERSIONS = frozenset({1})

MAX_BATCH_RECORDS = 500
MAX_BATCH_DECODED_BYTES = 1024 * 1024
MAX_RAW_LENGTH = 256
MAX_TEXT_LENGTH = 128
MAX_DETAIL_BYTES = 2048

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
StableId = Annotated[str, Field(pattern=ID_PATTERN)]


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must carry an RFC 3339 offset")
    return value.astimezone(UTC)


def format_utc(value: datetime) -> str:
    """Canonical text form: UTC with microseconds and a ``Z`` suffix."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


UtcDatetime = Annotated[
    datetime,
    AfterValidator(_require_utc),
    PlainSerializer(format_utc, return_type=str, when_used="json"),
]


class RecordKind(StrEnum):
    SAMPLE = "sample"
    EVENT = "event"


class Quality(StrEnum):
    GOOD = "good"
    UNCERTAIN = "uncertain"
    BAD = "bad"


class QualityReason(StrEnum):
    SENSOR_OPEN = "sensor_open"
    OVERRANGE = "overrange"
    MALFORMED = "malformed"
    CLOCK_SUSPECT = "clock_suspect"
    VENDOR_STATUS_FAULT = "vendor_status_fault"


class SourceTimeMeaning(StrEnum):
    """What a source-supplied timestamp means, declared by the adapter."""

    SAMPLE_TIME = "sample_time"
    LAST_CHANGE = "last_change"
    ABSENT = "absent"


class ClockConfidence(StrEnum):
    TRUSTED = "trusted"
    SUSPECT = "suspect"


class EventType(StrEnum):
    """Bounded diagnostic events carried in the same identity space as samples."""

    SPOOL_EPOCH_STARTED = "spool_epoch_started"
    ACQUISITION_STARTED = "acquisition_started"
    ACQUISITION_STOPPED = "acquisition_stopped"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_RECOVERED = "source_recovered"
    SCHEDULE_SKIPPED = "schedule_skipped"
    ACQUISITION_LOST_INTERVAL = "acquisition_lost_interval"
    CLOCK_JUMP = "clock_jump"
    RUN_STARTED = "run_started"
    RUN_ENDED = "run_ended"


ScalarValue = float | bool | int | None


class Envelope(BaseModel):
    """One immutable record. Samples carry a typed value; events carry a bounded detail object."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)

    v: Literal[1] = PROTOCOL_VERSION
    kind: RecordKind
    collector_id: StableId
    spool_epoch: int = Field(ge=1, le=2**31 - 1)
    seq: int = Field(ge=1, le=2**62)
    instrument_id: StableId
    profile_revision_id: StableId
    capture_time: UtcDatetime
    clock_confidence: ClockConfidence = ClockConfidence.TRUSTED
    simulated: bool

    # sample fields
    channel_id: StableId | None = None
    frame_id: Annotated[str, Field(max_length=MAX_TEXT_LENGTH)] | None = None
    value: ScalarValue = None
    raw: Annotated[str, Field(max_length=MAX_RAW_LENGTH)] | None = None
    source_status: Annotated[str, Field(max_length=MAX_TEXT_LENGTH)] | None = None
    quality: Quality | None = None
    quality_reason: QualityReason | None = None
    source_time: UtcDatetime | None = None
    source_time_meaning: SourceTimeMeaning = SourceTimeMeaning.ABSENT

    # event fields
    event_type: EventType | None = None
    detail: dict[str, Any] | None = None

    @field_validator("value")
    @classmethod
    def _finite(cls, v: ScalarValue) -> ScalarValue:
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("numeric values must be finite")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Envelope:
        if self.kind is RecordKind.SAMPLE:
            if self.channel_id is None or self.quality is None:
                raise ValueError("a sample needs channel_id and quality")
            if self.event_type is not None or self.detail is not None:
                raise ValueError("a sample cannot carry event fields")
            if self.value is None and self.quality is not Quality.BAD:
                raise ValueError("only a bad-quality sample may withhold its value")
            if self.quality is not Quality.GOOD and self.quality_reason is None:
                raise ValueError("uncertain or bad quality needs a reason")
            if (
                self.source_time is None
                and self.source_time_meaning is not SourceTimeMeaning.ABSENT
            ):
                raise ValueError("source_time_meaning requires source_time")
        else:
            if self.event_type is None:
                raise ValueError("an event needs event_type")
            if self.channel_id is not None or self.value is not None or self.quality is not None:
                raise ValueError("an event cannot carry sample fields")
            if self.detail is not None and len(canonical_json(self.detail)) > MAX_DETAIL_BYTES:
                raise ValueError("event detail too large")
        return self

    @property
    def identity(self) -> tuple[str, int, int]:
        return (self.collector_id, self.spool_epoch, self.seq)

    def canonical(self) -> str:
        return canonical_json(self.model_dump(mode="json"))

    def digest(self) -> str:
        return sha256_hex(self.canonical())


class Batch(BaseModel):
    """Upload unit. ``batch_id`` and membership are fixed before the first send."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: Literal[1] = PROTOCOL_VERSION
    batch_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    collector_id: StableId
    records: Annotated[list[Envelope], Field(min_length=1, max_length=MAX_BATCH_RECORDS)]
    # transport metadata excluded from the digest
    sent_at: UtcDatetime | None = None
    attempt: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _members(self) -> Batch:
        seen: set[tuple[str, int, int]] = set()
        for rec in self.records:
            if rec.collector_id != self.collector_id:
                raise ValueError("every record must belong to the batch collector")
            if rec.identity in seen:
                raise ValueError(f"duplicate identity in batch: {rec.identity}")
            seen.add(rec.identity)
        return self

    def digest(self) -> str:
        return batch_digest(self.batch_id, self.collector_id, [r.digest() for r in self.records])


def batch_digest(batch_id: str, collector_id: str, record_digests: list[str]) -> str:
    return sha256_hex(
        canonical_json(
            {
                "v": PROTOCOL_VERSION,
                "batch_id": batch_id,
                "collector_id": collector_id,
                "records": record_digests,
            }
        )
    )


def canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- acknowledgement contract -------------------------------------------------------------------


class AckOutcome(StrEnum):
    COMMITTED = "committed"
    REJECTED = "rejected"


class RejectedRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    collector_id: str
    spool_epoch: int
    seq: int
    code: str
    message: str


class BatchAck(BaseModel):
    """Returned only after the central transaction is durable (or the rejection is recorded)."""

    v: Literal[1] = PROTOCOL_VERSION
    batch_id: str
    outcome: AckOutcome
    receipt_id: int
    payload_digest: str
    accepted: int = 0
    duplicates: int = 0
    rejected: list[RejectedRecord] = Field(default_factory=list)
    replayed_receipt: bool = False
    server_time: UtcDatetime
