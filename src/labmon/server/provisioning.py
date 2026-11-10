"""Collector provisioning shared by the operator CLI (``admin``) and the web API.

Connection-level functions: the caller owns the transaction and writes the audit event with its
own actor (``cli:<user>`` or ``user:<name>``). Tokens are returned to the caller exactly once and
are stored only as digests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select, update

from labmon.server.bootstrap import InstrumentSeed, collector_config_text
from labmon.server.models import (
    Collector,
    CollectorAssignment,
    CollectorCredential,
    Instrument,
    ProfileRevision,
)
from labmon.server.security import issue_collector_credential


class ProvisioningError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def latest_revision(instrument_id: str) -> Any:
    return (
        select(ProfileRevision.id, ProfileRevision.revision, ProfileRevision.definition)
        .where(ProfileRevision.instrument_id == instrument_id)
        .order_by(ProfileRevision.revision.desc())
        .limit(1)
    )


def current_bindings(conn: Connection, collector_id: str) -> list[tuple[InstrumentSeed, str]]:
    """Instruments currently assigned to the collector, each with its latest profile revision."""
    rows = conn.execute(
        select(Instrument.id, Instrument.name, Instrument.profile_key)
        .join(CollectorAssignment, CollectorAssignment.instrument_id == Instrument.id)
        .where(
            CollectorAssignment.collector_id == collector_id,
            CollectorAssignment.valid_to.is_(None),
            Instrument.lifecycle != "retired",
        )
        .order_by(Instrument.id)
    ).all()
    out = []
    for iid, name, profile in rows:
        rev = conn.execute(latest_revision(iid)).one()
        seed = int((rev.definition or {}).get("seed", 0))
        out.append((InstrumentSeed(iid, name, profile, seed), str(rev.id)))
    return out


def config_text(conn: Connection, collector_id: str, server_url: str, generator: str) -> str:
    bindings = current_bindings(conn, collector_id)
    if not bindings:
        raise ProvisioningError(409, f"collector {collector_id!r} has no current assignments")
    return collector_config_text(collector_id, server_url, bindings, generator=generator)


def issue_credential(
    conn: Connection, collector_id: str, now: datetime, keep_existing: bool = False
) -> tuple[str, int]:
    """Issue a new credential; by default revoke the previous ones. Returns (token, revoked)."""
    row = conn.execute(select(Collector.revoked_at).where(Collector.id == collector_id)).first()
    if row is None:
        raise ProvisioningError(404, f"unknown collector {collector_id!r}")
    if row.revoked_at is not None:
        raise ProvisioningError(409, f"collector {collector_id!r} is revoked")
    revoked = 0
    if not keep_existing:
        revoked = conn.execute(
            update(CollectorCredential)
            .where(
                CollectorCredential.collector_id == collector_id,
                CollectorCredential.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        ).rowcount
    return issue_collector_credential(conn, collector_id), revoked


def revoke_collector(conn: Connection, collector_id: str, now: datetime) -> int:
    """Revoke every credential and the collector itself. Returns the number of credentials."""
    n = conn.execute(
        update(CollectorCredential)
        .where(
            CollectorCredential.collector_id == collector_id,
            CollectorCredential.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    ).rowcount
    conn.execute(update(Collector).where(Collector.id == collector_id).values(revoked_at=now))
    return int(n)
