"""Dedicated immutable one-shot routes; there is no pause or optimizer resume."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import select

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.independent_schemas import (
    ControlledExperimentCreate,
    ControlledExperimentPublic,
    DiagnosticReplayCreate,
    IndependentCapability,
    IndependentEvaluationDetail,
    IndependentEvaluationPublic,
    IndependentResultQuery,
)
from backend.app.independent_services import (
    owned_independent,
    retry_independent,
    source_capability,
    submit_controlled,
    submit_diagnostic,
)
from backend.app.independent_views import (
    controlled_public,
    independent_public,
    load_owned_independent_result,
    owned_controlled,
)
from backend.app.models import ExecutionAttempt
from backend.app.result_access import record_verified_result_access
from backend.app.schemas import AttemptPublic
from backend.app.services import DomainError, request_cancel
from packages.tradegrad_adapter.independent_config import (
    NATIVE_SCHEMA,
    IndependentSourcePin,
)

router = APIRouter(tags=["independent-evaluations"])
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]


@router.post(
    "/independent-evaluations/capability",
    response_model=IndependentCapability,
    operation_id="independentCapability",
)
def capability(
    body: IndependentSourcePin, user: CurrentUser, session: SessionDep, request: Request
):
    value = source_capability(session, user.id, body, request.app.state.settings)
    session.commit()
    return value


@router.post(
    "/evaluations/{evaluation_id}/diagnostic-replays",
    response_model=IndependentEvaluationPublic,
    status_code=202,
    operation_id="createDiagnosticReplay",
)
def diagnostic(
    evaluation_id: UUID,
    body: DiagnosticReplayCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    if body.source.evaluation_id != evaluation_id:
        raise DomainError(
            "source_pin_mismatch", "Path and exact source Evaluation differ", 422
        )
    item, run = submit_diagnostic(
        session, user.id, body, idempotency_key, request.app.state.settings
    )
    value = independent_public(
        session, item, run, user.id, request.app.state.settings, admit_result=True
    )
    session.commit()
    return value


@router.get(
    "/independent-evaluations/{run_id}",
    response_model=IndependentEvaluationDetail,
    operation_id="readIndependentEvaluation",
)
def detail(run_id: UUID, user: CurrentUser, session: SessionDep, request: Request):
    item, run = owned_independent(session, run_id, user.id)
    public = independent_public(
        session, item, run, user.id, request.app.state.settings, admit_result=True
    )
    attempts = session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.run_id == run.id)
        .order_by(ExecutionAttempt.generation)
    ).all()
    value = IndependentEvaluationDetail(
        **public.model_dump(),
        attempts=[
            AttemptPublic.model_validate(attempt).model_copy(
                update={
                    "error": "independent_execution_failed" if attempt.error else None
                }
            )
            for attempt in attempts
        ],
    )
    session.commit()
    return value


@router.post(
    "/independent-evaluations/{run_id}/cancel",
    response_model=IndependentEvaluationPublic,
    status_code=202,
    operation_id="cancelIndependentEvaluation",
)
def cancel(run_id: UUID, user: CurrentUser, session: SessionDep, request: Request):
    item, _ = owned_independent(session, run_id, user.id)
    run = request_cancel(session, run_id, user.id)
    value = independent_public(
        session, item, run, user.id, request.app.state.settings, admit_result=True
    )
    session.commit()
    return value


@router.post(
    "/independent-evaluations/{run_id}/retry",
    response_model=IndependentEvaluationPublic,
    status_code=202,
    operation_id="retryIndependentEvaluation",
)
def retry(
    run_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    item, run = retry_independent(
        session, run_id, user.id, request.app.state.settings, idempotency_key
    )
    value = independent_public(session, item, run, user.id, request.app.state.settings)
    session.commit()
    return value


@router.post(
    "/independent-evaluations/{run_id}/pause",
    operation_id="pauseIndependentUnsupported",
)
def pause(run_id: UUID, user: CurrentUser, session: SessionDep):
    owned_independent(session, run_id, user.id)
    raise DomainError(
        "pause_unsupported",
        "Independent evaluation cannot pause or restore an optimizer checkpoint; cancel and retry from the original start after confirmed stop",
        409,
    )


@router.post(
    "/controlled-experiments",
    response_model=ControlledExperimentPublic,
    status_code=202,
    operation_id="createControlledExperiment",
)
def controlled(
    body: ControlledExperimentCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    item = submit_controlled(
        session, user.id, body, idempotency_key, request.app.state.settings
    )
    value = controlled_public(session, item, user.id, request.app.state.settings)
    session.commit()
    return value


@router.get(
    "/controlled-experiments/{controlled_id}",
    response_model=ControlledExperimentPublic,
    operation_id="readControlledExperiment",
)
def read_controlled(
    controlled_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    item = owned_controlled(session, controlled_id, user.id)
    value = controlled_public(session, item, user.id, request.app.state.settings)
    session.commit()
    return value


@router.get(
    "/independent-evaluations/evaluations/{evaluation_id}/result",
    response_model=IndependentResultQuery,
    operation_id="queryIndependentResult",
)
def query_result(
    evaluation_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    execution_attempt_id: UUID,
    result_manifest_sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    section: str = Query("equity", max_length=64),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    result = load_owned_independent_result(
        session, evaluation_id, user.id, request.app.state.settings
    )
    if (
        result.evaluation.attempt_id != execution_attempt_id
        or result.artifact.sha256 != result_manifest_sha256
    ):
        raise DomainError(
            "result_pin_mismatch", "Requested exact independent result pins differ", 409
        )
    native = result.run.config["schema_version"] == NATIVE_SCHEMA
    if native:
        from backend.app.research_views import SECTIONS

        if section not in SECTIONS:
            raise DomainError(
                "invalid_result_query", "Unsupported independent result section", 422
            )
        original = result.bundle[section]
        # This is a table API, not a chart-downsampling API. Preserve exact
        # source order and total for series as well as ledger sections.
        table = {
            "columns": original["columns"],
            "rows": original["rows"][offset : offset + limit],
            "filtered_rows": len(original["rows"]),
        }
    else:
        from backend.app.portable_result_tables import query_portable_table

        section = "account_series" if section == "equity" else section
        table = query_portable_table(result.bundle, section, offset, limit)
    value = IndependentResultQuery(
        evaluation_id=evaluation_id,
        run_id=result.run.id,
        purpose=result.run.config["purpose"],
        artifact_sha256=result.artifact.sha256,
        evaluation_mode="native_akquant" if native else "portable",
        metrics={metric["name"]: metric["value"] for metric in result.bundle["metrics"]}
        if native
        else result.bundle["summary"],
        metric_details=result.bundle["metrics"] if native else [],
        result_trust=result.bundle["result_trust"],
        section=section,
        columns=[column["name"] for column in table.get("columns", [])]
        if native
        else list(table["rows"][0])
        if table["rows"]
        else [],
        execution_attempt_id=result.evaluation.attempt_id,
        rows=table["rows"],
        total=table["filtered_rows"] if native else table["count"],
        offset=offset,
        limit=limit,
        warnings=[
            "New observations are not original execution facts until separately parity-checked.",
            "A failed arm is not a zero-return result; no optimizer reward is assigned.",
        ],
    )
    record_verified_result_access(session, result, user.id)
    session.commit()
    return value


@router.get(
    "/independent-evaluations/evaluations/{evaluation_id}/result/download",
    operation_id="downloadIndependentResult",
    response_class=Response,
)
def download(
    evaluation_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    result = load_owned_independent_result(
        session, evaluation_id, user.id, request.app.state.settings
    )
    record_verified_result_access(session, result, user.id, purpose="result_download")
    session.commit()
    return Response(
        result.content,
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="independent-{evaluation_id}.json"',
        },
    )


from packages.research_trace.diagnostic_parity_contracts import (
    DiagnosticParityEventPage,
    DiagnosticParityView,
)


@router.get(
    "/independent-evaluations/{run_id}/parity",
    response_model=DiagnosticParityView,
    operation_id="readDiagnosticParity",
)
def read_parity(
    run_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    report_sha256: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
):
    from backend.app.diagnostic_parity_views import load_owned_parity, parity_view

    report, sha = load_owned_parity(
        session,
        run_id,
        user.id,
        request.app.state.settings,
        report_sha256=report_sha256,
    )
    value = parity_view(report, sha)
    session.commit()
    return value


@router.get(
    "/independent-evaluations/{run_id}/parity/events",
    response_model=DiagnosticParityEventPage,
    operation_id="queryDiagnosticParityEvents",
)
def read_parity_events(
    run_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    report_sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    from backend.app.diagnostic_parity_views import load_owned_parity, parity_events

    report, sha = load_owned_parity(
        session,
        run_id,
        user.id,
        request.app.state.settings,
        report_sha256=report_sha256,
    )
    value = parity_events(report, sha, offset=offset, limit=limit)
    session.commit()
    return value
