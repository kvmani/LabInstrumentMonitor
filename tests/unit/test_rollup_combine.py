"""Pure tests for merging minute rows into display buckets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from labmon.server.rollup import MinuteRow, combine

T = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)


def row(
    minute: int,
    *,
    vmin: float,
    tmin_s: int,
    vmax: float,
    tmax_s: int,
    n: int = 60,
    nv: int = 60,
    vsum: float = 0.0,
    first: float = 1.0,
    last: float = 2.0,
) -> MinuteRow:
    b = T + timedelta(minutes=minute)
    return MinuteRow(
        bucket=b,
        n=n,
        n_valid=nv,
        n_uncertain=0,
        v_sum=vsum,
        v_min=vmin,
        t_min=b + timedelta(seconds=tmin_s),
        v_max=vmax,
        t_max=b + timedelta(seconds=tmax_s),
        v_first=first,
        v_last=last,
        t_first=b,
        t_last=b + timedelta(seconds=59),
        gap_start=None,
        gap_s=None,
    )


def test_counts_add_and_mean_uses_valid_counts() -> None:
    out = combine(
        [
            row(0, vmin=1, tmin_s=0, vmax=2, tmax_s=0, nv=10, vsum=100.0),
            row(1, vmin=1, tmin_s=0, vmax=2, tmax_s=0, nv=30, vsum=900.0),
        ],
        120,
    )
    assert len(out) == 1
    assert out[0]["count"] == 120 and out[0]["valid_count"] == 40
    assert out[0]["mean"] == 25.0  # (100 + 900) / 40, not the mean of means (20)


def test_ties_go_to_the_earliest_timestamp_regardless_of_row_order() -> None:
    late = row(1, vmin=5, tmin_s=3, vmax=9, tmax_s=4, first=7.0, last=8.0)
    early = row(0, vmin=5, tmin_s=40, vmax=9, tmax_s=41, first=3.0, last=4.0)
    for rows in ([late, early], [early, late]):
        out = combine(rows, 120)
        b = out[0]
        assert b["min_t"] == "2026-09-01T06:00:40.000000Z"
        assert b["max_t"] == "2026-09-01T06:00:41.000000Z"
        assert (b["first"], b["last"]) == (3.0, 8.0)


def test_buckets_align_to_width() -> None:
    rows = [row(m, vmin=m, tmin_s=0, vmax=m, tmax_s=0) for m in range(0, 7)]
    out = combine(rows, 300)
    assert [b["t"] for b in out] == ["2026-09-01T06:00:00.000000Z", "2026-09-01T06:05:00.000000Z"]
    assert [b["count"] for b in out] == [300, 120]
