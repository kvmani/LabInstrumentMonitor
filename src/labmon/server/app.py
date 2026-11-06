"""Central API application factory.

The API never contacts instruments. It accepts authenticated collector batches and heartbeats and
serves scoped, read-only history to signed-in users. Static UI assets are served from a local
directory; nothing is loaded from external hosts.

No ``from __future__ import annotations`` here: FastAPI must evaluate the closure-scoped
dependency annotations at definition time.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import OperationalError

from labmon import __version__
from labmon.contracts.envelope import (
    MAX_BATCH_DECODED_BYTES,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    canonical_json,
    format_utc,
)
from labmon.server import migrate
from labmon.server.db import make_engine
from labmon.server.ingest import (
    IngestError,
    IngestLimits,
    decode_body,
    ingest,
    parse_batch,
    read_bounded,
)
from labmon.server.models import Channel, Collector, Instrument
from labmon.server.security import (
    CSRF_HEADER,
    SESSION_COOKIE,
    Principal,
    audit,
    authenticate_collector,
    load_principal,
    login,
    logout,
    recent_login_failures,
)
from labmon.server.series import (
    RangeError,
    SeriesLimits,
    channel_series,
    count_rows,
    csv_rows,
    parse_range,
)
from labmon.server.settings import Settings
from labmon.server.status import StatusConfig, instrument_status

log = logging.getLogger("labmon.server")

Now = Callable[[], datetime]


def _system_now() -> datetime:
    return datetime.now(UTC)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class HeartbeatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    v: int
    collector_time: datetime
    software_version: str = Field(max_length=32)
    spool_epoch: int = Field(ge=0)
    next_seq: int = Field(ge=1)
    unacknowledged: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    oldest_pending_capture_time: str | None = Field(default=None, max_length=40)
    spool_capacity: int = Field(ge=0)
    sampling: bool
    upload_fault: str | None = Field(default=None, max_length=200)
    sources: list[dict[str, str]] = Field(default_factory=list, max_length=64)


def create_app(
    settings: Settings,
    engine: Engine | None = None,
    now: Now = _system_now,
    after_commit: Callable[[str], None] | None = None,
) -> FastAPI:
    """``after_commit`` is a fault-injection hook for tests (runs after COMMIT, before the ACK)."""
    engine = engine or make_engine(settings.database_url)
    app = FastAPI(
        title="Lab Instrument Monitor",
        version=__version__,
        docs_url="/api/docs" if settings.dev_tools_allowed else None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.engine = engine
    app.state.settings = settings
    app.state.now = now
    app.state.after_commit = after_commit
    status_cfg = StatusConfig(heartbeat_offline_s=settings.heartbeat_offline_s)
    series_limits = SeriesLimits(
        max_range=timedelta(days=settings.max_series_range_days),
        raw_point_limit=settings.raw_point_limit,
        max_buckets=settings.max_buckets,
    )
    ingest_limits = IngestLimits(
        future_tolerance_s=settings.future_tolerance_s, max_future_s=settings.max_future_s
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(OperationalError)
    async def _db_unavailable(_: Request, exc: OperationalError) -> JSONResponse:
        log.warning("database unavailable: %s", exc.__class__.__name__)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "5"},
            content={"code": "database_unavailable", "detail": "retry later"},
        )

    @app.exception_handler(IngestError)
    async def _ingest_error(_: Request, exc: IngestError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"code": exc.code, "detail": exc.message}
        )

    # --- dependencies --------------------------------------------------------------------------

    def collector_identity(request: Request) -> str:
        auth = request.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                401, "collector credential required", headers={"WWW-Authenticate": "Bearer"}
            )
        with engine.connect() as conn:
            cid = authenticate_collector(conn, token.strip(), now())
        if cid is None:
            raise HTTPException(
                401,
                "invalid, expired or revoked collector credential",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return cid

    def principal(request: Request) -> Principal:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(401, "sign in required")
        with engine.connect() as conn:
            p = load_principal(conn, token, now())
        if p is None:
            raise HTTPException(401, "session expired or revoked")
        return p

    def require_same_origin(request: Request) -> None:
        # CSRF defence for cookie-authenticated writes: a custom header cannot be sent cross-site
        # without a CORS preflight (which this API never grants) and the cookie is SameSite=Strict.
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "missing request header")
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.headers.get("Host", ""):
            raise HTTPException(403, "cross-origin request refused")

    User = Annotated[Principal, Depends(principal)]

    def scoped_instrument(conn: Any, p: Principal, instrument_id: str) -> Any:
        row = conn.execute(select(Instrument).where(Instrument.id == instrument_id)).first()
        if row is None or not p.can_view(row.lab_id):
            raise HTTPException(404, "instrument not found")  # do not reveal existence
        return row

    # --- health --------------------------------------------------------------------------------

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            current = migrate.current_revision(engine)
        except OperationalError:
            return JSONResponse(status_code=503, content={"status": "database_unavailable"})
        head = migrate.head_revision()
        if current != head:
            return JSONResponse(
                status_code=503,
                content={"status": "schema_mismatch", "current": current, "expected": head},
            )
        return JSONResponse(
            {
                "status": "ready",
                "schema": current,
                "protocol_versions": sorted(SUPPORTED_PROTOCOL_VERSIONS),
            }
        )

    # --- collector-facing ----------------------------------------------------------------------

    @app.post("/api/v1/ingest/batches")
    async def ingest_batches(
        request: Request, collector_id: Annotated[str, Depends(collector_identity)]
    ) -> Response:
        raw = await read_bounded(request.stream(), MAX_BATCH_DECODED_BYTES)
        body = decode_body(raw, request.headers.get("Content-Encoding"))
        pb = parse_batch(body)
        try:
            result = await run_in_threadpool(ingest, engine, collector_id, pb, now, ingest_limits)
        except OperationalError as exc:
            log.warning("ingest unavailable: %s", exc.__class__.__name__)
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "5"},
                content={"code": "database_unavailable", "detail": "retry the same batch later"},
            )
        if app.state.after_commit is not None and result.status == 200:
            await run_in_threadpool(app.state.after_commit, pb.batch_id)
        return Response(
            content=canonical_json(result.ack.model_dump(mode="json")),
            status_code=result.status,
            media_type="application/json",
        )

    @app.post("/api/v1/collectors/heartbeat")
    def heartbeat(
        body: HeartbeatBody, collector_id: Annotated[str, Depends(collector_identity)]
    ) -> dict[str, Any]:
        if body.v != PROTOCOL_VERSION:
            raise HTTPException(400, "unsupported protocol version")
        if body.collector_time.tzinfo is None:
            raise HTTPException(422, "collector_time needs an offset")
        received = now()
        with engine.begin() as conn:
            conn.execute(
                update(Collector)
                .where(Collector.id == collector_id)
                .values(
                    last_heartbeat_at=received,
                    last_heartbeat_collector_time=body.collector_time,
                    last_heartbeat=body.model_dump(mode="json"),
                )
            )
        return {"v": PROTOCOL_VERSION, "server_time": format_utc(received)}

    # --- session -------------------------------------------------------------------------------

    @app.post("/api/v1/session", dependencies=[Depends(require_same_origin)])
    def create_session(body: LoginBody, response: Response) -> dict[str, Any]:
        at = now()
        with engine.begin() as conn:
            if recent_login_failures(conn, body.username, at) >= settings.login_attempts_per_15min:
                raise HTTPException(429, "too many failed sign-in attempts; try again later")
            token = login(
                conn, body.username, body.password, at, timedelta(hours=settings.session_ttl_hours)
            )
        if token is None:
            raise HTTPException(401, "invalid username or password")
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
            max_age=int(settings.session_ttl_hours * 3600),
        )
        with engine.connect() as conn:
            p = load_principal(conn, token, at)
        assert p is not None
        return _principal_json(p, settings)

    @app.get("/api/v1/session")
    def get_session(p: User) -> dict[str, Any]:
        return _principal_json(p, settings)

    @app.delete("/api/v1/session", dependencies=[Depends(require_same_origin)])
    def delete_session(p: User, response: Response) -> dict[str, str]:
        with engine.begin() as conn:
            logout(conn, p.session_id, now())
            audit(conn, now(), "user", str(p.user_id), "logout", p.username)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"status": "signed_out"}

    # --- read API ------------------------------------------------------------------------------

    @app.get("/api/v1/instruments")
    def list_instruments(p: User) -> dict[str, Any]:
        at = now()
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    select(Instrument.id)
                    .where(Instrument.lab_id.in_(list(p.labs)))
                    .order_by(Instrument.lab_id, Instrument.name)
                )
            ]
            items = [instrument_status(conn, i, at, status_cfg) for i in ids]
        return {"server_time": format_utc(at), "items": items}

    @app.get("/api/v1/instruments/{instrument_id}/status")
    def get_status(instrument_id: str, p: User) -> dict[str, Any]:
        at = now()
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            scoped_instrument(conn, p, instrument_id)
            return instrument_status(conn, instrument_id, at, status_cfg)

    @app.get("/api/v1/channels/{channel_id}/series")
    def get_series(
        channel_id: str,
        p: User,
        start: Annotated[str, Query(max_length=40)],
        end: Annotated[str, Query(max_length=40)],
        max_points: Annotated[int | None, Query(ge=10, le=5000)] = None,
    ) -> dict[str, Any]:
        try:
            s, e = parse_range(start, end, series_limits.max_range)
        except RangeError as exc:
            raise HTTPException(422, str(exc)) from exc
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            ch = conn.execute(select(Channel.instrument_id).where(Channel.id == channel_id)).first()
            if ch is None:
                raise HTTPException(404, "channel not found")
            scoped_instrument(conn, p, ch.instrument_id)
            return channel_series(conn, channel_id, s, e, now(), series_limits, max_points)

    @app.get("/api/v1/instruments/{instrument_id}/export.csv")
    def export_csv(
        instrument_id: str,
        p: User,
        start: Annotated[str, Query(max_length=40)],
        end: Annotated[str, Query(max_length=40)],
    ) -> StreamingResponse:
        try:
            s, e = parse_range(start, end, series_limits.max_range)
        except RangeError as exc:
            raise HTTPException(422, str(exc)) from exc
        with engine.begin() as conn:
            inst = scoped_instrument(conn, p, instrument_id)
            n = count_rows(conn, instrument_id, s, e)
            if n > settings.csv_row_limit:
                raise HTTPException(
                    413,
                    f"{n} rows exceed the {settings.csv_row_limit}-row "
                    "interactive export limit; narrow the range",
                )
            audit(
                conn,
                now(),
                "user",
                str(p.user_id),
                "export_csv",
                instrument_id,
                {"start": format_utc(s), "end": format_utc(e), "rows": n},
            )
        sim = "SIMULATED_" if inst.simulated else ""
        fname = f"{sim}{instrument_id}_{s:%Y%m%dT%H%M%SZ}_{e:%Y%m%dT%H%M%SZ}.csv"
        return StreamingResponse(
            csv_rows(engine, instrument_id, s, e),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def api_not_found(rest: str) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "not found"})

    _mount_ui(app, settings.static_dir)
    return app


def _principal_json(p: Principal, settings: Settings) -> dict[str, Any]:
    return {
        "username": p.username,
        "display_name": p.display_name,
        "labs": p.labs,
        "display_timezone": settings.display_timezone,
        "environment": settings.environment,
    }


def _mount_ui(app: FastAPI, static_dir: Path | None) -> None:
    if static_dir is None or not (static_dir / "index.html").exists():

        @app.get("/")
        def no_ui() -> JSONResponse:
            return JSONResponse({"detail": "UI not built; run `npm run build` in web/"})

        return
    root = static_dir.resolve()
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def ui(path: str) -> FileResponse:
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            cache = (
                "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
            )
            return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
