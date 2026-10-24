"""Bounded research reads. Every response records exposure before committing."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.result_access import record_verified_result_access
from backend.app.trace_schemas import (
    CandidatePage,
    DecisionDetail,
    DecisionPage,
    TraceManifestPublic,
)
from backend.app.trace_services import (
    candidates_view,
    decision_view,
    decisions_view,
    load_owned_trace,
    manifest_view,
)
from packages.research_trace.contracts import NonTradeReason

router = APIRouter(tags=["research-traces"])


class TracePins:
    def __init__(
        self,
        execution_attempt_id: UUID | None = None,
        result_manifest_sha256: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
        trace_manifest_sha256: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
    ):
        self.values = {
            "execution_attempt_id": execution_attempt_id,
            "result_manifest_sha256": result_manifest_sha256,
            "trace_manifest_sha256": trace_manifest_sha256,
        }


Pins = Annotated[TracePins, Depends()]


def _load(session, evaluation_id, user, request, pins):
    return load_owned_trace(
        session, evaluation_id, user.id, request.app.state.settings, **pins.values
    )


def _release(session, trace, user, response):
    record_verified_result_access(session, trace.result, user.id)
    session.commit()
    return response


@router.get(
    "/evaluations/{evaluation_id}/trace-manifest",
    response_model=TraceManifestPublic,
    operation_id="read_trace_manifest",
)
def read_trace_manifest(
    evaluation_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    pins: Pins,
):
    trace = _load(session, evaluation_id, user, request, pins)
    return _release(session, trace, user, manifest_view(trace))


@router.get(
    "/evaluations/{evaluation_id}/decisions",
    response_model=DecisionPage,
    operation_id="read_trace_decisions",
)
def read_trace_decisions(
    evaluation_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    pins: Pins,
    start: date | None = None,
    end: date | None = None,
    stage_id: str | None = Query(None, min_length=1, max_length=200),
    instrument_id: str | None = Query(None, min_length=1, max_length=200),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    trace = _load(session, evaluation_id, user, request, pins)
    response = decisions_view(
        trace,
        start=start,
        end=end,
        stage_id=stage_id,
        instrument_id=instrument_id,
        offset=offset,
        limit=limit,
    )
    return _release(session, trace, user, response)


@router.get(
    "/evaluations/{evaluation_id}/decisions/{decision_id}",
    response_model=DecisionDetail,
    operation_id="read_trace_decision",
)
def read_trace_decision(
    evaluation_id: UUID,
    decision_id: Annotated[str, Path(min_length=1, max_length=200)],
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    pins: Pins,
    stage_id: str | None = Query(None, min_length=1, max_length=200),
    instrument_id: str | None = Query(None, min_length=1, max_length=200),
    record_ordinal: int | None = Query(None, ge=0, le=2**63 - 1),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    trace = _load(session, evaluation_id, user, request, pins)
    response = decision_view(
        trace,
        decision_id,
        stage_id=stage_id,
        instrument_id=instrument_id,
        record_ordinal=record_ordinal,
        offset=offset,
        limit=limit,
    )
    return _release(session, trace, user, response)


@router.get(
    "/evaluations/{evaluation_id}/decisions/{decision_id}/candidates",
    response_model=CandidatePage,
    operation_id="read_trace_candidates",
)
def read_trace_candidates(
    evaluation_id: UUID,
    decision_id: Annotated[str, Path(min_length=1, max_length=200)],
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    pins: Pins,
    stage_id: str | None = Query(None, min_length=1, max_length=200),
    instrument_id: str | None = Query(None, min_length=1, max_length=200),
    record_ordinal: int | None = Query(None, ge=0, le=2**63 - 1),
    eligibility: Literal["eligible", "ineligible", "unknown", "not_sampled"]
    | None = None,
    reason: NonTradeReason | None = None,
    rank_min: int | None = Query(None, ge=1),
    rank_max: int | None = Query(None, ge=1),
    score_min: float | None = Query(None, allow_inf_nan=False),
    score_max: float | None = Query(None, allow_inf_nan=False),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    trace = _load(session, evaluation_id, user, request, pins)
    response = candidates_view(
        trace,
        decision_id,
        stage_id=stage_id,
        instrument_id=instrument_id,
        record_ordinal=record_ordinal,
        eligibility=eligibility,
        reason=reason,
        rank_min=rank_min,
        rank_max=rank_max,
        score_min=score_min,
        score_max=score_max,
        offset=offset,
        limit=limit,
    )
    return _release(session, trace, user, response)
