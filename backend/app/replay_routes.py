"""Exact immutable replay reads; schema-specific admission and exposure are shared."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.replay_schemas import (
    ReplayExecutionPage,
    ReplayInstrumentPage,
    ReplayMarketPage,
)
from backend.app.replay_services import (
    replay_execution,
    replay_instruments,
    replay_market,
)
from backend.app.trace_routes import Pins, _load, _release
from backend.app.workbench_services import _source_association

router = APIRouter(tags=["decision-replay"])


@router.get(
    "/evaluations/{evaluation_id}/replay-instruments",
    response_model=ReplayInstrumentPage,
    operation_id="read_replay_instruments",
)
def read_replay_instruments(
    evaluation_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    pins: Pins,
    session_date: date | None = None,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    trace = _load(session, evaluation_id, user, request, pins)
    source = _source_association(
        session, trace.result.evaluation, trace.result.run, user.id
    )
    response = replay_instruments(
        trace,
        request.app.state.settings,
        source,
        session_date=session_date,
        offset=offset,
        limit=limit,
    )
    return _release(session, trace, user, response)


@router.get(
    "/evaluations/{evaluation_id}/decisions/{decision_id}/market",
    response_model=ReplayMarketPage,
    operation_id="read_replay_market",
)
def read_replay_market(
    evaluation_id: UUID,
    decision_id: Annotated[str, Path(min_length=1, max_length=200)],
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    pins: Pins,
    instrument_id: str = Query(min_length=1, max_length=200),
    start: date | None = None,
    end: date | None = None,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    trace = _load(session, evaluation_id, user, request, pins)
    response = replay_market(
        trace,
        request.app.state.settings,
        decision_id,
        instrument_id=instrument_id,
        start=start,
        end=end,
        offset=offset,
        limit=limit,
    )
    return _release(session, trace, user, response)


@router.get(
    "/evaluations/{evaluation_id}/decisions/{decision_id}/execution",
    response_model=ReplayExecutionPage,
    operation_id="read_replay_execution",
)
def read_replay_execution(
    evaluation_id: UUID,
    decision_id: Annotated[str, Path(min_length=1, max_length=200)],
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    pins: Pins,
    instrument_id: str | None = Query(None, min_length=1, max_length=200),
    stage_id: str | None = Query(None, min_length=1, max_length=200),
    logical_order_id: str | None = Query(None, min_length=1, max_length=200),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    trace = _load(session, evaluation_id, user, request, pins)
    response = replay_execution(
        trace,
        decision_id,
        instrument_id=instrument_id,
        stage_id=stage_id,
        logical_order_id=logical_order_id,
        offset=offset,
        limit=limit,
    )
    return _release(session, trace, user, response)
