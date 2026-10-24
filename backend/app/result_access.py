"""Append-only owner-wide exposure evidence for reads, including pre-M5 results.

Call after ownership/byte validation and before committing a result-bearing response.
Nothing here backfills unseen historical activity or asserts an unseen holdout.
"""

from uuid import UUID

from backend.app.experiment_services import record_exposure
from backend.app.models import DatasetVersion, UniverseVersion
from backend.app.services import DomainError
from packages.tradegrad_adapter.config import DateWindow


def contains_result_data(value):
    """Status-only events/results do not imply disclosure of market outcomes."""
    if isinstance(value, dict):
        return any(
            (
                key
                in {
                    "metrics",
                    "combined_score",
                    "summary",
                    "window_results",
                    "best_candidate_id",
                }
                and item is not None
                and item != {}
                and item != []
            )
            or contains_result_data(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_result_data(item) for item in value)
    return False


def record_control_response_access(session, run, owner_id):
    """A completed idempotent control response is also a result read.

    The caller commits this evidence with the control transaction before returning
    its DTO. Status-only acknowledgements do not invent data exposure.
    """
    if contains_result_data(run.result):
        record_result_access(session, run, owner_id, source="run-control-response")


def record_result_access(
    session,
    run,
    owner_id,
    *,
    purpose="result_preview",
    evaluation=None,
    artifact=None,
    dataset=None,
    source="result",
    conservative=False,
):
    if run.owner_id != owner_id:
        raise DomainError("not_found", "Result not found", 404)
    dataset = dataset or session.get(DatasetVersion, run.dataset_version_id)
    if (
        dataset is None
        or dataset.owner_id != owner_id
        or dataset.id != run.dataset_version_id
    ):
        raise DomainError("not_found", "Result dataset not found", 404)
    config = run.config
    source_config = config.get("source_config", config)
    ref = source_config.get("dataset")
    if ref and (
        str(dataset.id) != ref.get("id") or dataset.content_sha256 != ref.get("sha256")
    ):
        raise DomainError("result_integrity", "Result dataset binding differs", 409)
    universe = None
    universe_ref = source_config.get("universe")
    if universe_ref:
        try:
            universe = session.get(UniverseVersion, UUID(universe_ref["id"]))
        except (KeyError, ValueError, TypeError):
            raise DomainError(
                "result_integrity", "Result universe binding differs", 409
            ) from None
        if (
            universe is None
            or universe.owner_id != owner_id
            or universe.content_sha256 != universe_ref.get("sha256")
        ):
            raise DomainError(
                "result_integrity", "Result universe binding differs", 409
            )
    if evaluation is not None and evaluation.run_id != run.id:
        raise DomainError("not_found", "Result evaluation not found", 404)
    if artifact is not None and artifact.run_id != run.id:
        raise DomainError("not_found", "Result artifact not found", 404)

    windows = []
    coverage = "persisted_evaluation_window"
    if evaluation is not None and not conservative:
        try:
            windows = [
                DateWindow(
                    start=evaluation.window["start"], end=evaluation.window["end"]
                )
            ]
        except (KeyError, ValueError, TypeError):
            pass
    elif not conservative:
        # A summary can describe every optimization window, not just the best one.
        raw = (
            {"holdout": config["holdout"]}
            if config.get("holdout")
            else source_config.get("windows", {})
        )
        windows = [
            DateWindow.model_validate(raw[key])
            for key in ("train", "validation", "holdout")
            if raw.get(key) and (key != "holdout" or config.get("holdout"))
        ]
        coverage = "configured_result_windows"
    if (
        not windows
        and not conservative
        and source_config.get("start_date")
        and source_config.get("end_date")
    ):
        try:
            windows = [
                DateWindow(
                    start=source_config["start_date"], end=source_config["end_date"]
                )
            ]
            coverage = "legacy_configured_result_window"
        except (ValueError, TypeError):
            pass
    if not windows:
        windows = [DateWindow(start=dataset.start_date, end=dataset.end_date)]
        coverage = "unknown_legacy_or_diagnostic_window_conservative_dataset_coverage"
    identifier = (
        str(artifact.id)
        if artifact is not None
        else (
            str(evaluation.id)
            if evaluation is not None
            else f"{run.id}:{run.generation}"
        )
    )
    for index, window in enumerate(windows):
        record_exposure(
            session,
            owner_id=owner_id,
            dataset=dataset,
            universe=universe,
            run=run,
            window=window,
            purpose=purpose,
            source_key=f"access:{source}:{identifier}:{purpose}:{index}",
            provenance={
                "coverage": coverage,
                "evaluation_id": str(evaluation.id) if evaluation is not None else None,
                "artifact_sha256": artifact.sha256 if artifact is not None else None,
                "universe": universe_ref,
                "schema_version": config.get(
                    "schema_version", "legacy_seed_evaluation"
                ),
                "tracking_coverage": "Explicit access only; earlier external and legacy exposure remains unknown.",
            },
        )


def record_verified_result_access(
    session, result, owner_id, *, purpose="result_preview"
):
    record_result_access(
        session,
        result.run,
        owner_id,
        purpose=purpose,
        evaluation=result.evaluation,
        artifact=result.artifact,
        dataset=getattr(result, "dataset", None),
    )


def load_result_aware_artifact(session, artifact, owner_id, settings):
    """Generic downloads cannot bypass typed final-result publication fences."""
    import re

    from backend.app.artifact_metadata import (
        METADATA_NAMES,
        SOURCE_NAMES,
        validate_non_result_artifact,
    )
    from backend.app.models import Evaluation, ResearchRun
    from backend.app.storage import read_verified

    run = session.get(ResearchRun, artifact.run_id)
    if run is None or run.owner_id != owner_id:
        raise DomainError("not_found", "Artifact not found", 404)
    if run.config.get("schema_version") == "tradegrad.comparison.v1":
        from backend.app.comparison_services import load_comparison_artifact

        return load_comparison_artifact(session, artifact, owner_id, settings)
    if run.config.get("execution_mode") == "independent_evaluation":
        from backend.app.independent_services import (
            require_independent_admission,
            validate_independent_inputs,
        )

        validate_independent_inputs(session, run, settings, execution=False)
        require_independent_admission(session, run)
    match = re.fullmatch(r"evaluation-([0-9a-f-]{36})-(.+)", artifact.name)
    suffix = match[2] if match else artifact.name
    evaluation = session.get(Evaluation, UUID(match[1])) if match else None
    if match and (
        evaluation is None
        or evaluation.run_id != run.id
        or evaluation.attempt_id != artifact.attempt_id
    ):
        raise DomainError(
            "result_integrity", "Artifact evaluation binding differs", 409
        )
    schema = run.config.get("schema_version")
    final = schema in {
        "tradegrad.final-evaluation.v1",
        "tradegrad.portable.final-evaluation.v1",
    }
    if evaluation is not None and suffix in {
        "result-bundle.json",
        "portable-result.json",
    }:
        if run.config.get("execution_mode") == "independent_evaluation":
            from backend.app.independent_views import (
                load_owned_independent_result as loader,
            )
        elif final:
            if schema == "tradegrad.final-evaluation.v1":
                from backend.app.final_evaluation_views import (
                    load_owned_final_result as loader,
                )
            else:
                from backend.app.portable_final_views import (
                    load_owned_final_result as loader,
                )
        elif schema == "tradegrad.portable.run.v1":
            from backend.app.portable_views import load_owned_portable_result as loader
        else:
            from backend.app.research_views import load_owned_result as loader
        result = loader(session, evaluation.id, owner_id, settings)
        if result.artifact.id != artifact.id:
            raise DomainError(
                "result_integrity", "Artifact is not the committed result", 409
            )
        record_verified_result_access(
            session, result, owner_id, purpose="result_download"
        )
        return result.content
    if evaluation is not None and suffix.startswith("trace-"):
        from backend.app.trace_services import load_owned_trace

        trace = load_owned_trace(session, evaluation.id, owner_id, settings)
        if trace.manifest is None:
            raise DomainError(
                "trace_integrity", "Trace artifact is not registered", 409
            )
        if suffix == "trace-manifest.json":
            expected = (evaluation.payload or {}).get("trace_manifest", {})
            valid = (
                expected.get("artifact_id") == str(artifact.id)
                and expected.get("sha256") == artifact.sha256
                and expected.get("size_bytes") == artifact.size_bytes
            )
        else:
            valid = any(
                ref.name == artifact.name
                and (ref.sha256, ref.size_bytes, ref.media_type)
                == (artifact.sha256, artifact.size_bytes, artifact.media_type)
                for ref in trace.manifest.artifacts
            )
        if not valid:
            raise DomainError("trace_integrity", "Trace artifact binding differs", 409)
        content = read_verified(
            settings.storage_root,
            artifact.storage_key,
            artifact.sha256,
            settings.max_artifact_bytes,
        )
        if len(content) != artifact.size_bytes:
            raise DomainError("trace_integrity", "Trace artifact size differs", 409)
        record_verified_result_access(
            session, trace.result, owner_id, purpose="result_download"
        )
        return content
    if evaluation is not None and suffix == "diagnostic-parity.json":
        from backend.app.diagnostic_parity_views import load_owned_parity
        from packages.research_trace.contracts import canonical_bytes

        report, sha = load_owned_parity(
            session, run.id, owner_id, settings, report_sha256=artifact.sha256
        )
        reference = (evaluation.payload or {}).get("diagnostic_parity", {})
        if (
            report.replay.binding.evaluation_id != evaluation.id
            or reference.get("artifact_id") != str(artifact.id)
            or reference.get("size_bytes") != artifact.size_bytes
            or reference.get("sha256") != sha
        ):
            raise DomainError(
                "diagnostic_parity_integrity",
                "Parity artifact is not the admitted physical leaf report",
                409,
            )
        return canonical_bytes(report)
    if evaluation is not None and suffix == "runtime-observation.json":
        from backend.app.independent_services import load_owned_runtime_observation
        from backend.app.trace_services import load_owned_trace

        result = load_owned_trace(session, evaluation.id, owner_id, settings).result
        value, sha = load_owned_runtime_observation(session, result, settings)
        if value is None or sha != artifact.sha256:
            raise DomainError(
                "runtime_observation_integrity",
                "Runtime observation is not the admitted leaf sidecar",
                409,
            )
        from packages.tradegrad_adapter.runtime_observation import canonical

        record_verified_result_access(
            session, result, owner_id, purpose="result_download"
        )
        return canonical(value)
    metadata_only = suffix in METADATA_NAMES | SOURCE_NAMES
    diagnostic = suffix in {
        "logs.txt",
        "stdout.log",
        "stderr.log",
        "engine.log",
        "policy.log",
    }
    if (
        (final or run.config.get("execution_mode") == "independent_evaluation")
        and not metadata_only
        and not diagnostic
    ):
        raise DomainError(
            "typed_final_result_required",
            "Use the verified final result download endpoint",
            409,
        )
    content = read_verified(
        settings.storage_root,
        artifact.storage_key,
        artifact.sha256,
        settings.max_artifact_bytes,
    )
    if metadata_only:
        validate_non_result_artifact(
            session, run, evaluation, artifact, suffix, content
        )
    else:
        record_result_access(
            session,
            run,
            owner_id,
            evaluation=evaluation,
            artifact=artifact,
            purpose="result_download",
            source="artifact",
            conservative=diagnostic,
        )
    return content
