"""M1 gate: collection continues without the API; recovered IDs land exactly once.

Real API and collector processes, real SQLite outbox and real PostgreSQL. Evidence is simulated.
"""

from __future__ import annotations

import signal
import sqlite3
import time

import pytest
from sqlalchemy import create_engine, text

from tests.e2e.conftest import Stack, wait_for

pytestmark = pytest.mark.e2e


def reconcile(stack: Stack) -> dict[str, int]:
    """Every identity the collector holds as acknowledged is in the historian exactly once,
    and the historian holds nothing the collector did not produce."""
    spool = sqlite3.connect(stack.workdir / "spool.sqlite3")
    local = {
        (e, s): (state, digest)
        for e, s, state, digest in spool.execute("SELECT epoch, seq, state, digest FROM record")
    }
    spool.close()
    eng = create_engine(stack.db_url)
    with eng.connect() as c:
        central = c.execute(
            text(
                "SELECT spool_epoch, seq, record_digest FROM ingest_identity "
                "WHERE collector_id = 'col-bay1-01'"
            )
        ).all()
        samples = c.execute(text("SELECT count(*) FROM sample")).scalar_one()
        events = c.execute(text("SELECT count(*) FROM acquisition_event")).scalar_one()
        dupes = c.execute(
            text(
                "SELECT count(*) FROM (SELECT spool_epoch, seq FROM sample "
                "GROUP BY 1, 2 HAVING count(*) > 1) d"
            )
        ).scalar_one()
    eng.dispose()
    central_ids = {(int(e), int(s)): d for e, s, d in central}
    acked = {k for k, (state, _) in local.items() if state == "acked"}
    assert dupes == 0
    assert acked <= set(central_ids), "acknowledged identity missing centrally"
    assert set(central_ids) <= set(local), "central identity unknown to the collector"
    for k, d in central_ids.items():
        assert local[k][1] == d, f"payload digest differs for {k}"
    assert samples + events == len(central_ids)
    return {"acked": len(acked), "central": len(central_ids), "local": len(local)}


def max_pv_gap_s(stack: Stack) -> float:
    eng = create_engine(stack.db_url)
    with eng.connect() as c:
        gap = c.execute(
            text(
                "SELECT coalesce(max(extract(epoch FROM t - prev)), 0) FROM ("
                " SELECT event_time t, lag(event_time) OVER (ORDER BY event_time) prev"
                " FROM sample WHERE channel_id = 'furnace-01.pv') x"
            )
        ).scalar_one()
    eng.dispose()
    return float(gap)


def test_api_outage_and_collector_crash_recover_exactly_once(stack: Stack) -> None:
    stack.start_server()
    stack.start_collector()
    wait_for(lambda: stack.outbox()["acked_cached"] >= 15, 30, "first acknowledgements")

    # 1. API killed: acquisition continues into the outbox
    stack.kill_server()
    before = stack.outbox()
    time.sleep(8)
    during = stack.outbox()
    assert during["next_seq"] - before["next_seq"] >= 18  # ~3 records/s kept being captured
    assert during["unacknowledged"] > before["unacknowledged"]

    # 2. API back: backlog drains, nothing lost, nothing doubled
    stack.start_server()
    wait_for(lambda: stack.outbox()["unacknowledged"] <= 3, 40, "backlog drained")
    first = reconcile(stack)
    assert first["acked"] > 40
    assert max_pv_gap_s(stack) < 2.5  # the outage left no hole in the history

    # 3. Collector crash (SIGKILL) mid-flight, then restart on the same spool
    epoch = stack.outbox()["epoch"]
    stack.stop_collector(signal.SIGKILL)
    seq_after_crash = stack.outbox()["next_seq"]
    stack.start_collector()
    wait_for(lambda: stack.outbox()["next_seq"] > seq_after_crash + 12, 20, "restarted capture")
    status = stack.outbox()
    assert status["epoch"] == epoch  # same spool, same epoch, sequence continues
    wait_for(lambda: stack.outbox()["unacknowledged"] <= 3, 30, "drain after restart")
    stack.stop_collector()  # graceful
    stack_status = stack.outbox()
    if stack_status["unacknowledged"]:
        # final records (acquisition_stopped) stay safely pending in the outbox
        assert stack_status["quarantined"] == 0
    second = reconcile(stack)
    assert second["central"] > first["central"]
    # the crash interval is a real, explicit gap (no collector was running), not invented data
    assert max_pv_gap_s(stack) >= 1.0
