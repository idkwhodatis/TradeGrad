"""Prepare immutable diagnostic parity before the physical leaf is committed.

The worker holds its attempt-directory guard from its fresh fence through actual
query closure and final COMMIT. Source admission is repeated at completion/read.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from backend.app.comparison_services import common_configuration
from backend.app.core.atomic_storage import publish_bytes
from backend.app.independent_services import validate_independent_inputs
from backend.app.models import Evaluation, ResearchRun
from backend.app.storage import read_verified, safe_path
from packages.research_trace.binding import make_binding
from packages.research_trace.comparison_contracts import (
    DEFAULT_LIMITS,
    ComparisonError,
    ComparisonInput,
)
from packages.research_trace.comparison_storage import TrustedTraceScan
from packages.research_trace.contracts import TraceManifest, canonical_bytes, digest
from packages.research_trace.diagnostic_parity import compare_diagnostic
from packages.research_trace.diagnostic_parity_contracts import (
    MAX_PARITY_EVENTS,
    MAX_PARITY_INPUT_BYTES,
    DiagnosticParitySide,
    ParityInput,
)
from packages.research_trace.portable import INTERNALS_MISSING
from packages.research_trace.storage import _table_ref
from packages.tradegrad_adapter.runtime_observation import (
    MAX_OBSERVATION_BYTES,
    _strict,
)

_ALLOWED_CAPTURE_GAPS = {
    INTERNALS_MISSING,
    "Submission receipts expose actual native child IDs, not final status, creation time or fills. Those are observed separately from the native execution result.",
    "Decisions outside the requested observation window were not sampled; computation was unchanged.",
    "Stages outside the requested capture scope were not sampled.",
    "Detailed observations outside the configured capture mode/scope were not sampled; the full candidate pool still determined the decision.",
    "Actual final native order states and fills are linked by emitted child IDs; intermediate order transitions are not collected.",
}


def read_parity_decisions(value, control):
    manifest = value.trace_manifest
    if manifest is None or manifest.capture.mode == "off":
        return (), ("Original or replay structured decisions were not collected.",)
    reasons = tuple(
        reason
        for reason in manifest.missing_reasons
        if reason not in _ALLOWED_CAPTURE_GAPS
    )
    if manifest.decisions_saved > MAX_PARITY_EVENTS:
        return (), (
            *reasons,
            "Structured decisions exceed the explicit diagnostic 10,000-event capacity; no event identity certification was attempted.",
        )
    # Input schema is already admitted; only the required tables are hashed/opened.
    refs = [
        ref
        for ref in manifest.artifacts
        if any(
            _table_ref(manifest, ref, table)
            for table in ("events", "decisions", "stages")
        )
    ]
    if sum(ref.size_bytes for ref in refs) > MAX_PARITY_INPUT_BYTES:
        return (), (
            *reasons,
            "Structured pre-state/target input exceeds the explicit 64 MiB diagnostic budget.",
        )
    restricted = replace(
        value, trace_manifest=manifest.model_copy(update={"artifacts": refs})
    )
    decisions, total = [], 0
    limits = DEFAULT_LIMITS.model_copy(
        update={
            "max_events": MAX_PARITY_EVENTS,
            "max_event_index_bytes": MAX_PARITY_INPUT_BYTES,
            "max_event_bytes": MAX_PARITY_INPUT_BYTES,
        }
    )
    try:
        with ExitStack() as stack, control.connection() as connection:
            scan = TrustedTraceScan(restricted, stack, control, limits)
            events = list(scan.rows(connection, "events"))
            for event in events:
                control.check()
                rows = list(
                    scan.rows(
                        connection,
                        "decisions",
                        decision_id=event["decision_id"],
                        day=event["session_date"],
                    )
                )
                if len(rows) != 1:
                    raise ValueError(
                        "Diagnostic decision header has no unique persisted state"
                    )
                row = rows[0]["payload"]
                stages = [
                    s["payload"]
                    for s in scan.rows(
                        connection,
                        "stages",
                        decision_id=event["decision_id"],
                        day=event["session_date"],
                    )
                ]
                if sorted(stage["stage_id"] for stage in stages) != sorted(
                    event["stage_ids"]
                ):
                    raise ValueError("Diagnostic stage inventory differs")
                value = {**row, "stages": stages}
                total += len(canonical_bytes(value))
                if total > MAX_PARITY_INPUT_BYTES:
                    return (), (
                        *reasons,
                        "Decoded pre-state/target input exceeds the explicit 64 MiB diagnostic budget.",
                    )
                decisions.append(value)
    except ComparisonError as error:
        if error.code != "comparison_capacity":
            raise
        return (), (
            *reasons,
            "Structured input exceeds a declared bounded query capacity; no rows were silently sampled.",
        )
    return tuple(decisions), reasons


def _record(records, name):
    found = [item for item in records if item["name"] == name]
    if len(found) != 1:
        raise ValueError("Diagnostic publication requires one exact artifact")
    return found[0]


def _pins(value, observation_sha, *, spec_revision):
    return DiagnosticParitySide(
        binding=value.binding,
        source_kind=value.source_kind,
        source_id=value.source_id,
        spec_revision=spec_revision,
        spec_sha256=value.spec_sha256,
        trace_manifest_sha256=value.trace_manifest_sha256,
        runtime_observation_sha256=observation_sha,
    )


def prepare_independent_parity_artifacts(
    *, factory, settings, config, evaluation_id, result, records, root, control
):
    if config.purpose != "diagnostic" or result.get("status") != "succeeded":
        return []
    portable = config.schema_version.startswith("tradegrad.portable.")
    economic = _record(
        records, "portable-result.json" if portable else "result-bundle.json"
    )
    trace = _record(records, "trace-manifest.json")
    runtime = _record(records, "runtime-observation.json")
    manifest = TraceManifest.model_validate_json(
        read_verified(
            settings.storage_root, trace["storage_key"], trace["sha256"], 4 * 1024**2
        )
    )
    observation = _strict(
        read_verified(
            settings.storage_root,
            runtime["storage_key"],
            runtime["sha256"],
            MAX_OBSERVATION_BYTES,
        )
    )
    with factory.begin() as session:
        evaluation = session.get(Evaluation, UUID(str(evaluation_id)))
        run = session.get(ResearchRun, evaluation.run_id)
        _, source, dataset, strategy, _ = validate_independent_inputs(
            session, run, settings
        )
        if (
            evaluation.attempt_id != run.active_attempt_id
            or evaluation.finished_at is not None
        ):
            raise ValueError("Diagnostic physical leaf is no longer pending")
        binding = make_binding(
            run=run,
            evaluation=evaluation,
            config=config,
            bundle=result["bundle"],
            result_sha256=economic["sha256"],
        )
        if binding != manifest.binding:
            raise ValueError("Diagnostic trace binds another physical leaf")
        paths = {}
        for ref in manifest.artifacts:
            record = _record(
                records, ref.name.removeprefix(f"evaluation-{evaluation_id}-")
            )
            if (record["sha256"], record["size_bytes"], record["media_type"]) != (
                ref.sha256,
                ref.size_bytes,
                ref.media_type,
            ):
                raise ValueError("Diagnostic trace artifact differs")
            paths[ref.name] = safe_path(settings.storage_root, record["storage_key"])
        comparison_source = source.admitted.input
        projected = SimpleNamespace(
            run=run, evaluation=evaluation, dataset=dataset, bundle=result["bundle"]
        )
        common = common_configuration(projected, config, comparison_source.namespace)
        new = ComparisonInput(
            binding=binding,
            source_kind="strategy_version",
            source_id=run.strategy_version_id,
            source=strategy.code,
            window=comparison_source.window,
            spec=None,
            spec_sha256=None,
            trace_manifest=manifest,
            trace_manifest_sha256=trace["sha256"],
            trace_paths=paths,
            common_config=common,
            economic_bundle=result["bundle"],
            namespace=comparison_source.namespace,
        )
        source_parameters, replay_parameters = source.parameters, strategy.parameters
        source_contract, replay_contract = source.manifest, strategy.manifest
        original_observation, original_observation_sha = (
            source.observation,
            source.observation_sha256,
        )
    left_rows, left_missing = read_parity_decisions(comparison_source, control)
    right_rows, right_missing = read_parity_decisions(new, control)

    def side(
        value, rows, missing, observed, observed_sha, parameters, contract, revision
    ):
        return ParityInput(
            pins=_pins(value, observed_sha, spec_revision=revision),
            source_code=value.source,
            bundle=value.economic_bundle,
            runtime_observation=observed,
            fixed_conditions={
                "execution": value.common_config,
                "business_parameters": parameters,
                "strategy_contract": contract,
            },
            decisions=rows,
            capture=value.trace_manifest.capture.model_dump(mode="json")
            if value.trace_manifest
            else None,
            capture_missing=missing,
        )

    report = compare_diagnostic(
        side(
            comparison_source,
            left_rows,
            left_missing,
            original_observation,
            original_observation_sha,
            source_parameters,
            source_contract,
            config.source.spec_revision,
        ),
        side(
            new,
            right_rows,
            right_missing,
            observation,
            runtime["sha256"],
            replay_parameters,
            replay_contract,
            None,
        ),
        control=control,
    )
    raw, sha = canonical_bytes(report), digest(report)
    path = Path(root) / "artifacts" / "diagnostic-parity.json"
    publish_bytes(path, raw, sha256=sha, durable_root=settings.storage_root)
    # No mutable report state is placed on ResearchRun; the physical payload binds
    # this exact registered artifact before its own immutable hash is calculated.
    return [
        {
            "name": path.name,
            "media_type": "application/json",
            "storage_key": str(path.relative_to(settings.storage_root)),
            "sha256": sha,
            "size_bytes": len(raw),
        }
    ]


def attach_independent_parity_reference(outcome, artifact, name, result):
    if name == "diagnostic-parity.json":
        outcome["diagnostic_parity"] = {
            "artifact_id": str(artifact.id),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }
