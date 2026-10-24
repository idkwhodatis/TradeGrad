"""Owner-first leaf trace reads; schema-specific result admission is mandatory."""

from __future__ import annotations

import errno
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from uuid import UUID

from sqlalchemy import select

from backend.app.models import Artifact, Evaluation, ResearchRun
from backend.app.services import DomainError
from backend.app.storage import read_verified, safe_path
from backend.app.trace_schemas import (
    CandidatePage,
    DecisionDetail,
    DecisionIndex,
    DecisionPage,
    TraceContext,
    TraceManifestPublic,
)
from packages.research_trace.contracts import (
    CaptureConfig,
    TraceManifest,
    canonical_bytes,
)
from packages.research_trace.storage import (
    TraceReader,
    TraceStorageError,
    manifest_name,
)
from packages.research_trace.transport import CaptureSummary

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
LOADERS = {
    "tradegrad.independent-evaluation.v1": (
        "backend.app.independent_views",
        "load_owned_independent_result",
    ),
    "tradegrad.portable.independent-evaluation.v1": (
        "backend.app.independent_views",
        "load_owned_independent_result",
    ),
    "tradegrad.run.v1": ("backend.app.research_views", "load_owned_result"),
    "tradegrad.final-evaluation.v1": (
        "backend.app.final_evaluation_views",
        "load_owned_final_result",
    ),
    "tradegrad.portable.run.v1": (
        "backend.app.portable_views",
        "load_owned_portable_result",
    ),
    "tradegrad.portable.final-evaluation.v1": (
        "backend.app.portable_final_views",
        "load_owned_final_result",
    ),
}


def _invalid():
    return DomainError(
        "trace_integrity", "Trace evidence does not match its immutable evaluation", 409
    )


@contextmanager
def trace_storage_admission():
    """Reject unsafe immutable trace paths as conflicts, without relaxing storage checks."""
    try:
        yield
    except DomainError as error:
        if error.code == "unsafe_storage_path":
            raise _invalid() from None
        raise
    except OSError as error:
        # A leaf changed after safe_path still fails O_NOFOLLOW at the open.
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _invalid() from None
        raise


@dataclass(frozen=True)
class OwnedTrace:
    result: object
    context: TraceContext
    manifest: TraceManifest | None
    reader: TraceReader | None


def timestamp_evidence(result):
    """Qualify exact saved times without guessing exchange closes or upgrading old evidence."""
    bundle = getattr(result, "bundle", {})
    if bundle.get("evaluation_mode") == "portable":
        return {
            "timestamp_semantics": "portable_engine_instants",
            "timestamp_caveat": None,
        }
    engine = bundle.get("engine", {})
    if engine.get("name") == "akquant":
        semantics = engine.get("series_semantics", {}).get("timestamps", "")
        if (
            bundle.get("identity", {}).get("profile") == "corrected_explicit_config"
            and "market-local midnight session labels" in semantics
        ):
            return {
                "timestamp_semantics": "native_daily_session_labels",
                "timestamp_caveat": "Native daily decision, feature and information-cutoff timestamps are market-local midnight session labels, not observed exchange-close or data-availability instants. Same-session OHLC observations do not prove availability at midnight, intraday causality or absence of look-ahead. Saved labels are not rewritten to guessed exchange closes.",
            }
        return {
            "timestamp_semantics": "native_recorded_unqualified",
            "timestamp_caveat": "Historical native timestamps are retained exactly as recorded. Their daily session-label conversion and actual data-availability timing are not independently verified; do not infer an exchange-close instant, intraday causality or absence of look-ahead.",
        }
    return {
        "timestamp_semantics": "unknown",
        "timestamp_caveat": "Saved timestamp semantics are unavailable; no actual data-availability instant is inferred.",
    }


@trace_storage_admission()
def load_owned_trace(
    session,
    evaluation_id,
    owner_id,
    settings,
    *,
    execution_attempt_id=None,
    result_manifest_sha256=None,
    trace_manifest_sha256=None,
):
    import importlib

    # Never use an unscoped get before ownership, even for existence/status checks.
    pair = session.execute(
        select(Evaluation, ResearchRun)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .where(Evaluation.id == evaluation_id, ResearchRun.owner_id == owner_id)
    ).one_or_none()
    if pair is None:
        raise DomainError("not_found", "Evaluation not found", 404)
    evaluation, run = pair
    dispatch = LOADERS.get(run.config.get("schema_version"))
    if dispatch is None:
        raise DomainError(
            "trace_unavailable",
            "This historical evaluation has no supported immutable result binding",
            409,
        )
    result = getattr(importlib.import_module(dispatch[0]), dispatch[1])(
        session, evaluation_id, owner_id, settings
    )
    if (
        execution_attempt_id is not None
        and execution_attempt_id != evaluation.attempt_id
    ) or (
        result_manifest_sha256 is not None
        and result_manifest_sha256 != result.artifact.sha256
    ):
        raise DomainError(
            "trace_pin_mismatch",
            "Requested evidence pins differ from this immutable evaluation",
            409,
        )
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.attempt_id == evaluation.attempt_id,
            Artifact.name == manifest_name(evaluation.id),
        )
    )
    context = TraceContext(
        evaluation_id=evaluation.id,
        execution_attempt_id=evaluation.attempt_id,
        result_manifest_sha256=result.artifact.sha256,
        trace_manifest_sha256=None,
        status="not_collected",
        missing_reason="This immutable evaluation has no structured research capture; absent observations are not reconstructed.",
        **timestamp_evidence(result),
    )
    reference = (evaluation.payload or {}).get("trace_manifest")
    if artifact is None:
        if reference is not None:
            raise _invalid()
        if trace_manifest_sha256 is not None:
            raise DomainError(
                "trace_pin_mismatch",
                "The requested immutable trace is not registered for this evaluation",
                409,
            )
        return OwnedTrace(result, context, None, None)
    try:
        if (
            artifact.media_type != "application/json"
            or not 0 < artifact.size_bytes <= MAX_MANIFEST_BYTES
            or not isinstance(reference, dict)
            or UUID(reference["artifact_id"]) != artifact.id
            or reference["sha256"] != artifact.sha256
            or reference["size_bytes"] != artifact.size_bytes
        ):
            raise _invalid()
        if (
            trace_manifest_sha256 is not None
            and trace_manifest_sha256 != artifact.sha256
        ):
            raise DomainError(
                "trace_pin_mismatch",
                "Requested trace hash differs; evidence was not retargeted",
                409,
            )
        raw = read_verified(
            settings.storage_root,
            artifact.storage_key,
            artifact.sha256,
            min(settings.max_artifact_bytes, MAX_MANIFEST_BYTES),
        )
        manifest = TraceManifest.model_validate_json(raw)
        if len(raw) != artifact.size_bytes or canonical_bytes(manifest) != raw:
            raise _invalid()
        binding = manifest.binding
        source_config = run.config.get("source_config", run.config)
        universe = source_config.get("universe")
        if (
            binding.run_id != run.id
            or binding.evaluation_id != evaluation.id
            or binding.execution_attempt_id != evaluation.attempt_id
            or (
                binding.candidate_id is not None
                and binding.candidate_id != evaluation.candidate_id
            )
            or binding.strategy_version_id != run.strategy_version_id
            or binding.code_sha256 != evaluation.code_sha256
            or binding.dataset_version_id != result.dataset.id
            or binding.dataset_sha256 != result.dataset.content_sha256
            or binding.result_manifest_sha256 != result.artifact.sha256
            or (
                str(binding.universe_version_id)
                if binding.universe_version_id
                else None
            )
            != (universe.get("id") if universe else None)
            or binding.universe_sha256 != (universe.get("sha256") if universe else None)
        ):
            raise _invalid()
        from packages.research_trace.binding import make_binding
        from packages.tradegrad_adapter.config import RunConfig
        from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
        from packages.tradegrad_adapter.independent_config import (
            IndependentEvaluationConfig,
            PortableIndependentEvaluationConfig,
        )
        from packages.tradegrad_adapter.portable_config import PortableRunConfig
        from packages.tradegrad_adapter.portable_final_config import (
            PortableFinalEvaluationConfig,
        )

        config_types = {
            "tradegrad.independent-evaluation.v1": IndependentEvaluationConfig,
            "tradegrad.portable.independent-evaluation.v1": PortableIndependentEvaluationConfig,
            "tradegrad.run.v1": RunConfig,
            "tradegrad.final-evaluation.v1": FinalEvaluationConfig,
            "tradegrad.portable.run.v1": PortableRunConfig,
            "tradegrad.portable.final-evaluation.v1": PortableFinalEvaluationConfig,
        }
        config = config_types[run.config["schema_version"]].model_validate(run.config)
        expected = make_binding(
            run=run,
            evaluation=evaluation,
            config=config,
            bundle=result.bundle,
            result_sha256=result.artifact.sha256,
            candidate_id=binding.candidate_id,
        )
        if binding != expected:
            raise _invalid()
        # The immutable result payload pins every capture byte and declared counter.
        capture = CaptureSummary.model_validate(
            (evaluation.payload or {}).get("research_capture")
        )
        captured_fields = capture.model_dump(mode="json")
        if any(
            manifest.model_dump(mode="json")[key] != value
            for key, value in captured_fields.items()
        ):
            raise _invalid()
        source = getattr(config, "source_config", config)
        if capture.capture != (
            (
                config.research_capture
                if getattr(config, "execution_mode", None) == "independent_evaluation"
                else source.research_capture
            )
            or CaptureConfig()
        ) or capture.source != (
            "engine_observed"
            if config.schema_version.startswith("tradegrad.portable.")
            else "strategy_emitted"
        ):
            raise _invalid()
        names = [ref.name for ref in manifest.artifacts]
        if len(names) > 20_000 or len(set(names)) != len(names):
            raise _invalid()
        registrations = (
            session.scalars(
                select(Artifact).where(
                    Artifact.run_id == run.id,
                    Artifact.attempt_id == evaluation.attempt_id,
                    Artifact.name.in_(names),
                )
            ).all()
            if names
            else []
        )
        registered = {item.name: item for item in registrations}
        paths = {}
        for ref in manifest.artifacts:
            item = registered.get(ref.name)
            if item is None or (item.sha256, item.size_bytes, item.media_type) != (
                ref.sha256,
                ref.size_bytes,
                ref.media_type,
            ):
                raise _invalid()
            paths[ref.name] = safe_path(settings.storage_root, item.storage_key)
        reader = TraceReader(manifest, paths)
        context = context.model_copy(
            update={
                "status": "available",
                "missing_reason": None,
                "trace_manifest_sha256": artifact.sha256,
                "binding": binding,
            }
        )
        if (
            manifest.coverage in {"not_collected", "unsupported"}
            and manifest.decisions_saved == 0
        ):
            context = context.model_copy(
                update={
                    "status": "not_collected",
                    "missing_reason": "; ".join(manifest.missing_reasons),
                }
            )
            reader = None
        return OwnedTrace(result, context, manifest, reader)
    except DomainError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise _invalid() from None


def _query(trace, table, **query):
    try:
        return trace.reader.query(table, **query)
    except TraceStorageError as error:
        raise DomainError(
            error.code,
            error.message,
            422
            if error.code
            in {
                "invalid_trace_query",
                "trace_query_too_large",
                "trace_detail_too_large",
            }
            else 409,
        ) from None


def _structural_view(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DomainError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise _invalid() from None

    return checked


@_structural_view
def manifest_view(trace):
    return TraceManifestPublic(context=trace.context, manifest=trace.manifest)


@_structural_view
def decisions_view(trace, *, offset=0, limit=100, **filters):
    if trace.reader is None:
        return DecisionPage(
            context=trace.context, items=None, total=None, offset=offset, limit=limit
        )
    page = _query(trace, "events", offset=offset, limit=limit, **filters)
    return DecisionPage(context=trace.context, **page)


def _event(trace, decision_id):
    page = _query(trace, "events", decision_id=decision_id, limit=2)
    if not page["total"]:
        raise DomainError(
            "not_found", "Decision not found in this immutable evaluation", 404
        )
    if page["total"] != 1:
        raise _invalid()
    event = DecisionIndex.model_validate(page["items"][0])
    return {
        "decision_id": event.decision_id,
        "start": event.session_date,
        "end": event.session_date + timedelta(days=1),
    }


def _validate_record_ordinal(record_ordinal, stage_id):
    if record_ordinal is not None and (
        stage_id is None
        or type(record_ordinal) is not int
        or not 0 <= record_ordinal <= 2**63 - 1
    ):
        raise DomainError(
            "invalid_trace_query",
            "An exact recorded ordinal requires its stage and a nonnegative integer",
            422,
        )


@_structural_view
def decision_view(
    trace,
    decision_id,
    *,
    instrument_id=None,
    stage_id=None,
    record_ordinal=None,
    offset=0,
    limit=100,
):
    _validate_record_ordinal(record_ordinal, stage_id)
    empty = {"items": None, "total": None, "offset": offset, "limit": limit}
    if trace.reader is None:
        return DecisionDetail(
            context=trace.context,
            decision=None,
            stages=None,
            features=empty,
            predicates=empty,
            instrument_id=instrument_id,
            stage_id=stage_id,
        )
    event = _event(trace, decision_id)
    decision = _query(trace, "decisions", **event, limit=2)
    if decision["total"] != 1:
        raise _invalid()
    stages = _query(trace, "stages", **event, stage_id=stage_id, limit=100)
    if stages["total"] > 100:
        raise _invalid()
    details = {}
    for table in ("features", "predicates"):
        page = _query(
            trace,
            table,
            **event,
            stage_id=stage_id,
            instrument_id=instrument_id,
            record_ordinal=record_ordinal,
            offset=offset,
            limit=limit,
        )
        page["items"] = [
            {
                "stage_id": item["stage_id"],
                "ordinal": item["ordinal"],
                "observation": item["payload"],
            }
            for item in page["items"]
        ]
        details[table] = page
    return DecisionDetail(
        context=trace.context,
        decision={**decision["items"][0]["payload"], "session_date": event["start"]},
        stages=[item["payload"] for item in stages["items"]],
        **details,
        instrument_id=instrument_id,
        stage_id=stage_id,
    )


@_structural_view
def candidates_view(trace, decision_id, *, offset=0, limit=100, **filters):
    _validate_record_ordinal(filters.get("record_ordinal"), filters.get("stage_id"))
    if trace.reader is None:
        return CandidatePage(
            context=trace.context,
            decision_id=decision_id,
            items=None,
            total=None,
            offset=offset,
            limit=limit,
        )
    event = _event(trace, decision_id)
    page = _query(trace, "candidates", **event, offset=offset, limit=limit, **filters)
    page["items"] = [
        {
            "stage_id": item["stage_id"],
            "ordinal": item["ordinal"],
            "observation": item["payload"],
        }
        for item in page["items"]
    ]
    return CandidatePage(context=trace.context, decision_id=decision_id, **page)
