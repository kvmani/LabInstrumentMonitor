"""Envelope and batch contract: canonical form, golden digests and shape rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from labmon.contracts.envelope import Batch, Envelope
from tests.conftest import FIXTURES


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name", ["envelope_sample_v1.json", "envelope_bad_sample_v1.json", "envelope_event_v1.json"]
)
def test_envelope_fixture_golden_digest(name: str) -> None:
    fx = load(name)
    env = Envelope.model_validate(fx["envelope"])
    assert env.canonical() == fx["canonical"]
    assert env.digest() == fx["digest"]
    # round trip through the canonical text is stable
    assert Envelope.model_validate_json(env.canonical()).digest() == fx["digest"]


def test_batch_fixture_digest_excludes_transport_metadata() -> None:
    fx = load("batch_v1.json")
    batch = Batch.model_validate(fx["batch"])
    assert batch.digest() == fx["digest"]
    resent = Batch.model_validate({**fx["batch"], "attempt": 7, "sent_at": "2030-01-01T00:00:00Z"})
    assert resent.digest() == fx["digest"]


def _sample(**kw: object) -> dict:
    base = load("envelope_sample_v1.json")["envelope"]
    base.update(kw)
    return base


def test_offsets_are_normalised_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    env = Envelope.model_validate(_sample(capture_time=datetime(2026, 11, 6, 14, 0, tzinfo=ist)))
    assert env.capture_time == datetime(2026, 11, 6, 8, 30, tzinfo=UTC)
    assert env.model_dump(mode="json")["capture_time"] == "2026-11-06T08:30:00.000000Z"


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"capture_time": "2026-11-06T08:30:00"}, "naive timestamp"),
        ({"value": float("nan")}, "non-finite"),
        ({"value": float("inf")}, "non-finite"),
        ({"value": None}, "good sample without value"),
        ({"quality": "uncertain"}, "uncertain without reason"),
        ({"quality": None}, "sample without quality"),
        ({"event_type": "source_unavailable"}, "sample with event fields"),
        ({"raw": "x" * 300}, "raw too long"),
        ({"seq": 0}, "sequence starts at 1"),
        ({"collector_id": "bad id with spaces"}, "id pattern"),
        ({"unexpected": 1}, "unknown field"),
        ({"source_time_meaning": "sample_time"}, "meaning without source time"),
    ],
)
def test_invalid_envelopes(override: dict, why: str) -> None:
    with pytest.raises(ValidationError):
        Envelope.model_validate(_sample(**override))


def test_bad_sample_may_withhold_value() -> None:
    env = Envelope.model_validate(
        _sample(value=None, quality="bad", quality_reason="sensor_open", raw="OPEN")
    )
    assert env.value is None


def test_batch_rejects_foreign_and_duplicate_records() -> None:
    fx = load("batch_v1.json")["batch"]
    recs = fx["records"]
    with pytest.raises(ValidationError):
        Batch.model_validate({**fx, "records": [recs[0], recs[0]]})
    foreign = {**recs[1], "collector_id": "col-other"}
    with pytest.raises(ValidationError):
        Batch.model_validate({**fx, "records": [recs[0], foreign]})
    with pytest.raises(ValidationError):
        Batch.model_validate({**fx, "records": []})
