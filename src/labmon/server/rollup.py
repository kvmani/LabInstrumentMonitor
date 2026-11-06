"""One-minute rollups for long time windows.

* ``sample_rollup_1m`` is a rebuildable cache of ``sample``: per channel and UTC minute it keeps
  counts by quality, the sum of valid values, min/max **with their original timestamps**,
  first/last valid values, the first/last sample times and the largest internal gap.
* Ingest marks every touched ``(channel, minute)`` dirty in the same transaction as the samples
  (``rollup_dirty``), bumping a revision. The worker reads the revision *before* reading raw
  rows, recomputes, and deletes the dirty row only if the revision is unchanged, so it can never
  clear a newer invalidation (late or replayed samples).
* Queries use clean rollups for whole minutes inside the range and compute dirty minutes and the
  partial minutes at the range edges from raw rows, so results are always complete; the number
  of minutes still awaiting the worker is reported as ``rebuilding_minutes``.
* Gaps for coarse windows come from minute rows: the spacing between consecutive minutes'
  last/first samples, plus the largest internal gap of each minute. Several sub-minute gaps in
  the same minute are reported as the largest one; zooming in returns exact raw gaps.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from labmon.contracts.envelope import format_utc
from labmon.server.models import RollupDirty, SampleRollup1m

log = logging.getLogger(__name__)

ORIGIN = datetime(2000, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
CHUNK_MINUTES = 360  # bound the raw rows one recomputation reads

_VALUE_COL = {"float": "value_num", "enum": "value_int", "bool": "value_bool::int"}


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def ceil_minute(dt: datetime) -> datetime:
    f = floor_minute(dt)
    return f if f == dt else f + MINUTE


@dataclass
class MinuteRow:
    bucket: datetime
    n: int
    n_valid: int
    n_uncertain: int
    v_sum: float | None
    v_min: float | None
    t_min: datetime | None
    v_max: float | None
    t_max: datetime | None
    v_first: float | None
    v_last: float | None
    t_first: datetime
    t_last: datetime
    gap_start: datetime | None
    gap_s: float | None


def _minute_sql(data_type: str) -> str:
    col = _VALUE_COL.get(data_type, "value_num")
    valid = "q <> 'bad'"
    return f"""
    WITH x AS (
      SELECT event_time AS t, {col} AS v, quality AS q, seq,
             lag(event_time) OVER (ORDER BY event_time, collector_id, spool_epoch, seq) AS prev_t
      FROM sample WHERE channel_id = :cid AND event_time >= :s AND event_time < :e
    ), y AS (SELECT *, date_bin(interval '1 minute', t, :origin) AS b FROM x)
    SELECT b AS bucket, count(*) AS n,
      count(*) FILTER (WHERE {valid}) AS n_valid,
      count(*) FILTER (WHERE q = 'uncertain') AS n_uncertain,
      sum(v) FILTER (WHERE {valid}) AS v_sum,
      min(v) FILTER (WHERE {valid}) AS v_min,
      (array_agg(t ORDER BY v ASC, t, seq) FILTER (WHERE {valid}))[1] AS t_min,
      max(v) FILTER (WHERE {valid}) AS v_max,
      (array_agg(t ORDER BY v DESC, t, seq) FILTER (WHERE {valid}))[1] AS t_max,
      (array_agg(v ORDER BY t, seq) FILTER (WHERE {valid}))[1] AS v_first,
      (array_agg(v ORDER BY t DESC, seq DESC) FILTER (WHERE {valid}))[1] AS v_last,
      min(t) AS t_first, max(t) AS t_last,
      (array_agg(prev_t ORDER BY t - prev_t DESC) FILTER (WHERE prev_t >= b))[1] AS gap_start,
      max(extract(epoch FROM t - prev_t)) FILTER (WHERE prev_t >= b) AS gap_s
    FROM y GROUP BY b ORDER BY b
    """  # noqa: S608 - col comes from a fixed mapping, never from input


def raw_minutes(
    conn: Connection, channel_id: str, data_type: str, start: datetime, end: datetime
) -> list[MinuteRow]:
    """Minute aggregates computed from raw rows for ``[start, end)``."""
    if end <= start:
        return []
    rows = conn.execute(
        text(_minute_sql(data_type)), {"cid": channel_id, "s": start, "e": end, "origin": ORIGIN}
    ).all()
    return [
        MinuteRow(
            bucket=r.bucket,
            n=int(r.n),
            n_valid=int(r.n_valid),
            n_uncertain=int(r.n_uncertain),
            v_sum=None if r.v_sum is None else float(r.v_sum),
            v_min=None if r.v_min is None else float(r.v_min),
            t_min=r.t_min,
            v_max=None if r.v_max is None else float(r.v_max),
            t_max=r.t_max,
            v_first=None if r.v_first is None else float(r.v_first),
            v_last=None if r.v_last is None else float(r.v_last),
            t_first=r.t_first,
            t_last=r.t_last,
            gap_start=r.gap_start,
            gap_s=None if r.gap_s is None else float(r.gap_s),
        )
        for r in rows
    ]

    # --- invalidation (ingest side) --------------------------------------------------------------


def mark_dirty(conn: Connection, touched: Iterable[tuple[str, datetime]], now: datetime) -> None:
    """Invalidate minutes touched by an ingest transaction (sorted: no lock-order deadlocks)."""
    keys = sorted({(cid, floor_minute(t)) for cid, t in touched})
    if not keys:
        return
    stmt = pg_insert(RollupDirty).values(
        [{"channel_id": c, "bucket": b, "revision": 1, "marked_at": now} for c, b in keys]
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["channel_id", "bucket"],
            set_={"revision": RollupDirty.revision + 1, "marked_at": now},
        )
    )


# --- worker ----------------------------------------------------------------------------------


def _chunks(buckets: Sequence[datetime]) -> list[tuple[datetime, datetime]]:
    """Group sorted minute buckets into bounded contiguous-ish ranges."""
    out: list[tuple[datetime, datetime]] = []
    start = prev = buckets[0]
    for b in buckets[1:]:
        if b - prev > timedelta(minutes=10) or b - start >= timedelta(minutes=CHUNK_MINUTES):
            out.append((start, prev + MINUTE))
            start = b
        prev = b
    out.append((start, prev + MINUTE))
    return out


def process_dirty(engine: Engine, now: datetime, limit: int = 5000) -> int:
    """Recompute up to ``limit`` dirty minutes. Returns the number of minutes cleared."""
    with engine.connect() as conn:
        dirty = conn.execute(
            select(RollupDirty.channel_id, RollupDirty.bucket, RollupDirty.revision)
            .order_by(RollupDirty.channel_id, RollupDirty.bucket)
            .limit(limit)
        ).all()
        types = {
            str(cid): str(dtype)
            for cid, dtype in conn.execute(text("SELECT id, data_type FROM channel"))
        }
    by_channel: dict[str, list[tuple[datetime, int]]] = {}
    for cid, bucket, rev in dirty:
        by_channel.setdefault(cid, []).append((bucket, int(rev)))
    cleared = 0
    for cid, items in by_channel.items():
        revs = dict(items)
        for lo, hi in _chunks([b for b, _ in items]):
            with engine.begin() as conn:
                wanted = {b for b in revs if lo <= b < hi}
                rows = {
                    r.bucket: r for r in raw_minutes(conn, cid, types.get(cid, "float"), lo, hi)
                }
                upserts = [_row_values(cid, rows[b], now) for b in sorted(wanted) if b in rows]
                if upserts:
                    stmt = pg_insert(SampleRollup1m).values(upserts)
                    conn.execute(
                        stmt.on_conflict_do_update(
                            index_elements=["channel_id", "bucket"],
                            set_={
                                k: stmt.excluded[k]
                                for k in upserts[0]
                                if k not in ("channel_id", "bucket")
                            },
                        )
                    )
                empty = [b for b in wanted if b not in rows]
                if empty:
                    conn.execute(
                        delete(SampleRollup1m).where(
                            SampleRollup1m.channel_id == cid, SampleRollup1m.bucket.in_(empty)
                        )
                    )
                for b in sorted(wanted):
                    res = conn.execute(
                        delete(RollupDirty).where(
                            RollupDirty.channel_id == cid,
                            RollupDirty.bucket == b,
                            RollupDirty.revision == revs[b],
                        )
                    )
                    cleared += res.rowcount
    return cleared


def _row_values(cid: str, r: MinuteRow, now: datetime) -> dict[str, Any]:
    return {
        "channel_id": cid,
        "bucket": r.bucket,
        "n": r.n,
        "n_valid": r.n_valid,
        "n_uncertain": r.n_uncertain,
        "v_sum": r.v_sum,
        "v_min": r.v_min,
        "t_min": r.t_min,
        "v_max": r.v_max,
        "t_max": r.t_max,
        "v_first": r.v_first,
        "v_last": r.v_last,
        "t_first": r.t_first,
        "t_last": r.t_last,
        "gap_start": r.gap_start,
        "gap_s": r.gap_s,
        "computed_at": now,
    }


# --- query side -------------------------------------------------------------------------------


_CLEAN_BUCKETS_SQL = """
SELECT date_bin(make_interval(secs => :w), r.bucket, :origin) AS bucket,
  sum(r.n)::int AS n, sum(r.n_valid)::int AS n_valid, sum(r.n_uncertain)::int AS n_uncertain,
  sum(r.v_sum) AS v_sum,
  min(r.v_min) AS v_min,
  (array_agg(r.t_min ORDER BY r.v_min ASC, r.t_min) FILTER (WHERE r.v_min IS NOT NULL))[1]
    AS t_min,
  max(r.v_max) AS v_max,
  (array_agg(r.t_max ORDER BY r.v_max DESC, r.t_max) FILTER (WHERE r.v_max IS NOT NULL))[1]
    AS t_max,
  (array_agg(r.v_first ORDER BY r.bucket) FILTER (WHERE r.v_first IS NOT NULL))[1] AS v_first,
  (array_agg(r.v_last ORDER BY r.bucket DESC) FILTER (WHERE r.v_last IS NOT NULL))[1] AS v_last,
  min(r.t_first) AS t_first, max(r.t_last) AS t_last
FROM sample_rollup_1m r
WHERE r.channel_id = :cid AND r.bucket >= :m0 AND r.bucket < :m1
  AND NOT EXISTS (SELECT 1 FROM rollup_dirty d
                  WHERE d.channel_id = r.channel_id AND d.bucket = r.bucket)
GROUP BY 1 ORDER BY 1
"""

# Gaps between consecutive *clean* minutes and inside each clean minute. Pairs that span a dirty
# minute are discarded by the caller, which recomputes those regions from raw rows.
_CLEAN_GAPS_SQL = """
WITH m AS (
  SELECT r.bucket, r.t_first, r.t_last, r.gap_start, r.gap_s,
         lag(r.t_last) OVER (ORDER BY r.bucket) AS prev_last
  FROM sample_rollup_1m r
  WHERE r.channel_id = :cid AND r.bucket >= :m0 AND r.bucket < :m1
    AND NOT EXISTS (SELECT 1 FROM rollup_dirty d
                    WHERE d.channel_id = r.channel_id AND d.bucket = r.bucket)
)
SELECT prev_last AS a, t_first AS b FROM m
  WHERE prev_last IS NOT NULL AND t_first - prev_last > make_interval(secs => :thr)
UNION ALL
SELECT gap_start AS a, gap_start + make_interval(secs => gap_s) AS b FROM m
  WHERE gap_s > :thr
"""


@dataclass
class RollupView:
    buckets: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    sample_count: int
    rebuilding_minutes: int


def _neighbour_times(
    conn: Connection, channel_id: str, lo: datetime, hi: datetime
) -> tuple[datetime | None, datetime | None]:
    before = conn.execute(
        text("SELECT max(event_time) FROM sample WHERE channel_id = :c AND event_time < :lo"),
        {"c": channel_id, "lo": lo},
    ).scalar()
    after = conn.execute(
        text("SELECT min(event_time) FROM sample WHERE channel_id = :c AND event_time >= :hi"),
        {"c": channel_id, "hi": hi},
    ).scalar()
    return before, after


def rollup_view(
    conn: Connection,
    channel_id: str,
    data_type: str,
    start: datetime,
    end: datetime,
    width_s: int,
    threshold_s: float,
    now: datetime,
) -> RollupView:
    """Buckets and gaps for ``[start, end)`` using clean rollups aggregated in SQL, plus raw
    computation for dirty minutes and the partial minutes at the range edges."""
    m0, m1 = ceil_minute(start), floor_minute(end)
    if m1 < m0:
        m0 = m1 = start
    dirty = sorted(
        b
        for (b,) in conn.execute(
            select(RollupDirty.bucket).where(
                RollupDirty.channel_id == channel_id,
                RollupDirty.bucket >= m0,
                RollupDirty.bucket < m1,
            )
        )
    )
    chunks: list[tuple[datetime, datetime]] = [(start, m0)]
    if dirty:
        chunks += _chunks(dirty)
    chunks.append((m1, end))
    partials: list[MinuteRow] = []
    if m1 > m0:
        partials = [
            MinuteRow(
                bucket=r.bucket,
                n=r.n,
                n_valid=r.n_valid,
                n_uncertain=r.n_uncertain,
                v_sum=r.v_sum,
                v_min=r.v_min,
                t_min=r.t_min,
                v_max=r.v_max,
                t_max=r.t_max,
                v_first=r.v_first,
                v_last=r.v_last,
                t_first=r.t_first,
                t_last=r.t_last,
                gap_start=None,
                gap_s=None,
            )
            for r in conn.execute(
                text(_CLEAN_BUCKETS_SQL),
                {"w": width_s, "origin": ORIGIN, "cid": channel_id, "m0": m0, "m1": m1},
            )
        ]
    dirty_set = set(dirty)
    raw_rows: list[MinuteRow] = []  # rows that replace missing/dirty rollups in buckets
    raw_spans: list[tuple[datetime, datetime, list[MinuteRow]]] = []  # all rows, for gaps
    for idx, (lo, hi) in enumerate(chunks):
        rows = raw_minutes(conn, channel_id, data_type, lo, hi)
        edge = idx in (0, len(chunks) - 1)
        raw_rows += rows if edge else [r for r in rows if r.bucket in dirty_set]
        raw_spans.append((lo, hi, rows))
    # (bucket, first sample time): partials and edge minutes can share a display bucket, and
    # min/max ties must go to the earliest original timestamp
    allrows = sorted(partials + raw_rows, key=lambda r: (r.bucket, r.t_first))
    buckets = combine(allrows, width_s)

    gap_set: set[tuple[datetime, datetime, bool]] = set()
    horizon = min(end, now)

    def add(a: datetime, b: datetime, open_: bool = False) -> None:
        if (b - a).total_seconds() > threshold_s:
            gap_set.add((a, b, open_))

    if m1 > m0:
        for a, b in conn.execute(
            text(_CLEAN_GAPS_SQL), {"cid": channel_id, "m0": m0, "m1": m1, "thr": threshold_s}
        ):
            if not any(a < hi and b > lo for lo, hi, _ in raw_spans[1:-1]):
                add(a, b)
    # raw regions (edges and dirty chunks): local gaps plus the gaps to their neighbours
    for lo, hi, rows in raw_spans:
        before, after = _neighbour_times(conn, channel_id, lo, hi)
        if not rows:
            if before is not None:
                if after is not None:
                    add(before, after)
                elif hi >= end:
                    add(before, horizon, True)
            continue
        if before is not None:
            add(before, rows[0].t_first)
        for x, y in zip(rows, rows[1:], strict=False):
            add(x.t_last, y.t_first)
        for r in rows:
            if r.gap_s is not None and r.gap_start is not None:
                add(r.gap_start, r.gap_start + timedelta(seconds=r.gap_s))
        if after is not None:
            add(rows[-1].t_last, after)
        elif hi >= end and horizon > rows[-1].t_last:
            add(rows[-1].t_last, horizon, True)
    gaps = [
        {
            "start": format_utc(max(a, start)),
            "end": format_utc(min(b, end)),
            "duration_s": round((b - a).total_seconds(), 3),
            "open": o,
        }
        for a, b, o in sorted(gap_set)
        if b > start and a < end
    ]
    return RollupView(buckets, gaps, sum(r.n for r in allrows), len(dirty))


def combine(rows: list[MinuteRow], width_s: int) -> list[dict[str, Any]]:
    """Merge minute rows into ``width_s`` buckets (a multiple of 60, UTC-aligned to 2000-01-01).

    Counts and sums add; means are recomputed from valid counts (never averages of means);
    min/max keep their original timestamps with the earliest time winning ties.
    """
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for r in rows:
        offset = int((r.bucket - ORIGIN).total_seconds()) // width_s * width_s
        b = ORIGIN + timedelta(seconds=offset)
        if cur is None or cur["_b"] != b:
            if cur is not None:
                out.append(_finish(cur))
            cur = {
                "_b": b,
                "n": 0,
                "nv": 0,
                "nu": 0,
                "sum": None,
                "min": None,
                "tmin": None,
                "max": None,
                "tmax": None,
                "first": None,
                "last": None,
                "tf": r.t_first,
                "tl": r.t_last,
                "first_at": r.t_first,
                "last_at": r.t_last,
            }
        cur["n"] += r.n
        cur["nv"] += r.n_valid
        cur["nu"] += r.n_uncertain
        if r.v_sum is not None:
            cur["sum"] = (cur["sum"] or 0.0) + r.v_sum
        if r.v_min is not None and (
            cur["min"] is None
            or r.v_min < cur["min"]
            or (r.v_min == cur["min"] and r.t_min is not None and r.t_min < cur["tmin"])
        ):
            cur["min"], cur["tmin"] = r.v_min, r.t_min
        if r.v_max is not None and (
            cur["max"] is None
            or r.v_max > cur["max"]
            or (r.v_max == cur["max"] and r.t_max is not None and r.t_max < cur["tmax"])
        ):
            cur["max"], cur["tmax"] = r.v_max, r.t_max
        if r.v_first is not None and (cur["first"] is None or r.t_first < cur["first_at"]):
            cur["first"], cur["first_at"] = r.v_first, r.t_first
        if r.v_last is not None and (cur["last"] is None or r.t_last > cur["last_at"]):
            cur["last"], cur["last_at"] = r.v_last, r.t_last
        cur["tf"] = min(cur["tf"], r.t_first)
        cur["tl"] = max(cur["tl"], r.t_last)
    if cur is not None:
        out.append(_finish(cur))
    return out


def _finish(c: dict[str, Any]) -> dict[str, Any]:
    mean = None if not c["nv"] or c["sum"] is None else c["sum"] / c["nv"]
    return {
        "t": format_utc(c["_b"]),
        "count": c["n"],
        "valid_count": c["nv"],
        "uncertain_count": c["nu"],
        "bad_count": c["n"] - c["nv"],
        "sum": c["sum"],
        "mean": mean,
        "min": c["min"],
        "min_t": None if c["tmin"] is None else format_utc(c["tmin"]),
        "max": c["max"],
        "max_t": None if c["tmax"] is None else format_utc(c["tmax"]),
        "first": c["first"],
        "last": c["last"],
        "first_t": format_utc(c["tf"]),
        "last_t": format_utc(c["tl"]),
    }


def pending_count(conn: Connection) -> int:
    return int(conn.execute(text("SELECT count(*) FROM rollup_dirty")).scalar_one())


__all__ = [
    "MinuteRow",
    "RollupView",
    "combine",
    "mark_dirty",
    "pending_count",
    "process_dirty",
    "raw_minutes",
    "rollup_view",
]
