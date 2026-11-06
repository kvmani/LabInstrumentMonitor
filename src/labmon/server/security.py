"""Credentials: collector bearer tokens, user passwords (Argon2id) and opaque sessions.

High-entropy random tokens are stored as SHA-256 digests (constant-time lookup by digest);
passwords use Argon2id via argon2-cffi. Tokens never appear in URLs or logs.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import Connection, and_, func, insert, or_, select, update

from labmon.server.models import (
    AppUser,
    AuditEvent,
    Collector,
    CollectorCredential,
    LabGrant,
    UserSession,
)

COLLECTOR_TOKEN_PREFIX = "lmc_"  # noqa: S105 - a public prefix, not a secret
SESSION_COOKIE = "labmon_session"
CSRF_HEADER = "X-Labmon-Request"
# Role matrix (SPECIFICATIONS §10). Viewer and auditor are read-only.
ROLES = ("viewer", "scientist", "custodian", "administrator", "auditor")
RECORD_ROLES = frozenset({"scientist", "custodian", "administrator"})

_hasher = PasswordHasher()  # Argon2id, library-maintained defaults
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-password")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_collector_token() -> str:
    return COLLECTOR_TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY_HASH, password) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def audit(
    conn: Connection,
    at: datetime,
    actor_type: str,
    actor_id: str | None,
    action: str,
    target: str | None = None,
    detail: dict[str, object] | None = None,
) -> None:
    conn.execute(
        insert(AuditEvent).values(
            at=at,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target=target,
            detail=detail,
        )
    )


def authenticate_collector(conn: Connection, token: str, now: datetime) -> str | None:
    row = conn.execute(
        select(CollectorCredential.collector_id)
        .join(Collector, Collector.id == CollectorCredential.collector_id)
        .where(
            CollectorCredential.token_hash == token_digest(token),
            CollectorCredential.revoked_at.is_(None),
            Collector.revoked_at.is_(None),
            or_(CollectorCredential.expires_at.is_(None), CollectorCredential.expires_at > now),
        )
    ).first()
    return None if row is None else str(row[0])


def issue_collector_credential(
    conn: Connection, collector_id: str, expires_at: datetime | None = None
) -> str:
    token = new_collector_token()
    conn.execute(
        insert(CollectorCredential).values(
            collector_id=collector_id, token_hash=token_digest(token), expires_at=expires_at
        )
    )
    return token


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    display_name: str
    labs: dict[str, str]  # lab_id -> role
    session_id: int

    def can_view(self, lab_id: str) -> bool:
        return lab_id in self.labs

    def can_record(self, lab_id: str) -> bool:
        """Create or correct operator records (runs, annotations). Never instrument control."""
        return self.labs.get(lab_id) in RECORD_ROLES


def load_principal(conn: Connection, token: str, now: datetime) -> Principal | None:
    row = conn.execute(
        select(UserSession.id, AppUser.id, AppUser.username, AppUser.display_name)
        .join(AppUser, AppUser.id == UserSession.user_id)
        .where(
            UserSession.token_hash == token_digest(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
            AppUser.disabled_at.is_(None),
        )
    ).first()
    if row is None:
        return None
    labs = {
        str(r[0]): str(r[1])
        for r in conn.execute(
            select(LabGrant.lab_id, LabGrant.role).where(LabGrant.user_id == row[1])
        )
    }
    return Principal(int(row[1]), str(row[2]), str(row[3]), labs, int(row[0]))


def recent_login_failures(conn: Connection, username: str, now: datetime) -> int:
    return int(
        conn.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                and_(
                    AuditEvent.action == "login_failed",
                    AuditEvent.target == username,
                    AuditEvent.at > now - timedelta(minutes=15),
                )
            )
        ).scalar_one()
    )


def login(
    conn: Connection, username: str, password: str, now: datetime, ttl: timedelta
) -> str | None:
    row = conn.execute(
        select(AppUser.id, AppUser.password_hash).where(
            AppUser.username == username, AppUser.disabled_at.is_(None)
        )
    ).first()
    if not verify_password(None if row is None else str(row[1]), password) or row is None:
        audit(conn, now, "anonymous", None, "login_failed", username)
        return None
    token = new_session_token()
    conn.execute(
        insert(UserSession).values(
            token_hash=token_digest(token), user_id=row[0], created_at=now, expires_at=now + ttl
        )
    )
    audit(conn, now, "user", str(row[0]), "login", username)
    return token


def logout(conn: Connection, session_id: int, now: datetime) -> None:
    conn.execute(update(UserSession).where(UserSession.id == session_id).values(revoked_at=now))
