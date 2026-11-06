"""Channel history: raw points (short windows), UTC-aligned buckets (long windows), explicit gaps.

* Ranges are half-open ``[start, end)`` and bounded.
* Raw mode returns every committed sample with quality and clock flags; bad samples carry no
  plotted value but stay visible as diagnostics.
* Bucket mode (M1: computed from raw rows; minute/hour rollups arrive in M2) returns count, valid
  count, sum, mean, min/max **with their original timestamps**, first/last, so a one-sample peak
  stays discoverable. A bucket is never labelled as a raw sample.
* Gaps are intervals with no samples longer than the channel's freshness window. They are computed
  from raw rows including the neighbours just outside the range, so a gap crossing a range edge is
  reported (clipped), and an *open* gap marks "no data since" up to ``min(end, now)``.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, desc, func, select, text

from labmon.contracts.envelope import format_utc
from labmon.server.models import AcquisitionEvent, Channel, Instrument, Sample

BUCKET_LADDER_S = (
    1,
    2,
    5,
    10,
    15,
    30,
    60,
    120,
    300,
    600,
    900,
    1800,
    3600,
    7200,
    10800,
    21600,
    43200,
    86400,
)
BUCKET_ORIGIN = datetime(2000, 1, 1, tzinfo=UTC)


class RangeError(ValueError):
    pass


@dataclass(frozen=True)
class SeriesLimits:
    max_range: timedelta = timedelta(days=31)
    raw_point_limit: int = 5_000
    max_buckets: int = 1_200


def parse_range(start: str, end: str, max_range: timedelta) -> tuple[datetime, datetime]:
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RangeError("start and end must be RFC 3339 timestamps") from exc
    if s.tzinfo is None or e.tzinfo is None:
        raise RangeError("start and end need an explicit UTC offset")
    s, e = s.astimezone(UTC), e.astimezone(UTC)
    if e <= s:
        raise RangeError("end must be after start")
    if e - s > max_range:
        raise RangeError(f"range exceeds {max_range.days} days; narrow the window")
    return s, e


def choose_bucket(span: timedelta, max_buckets: int) -> int:
    seconds = span.total_seconds()
    for w in BUCKET_LADDER_S:
        if math.ceil(seconds / w) <= max_buckets:
            return w
    return BUCKET_LADDER_S[-1]


def _value_expr(data_type: str) -> Any:
    return {"float": Sample.value_num, "enum": Sample.value_int}.get(data_type, Sample.value_bool)


def _channel_meta(ch: Any) -> dict[str, Any]:
    return {
        "id": ch.id,
        "key": ch.key,
        "name": ch.name,
        "quantity": ch.quantity,
        "unit": ch.unit,
        "data_type": ch.data_type,
        "role": ch.role,
        "display_precision": ch.display_precision,
        "enum_labels": ch.enum_labels,
        "display_format": ch.display_format,
        "note": ch.note,
        "expected_cadence_s": ch.expected_cadence_s,
        "gap_threshold_s": gap_threshold(ch.expected_cadence_s),
    }


def gap_threshold(cadence_s: float) -> float:
    return max(3.0 * cadence_s, 5.0)


def compute_gaps(
    conn: Connection, ch: Any, start: datetime, end: datetime, now: datetime
) -> list[dict[str, Any]]:
    threshold = gap_threshold(ch.expected_cadence_s)
    before = conn.execute(
        select(Sample.event_time)
        .where(Sample.channel_id == ch.id, Sample.event_time < start)
        .order_by(desc(Sample.event_time))
        .limit(1)
    ).scalar()
    after = conn.execute(
        select(Sample.event_time)
        .where(Sample.channel_id == ch.id, Sample.event_time >= end)
        .order_by(Sample.event_time)
        .limit(1)
    ).scalar()
    rows = conn.execute(
        text(
            """
        SELECT prev_t, t FROM (
          SELECT event_time AS t, lag(event_time) OVER (ORDER BY event_time, collector_id,
                 spool_epoch, seq) AS prev_t
          FROM sample WHERE channel_id = :cid AND event_time >= :s AND event_time < :e
        ) x WHERE prev_t IS NOT NULL AND t - prev_t > make_interval(secs => :thr)
        ORDER BY t
        """
        ),
        {"cid": ch.id, "s": start, "e": end, "thr": threshold},
    ).all()
    first_last = conn.execute(
        select(func.min(Sample.event_time), func.max(Sample.event_time)).where(
            Sample.channel_id == ch.id, Sample.event_time >= start, Sample.event_time < end
        )
    ).one()
    first, last = first_last
    gaps: list[dict[str, Any]] = []

    def add(a: datetime, b: datetime, open_: bool = False) -> None:
        if (b - a).total_seconds() > threshold:
            gaps.append(
                {
                    "start": format_utc(max(a, start)),
                    "end": format_utc(min(b, end)),
                    "duration_s": round((b - a).total_seconds(), 3),
                    "open": open_,
                }
            )

    horizon = min(end, now)
    if first is None:
        if before is not None:
            add(before, after if after is not None else horizon, open_=after is None)
        return gaps
    if before is not None:
        add(before, first)
    for prev_t, t in rows:
        add(prev_t, t)
    if after is not None:
        add(last, after)
    elif horizon > last:
        add(last, horizon, open_=True)
    return gaps


def channel_series(
    conn: Connection,
    channel_id: str,
    start: datetime,
    end: datetime,
    now: datetime,
    limits: SeriesLimits,
    max_points: int | None = None,
) -> dict[str, Any]:
    ch = conn.execute(select(Channel).where(Channel.id == channel_id)).one()
    inst = conn.execute(select(Instrument.simulated).where(Instrument.id == ch.instrument_id)).one()
    budget = min(max_points or limits.max_buckets, limits.max_buckets)
    count = conn.execute(
        select(func.count())
        .select_from(Sample)
        .where(Sample.channel_id == ch.id, Sample.event_time >= start, Sample.event_time < end)
    ).scalar_one()
    revision = conn.execute(
        select(func.max(Sample.receipt_id)).where(
            Sample.channel_id == ch.id, Sample.event_time >= start, Sample.event_time < end
        )
    ).scalar()
    out: dict[str, Any] = {
        "channel": _channel_meta(ch),
        "instrument_id": ch.instrument_id,
        "simulated": inst.simulated,
        "range": {"start": format_utc(start), "end": format_utc(end), "interval": "[start,end)"},
        "server_time": format_utc(now),
        "sample_count": int(count),
        "data_revision": revision,
        "gaps": compute_gaps(conn, ch, start, end, now),
        "events": _events(conn, ch.instrument_id, start, end),
    }
    # an explicit point budget (e.g. a 60-point sparkline) also caps raw mode
    raw_limit = (
        limits.raw_point_limit if max_points is None else min(limits.raw_point_limit, max_points)
    )
    if count <= raw_limit:
        out["mode"] = "raw"
        out["points"] = _raw_points(conn, ch, start, end)
    else:
        width = choose_bucket(end - start, budget)
        out["mode"] = "bucket"
        out["bucket_s"] = width
        out["buckets"] = _buckets(conn, ch, start, end, width)
    return out


def _raw_points(conn: Connection, ch: Any, start: datetime, end: datetime) -> list[list[Any]]:
    v = _value_expr(ch.data_type)
    rows = conn.execute(
        select(
            Sample.event_time,
            v,
            Sample.quality,
            Sample.quality_reason,
            Sample.clock_confidence,
            Sample.spool_epoch,
            Sample.seq,
        )
        .where(Sample.channel_id == ch.id, Sample.event_time >= start, Sample.event_time < end)
        .order_by(Sample.event_time, Sample.collector_id, Sample.spool_epoch, Sample.seq)
    ).all()
    return [
        [format_utc(r[0]), None if r[2] == "bad" else r[1], r[2], r[3], r[4], f"{r[5]}:{r[6]}"]
        for r in rows
    ]


def _buckets(
    conn: Connection, ch: Any, start: datetime, end: datetime, width_s: int
) -> list[dict[str, Any]]:
    col = {"float": "value_num", "enum": "value_int"}.get(ch.data_type, "value_bool::int")
    rows = conn.execute(
        text(
            f"""
        SELECT date_bin(make_interval(secs => :w), event_time, :origin) AS b,
          count(*) AS n,
          count(*) FILTER (WHERE quality <> 'bad') AS nv,
          sum({col}) FILTER (WHERE quality <> 'bad') AS total,
          min({col}) FILTER (WHERE quality <> 'bad') AS vmin,
          max({col}) FILTER (WHERE quality <> 'bad') AS vmax,
          (array_agg(event_time ORDER BY {col} ASC, event_time, seq)
             FILTER (WHERE quality <> 'bad'))[1] AS tmin,
          (array_agg(event_time ORDER BY {col} DESC, event_time, seq)
             FILTER (WHERE quality <> 'bad'))[1] AS tmax,
          (array_agg({col} ORDER BY event_time, seq) FILTER (WHERE quality <> 'bad'))[1] AS vfirst,
          (array_agg({col} ORDER BY event_time DESC, seq DESC)
             FILTER (WHERE quality <> 'bad'))[1] AS vlast,
          min(event_time) AS tfirst, max(event_time) AS tlast,
          count(*) FILTER (WHERE quality = 'uncertain') AS nu
        FROM sample
        WHERE channel_id = :cid AND event_time >= :s AND event_time < :e
        GROUP BY b ORDER BY b
        """  # noqa: S608 - col is chosen from a fixed mapping, never user input
        ),
        {"w": width_s, "origin": BUCKET_ORIGIN, "cid": ch.id, "s": start, "e": end},
    ).all()
    out = []
    for r in rows:
        mean = None if not r.nv or r.total is None else float(r.total) / r.nv
        out.append(
            {
                "t": format_utc(r.b),
                "count": r.n,
                "valid_count": r.nv,
                "uncertain_count": r.nu,
                "bad_count": r.n - r.nv,
                "sum": None if r.total is None else float(r.total),
                "mean": mean,
                "min": r.vmin,
                "min_t": None if r.tmin is None else format_utc(r.tmin),
                "max": r.vmax,
                "max_t": None if r.tmax is None else format_utc(r.tmax),
                "first": r.vfirst,
                "last": r.vlast,
                "first_t": format_utc(r.tfirst),
                "last_t": format_utc(r.tlast),
            }
        )
    return out


def _events(
    conn: Connection, instrument_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(AcquisitionEvent.event_type, AcquisitionEvent.capture_time, AcquisitionEvent.detail)
        .where(
            AcquisitionEvent.instrument_id == instrument_id,
            AcquisitionEvent.capture_time >= start,
            AcquisitionEvent.capture_time < end,
        )
        .order_by(AcquisitionEvent.capture_time, AcquisitionEvent.seq)
        .limit(200)
    ).all()
    return [{"type": r[0], "at": format_utc(r[1]), "detail": r[2]} for r in rows]


# --- CSV ----------------------------------------------------------------------------------------

CSV_COLUMNS = [
    "event_time_utc",
    "event_time_source",
    "instrument_id",
    "channel_id",
    "channel_key",
    "quantity",
    "unit",
    "value",
    "value_label",
    "quality",
    "quality_reason",
    "clock_confidence",
    "clock_flag",
    "capture_time_utc",
    "source_time_utc",
    "source_time_meaning",
    "receive_time_utc",
    "raw",
    "source_status",
    "collector_id",
    "spool_epoch",
    "seq",
    "frame_id",
    "profile_revision_id",
    "simulated",
]
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: str | None) -> str:
    """Neutralise spreadsheet formula injection in free-text fields."""
    if value is None:
        return ""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def count_rows(conn: Connection, instrument_id: str, start: datetime, end: datetime) -> int:
    return int(
        conn.execute(
            select(func.count())
            .select_from(Sample)
            .where(
                Sample.instrument_id == instrument_id,
                Sample.event_time >= start,
                Sample.event_time < end,
            )
        ).scalar_one()
    )


def csv_rows(engine: Engine, instrument_id: str, start: datetime, end: datetime) -> Iterator[str]:
    """Stream raw rows in a single REPEATABLE READ snapshot, ordered by time then identity."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    yield buf.getvalue()
    with engine.connect().execution_options(
        isolation_level="REPEATABLE READ",
        postgresql_readonly=True,
        stream_results=True,
        yield_per=2000,
    ) as conn:
        channels = {
            c.id: c
            for c in conn.execute(select(Channel).where(Channel.instrument_id == instrument_id))
        }
        result = conn.execute(
            select(Sample)
            .where(
                Sample.instrument_id == instrument_id,
                Sample.event_time >= start,
                Sample.event_time < end,
            )
            .order_by(
                Sample.event_time,
                Sample.channel_id,
                Sample.collector_id,
                Sample.spool_epoch,
                Sample.seq,
            )
        )
        for part in result.partitions():
            buf.seek(0)
            buf.truncate()
            for row in part:
                s = row
                ch = channels[s.channel_id]
                value: Any = {"float": s.value_num, "enum": s.value_int}.get(
                    ch.data_type, s.value_bool
                )
                label = (
                    (ch.enum_labels or {}).get(str(value), "")
                    if ch.data_type == "enum" and value is not None
                    else ""
                )
                writer.writerow(
                    [
                        format_utc(s.event_time),
                        s.event_time_source,
                        s.instrument_id,
                        s.channel_id,
                        ch.key,
                        ch.quantity,
                        ch.unit,
                        "" if value is None else value,
                        safe_text(label),
                        s.quality,
                        s.quality_reason or "",
                        s.clock_confidence,
                        s.clock_flag or "",
                        format_utc(s.capture_time),
                        "" if s.source_time is None else format_utc(s.source_time),
                        s.source_time_meaning,
                        format_utc(s.receive_time),
                        safe_text(s.raw),
                        safe_text(s.source_status),
                        s.collector_id,
                        s.spool_epoch,
                        s.seq,
                        safe_text(s.frame_id),
                        s.profile_revision_id,
                        "true" if s.simulated else "false",
                    ]
                )
            yield buf.getvalue()
