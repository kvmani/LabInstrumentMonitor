"""Commissioning preview (wizard step 4) and the quality rule shared with the collector.

``range_checked`` is the single implementation of the collector's range rule: a good float outside
the profile's valid range becomes bad/overrange. The collector applies it to every sample. The
preview applies it to the frames it shows, so the preview matches what the collector records.

``preview`` runs the same deterministic simulator the collector would run for a profile and seed,
over a short window of nominal time, and then a scripted dropout drill: the source stops answering
for part of the window and must come back. Nothing is spooled or stored. The result is plain JSON
for the wizard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from labmon.contracts.envelope import Quality, QualityReason, format_utc
from labmon.profiles import PROFILES
from labmon.profiles.base import ChannelSpec, DataType
from labmon.sim.base import Fault, FaultKind, SourceUnavailable
from labmon.sim.sources import build_source

PREVIEW_FRAMES = 120
SHOWN_FRAMES = 8


def range_checked(
    spec: ChannelSpec,
    value: float | int | bool | None,
    quality: Quality,
    reason: QualityReason | None,
) -> tuple[Quality, QualityReason | None]:
    if (
        quality is Quality.GOOD
        and isinstance(value, float)
        and spec.valid_min is not None
        and spec.valid_max is not None
        and not spec.valid_min <= value <= spec.valid_max
    ):
        return Quality.BAD, QualityReason.OVERRANGE
    return quality, reason


def _frames(
    profile_key: str, seed: int, start_ms: int, n: int, faults: tuple[Fault, ...]
) -> list[dict[str, Any]]:
    source = build_source(profile_key, seed, faults)
    profile = source.profile
    out: list[dict[str, Any]] = []
    t = start_ms
    for _ in range(n):
        frame: dict[str, Any] = {"t": format_utc_ms(t), "t_ms": t}
        try:
            reading = source.read(t)
        except SourceUnavailable as exc:
            frame["unavailable"] = str(exc)[:120]
        else:
            channels = {}
            for ch in reading.channels:
                spec = profile.channel(ch.key)
                quality, reason = range_checked(spec, ch.value, ch.quality, ch.quality_reason)
                channels[ch.key] = {
                    "value": ch.value,
                    "raw": ch.raw,
                    "quality": quality.value,
                    "reason": reason.value if reason else None,
                    "unit": spec.unit,
                }
            frame["channels"] = channels
            if reading.run is not None:
                frame["run"] = reading.run.name
        out.append(frame)
        t += source.cadence_ms(t)
    return out


def format_utc_ms(t_ms: int) -> str:
    return format_utc(datetime.fromtimestamp(t_ms / 1000, UTC))


def preview(profile_key: str, seed: int, start_ms: int) -> dict[str, Any]:
    if profile_key not in PROFILES:
        raise ValueError(f"unknown profile {profile_key!r}")
    profile = PROFILES[profile_key]
    nominal = _frames(profile_key, seed, start_ms, PREVIEW_FRAMES, ())
    # the drill window starts after the nominal one so the two never overlap
    drill_start = nominal[-1]["t_ms"] + 1000
    probe = _frames(profile_key, seed, drill_start, PREVIEW_FRAMES, ())
    fault_from = probe[PREVIEW_FRAMES // 3]["t_ms"] / 1000
    fault_to = probe[2 * PREVIEW_FRAMES // 3]["t_ms"] / 1000
    drill = _frames(
        profile_key,
        seed,
        drill_start,
        PREVIEW_FRAMES,
        (Fault(FaultKind.DROPOUT, fault_from, fault_to),),
    )

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    keys = {c.key for c in profile.channels}
    answered = [f for f in nominal if "channels" in f]
    check(
        "source answers",
        len(answered) == len(nominal),
        f"{len(answered)}/{len(nominal)} frames answered",
    )
    complete = all(set(f["channels"]) == keys for f in answered)
    check("channel map", complete, f"every frame carries {len(keys)} mapped channels")
    # readings the source itself flags (e.g. the dilatometer's scripted gauge error) are part of
    # the simulated scenario; a reading the range rule rejects means a wrong mapping or scale
    numeric = [c for c in profile.channels if c.data_type is DataType.FLOAT]
    readings = [f["channels"][c.key] for f in answered for c in numeric]
    overrange = sum(1 for r in readings if r["reason"] == QualityReason.OVERRANGE.value)
    flagged = sum(
        1
        for r in readings
        if r["quality"] != Quality.GOOD.value and r["reason"] != QualityReason.OVERRANGE.value
    )
    good = len(readings) - overrange - flagged
    check(
        "range and units",
        overrange == 0 and good > 0,
        f"{good} good, {flagged} flagged by the source, {overrange} outside the valid range "
        f"(of {len(readings)} numeric readings)",
    )
    lost = [f for f in drill if "unavailable" in f]
    recovered = all("channels" in f for f in drill if f["t_ms"] >= fault_to * 1000)
    check(
        "dropout drill",
        bool(lost) and recovered,
        f"{len(lost)} frames unavailable during the scripted dropout, "
        + ("readings resumed afterwards" if recovered else "readings did NOT resume"),
    )
    trend = {
        c.key: [[f["t"], f["channels"][c.key]["value"]] for f in answered]
        for c in profile.channels
        if c.data_type is DataType.FLOAT
    }
    return {
        "profile": profile_key,
        "seed": seed,
        "simulated": True,
        "window": {"start": nominal[0]["t"], "end": nominal[-1]["t"], "frames": len(nominal)},
        "channels": [
            {
                "key": c.key,
                "name": c.name,
                "unit": c.unit,
                "quantity": c.quantity,
                "data_type": c.data_type.value,
                "expected_cadence_s": c.expected_cadence_s,
                "valid_min": c.valid_min,
                "valid_max": c.valid_max,
                "role": c.role,
            }
            for c in profile.channels
        ],
        "frames": [{k: v for k, v in f.items() if k != "t_ms"} for f in nominal[-SHOWN_FRAMES:]],
        "trend": trend,
        "drill": {
            "fault": "dropout",
            "from": format_utc_ms(int(fault_from * 1000)),
            "to": format_utc_ms(int(fault_to * 1000)),
            "unavailable_frames": len(lost),
        },
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
    }
