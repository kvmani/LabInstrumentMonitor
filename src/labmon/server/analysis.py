"""Run-specific XY analyses from raw values paired by acquisition frame.

Pairing uses the stable ``frame_id`` the collector assigns to all channels read together, never
time interpolation. Acquisition order is preserved (unloading, fracture and cooling are not
re-sorted). An invalid or missing pair breaks the trace (``None`` entry) instead of being bridged.

* **Tensile:** engineering stress = force / A0 (kN → N, mm² → MPa); engineering strain =
  extension / L0 (dimensionless). UTS is reported only for a complete run (ended, fracture
  observed) with the required geometry.
* **DSC:** heat flow (mW, sign convention from the profile) versus measured sample temperature,
  segmented into heating / cooling / isothermal from the programmed-temperature slope. Peak
  areas are integrated against a straight baseline drawn between the window edges and are
  labelled as estimates.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, select

from labmon.contracts.envelope import format_utc
from labmon.server.models import Sample

MAX_FRAMES = 60_000


class TooLarge(Exception):
    pass


@dataclass(frozen=True)
class Frame:
    t: datetime
    values: dict[str, tuple[float | None, str]]  # key -> (value, quality)

    def good(self, key: str) -> float | None:
        v = self.values.get(key)
        if v is None or v[0] is None or v[1] == "bad":
            return None
        return float(v[0])


def load_frames(
    conn: Connection,
    instrument_id: str,
    channel_ids: dict[str, str],
    start: datetime,
    end: datetime,
    max_frames: int = MAX_FRAMES,
) -> list[Frame]:
    by_id = {cid: key for key, cid in channel_ids.items()}
    where = (
        Sample.instrument_id == instrument_id,
        Sample.channel_id.in_(list(by_id)),
        Sample.event_time >= start,
        Sample.event_time < end,
    )
    n = conn.execute(select(func.count()).select_from(Sample).where(*where)).scalar_one()
    if n > max_frames * len(by_id):
        raise TooLarge(f"{n} samples exceed the XY view limit; narrow the run window")
    rows = conn.execute(
        select(
            Sample.frame_id,
            Sample.channel_id,
            Sample.value_num,
            Sample.value_int,
            Sample.quality,
            Sample.event_time,
        )
        .where(*where)
        .order_by(Sample.event_time, Sample.collector_id, Sample.spool_epoch, Sample.seq)
    ).all()
    frames: OrderedDict[str, Frame] = OrderedDict()
    for fid, cid, vnum, vint, quality, t in rows:
        key = fid or f"{t.isoformat()}"
        fr = frames.get(key)
        if fr is None:
            fr = Frame(t=t, values={})
            frames[key] = fr
        fr.values[by_id[cid]] = (vnum if vnum is not None else vint, quality)
    return list(frames.values())


# --- tensile --------------------------------------------------------------------------------


def tensile(frames: list[Frame], geometry: dict[str, Any] | None, ended: bool) -> dict[str, Any]:
    reasons: list[str] = []
    area = (geometry or {}).get("area_mm2")
    gauge = (geometry or {}).get("gauge_length_mm")
    if not isinstance(area, int | float) or area <= 0:
        reasons.append("cross-sectional area missing")
        area = None
    if not isinstance(gauge, int | float) or gauge <= 0:
        reasons.append("gauge length missing")
        gauge = None
    points: list[list[Any] | None] = []
    invalid = 0
    uts: tuple[float, float, datetime] | None = None
    max_force: float | None = None
    fracture_t: datetime | None = None
    last_strain: float | None = None
    for fr in frames:
        state = fr.values.get("state", (None, ""))[0]
        if state == 3 and fracture_t is None and last_strain is not None:
            fracture_t = fr.t  # first fractured frame: show the load drop, then stop
        if state not in (1, 2) and not (state == 3 and fracture_t == fr.t):
            continue
        force, ext = fr.good("force"), fr.good("extension")
        if force is None or ext is None or area is None or gauge is None:
            invalid += 1
            if points and points[-1] is not None:
                points.append(None)
            continue
        stress = force * 1000.0 / area
        strain = ext / gauge
        points.append([round(strain, 6), round(stress, 3), format_utc(fr.t), int(state)])
        if state in (1, 2):
            last_strain = strain
            max_force = force if max_force is None else max(max_force, force)
            if uts is None or stress > uts[0]:
                uts = (stress, strain, fr.t)
    modulus, rp02 = _elastic_and_offset_yield(
        [p for p in points if p is not None and p[3] in (1, 2)], uts[0] if uts else None
    )
    if fracture_t is None:
        reasons.append("fracture not observed")
    if not ended:
        reasons.append("run not complete")
    complete = not reasons
    summary: dict[str, Any] = {
        "complete": complete,
        "reasons": reasons,
        "invalid_pairs": invalid,
        "max_force_kn": None if max_force is None else round(max_force, 4),
        "fracture_time": None if fracture_t is None else format_utc(fracture_t),
        "fracture_strain": None
        if fracture_t is None or last_strain is None
        else round(last_strain, 5),
        "uts_mpa": None,
        "uts_strain": None,
        "uts_time": None,
        "fracture_after_uts": None,
        "modulus_gpa": None
        if modulus is None or "gpa" not in modulus
        else round(modulus["gpa"], 1),
        "modulus_fit": modulus,
        "rp02_mpa": None,
        "rp02_strain": None,
    }
    if complete and rp02 is not None:
        summary.update(rp02_mpa=round(rp02[0], 1), rp02_strain=round(rp02[1], 5))
    if complete and uts is not None:
        summary.update(
            uts_mpa=round(uts[0], 2),
            uts_strain=round(uts[1], 5),
            uts_time=format_utc(uts[2]),
            fracture_after_uts=fracture_t is not None and fracture_t > uts[2],
        )
    return {
        "kind": "tensile",
        "x": {"label": "Engineering strain", "unit": "", "key": "strain"},
        "y": {"label": "Engineering stress", "unit": "MPa", "key": "stress"},
        "point_fields": ["x", "y", "time", "state"],
        "points": points,
        "summary": summary,
        "geometry": geometry,
    }


def _elastic_and_offset_yield(
    pts: list[list[Any]], uts: float | None
) -> tuple[dict[str, Any] | None, tuple[float, float] | None]:
    """Young's modulus by least squares on the elastic window (10–50 % of the maximum stress,
    before the maximum) and the 0.2 % offset yield where the curve first falls below the offset
    line ``stress = E · (strain − strain0 − 0.002)`` (linear interpolation between points)."""
    if uts is None or uts <= 0:
        return None, None
    window: list[tuple[float, float]] = []
    for x, y, *_ in pts:
        if y >= 0.5 * uts:
            break
        if y >= 0.1 * uts:
            window.append((x, y))
    if len(window) < 5:
        return {"points": len(window), "note": "too few elastic points for a modulus fit"}, None
    n = len(window)
    mx = sum(x for x, _ in window) / n
    my = sum(y for _, y in window) / n
    sxx = sum((x - mx) ** 2 for x, _ in window)
    if sxx <= 0:
        return None, None
    e = sum((x - mx) * (y - my) for x, y in window) / sxx  # MPa per unit strain
    b = my - e * mx
    resid = sum((y - (e * x + b)) ** 2 for x, y in window) / max(n - 2, 1)
    fit = {
        "gpa": e / 1000.0,
        "points": n,
        "intercept_mpa": round(b, 3),
        "rmse_mpa": round(resid**0.5, 3),
        "window": "10–50 % of maximum stress, before the maximum",
    }
    strain0 = -b / e

    def gap(x: float, y: float) -> float:  # curve minus offset line
        return y - e * (x - strain0 - 0.002)

    prev: tuple[float, float] | None = None
    for x, y, *_ in pts:
        if x <= strain0 + 0.002:
            prev = (x, y)
            continue
        if prev is not None and gap(*prev) > 0 >= gap(x, y):
            g0, g1 = gap(*prev), gap(x, y)
            f = g0 / (g0 - g1)
            return fit, (prev[1] + f * (y - prev[1]), prev[0] + f * (x - prev[0]))
        prev = (x, y)
    return fit, None


# --- DSC ------------------------------------------------------------------------------------

SEGMENT_RATE_K_PER_MIN = 1.0


def _segments(frames: list[tuple[datetime, float, float, float]]) -> list[str]:
    out = []
    n = len(frames)
    for i in range(n):
        a, b = frames[max(0, i - 5)], frames[min(n - 1, i + 5)]
        dt = (b[0] - a[0]).total_seconds()
        rate = 0.0 if dt <= 0 else (b[2] - a[2]) / dt * 60.0
        out.append(
            "heating"
            if rate > SEGMENT_RATE_K_PER_MIN
            else "cooling"
            if rate < -SEGMENT_RATE_K_PER_MIN
            else "isothermal"
        )
    return out


def _peak(
    rows: list[tuple[datetime, float, float, float]],
    want_min: bool,
    before_k: float,
    after_k: float,
    heating: bool,
) -> dict[str, Any] | None:
    if len(rows) < 10:
        return None
    pick = min if want_min else max
    i_pk = rows.index(pick(rows, key=lambda r: r[3]))
    t_pk = rows[i_pk][1]
    sign = 1 if heating else -1  # temperature increases along a heating segment

    def inside(r: tuple[datetime, float, float, float]) -> bool:
        d = (r[1] - t_pk) * sign
        return -before_k <= d <= after_k

    window = [r for r in rows if inside(r)]
    if len(window) < 5:
        return None
    a, b = window[0], window[-1]
    span = (b[0] - a[0]).total_seconds() or 1.0
    area = 0.0
    for r0, r1 in zip(window, window[1:], strict=False):
        dt = (r1[0] - r0[0]).total_seconds()
        base0 = a[3] + (b[3] - a[3]) * (r0[0] - a[0]).total_seconds() / span
        base1 = a[3] + (b[3] - a[3]) * (r1[0] - a[0]).total_seconds() / span
        area += 0.5 * ((r0[3] - base0) + (r1[3] - base1)) * dt  # mW·s = mJ
    pk = rows[i_pk]
    onset = _onset(rows, i_pk)
    return {
        "onset_c": None if onset is None else round(onset, 2),
        "onset_method": "extrapolated: tangent at the steepest leading-edge point intersected "
        "with a straight baseline fitted 12–4 K before the peak",
        "peak_mw": round(pk[3], 3),
        "sample_t_c": round(pk[1], 2),
        "time": format_utc(pk[0]),
        "area_mj": round(area, 1),
        "area_method": f"linear baseline from {before_k:g} K before to {after_k:g} K after "
        "the peak (estimate)",
    }


def _line(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    n = len(points)
    if n < 2:
        return None
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx <= 0:
        return None
    m = sum((x - mx) * (y - my) for x, y in points) / sxx
    return m, my - m * mx


def _onset(rows: list[tuple[datetime, float, float, float]], i_pk: int) -> float | None:
    """Extrapolated onset temperature (sample temperature axis) of the peak at ``i_pk``."""
    t_pk = rows[i_pk][1]
    before = rows[: i_pk + 1]
    base = [(r[1], r[3]) for r in before if 4.0 <= abs(r[1] - t_pk) <= 12.0]
    edge = [r for r in before if abs(r[1] - t_pk) < 4.0]
    fit = _line(base)
    if fit is None or len(edge) < 2:
        return None
    best: tuple[float, float, float] | None = None  # (slope, x, y) of the steepest segment
    for a, b in zip(edge, edge[1:], strict=False):
        dx = b[1] - a[1]
        if dx == 0:
            continue
        slope = (b[3] - a[3]) / dx
        if best is None or abs(slope) > abs(best[0]):
            best = (slope, (a[1] + b[1]) / 2, (a[3] + b[3]) / 2)
    if best is None:
        return None
    m_t, x0, y0 = best
    m_b, c_b = fit
    if m_t == m_b:
        return None
    # tangent: y = y0 + m_t (x - x0); baseline: y = m_b x + c_b
    return (c_b - y0 + m_t * x0) / (m_t - m_b)


def dsc(
    frames: list[Frame], sign_convention: str | None, sample_mass_mg: float | None
) -> dict[str, Any]:
    rows: list[tuple[datetime, float, float, float]] = []
    points: list[list[Any] | None] = []
    invalid = 0
    for fr in frames:
        ts, tp, hf = fr.good("sample_t"), fr.good("program_t"), fr.good("heat_flow")
        if ts is None or tp is None or hf is None:
            invalid += 1
            if points and points[-1] is not None:
                points.append(None)
            continue
        rows.append((fr.t, ts, tp, hf))
        points.append([round(ts, 3), round(hf, 4), format_utc(fr.t), ""])
    segs = _segments(rows)
    it = iter(segs)
    for p in points:
        if p is not None:
            p[3] = next(it)
    heating = [r for r, s in zip(rows, segs, strict=True) if s == "heating"]
    cooling = [r for r, s in zip(rows, segs, strict=True) if s == "cooling"]
    exo_up = (sign_convention or "").startswith("exo up")
    endo = _peak(heating, want_min=exo_up, before_k=12.0, after_k=8.0, heating=True)
    exo = _peak(cooling, want_min=not exo_up, before_k=12.0, after_k=8.0, heating=False)
    for pk in (endo, exo):
        if pk is not None and sample_mass_mg:
            pk["specific_j_per_g"] = round(pk["area_mj"] / sample_mass_mg, 2)
    return {
        "kind": "dsc",
        "x": {"label": "Sample temperature", "unit": "°C", "key": "sample_t"},
        "y": {"label": "Heat flow", "unit": "mW", "key": "heat_flow"},
        "sign_convention": sign_convention,
        "point_fields": ["x", "y", "time", "segment"],
        "points": points,
        "summary": {
            "invalid_pairs": invalid,
            "endotherm": endo,
            "exotherm": exo,
            "segments": {s: segs.count(s) for s in ("heating", "cooling", "isothermal")},
        },
    }
