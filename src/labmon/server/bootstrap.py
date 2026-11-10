"""Development-only bootstrap: one lab, the simulated furnace, one collector and one scientist.

Refuses to run unless ``LABMON_ENV`` is ``development`` or ``test``. Writes the collector
configuration and a 0600 token file into ``--out``. Idempotent: existing rows are kept; the
collector credential is re-issued only with ``--rotate-token``.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Connection, Engine, insert, select, update

from labmon.profiles import PROFILES
from labmon.profiles.base import ProfileSpec
from labmon.server.models import (
    AppUser,
    Channel,
    Collector,
    CollectorAssignment,
    CollectorCredential,
    Instrument,
    Lab,
    LabGrant,
    ProfileRevision,
)
from labmon.server.security import audit, hash_password, issue_collector_credential


@dataclass(frozen=True)
class InstrumentSeed:
    instrument_id: str
    name: str
    profile_key: str
    seed: int
    asset_id: str | None = None
    location: str | None = None


DEMO_LAB = ("lab-demo", "Demonstration lab (simulated)")
FURNACE_SEED = InstrumentSeed(
    "furnace-01",
    "Heat-treatment furnace F-01",
    "sim-furnace",
    42,
    asset_id="SIM-F01",
    location="Bay 1 (virtual)",
)
UTM_SEED = InstrumentSeed(
    "utm-01",
    "Universal testing machine U-01",
    "sim-utm",
    11,
    asset_id="SIM-U01",
    location="Bay 2 (virtual)",
)
DILATOMETER_SEED = InstrumentSeed(
    "dil-01",
    "Vacuum dilatometer D-01",
    "sim-dilatometer",
    23,
    asset_id="SIM-D01",
    location="Bay 3 (virtual)",
)
DSC_SEED = InstrumentSeed(
    "dsc-01",
    "Differential scanning calorimeter C-01",
    "sim-dsc",
    31,
    asset_id="SIM-C01",
    location="Bay 3 (virtual)",
)
DEFAULT_FLEET_SEEDS = (FURNACE_SEED, UTM_SEED, DILATOMETER_SEED)
AMBIENT_SEED = InstrumentSeed(
    "ambient-01",
    "Ambient logger A-01",
    "sim-ambient",
    7,
    asset_id="SIM-A01",
    location="Bay 1 (virtual)",
)


def ensure_lab(conn: Connection, lab_id: str, name: str) -> None:
    if conn.execute(select(Lab.id).where(Lab.id == lab_id)).first() is None:
        conn.execute(insert(Lab).values(id=lab_id, name=name))


def ensure_instrument(
    conn: Connection, lab_id: str, seed: InstrumentSeed, actor: str, simulated: bool = True
) -> str:
    """Create the instrument, its channels and revision 1 of its profile. Returns revision id."""
    profile: ProfileSpec = PROFILES[seed.profile_key]
    rev_id = f"{seed.instrument_id}-r1"
    if conn.execute(select(Instrument.id).where(Instrument.id == seed.instrument_id)).first():
        return rev_id
    conn.execute(
        insert(Instrument).values(
            id=seed.instrument_id,
            lab_id=lab_id,
            name=seed.name,
            asset_id=seed.asset_id,
            location=seed.location,
            lifecycle="active",
            simulated=simulated,
            profile_key=profile.key,
        )
    )
    conn.execute(
        insert(ProfileRevision).values(
            id=rev_id,
            instrument_id=seed.instrument_id,
            revision=1,
            profile_key=profile.key,
            adapter=profile.adapter,
            adapter_version=profile.adapter_version,
            definition={**profile.model_dump(mode="json"), "seed": seed.seed},
            definition_digest=profile.definition_digest(),
            created_by=actor,
        )
    )
    for i, ch in enumerate(profile.channels):
        conn.execute(
            insert(Channel).values(
                id=f"{seed.instrument_id}.{ch.key}",
                instrument_id=seed.instrument_id,
                key=ch.key,
                name=ch.name,
                quantity=ch.quantity,
                unit=ch.unit,
                data_type=ch.data_type.value,
                role=ch.role,
                expected_cadence_s=ch.expected_cadence_s,
                display_precision=ch.display_precision,
                enum_labels=ch.enum_labels,
                display_format=ch.display,
                note=ch.note,
                sort_order=i,
            )
        )
    return rev_id


def ensure_collector(conn: Connection, collector_id: str, lab_id: str, name: str) -> None:
    if conn.execute(select(Collector.id).where(Collector.id == collector_id)).first() is None:
        conn.execute(insert(Collector).values(id=collector_id, lab_id=lab_id, name=name))


def assign(
    conn: Connection, collector_id: str, instrument_id: str, valid_from: datetime, reason: str
) -> None:
    exists = conn.execute(
        select(CollectorAssignment.id).where(
            CollectorAssignment.collector_id == collector_id,
            CollectorAssignment.instrument_id == instrument_id,
            CollectorAssignment.valid_to.is_(None),
        )
    ).first()
    if exists is None:
        conn.execute(
            insert(CollectorAssignment).values(
                collector_id=collector_id,
                instrument_id=instrument_id,
                valid_from=valid_from,
                reason=reason,
            )
        )


def ensure_user(
    conn: Connection, username: str, display: str, password: str, grants: dict[str, str]
) -> int:
    row = conn.execute(select(AppUser.id).where(AppUser.username == username)).first()
    if row is None:
        uid = int(
            conn.execute(
                insert(AppUser)
                .values(
                    username=username, display_name=display, password_hash=hash_password(password)
                )
                .returning(AppUser.id)
            ).scalar_one()
        )
    else:
        uid = int(row[0])
        conn.execute(
            update(AppUser).where(AppUser.id == uid).values(password_hash=hash_password(password))
        )
    for lab_id, role in grants.items():
        if (
            conn.execute(
                select(LabGrant.user_id).where(LabGrant.user_id == uid, LabGrant.lab_id == lab_id)
            ).first()
            is None
        ):
            conn.execute(insert(LabGrant).values(user_id=uid, lab_id=lab_id, role=role))
    return uid


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def collector_config_text(
    collector_id: str,
    server_url: str,
    instruments: list[tuple[InstrumentSeed, str]],
    spool_name: str = "spool.sqlite3",
    generator: str = "`labmon-server bootstrap-dev` (development only)",
) -> str:
    """``collector.toml`` for the given instruments. Never contains the credential."""
    lines = [
        f"# Generated by {generator}.",
        f"collector_id = {_toml_str(collector_id)}",
        f"server_url = {_toml_str(server_url)}",
        'token_file = "collector.token"',
        f"spool_path = {_toml_str(spool_name)}",
        f"allow_insecure_http = {'true' if server_url.startswith('http://') else 'false'}",
        "heartbeat_s = 10.0",
        "",
    ]
    for seed, rev_id in instruments:
        profile = PROFILES[seed.profile_key]
        lines += [
            "[[instruments]]",
            f"instrument_id = {_toml_str(seed.instrument_id)}",
            f"profile = {_toml_str(profile.key)}",
            f"profile_revision_id = {_toml_str(rev_id)}",
            f"seed = {seed.seed}",
            "[instruments.channels]",
            *[f"{c.key} = {_toml_str(f'{seed.instrument_id}.{c.key}')}" for c in profile.channels],
            "",
        ]
    return "\n".join(lines)


def write_collector_config(
    out: Path,
    collector_id: str,
    server_url: str,
    instruments: list[tuple[InstrumentSeed, str]],
    spool_name: str = "spool.sqlite3",
    generator: str = "`labmon-server bootstrap-dev` (development only)",
) -> Path:
    path = out / "collector.toml"
    text = collector_config_text(collector_id, server_url, instruments, spool_name, generator)
    path.write_text(text, encoding="utf-8")
    return path


def write_secret(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    os.chmod(path, 0o600)


def bootstrap_dev(
    engine: Engine,
    out: Path,
    server_url: str,
    password: str | None,
    with_ambient: bool = False,
    rotate_token: bool = False,
    collector_id: str = "col-bay1-01",
    with_dsc: bool = False,
) -> dict[str, str]:
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    password = password or secrets.token_urlsafe(12)
    seeds = [*DEFAULT_FLEET_SEEDS]
    seeds += [DSC_SEED] if with_dsc else []
    seeds += [AMBIENT_SEED] if with_ambient else []
    token_path = out / "collector.token"
    with engine.begin() as conn:
        ensure_lab(conn, *DEMO_LAB)
        ensure_collector(conn, collector_id, DEMO_LAB[0], "Bay 1 simulator collector")
        revs = []
        for s in seeds:
            rev = ensure_instrument(conn, DEMO_LAB[0], s, actor="bootstrap-dev")
            # dev assignments start 31 days back so rehearsal playback (backfill) is accepted
            assign(
                conn,
                collector_id,
                s.instrument_id,
                now - timedelta(days=31),
                "development bootstrap",
            )
            revs.append((s, rev))
        has_cred = (
            conn.execute(
                select(CollectorCredential.id).where(
                    CollectorCredential.collector_id == collector_id,
                    CollectorCredential.revoked_at.is_(None),
                )
            ).first()
            is not None
        )
        if rotate_token or not has_cred or not token_path.exists():
            conn.execute(
                update(CollectorCredential)
                .where(
                    CollectorCredential.collector_id == collector_id,
                    CollectorCredential.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            write_secret(token_path, issue_collector_credential(conn, collector_id))
        ensure_user(conn, "scientist", "Demo Scientist", password, {DEMO_LAB[0]: "scientist"})
        audit(
            conn,
            now,
            "system",
            "bootstrap-dev",
            "bootstrap_dev",
            DEMO_LAB[0],
            {"instruments": [s.instrument_id for s in seeds]},
        )
    cfg = write_collector_config(out, collector_id, server_url, revs)
    return {"collector_config": str(cfg), "username": "scientist", "password": password}
