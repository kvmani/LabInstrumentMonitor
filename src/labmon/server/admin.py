"""Administration for operators (``labmon-server admin ...``).

Runs on the central host with database access (no web endpoint), so it works before any user
exists and cannot be reached from browsers. Every change is audited with actor
``cli:<OS user>``. Passwords are read from a prompt or stdin, never from arguments; collector
tokens are written only to owner-only files.
"""

from __future__ import annotations

import getpass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, delete, insert, select, update

from labmon.profiles import PROFILES
from labmon.server.bootstrap import (
    InstrumentSeed,
    assign,
    ensure_collector,
    ensure_instrument,
    ensure_lab,
    write_collector_config,
    write_secret,
)
from labmon.server.models import (
    AppUser,
    Collector,
    CollectorAssignment,
    CollectorCredential,
    Instrument,
    Lab,
    LabGrant,
    ProfileRevision,
    UserSession,
)
from labmon.server.security import ROLES, audit, hash_password, issue_collector_credential


class AdminError(Exception):
    pass


def actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except OSError:  # pragma: no cover - no login name in some service contexts
        return "cli:unknown"


def _now() -> datetime:
    return datetime.now(UTC)


def _lab(conn: Connection, lab_id: str) -> None:
    if conn.execute(select(Lab.id).where(Lab.id == lab_id)).first() is None:
        raise AdminError(f"unknown lab {lab_id!r}")


def _user_id(conn: Connection, username: str) -> int:
    uid = conn.execute(select(AppUser.id).where(AppUser.username == username)).scalar()
    if uid is None:
        raise AdminError(f"unknown user {username!r}")
    return int(uid)


def add_lab(engine: Engine, lab_id: str, name: str) -> str:
    with engine.begin() as c:
        if c.execute(select(Lab.id).where(Lab.id == lab_id)).first():
            raise AdminError(f"lab {lab_id!r} exists")
        ensure_lab(c, lab_id, name)
        audit(c, _now(), "cli", actor(), "lab_add", lab_id, {"name": name})
    return lab_id


def add_user(engine: Engine, username: str, display_name: str, password: str) -> int:
    with engine.begin() as c:
        if c.execute(select(AppUser.id).where(AppUser.username == username)).first():
            raise AdminError(f"user {username!r} exists")
        uid = int(
            c.execute(
                insert(AppUser)
                .values(
                    username=username,
                    display_name=display_name,
                    password_hash=hash_password(password),
                )
                .returning(AppUser.id)
            ).scalar_one()
        )
        audit(c, _now(), "cli", actor(), "user_add", username, {"display_name": display_name})
    return uid


def set_password(engine: Engine, username: str, password: str) -> None:
    with engine.begin() as c:
        uid = _user_id(c, username)
        c.execute(
            update(AppUser).where(AppUser.id == uid).values(password_hash=hash_password(password))
        )
        c.execute(
            update(UserSession)
            .where(UserSession.user_id == uid, UserSession.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
        audit(c, _now(), "cli", actor(), "user_password_set", username, {"sessions_revoked": True})


def grant(engine: Engine, username: str, lab_id: str, role: str) -> None:
    if role not in ROLES:
        raise AdminError(f"role must be one of {', '.join(ROLES)}")
    with engine.begin() as c:
        uid = _user_id(c, username)
        _lab(c, lab_id)
        before = c.execute(
            select(LabGrant.role).where(LabGrant.user_id == uid, LabGrant.lab_id == lab_id)
        ).scalar()
        c.execute(delete(LabGrant).where(LabGrant.user_id == uid, LabGrant.lab_id == lab_id))
        c.execute(insert(LabGrant).values(user_id=uid, lab_id=lab_id, role=role))
        audit(
            c,
            _now(),
            "cli",
            actor(),
            "grant",
            username,
            {"lab": lab_id, "before": before, "after": role},
        )


def revoke_grant(engine: Engine, username: str, lab_id: str) -> None:
    with engine.begin() as c:
        uid = _user_id(c, username)
        n = c.execute(
            delete(LabGrant).where(LabGrant.user_id == uid, LabGrant.lab_id == lab_id)
        ).rowcount
        if not n:
            raise AdminError(f"{username!r} has no grant in {lab_id!r}")
        audit(c, _now(), "cli", actor(), "grant_revoke", username, {"lab": lab_id})


def disable_user(engine: Engine, username: str) -> None:
    now = _now()
    with engine.begin() as c:
        uid = _user_id(c, username)
        c.execute(update(AppUser).where(AppUser.id == uid).values(disabled_at=now))
        n = c.execute(
            update(UserSession)
            .where(UserSession.user_id == uid, UserSession.revoked_at.is_(None))
            .values(revoked_at=now)
        ).rowcount
        audit(c, now, "cli", actor(), "user_disable", username, {"sessions_revoked": n})


def list_users(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as c:
        users = c.execute(select(AppUser).order_by(AppUser.username)).all()
        grants = c.execute(select(LabGrant.user_id, LabGrant.lab_id, LabGrant.role)).all()
    by_user: dict[int, dict[str, str]] = {}
    for uid, lab, role in grants:
        by_user.setdefault(uid, {})[lab] = role
    return [
        {
            "username": u.username,
            "display_name": u.display_name,
            "disabled": u.disabled_at is not None,
            "grants": by_user.get(u.id, {}),
        }
        for u in users
    ]


def add_collector(engine: Engine, collector_id: str, lab_id: str, name: str) -> None:
    with engine.begin() as c:
        _lab(c, lab_id)
        if c.execute(select(Collector.id).where(Collector.id == collector_id)).first():
            raise AdminError(f"collector {collector_id!r} exists")
        ensure_collector(c, collector_id, lab_id, name)
        audit(c, _now(), "cli", actor(), "collector_add", collector_id, {"lab": lab_id})


def issue_token(engine: Engine, collector_id: str, out: Path, keep_existing: bool = False) -> None:
    """Issue a credential into an owner-only file; by default revoke the previous ones."""
    now = _now()
    with engine.begin() as c:
        if c.execute(select(Collector.id).where(Collector.id == collector_id)).first() is None:
            raise AdminError(f"unknown collector {collector_id!r}")
        revoked = 0
        if not keep_existing:
            revoked = c.execute(
                update(CollectorCredential)
                .where(
                    CollectorCredential.collector_id == collector_id,
                    CollectorCredential.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            ).rowcount
        token = issue_collector_credential(c, collector_id)
        write_secret(out, token)
        audit(
            c,
            now,
            "cli",
            actor(),
            "collector_token_issue",
            collector_id,
            {"previous_revoked": revoked, "file": str(out)},
        )


def revoke_collector(engine: Engine, collector_id: str) -> None:
    now = _now()
    with engine.begin() as c:
        n = c.execute(
            update(CollectorCredential)
            .where(
                CollectorCredential.collector_id == collector_id,
                CollectorCredential.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        ).rowcount
        c.execute(update(Collector).where(Collector.id == collector_id).values(revoked_at=now))
        audit(c, now, "cli", actor(), "collector_revoke", collector_id, {"credentials": n})


def enrol_simulated(
    engine: Engine,
    instrument_id: str,
    lab_id: str,
    name: str,
    profile: str,
    seed: int,
    collector_id: str,
    location: str | None = None,
    asset_id: str | None = None,
) -> str:
    """Enrol a *simulated* instrument (the only kind v1 supports) and assign its collector."""
    if profile not in PROFILES:
        raise AdminError(f"profile must be one of {', '.join(PROFILES)}")
    now = _now()
    with engine.begin() as c:
        _lab(c, lab_id)
        if c.execute(select(Instrument.id).where(Instrument.id == instrument_id)).first():
            raise AdminError(f"instrument {instrument_id!r} exists")
        if (
            c.execute(select(Collector.lab_id).where(Collector.id == collector_id)).scalar()
            != lab_id
        ):
            raise AdminError("the collector must exist and belong to the same lab")
        rev = ensure_instrument(
            c,
            lab_id,
            InstrumentSeed(
                instrument_id, name, profile, seed, asset_id=asset_id, location=location
            ),
            actor=actor(),
        )
        assign(c, collector_id, instrument_id, now, "enrolled by administrator")
        audit(
            c,
            now,
            "cli",
            actor(),
            "instrument_enrol",
            instrument_id,
            {
                "lab": lab_id,
                "profile": profile,
                "seed": seed,
                "collector": collector_id,
                "revision": rev,
                "simulated": True,
            },
        )
    return rev


def write_config(engine: Engine, collector_id: str, server_url: str, out_dir: Path) -> Path:
    """Write ``collector.toml`` for every instrument currently assigned to the collector."""
    with engine.connect() as c:
        rows = c.execute(
            select(Instrument.id, Instrument.name, Instrument.profile_key)
            .join(CollectorAssignment, CollectorAssignment.instrument_id == Instrument.id)
            .where(
                CollectorAssignment.collector_id == collector_id,
                CollectorAssignment.valid_to.is_(None),
            )
            .order_by(Instrument.id)
        ).all()
        seeds = []
        for iid, name, profile in rows:
            rev = c.execute(_latest_revision(iid)).one()
            seeds.append(
                (
                    InstrumentSeed(iid, name, profile, int((rev.definition or {}).get("seed", 0))),
                    rev.id,
                )
            )
    if not seeds:
        raise AdminError(f"collector {collector_id!r} has no current assignments")
    out_dir.mkdir(parents=True, exist_ok=True)
    return write_collector_config(out_dir, collector_id, server_url, seeds)


def _latest_revision(instrument_id: str) -> Any:
    return (
        select(ProfileRevision.id, ProfileRevision.definition)
        .where(ProfileRevision.instrument_id == instrument_id)
        .order_by(ProfileRevision.revision.desc())
        .limit(1)
    )


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        import sys

        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password (>= 12 characters): ")
    if first != getpass.getpass("Repeat password: "):
        raise AdminError("passwords do not match")
    return first


__all__ = [
    "AdminError",
    "add_collector",
    "add_lab",
    "add_user",
    "disable_user",
    "enrol_simulated",
    "grant",
    "issue_token",
    "list_users",
    "read_password",
    "revoke_collector",
    "revoke_grant",
    "set_password",
    "write_config",
]
