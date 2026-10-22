"""Authenticated M1 control/query API. This process never evaluates strategy code."""

import asyncio
import re
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from jwt import InvalidTokenError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.core.config import Settings, get_settings
from backend.app.core.db import create_db_engine, session_factory
from backend.app.core.security import (
    DUMMY_HASH,
    create_access_token,
    decode_access_token,
    verify_password,
)
from backend.app.models import (
    Artifact,
    DataImport,
    DataOutbox,
    DatasetVersion,
    ExecutionAttempt,
    OutboxEvent,
    ResearchRun,
    RunEvent,
    StrategyVersion,
    User,
    WorkerHeartbeat,
    utcnow,
)
from backend.app.schemas import (
    ArtifactPublic,
    AttemptPublic,
    DatasetPublic,
    ErrorResponse,
    EventPublic,
    HealthResponse,
    Page,
    RunCreate,
    RunDetail,
    RunPublic,
    StrategyDetail,
    StrategyPublic,
    Token,
    UserPublic,
)
from backend.app.services import (
    TERMINAL,
    DomainError,
    owned_run,
    request_cancel,
    resume_run,
    submit_run,
)
from backend.app.storage import read_verified

PREFIX = "/api/v1"


def create_app(settings: Settings | None = None, engine=None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="TradeGrad WebUI",
        version="0.1.0",
        description="Authenticated research API with immutable inputs, fenced optimization and verified checkpoint resume. Synthetic fixtures are not market data; final holdout execution is not enabled.",
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    from backend.app.api.body_limit import BodyLimitMiddleware

    app.add_middleware(
        BodyLimitMiddleware, max_bytes=settings.max_upload_bytes + 100_000
    )
    app.state.settings = settings
    app.state.engine = engine if engine is not None else create_db_engine(settings)
    app.state.session_factory = session_factory(app.state.engine)
    login_failures: dict[str, deque] = defaultdict(deque)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "Last-Event-ID",
            ],
            expose_headers=["X-Correlation-ID"],
        )

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.correlation_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "correlation_id": request.state.correlation_id,
            },
            headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "Request validation failed",
                "details": details,
                "correlation_id": request.state.correlation_id,
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        return JSONResponse(
            status_code=503,
            content={
                "code": "database_unavailable",
                "message": "Database operation failed",
                "details": None,
                "correlation_id": request.state.correlation_id,
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "Unexpected server error",
                "details": None,
                "correlation_id": request.state.correlation_id,
            },
        )

    @app.get(PREFIX + "/health/live", operation_id="health_live")
    def health_live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get(
        PREFIX + "/health/ready",
        response_model=HealthResponse,
        operation_id="health_ready",
    )
    def health_ready(session: SessionDep, response: Response):
        try:
            session.execute(
                text(
                    "SELECT version_num FROM alembic_version WHERE version_num = '003_m3'"
                )
            ).scalar_one()
        except SQLAlchemyError:
            response.status_code = 503
            return HealthResponse(status="unavailable", database=False)
        return HealthResponse(status="ready", database=True)

    @app.get(
        PREFIX + "/status", response_model=HealthResponse, operation_id="read_status"
    )
    def status(user: CurrentUser, session: SessionDep):
        cutoff = utcnow() - timedelta(seconds=settings.lease_seconds * 2)
        records = session.scalars(select(WorkerHeartbeat)).all()
        workers = {
            role: (
                "healthy"
                if any(h.role == role and h.heartbeat_at > cutoff for h in records)
                else "unavailable"
            )
            for role in (
                "research",
                "dispatcher",
                "reconciler",
                "data",
                "data_dispatcher",
            )
        }
        pending = session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .join(ResearchRun, ResearchRun.id == OutboxEvent.run_id)
            .where(ResearchRun.owner_id == user.id, OutboxEvent.dispatched_at.is_(None))
        )
        pending += session.scalar(
            select(func.count())
            .select_from(DataOutbox)
            .join(DataImport, DataImport.id == DataOutbox.import_id)
            .where(DataImport.owner_id == user.id, DataOutbox.dispatched_at.is_(None))
        )
        return HealthResponse(
            status="ready"
            if all(value == "healthy" for value in workers.values())
            else "worker_unavailable",
            database=True,
            queue_pending=pending,
            workers=workers,
        )

    @app.post(
        PREFIX + "/login/access-token",
        response_model=Token,
        operation_id="login_access_token",
    )
    def login_access_token(
        request: Request,
        session: SessionDep,
        form: Annotated[OAuth2PasswordRequestForm, Depends()],
    ):
        # Per-process backstop; production proxy must enforce a shared rate limit.
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        failures = login_failures[key]
        while failures and failures[0] < now - 60:
            failures.popleft()
        if len(failures) >= 10:
            raise DomainError(
                "rate_limited", "Too many login attempts; try again in one minute", 429
            )
        user = session.scalar(
            select(User).where(User.email == form.username.lower().strip())
        )
        valid = verify_password(
            form.password, user.hashed_password if user else DUMMY_HASH
        )
        if not user or not valid or not user.is_active:
            failures.append(now)
            if len(login_failures) > 10_000:
                login_failures.clear()
            raise DomainError("unauthorized", "Incorrect email or password", 401)
        return Token(
            access_token=create_access_token(user.id, settings),
            expires_in=settings.access_token_minutes * 60,
        )

    @app.get(PREFIX + "/users/me", response_model=UserPublic, operation_id="read_me")
    def read_me(user: CurrentUser):
        return user

    def page(session, model, owner_id, offset, limit):
        clause = model.owner_id == owner_id
        data = session.scalars(
            select(model)
            .where(clause)
            .order_by(model.created_at.desc(), model.id)
            .offset(offset)
            .limit(limit)
        ).all()
        count = session.scalar(select(func.count()).select_from(model).where(clause))
        return {"data": data, "count": count}

    @app.get(
        PREFIX + "/datasets",
        response_model=Page[DatasetPublic],
        operation_id="list_datasets",
    )
    def list_datasets(
        user: CurrentUser,
        session: SessionDep,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ):
        return page(session, DatasetVersion, user.id, offset, limit)

    @app.get(
        PREFIX + "/datasets/{dataset_id}",
        response_model=DatasetPublic,
        operation_id="read_dataset",
    )
    def read_dataset(dataset_id: UUID, user: CurrentUser, session: SessionDep):
        item = session.scalar(
            select(DatasetVersion).where(
                DatasetVersion.id == dataset_id, DatasetVersion.owner_id == user.id
            )
        )
        if not item:
            raise DomainError("not_found", "Dataset version not found", 404)
        return item

    @app.get(
        PREFIX + "/strategy-versions",
        response_model=Page[StrategyPublic],
        operation_id="list_strategies",
    )
    def list_strategies(
        user: CurrentUser,
        session: SessionDep,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ):
        return page(session, StrategyVersion, user.id, offset, limit)

    @app.get(
        PREFIX + "/strategy-versions/{strategy_id}",
        response_model=StrategyDetail,
        operation_id="read_strategy",
    )
    def read_strategy(strategy_id: UUID, user: CurrentUser, session: SessionDep):
        item = session.scalar(
            select(StrategyVersion).where(
                StrategyVersion.id == strategy_id, StrategyVersion.owner_id == user.id
            )
        )
        if not item:
            raise DomainError("not_found", "Strategy version not found", 404)
        return item

    @app.post(
        PREFIX + "/runs",
        response_model=RunPublic,
        status_code=202,
        operation_id="create_run",
    )
    def create_run(
        body: RunCreate,
        user: CurrentUser,
        session: SessionDep,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ):
        run = submit_run(session, user.id, body, idempotency_key, settings)
        session.commit()
        return run

    @app.get(PREFIX + "/runs", response_model=Page[RunPublic], operation_id="list_runs")
    def list_runs(
        user: CurrentUser,
        session: SessionDep,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ):
        return page(session, ResearchRun, user.id, offset, limit)

    @app.get(
        PREFIX + "/runs/{run_id}", response_model=RunDetail, operation_id="read_run"
    )
    def read_run(run_id: UUID, user: CurrentUser, session: SessionDep):
        run = owned_run(session, run_id, user.id)
        attempts = session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.run_id == run_id)
            .order_by(ExecutionAttempt.generation)
        ).all()
        return RunDetail(
            **RunPublic.model_validate(run).model_dump(),
            attempts=[AttemptPublic.model_validate(a) for a in attempts],
        )

    @app.post(
        PREFIX + "/runs/{run_id}/cancel",
        response_model=RunPublic,
        status_code=202,
        operation_id="cancel_run",
    )
    def cancel_run(run_id: UUID, user: CurrentUser, session: SessionDep):
        run = request_cancel(session, run_id, user.id)
        session.commit()
        return run

    @app.post(
        PREFIX + "/runs/{run_id}/resume",
        response_model=RunPublic,
        status_code=202,
        operation_id="resume_run",
    )
    def resume(run_id: UUID, user: CurrentUser, session: SessionDep):
        run = resume_run(session, run_id, user.id, settings)
        session.commit()
        return run

    @app.get(
        PREFIX + "/runs/{run_id}/events",
        operation_id="list_run_events",
        response_class=StreamingResponse,
        responses={
            200: {
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
                "description": "Durable EventPublic envelopes, SSE id = seq. Reconnect with after_seq or Last-Event-ID; Bearer header only.",
            }
        },
    )
    def list_run_events(
        run_id: UUID,
        user: CurrentUser,
        session: SessionDep,
        request: Request,
        after_seq: int = Query(0, ge=0),
        follow: bool = True,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        owned_run(session, run_id, user.id)
        if last_event_id:
            if not re.fullmatch(r"\d{1,18}", last_event_id):
                raise DomainError(
                    "invalid_event_cursor",
                    "Last-Event-ID must be a nonnegative event sequence",
                    422,
                )
            after_seq = max(after_seq, int(last_event_id))
        owner_id = user.id
        session.rollback()

        stream_token = request.headers.get("Authorization", "").partition(" ")[2]

        def read_batch(cursor):
            try:
                decode_access_token(stream_token, settings)
            except (InvalidTokenError, ValueError, TypeError):
                return [], True
            with app.state.session_factory() as stream_session:
                fresh_user = stream_session.get(User, owner_id)
                if not fresh_user or not fresh_user.is_active:
                    return [], True
                current = owned_run(stream_session, run_id, owner_id)
                events = stream_session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.seq > cursor)
                    .order_by(RunEvent.seq)
                    .limit(200)
                ).all()
                return [
                    EventPublic.model_validate(e) for e in events
                ], current.status in TERMINAL

        async def stream():
            cursor = after_seq
            while True:
                if await request.is_disconnected():
                    break
                events, terminal = await asyncio.to_thread(read_batch, cursor)
                for event in events:
                    cursor = event.seq
                    yield f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
                if len(events) == 200:
                    continue
                if not follow or terminal:
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(settings.sse_poll_seconds)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get(
        PREFIX + "/runs/{run_id}/artifacts",
        response_model=Page[ArtifactPublic],
        operation_id="list_run_artifacts",
    )
    def list_run_artifacts(
        run_id: UUID,
        user: CurrentUser,
        session: SessionDep,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ):
        owned_run(session, run_id, user.id)
        query = select(Artifact).where(Artifact.run_id == run_id)
        data = session.scalars(
            query.order_by(Artifact.created_at, Artifact.id).offset(offset).limit(limit)
        ).all()
        count = session.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.run_id == run_id)
        )
        return {"data": data, "count": count}

    @app.get(
        PREFIX + "/artifacts/{artifact_id}/download",
        operation_id="download_artifact",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                }
            }
        },
    )
    def download_artifact(artifact_id: UUID, user: CurrentUser, session: SessionDep):
        artifact = session.scalar(
            select(Artifact)
            .join(ResearchRun, ResearchRun.id == Artifact.run_id)
            .where(Artifact.id == artifact_id, ResearchRun.owner_id == user.id)
        )
        if not artifact:
            raise DomainError("not_found", "Artifact not found", 404)
        content = read_verified(
            settings.storage_root,
            artifact.storage_key,
            artifact.sha256,
            settings.max_artifact_bytes,
        )
        filename = re.sub(r"[^a-zA-Z0-9._-]", "_", artifact.name)
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    from backend.app.data_routes import router as data_router

    app.include_router(data_router, prefix=PREFIX)
    from backend.app.research_routes import router as research_router

    app.include_router(research_router, prefix=PREFIX)
    return app
