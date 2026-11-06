"""Uploader response handling against a mock transport (no network, no sleeps)."""

from __future__ import annotations

import gzip
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from labmon.collector.outbox import Outbox
from labmon.collector.uploader import Backoff, StepResult, Uploader
from labmon.contracts.envelope import Envelope, RecordKind
from labmon.sim.clock import VirtualClock

T = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)


def fill(ob: Outbox, n: int) -> None:
    for _ in range(n):
        ob.append(
            lambda e, s: Envelope(
                kind=RecordKind.SAMPLE,
                collector_id="col-1",
                spool_epoch=e,
                seq=s,
                instrument_id="furnace-01",
                profile_revision_id="furnace-01-r1",
                channel_id="furnace-01.pv",
                value=float(s),
                quality="good",
                capture_time=T,
                simulated=True,
            )
        )


class Server:
    """Scripted responses; records every request body."""

    def __init__(self, script: list) -> None:  # type: ignore[type-arg]
        self.script = script
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(gzip.decompress(request.content))
        self.bodies.append(body)
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        status, extra, headers = action
        if status == 200:
            payload = {
                "outcome": "committed",
                "batch_id": body["batch_id"],
                "receipt_id": 9,
                **extra,
            }
        else:
            payload = extra
        return httpx.Response(status, json=payload, headers=headers)


def make(tmp_path: Path, script: list) -> tuple[Uploader, Outbox, Server]:  # type: ignore[type-arg]
    clock = VirtualClock(T)
    ob = Outbox(tmp_path / "s.sqlite3", "col-1", now=clock.now)
    server = Server(script)
    client = httpx.Client(transport=httpx.MockTransport(server), base_url="http://central")
    up = Uploader(ob, client, "lmc_" + "x" * 40, clock)
    up.backoff.rng = random.Random(1)
    return up, ob, server


def digest_of(ob: Outbox) -> str:
    b = ob.next_batch()
    assert b is not None
    return b.digest


def test_lost_ack_retries_the_same_batch(tmp_path: Path) -> None:
    up, ob, server = make(tmp_path, [httpx.ReadTimeout("lost ack"), (200, {}, {})])
    fill(ob, 3)
    d = digest_of(ob)
    server.script[1] = (200, {"payload_digest": d}, {})
    res, delay = up.step()
    assert res is StepResult.RETRY and delay > 0
    res, _ = up.step()
    assert res is StepResult.ACKED
    first, second = server.bodies
    assert first["batch_id"] == second["batch_id"]
    assert first["records"] == second["records"]
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert ob.counts().acked_cached == 3


def test_ack_for_a_different_digest_is_not_trusted(tmp_path: Path) -> None:
    up, ob, _ = make(tmp_path, [(200, {"payload_digest": "0" * 64}, {})])
    fill(ob, 1)
    res, _ = up.step()
    assert res is StepResult.FAULT
    assert ob.counts().acked_cached == 0 and ob.counts().batched == 1


def test_auth_failure_stops_upload_but_keeps_data(tmp_path: Path) -> None:
    up, ob, server = make(tmp_path, [(401, {"detail": "revoked"}, {})])
    fill(ob, 2)
    assert up.step()[0] is StepResult.FAULT
    assert up.step()[0] is StepResult.FAULT  # no further requests
    assert len(server.bodies) == 1
    c = ob.counts()
    assert c.unacknowledged == 2 and c.upload_fault and "authentication" in c.upload_fault
    fill(ob, 1)  # collection continues
    assert ob.counts().unacknowledged == 3


def test_rejection_quarantines_and_rebatches(tmp_path: Path) -> None:
    up, ob, server = make(tmp_path, [])
    fill(ob, 3)
    epoch = ob.epoch
    server.script = [
        (
            422,
            {
                "outcome": "rejected",
                "rejected": [
                    {
                        "collector_id": "col-1",
                        "spool_epoch": epoch,
                        "seq": 2,
                        "code": "type_mismatch",
                        "message": "no",
                    }
                ],
            },
            {},
        ),
        (200, {}, {}),
    ]
    assert up.step()[0] is StepResult.REJECTED
    b2 = ob.next_batch()
    assert b2 is not None
    server.script[0] = (200, {"payload_digest": b2.digest}, {})
    assert up.step()[0] is StepResult.ACKED
    assert server.bodies[0]["batch_id"] != server.bodies[1]["batch_id"]
    assert [r["seq"] for r in server.bodies[1]["records"]] == [1, 3]
    assert ob.counts().quarantined == 1


def test_retryable_status_honours_retry_after(tmp_path: Path) -> None:
    up, ob, _ = make(tmp_path, [(503, {"code": "database_unavailable"}, {"Retry-After": "20"})])
    fill(ob, 1)
    res, delay = up.step()
    assert res is StepResult.RETRY and delay >= 20


def test_batch_conflict_stops_and_preserves_records(tmp_path: Path) -> None:
    up, ob, _ = make(tmp_path, [(409, {"code": "batch_conflict"}, {})])
    fill(ob, 2)
    assert up.step()[0] is StepResult.FAULT
    c = ob.counts()
    assert c.batched == 2 and c.upload_fault and c.upload_fault.startswith("batch_conflict")


@pytest.mark.parametrize("failures", [1, 2, 5, 10, 50])
def test_backoff_is_bounded_with_jitter(failures: int) -> None:
    b = Backoff(base_s=1.0, cap_s=60.0, rng=random.Random(4))
    delays = [b.next_delay() for _ in range(failures)]
    assert all(0 < d <= 60.0 for d in delays)
    ceiling = min(60.0, 2 ** (failures - 1))
    assert ceiling / 2 <= delays[-1] <= ceiling
