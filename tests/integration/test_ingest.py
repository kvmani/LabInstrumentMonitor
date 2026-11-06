"""Ingest contract on real PostgreSQL: idempotency, conflicts, rejection, commit-before-ACK."""

from __future__ import annotations

import gzip
import json
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from labmon.contracts.envelope import EventType
from labmon.server.ingest import IngestError, ingest, parse_batch
from labmon.server.models import (
    AcquisitionEvent,
    BatchReceipt,
    IngestConflict,
    IngestIdentity,
    Sample,
)
from tests.conftest import FakeNow
from tests.integration.helpers import (
    EPOCH,
    Seq,
    World,
    batch_body,
    client_for,
    event,
    frames,
    post_batch,
    sample,
    seed_world,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def world(engine, t0: datetime) -> World:  # type: ignore[no-untyped-def]
    return seed_world(engine, t0 - timedelta(days=60))


def count(engine, model) -> int:  # type: ignore[no-untyped-def]
    with engine.connect() as c:
        return int(c.execute(select(func.count()).select_from(model)).scalar_one())


def test_commit_and_ack(world: World, db_url: str, fake_now: FakeNow, t0: datetime) -> None:
    client = client_for(world, db_url, fake_now)
    recs = frames(Seq(), t0 - timedelta(seconds=3), 3) + [event(10, t0, EventType.SOURCE_RECOVERED)]
    bid, body = batch_body(recs)
    r = post_batch(client, world.token, body, gzip_=True)
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack["outcome"] == "committed" and ack["batch_id"] == bid
    assert ack["accepted"] == 10 and ack["duplicates"] == 0 and not ack["replayed_receipt"]
    assert ack["payload_digest"] == parse_batch(body).digest
    assert count(world.engine, Sample) == 9
    assert count(world.engine, AcquisitionEvent) == 1
    assert count(world.engine, IngestIdentity) == 10
    with world.engine.connect() as c:
        row = c.execute(select(Sample).where(Sample.seq == 1)).one()
    assert row.receive_time == t0 and row.event_time == t0 - timedelta(seconds=3)
    assert row.event_time_source == "capture" and row.clock_confidence == "trusted"


def test_identical_retransmission_returns_recorded_outcome(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    client = client_for(world, db_url, fake_now)
    _, body = batch_body(frames(Seq(), t0, 2))
    first = post_batch(client, world.token, body).json()
    fake_now.advance(30)
    resent = json.loads(body)
    resent["attempt"] = 2  # transport metadata differs, digest does not
    second = post_batch(client, world.token, json.dumps(resent).encode())
    assert second.status_code == 200
    assert second.json()["receipt_id"] == first["receipt_id"]
    assert second.json()["replayed_receipt"] is True
    assert count(world.engine, Sample) == 6 and count(world.engine, BatchReceipt) == 1


def test_same_batch_id_with_different_payload_is_a_conflict(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    client = client_for(world, db_url, fake_now)
    bid, body = batch_body(frames(Seq(), t0, 1))
    assert post_batch(client, world.token, body).status_code == 200
    _, other = batch_body(frames(Seq(50), t0, 1), batch_id=bid)
    r = post_batch(client, world.token, other)
    assert r.status_code == 409 and r.json()["code"] == "batch_conflict"
    assert count(world.engine, Sample) == 3


def test_identical_record_in_a_new_batch_counts_as_duplicate(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    client = client_for(world, db_url, fake_now)
    recs = frames(Seq(), t0, 2)
    post_batch(client, world.token, batch_body(recs[:4])[1])
    r = post_batch(client, world.token, batch_body(recs)[1])  # re-batched superset
    assert r.status_code == 200
    assert (r.json()["accepted"], r.json()["duplicates"]) == (2, 4)
    assert count(world.engine, Sample) == 6


def test_identity_conflict_rejects_batch_and_is_surfaced(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    client = client_for(world, db_url, fake_now)
    post_batch(client, world.token, batch_body([sample(1, t0, value=850.0)])[1])
    tampered = [sample(1, t0, value=999.0), sample(2, t0, channel="sp")]
    r = post_batch(client, world.token, batch_body(tampered)[1])
    assert r.status_code == 422
    body = r.json()
    assert body["outcome"] == "rejected"
    assert [(x["seq"], x["code"]) for x in body["rejected"]] == [(1, "identity_conflict")]
    assert count(world.engine, Sample) == 1  # whole batch rolled back, original kept
    with world.engine.connect() as c:
        kept = c.execute(select(Sample.value_num).where(Sample.seq == 1)).scalar_one()
        conflicts = c.execute(select(IngestConflict)).all()
    assert kept == 850.0 and len(conflicts) == 1


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        (
            lambda t: sample(2, t, channel="sp", instrument="furnace-01", value=True),
            "type_mismatch",
        ),
        (lambda t: sample(2, t, channel="phase", value=9), "type_mismatch"),
        (lambda t: sample(2, t, instrument="furnace-b"), "not_assigned"),
        (lambda t: sample(2, t, value=1.0, simulated=False), "simulation_mismatch"),
        (lambda t: sample(2, t - timedelta(days=365), value=1.0), "outside_assignment"),
        (lambda t: sample(2, t + timedelta(days=3), value=1.0), "time_out_of_bounds"),
    ],
)
def test_permanent_record_faults_reject_whole_batch(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime, bad, code: str
) -> None:  # type: ignore[no-untyped-def]
    client = client_for(world, db_url, fake_now)
    if code == "simulation_mismatch":
        rec = sample(2, t0, value=1.0)
        bad_rec = rec.model_copy(update={"simulated": False})
    else:
        bad_rec = bad(t0)
    recs = [sample(1, t0), bad_rec, sample(3, t0, channel="sp")]
    r = post_batch(client, world.token, batch_body(recs)[1])
    assert r.status_code == 422, r.text
    assert [(x["seq"], x["code"]) for x in r.json()["rejected"]] == [(2, code)]
    assert count(world.engine, Sample) == 0
    # the edge re-batches the valid siblings under a new batch id
    r2 = post_batch(client, world.token, batch_body([recs[0], recs[2]])[1])
    assert r2.status_code == 200 and r2.json()["accepted"] == 2


def test_rejection_is_recorded_and_replayed(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    client = client_for(world, db_url, fake_now)
    _, body = batch_body([sample(1, t0, channel="phase", value=99)])
    a = post_batch(client, world.token, body)
    b = post_batch(client, world.token, body)
    assert a.status_code == b.status_code == 422
    assert b.json()["replayed_receipt"] and a.json()["receipt_id"] == b.json()["receipt_id"]


def test_malformed_record_is_rejected_with_its_identity(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    client = client_for(world, db_url, fake_now)
    bid, body = batch_body([sample(1, t0), sample(2, t0, channel="sp")])
    data = json.loads(body)
    data["records"][1]["quality"] = "excellent"
    r = post_batch(client, world.token, json.dumps(data).encode())
    assert r.status_code == 422
    assert r.json()["rejected"][0]["code"] == "malformed_record"
    _, raw = batch_body([sample(5, t0)])
    raw = raw.replace(b"850.0", b"NaN")
    r = post_batch(client, world.token, raw)
    assert r.status_code == 400 and r.json()["code"] == "malformed_batch"


def test_auth_and_collector_binding(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    client = client_for(world, db_url, fake_now)
    _, body = batch_body([sample(1, t0)])
    assert post_batch(client, "lmc_wrong" + "x" * 40, body).status_code == 401
    assert client.post("/api/v1/ingest/batches", content=body).status_code == 401
    # a valid credential for another collector cannot submit col-1 data
    r = post_batch(client, world.other_token, body)
    assert r.status_code == 403 and r.json()["code"] == "collector_mismatch"
    with world.engine.begin() as c:
        c.execute(
            text("UPDATE collector_credential SET revoked_at = now() WHERE collector_id = 'col-1'")
        )
    assert post_batch(client, world.token, body).status_code == 401
    assert count(world.engine, Sample) == 0


def test_size_limits_and_compressed_expansion(world: World, db_url: str, fake_now: FakeNow) -> None:
    client = client_for(world, db_url, fake_now)
    bomb = gzip.compress(b"{" + b" " * (2 * 1024 * 1024) + b"}")
    r = client.post(
        "/api/v1/ingest/batches",
        content=bomb,
        headers={"Authorization": f"Bearer {world.token}", "Content-Encoding": "gzip"},
    )
    assert r.status_code == 413
    r = client.post(
        "/api/v1/ingest/batches",
        content=b"x" * (1024 * 1024 + 10),
        headers={"Authorization": f"Bearer {world.token}"},
    )
    assert r.status_code == 413


def test_future_capture_time_is_kept_but_flagged(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    client = client_for(world, db_url, fake_now)
    post_batch(client, world.token, batch_body([sample(1, t0 + timedelta(minutes=10))])[1])
    with world.engine.connect() as c:
        row = c.execute(select(Sample.clock_confidence, Sample.clock_flag, Sample.quality)).one()
    assert tuple(row) == ("suspect", "future_capture_time", "good")  # original quality kept


def test_commit_before_ack_lost_ack_recovers_exactly_once(
    world: World, db_url: str, fake_now: FakeNow, t0: datetime
) -> None:
    fail = {"on": True}

    def lose_ack(_batch_id: str) -> None:
        if fail["on"]:
            raise RuntimeError("connection dropped after commit")

    client = client_for(world, db_url, fake_now, after_commit=lose_ack)
    _, body = batch_body(frames(Seq(), t0, 5))
    r = post_batch(client, world.token, body)
    assert r.status_code == 500  # the collector never saw an ACK
    assert count(world.engine, Sample) == 15  # but the commit is durable
    fail["on"] = False
    r = post_batch(client, world.token, body)
    assert r.status_code == 200 and r.json()["replayed_receipt"]
    assert count(world.engine, Sample) == 15


def test_concurrent_retransmissions_commit_once(world: World, t0: datetime) -> None:
    pb = parse_batch(batch_body(frames(Seq(), t0, 20))[1])
    results, errors = [], []
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait()
        try:
            results.append(ingest(world.engine, "col-1", pb, lambda: t0))
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert {r.status for r in results} == {200}
    assert len({r.ack.receipt_id for r in results}) == 1
    assert sum(not r.ack.replayed_receipt for r in results) == 1
    assert count(world.engine, Sample) == 60 and count(world.engine, BatchReceipt) == 1


def test_concurrent_overlapping_batches_never_double_insert(world: World, t0: datetime) -> None:
    recs = frames(Seq(), t0, 30)
    batches = [parse_batch(batch_body(recs[i : i + 60])[1]) for i in (0, 15, 30)]
    errors = []

    def worker(pb) -> None:  # type: ignore[no-untyped-def]
        try:
            ingest(world.engine, "col-1", pb, lambda: t0)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(pb,)) for pb in batches * 2]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert count(world.engine, Sample) == 90
    with world.engine.connect() as c:
        seqs = [r[0] for r in c.execute(select(Sample.seq).order_by(Sample.seq))]
    assert seqs == list(range(1, 91))


def test_wrong_collector_in_parsed_batch_is_refused(world: World, t0: datetime) -> None:
    pb = parse_batch(batch_body([sample(1, t0)])[1])
    with pytest.raises(IngestError) as exc:
        ingest(world.engine, "col-b", pb, lambda: t0)
    assert exc.value.status == 403
    assert EPOCH == pb.records[0].spool_epoch  # type: ignore[union-attr]
