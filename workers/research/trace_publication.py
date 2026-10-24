"""Publish leaf-bound trace sidecars only after the sandbox has stopped.

The worker registers these records in the same fenced transaction as the exact
economic result. Trace bytes never pass through the result's 8 MiB JSON channel.
"""

from pathlib import Path
from uuid import UUID

from backend.app.models import Evaluation, ResearchRun
from packages.research_trace.binding import make_binding
from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.storage import write_trace_artifacts
from packages.research_trace.transport import (
    CAPTURE_FILENAME,
    CaptureSummary,
    iter_capture_records,
)


def prepare_trace_artifacts(
    *, factory, settings, config, evaluation_id, result, records, root
):
    """Return normal worker artifact records, without committing registrations.

    Old evaluators without a capture channel remain explicitly uncollected;
    partial or malformed new channels may not masquerade as missing evidence.
    """
    if result.get("status") != "succeeded":
        return []
    keys = {
        "research_capture",
        "research_capture_path",
        "research_capture_sha256",
        "research_capture_size_bytes",
    }
    present = keys.intersection(result)
    if not present:
        return []
    if present != keys:
        raise ValueError("Incomplete research capture publication envelope")
    summary = CaptureSummary.model_validate(result["research_capture"])
    source = getattr(config, "source_config", config)
    expected_capture = (
        config.research_capture
        if getattr(config, "execution_mode", None) == "independent_evaluation"
        else source.research_capture
    ) or CaptureConfig()
    portable = config.schema_version.startswith("tradegrad.portable.")
    if summary.capture != expected_capture or summary.source != (
        "engine_observed" if portable else "strategy_emitted"
    ):
        raise ValueError("Capture differs from immutable execution configuration")
    path = Path(root) / "output" / CAPTURE_FILENAME
    if Path(result["research_capture_path"]).absolute() != path.absolute():
        raise ValueError("Capture path is not the supervisor-owned output")
    result_name = "portable-result.json" if portable else "result-bundle.json"
    matches = [item for item in records if item["name"] == result_name]
    if len(matches) != 1:
        raise ValueError("Capture requires one exact economic result artifact")
    with factory() as session:
        evaluation = session.get(Evaluation, UUID(str(evaluation_id)))
        if evaluation is None:
            raise ValueError("Capture Evaluation is not registered")
        run = session.get(ResearchRun, evaluation.run_id)
        binding = make_binding(
            run=run,
            evaluation=evaluation,
            config=config,
            bundle=result["bundle"],
            result_sha256=matches[0]["sha256"],
            candidate_id=evaluation.candidate_id,
        )
    stream = iter_capture_records(
        path,
        result["research_capture_sha256"],
        result["research_capture_size_bytes"],
        expected_capture,
    )
    _, written = write_trace_artifacts(
        stream,
        binding,
        summary.model_dump(mode="json"),
        Path(root) / "artifacts",
        durable_root=settings.storage_root,
    )
    prefix = f"evaluation-{evaluation_id}-"
    return [
        {
            "name": item.name.removeprefix(prefix),
            "media_type": item.media_type,
            "storage_key": str(item.path.relative_to(settings.storage_root)),
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in written
    ]


def attach_trace_reference(outcome, artifact, name, result):
    """Called before completion hashes the payload, inside its fenced transaction."""
    if name == "trace-manifest.json":
        outcome["trace_manifest"] = {
            "artifact_id": str(artifact.id),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }
        outcome["research_capture"] = CaptureSummary.model_validate(
            result["research_capture"]
        ).model_dump(mode="json")
