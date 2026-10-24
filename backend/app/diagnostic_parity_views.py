"""Owner-first immutable parity admission and bounded event-map pages."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from backend.app.models import Artifact, Evaluation
from backend.app.services import DomainError
from backend.app.storage import read_verified
from packages.research_trace.binding import make_binding
from packages.research_trace.contracts import TraceManifest, canonical_bytes
from packages.research_trace.diagnostic_parity_contracts import (
    MAX_PARITY_BYTES,
    DiagnosticParityEventPage,
    DiagnosticParityReport,
    DiagnosticParitySide,
    DiagnosticParityView,
)


def _invalid():
    return DomainError(
        "diagnostic_parity_integrity",
        "Diagnostic parity differs from its immutable original/replay evidence",
        409,
    )


def validate_parity_payload(session, result, payload, settings):
    """Also runs before leaf completion, without pretending it is already terminal."""
    from backend.app.independent_services import (
        independent_config,
        load_owned_runtime_observation,
        resolve_source,
    )

    run, evaluation = result.run, result.evaluation
    config = independent_config(run)
    ref = payload.get("diagnostic_parity")
    if config.purpose != "diagnostic":
        if ref is not None:
            raise _invalid()
        return None
    try:
        if not isinstance(ref, dict) or set(ref) != {
            "artifact_id",
            "sha256",
            "size_bytes",
        }:
            raise ValueError("Diagnostic report is required")
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.id == UUID(ref["artifact_id"]),
                Artifact.run_id == run.id,
                Artifact.attempt_id == evaluation.attempt_id,
                Artifact.name == f"evaluation-{evaluation.id}-diagnostic-parity.json",
            )
        )
        if (
            artifact is None
            or (artifact.sha256, artifact.size_bytes)
            != (ref["sha256"], ref["size_bytes"])
            or artifact.media_type != "application/json"
            or not 0 < artifact.size_bytes <= MAX_PARITY_BYTES
        ):
            raise ValueError("Diagnostic report registration differs")
        raw = read_verified(
            settings.storage_root,
            artifact.storage_key,
            artifact.sha256,
            min(settings.max_artifact_bytes, MAX_PARITY_BYTES),
        )
        report = DiagnosticParityReport.model_validate_json(raw)
        if len(raw) != artifact.size_bytes or canonical_bytes(report) != raw:
            raise ValueError("Diagnostic report bytes differ")
        # Source reads enforce owner, physical generation, terminal/final admission,
        # spec/code and every immutable artifact pin. Exposure is recorded first.
        source = resolve_source(session, run.owner_id, config.source, settings)
        expected_source = DiagnosticParitySide(
            binding=source.admitted.input.binding,
            source_kind=config.source.source_kind,
            source_id=config.source.source_id,
            spec_revision=config.source.spec_revision,
            spec_sha256=config.source.spec_sha256,
            trace_manifest_sha256=config.source.trace_manifest_sha256,
            runtime_observation_sha256=source.observation_sha256,
        )
        trace_ref = payload["trace_manifest"]
        trace = session.scalar(
            select(Artifact).where(
                Artifact.id == UUID(trace_ref["artifact_id"]),
                Artifact.run_id == run.id,
                Artifact.attempt_id == evaluation.attempt_id,
                Artifact.name == f"evaluation-{evaluation.id}-trace-manifest.json",
            )
        )
        if trace is None or (trace.sha256, trace.size_bytes) != (
            trace_ref["sha256"],
            trace_ref["size_bytes"],
        ):
            raise ValueError("Diagnostic trace registration differs")
        manifest = TraceManifest.model_validate_json(
            read_verified(
                settings.storage_root,
                trace.storage_key,
                trace.sha256,
                min(settings.max_artifact_bytes, 4 * 1024**2),
            )
        )
        expected_binding = make_binding(
            run=run,
            evaluation=evaluation,
            config=config,
            bundle=result.bundle,
            result_sha256=result.artifact.sha256,
        )
        if (
            manifest.binding != expected_binding
            or manifest.capture != config.research_capture
        ):
            raise ValueError("Diagnostic trace physical binding differs")
        projected = SimpleNamespace(
            **{
                column.name: getattr(evaluation, column.name)
                for column in Evaluation.__table__.columns
            }
        )
        projected.payload = payload
        observed_result = SimpleNamespace(
            run=run, evaluation=projected, bundle=result.bundle, dataset=result.dataset
        )
        observation, observation_sha = load_owned_runtime_observation(
            session, observed_result, settings
        )
        if observation is None:
            raise ValueError("New diagnostic runtime observation is required")
        expected_replay = DiagnosticParitySide(
            binding=expected_binding,
            source_kind="strategy_version",
            source_id=run.strategy_version_id,
            spec_revision=None,
            spec_sha256=None,
            trace_manifest_sha256=trace.sha256,
            runtime_observation_sha256=observation_sha,
        )
        if report.source != expected_source or report.replay != expected_replay:
            raise ValueError("Diagnostic original/replay pins differ")
        return report, artifact.sha256
    except DomainError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise _invalid() from None


def load_owned_parity(session, run_id, owner_id, settings, *, report_sha256=None):
    from backend.app.independent_services import owned_independent
    from backend.app.independent_views import load_owned_independent_result
    from backend.app.result_access import record_verified_result_access
    from backend.app.trace_services import load_owned_trace

    item, run = owned_independent(session, run_id, owner_id)
    if item.purpose != "diagnostic":
        raise DomainError(
            "diagnostic_parity_unavailable",
            "Controlled executions have comparisons, not original-source parity certification",
            409,
        )
    evaluation = session.scalar(
        select(Evaluation).where(
            Evaluation.run_id == run.id, Evaluation.attempt_id == run.active_attempt_id
        )
    )
    if evaluation is None or run.status != "succeeded":
        raise DomainError(
            "diagnostic_parity_pending",
            "No completed immutable diagnostic report is available",
            409,
        )
    result = load_owned_independent_result(session, evaluation.id, owner_id, settings)
    # Read-time trace verification still takes the explicit schema-specific path.
    load_owned_trace(session, evaluation.id, owner_id, settings)
    report, sha = validate_parity_payload(session, result, evaluation.payload, settings)
    if report_sha256 is not None and sha != report_sha256:
        raise DomainError(
            "diagnostic_parity_pin_mismatch",
            "Requested parity hash differs; evidence was not retargeted",
            409,
        )
    record_verified_result_access(session, result, owner_id)
    return report, sha


def parity_view(report, sha):
    fields = report.model_dump(exclude={"events"})
    return DiagnosticParityView(
        **fields,
        report_sha256=sha,
        mapping_counts=dict(Counter(event.mapping for event in report.events)),
    )


def parity_events(report, sha, *, offset=0, limit=100):
    if (
        type(offset) is not int
        or not 0 <= offset <= 100000
        or type(limit) is not int
        or not 1 <= limit <= 500
    ):
        raise DomainError(
            "invalid_parity_query",
            "Diagnostic event page is outside its fixed bound",
            422,
        )
    value = DiagnosticParityEventPage(
        report_sha256=sha,
        items=report.events[offset : offset + limit],
        total=len(report.events),
        offset=offset,
        limit=limit,
    )
    if len(canonical_bytes(value)) > 4 * 1024**2:
        raise DomainError(
            "parity_query_too_large", "Narrow the diagnostic event page", 422
        )
    return value
