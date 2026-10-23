"""M3 research transactions. Filesystem completion never implies DB publication.

Every worker write uses the existing lease/generation fence. Caller owns commit.
Only an explicit, complete, verified DB checkpoint can be selected for resume.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.config import Settings
from backend.app.models import (
    Artifact,
    Candidate,
    DatasetVersion,
    Evaluation,
    ExecutionAttempt,
    ModelCall,
    OutboxEvent,
    ResearchCheckpoint,
    ResearchRun,
    RunEvent,
    StrategyVersion,
    UniverseVersion,
    utcnow,
)
from backend.app.services import (
    TERMINAL,
    DomainError,
    advisory_lock,
    append_event,
    fenced_attempt,
    owned_run,
)
from backend.app.storage import read_verified, redact, safe_path
from packages.tradegrad_adapter.checkpoint import CheckpointRef, CheckpointStore
from packages.tradegrad_adapter.config import RunConfig, evaluation_windows

MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_CODE_BYTES = 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def bounded_json(value, max_bytes=MAX_PAYLOAD_BYTES):
    """Reject non-JSON, nonfinite, excessively deep/wide or unbounded payloads."""
    count = 0

    def visit(item, depth=0):
        nonlocal count
        count += 1
        if depth > 32 or count > 100_000:
            raise ValueError("Payload nesting or item budget exceeded")
        if item is None or isinstance(item, (str, bool)):
            return
        if isinstance(item, int):
            if abs(item) > 2**63 - 1:
                raise ValueError("Integer exceeds signed 64-bit bounds")
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("Nonfinite payload number")
            return
        if isinstance(item, dict):
            if not all(isinstance(key, str) and len(key) <= 500 for key in item):
                raise ValueError("Invalid JSON object key")
            for member in item.values():
                visit(member, depth + 1)
            return
        if isinstance(item, list):
            for member in item:
                visit(member, depth + 1)
            return
        raise ValueError("Payload must contain JSON primitives only")

    try:
        visit(value)
        content = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if len(content) > max_bytes:
            raise ValueError("Payload byte budget exceeded")
        return content
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        raise DomainError("invalid_research_payload", str(error), 422) from None


def payload_hash(value):
    return hashlib.sha256(bounded_json(value)).hexdigest()


def _sha(value):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise DomainError("invalid_research_payload", "Expected lowercase SHA-256", 422)
    return value


def _key(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise DomainError("invalid_research_payload", "Invalid logical identifier", 422)
    return value


def research_config(run):
    try:
        return RunConfig.model_validate(run.config)
    except ValueError:
        raise DomainError(
            "invalid_research_config",
            "Run does not have a compatible immutable research configuration",
            409,
        ) from None


def execution_config(run):
    """Strict family dispatch for shared checkpoint persistence, not native APIs."""
    if run.config.get("schema_version") == "tradegrad.portable.run.v1":
        from backend.app.portable_services import portable_config

        return portable_config(run)
    return research_config(run)


def execution_windows(config):
    if config.schema_version == "tradegrad.portable.run.v1":
        from packages.tradegrad_adapter.portable_config import (
            portable_evaluation_windows,
        )

        return portable_evaluation_windows(config)
    return evaluation_windows(config)


def owned_research_run(session, run_id, owner_id, lock=False):
    run = owned_run(session, run_id, owner_id, lock)
    research_config(run)
    return run


def _reference(session, model, reference, owner_id):
    try:
        identifier = UUID(reference.id)
    except ValueError:
        raise DomainError(
            "invalid_reference", "Version references must be UUIDs", 422
        ) from None
    item = session.scalar(
        select(model).where(model.id == identifier, model.owner_id == owner_id)
    )
    if item is None:
        raise DomainError("not_found", "Referenced version not found", 404)
    if item.content_sha256 != reference.sha256:
        raise DomainError(
            "reference_hash_mismatch",
            "Reference hash does not match immutable owned version",
            422,
        )
    return item


def validate_model_selection(model, settings, owner_id):
    from packages.tradegrad_adapter.model_profiles import (
        ModelProfileError,
        resolve_profile,
    )

    try:
        return resolve_profile(model, settings, owner_id)
    except ModelProfileError as error:
        raise DomainError(
            error.code,
            "Operator model profile is unavailable or changed; select a currently admitted profile",
            422,
        ) from None


def validate_research_submission(session, owner_id, body, settings):
    """Shared read-only preflight. No locks, quota reservation, or writes."""
    from packages.tradegrad_adapter.environment import environment_digest

    if body.execution_mode != "optimization":
        raise DomainError(
            "unsupported_research_mode",
            "Use legacy /runs for seed evaluation, or optimization with max_iterations=0 for a configured seed-only research run",
            422,
        )
    if body.environment_digest != environment_digest():
        raise DomainError(
            "environment_mismatch",
            "Research environment digest differs from this deployment",
            422,
        )
    if body.prompt_version != "source_prompts.v1":
        raise DomainError(
            "research_capability_unavailable",
            "Unsupported research prompt version",
            422,
        )
    validate_model_selection(body.model, settings, owner_id)
    dataset = _reference(session, DatasetVersion, body.dataset, owner_id)
    strategy = _reference(session, StrategyVersion, body.seed_strategy, owner_id)
    universe = _reference(session, UniverseVersion, body.universe, owner_id)
    if (
        dataset.market != body.market_profile.market
        or strategy.task != body.task
        or strategy.mode != "native_akquant"
    ):
        raise DomainError(
            "reference_mismatch",
            "Dataset market, strategy and research task must agree",
            422,
        )
    if hashlib.sha256(strategy.code.encode()).hexdigest() != strategy.content_sha256:
        raise DomainError(
            "reference_hash_mismatch",
            "Seed strategy bytes do not match immutable hash",
            422,
        )
    for _split, window in body.windows.items():  # noqa: PERF102 (WindowSet is not a dict)
        if window.start < dataset.start_date or window.end > dataset.end_date:
            raise DomainError(
                "coverage_error",
                "Configured windows lie outside immutable dataset coverage",
                422,
            )
    pinned = dataset.manifest.get("universe", {})
    if pinned.get("id") and (
        pinned["id"] != str(universe.id)
        or pinned.get("content_sha256") != universe.content_sha256
    ):
        raise DomainError(
            "universe_mismatch", "Dataset pins a different universe version", 422
        )
    listings = {str(row["listing_id"]) for row in universe.members}
    snapshot_listings = {
        str(row["listing_id"]) for row in dataset.manifest.get("listings", [])
    }
    if not listings or snapshot_listings != listings:
        raise DomainError(
            "universe_mismatch", "Universe and dataset listings must match exactly", 422
        )
    from packages.tradegrad_adapter.windows import preflight_dataset

    hashes = dataset.manifest.get("files", {}) | dataset.manifest.get(
        "artifact_files", {}
    )

    def read_file(name):
        if name not in hashes:
            raise DomainError(
                "invalid_dataset_file",
                "Dataset file is absent from immutable manifest",
                422,
            )
        return read_verified(
            settings.storage_root,
            f"{dataset.storage_key}/{name}",
            hashes[name],
            settings.max_artifact_bytes,
        )

    reports = []
    try:
        # Holdout is metadata-only: no holdout evaluation, metrics or selection in M3.
        for window in evaluation_windows(body):
            reports.append(
                preflight_dataset(
                    body,
                    window["split"],
                    dataset.manifest,
                    read_file,
                    evaluation_window=window,
                )
            )
    except DomainError:
        raise
    except (ValueError, KeyError, TypeError) as error:
        raise DomainError(
            "research_preflight_failed", redact(str(error))[:2000], 422
        ) from None
    return dataset, strategy, universe, reports


def submit_research_run(
    session: Session,
    owner_id: UUID,
    body: RunConfig,
    idempotency_key: str,
    settings: Settings,
    *,
    experiment_id: UUID | None = None,
    split_version_id: UUID | None = None,
):
    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key)
    ):
        raise DomainError(
            "invalid_idempotency_key",
            "Idempotency-Key must be 1 to 128 printable ASCII characters without spaces",
            422,
        )
    advisory_lock(session, f"submit:{owner_id}")
    previous = session.scalar(
        select(ResearchRun).where(
            ResearchRun.owner_id == owner_id,
            ResearchRun.idempotency_key == idempotency_key,
        )
    )
    binding = {
        "experiment_id": str(experiment_id) if experiment_id else None,
        "split_version_id": str(split_version_id) if split_version_id else None,
    }
    if previous:
        saved_binding = previous.submission_binding or {
            "experiment_id": None,
            "split_version_id": None,
        }
        if previous.request_sha256 != body.sha256 or saved_binding != binding:
            raise DomainError(
                "idempotency_conflict",
                "This Idempotency-Key was already used with a different request",
            )
        return previous
    dataset, strategy, _universe, _reports = validate_research_submission(
        session, owner_id, body, settings
    )
    active = session.scalar(
        select(func.count())
        .select_from(ResearchRun)
        .where(ResearchRun.owner_id == owner_id, ResearchRun.status.not_in(TERMINAL))
    )
    if active >= settings.max_active_runs_per_user:
        raise DomainError("run_quota", "Active run limit reached", 429)
    from backend.app.experiment_services import (
        bind_research_submission,
        record_research_exposure,
    )

    experiment, split = bind_research_submission(
        session,
        owner_id,
        body,
        dataset,
        _universe,
        experiment_id=experiment_id,
        split_version_id=split_version_id,
    )
    run = ResearchRun(
        id=uuid4(),
        owner_id=owner_id,
        idempotency_key=idempotency_key,
        request_sha256=body.sha256,
        config=body.model_dump(mode="json"),
        experiment_id=experiment.id,
        split_version_id=split.id,
        submission_binding=binding,
        dataset_version_id=dataset.id,
        strategy_version_id=strategy.id,
        status="queued",
        generation=1,
        next_event_seq=0,
    )
    session.add(run)
    session.flush()
    record_research_exposure(session, run, dataset, _universe, body)
    session.add(OutboxEvent(run_id=run.id, generation=1))
    append_event(
        session,
        run,
        "stage_changed",
        {
            "status": "queued",
            "execution_mode": body.execution_mode,
            "config_sha256": body.sha256,
            "is_synthetic": dataset.is_synthetic,
        },
    )
    session.flush()
    return run


def _validated_window(run, window):
    config = research_config(run)
    bounded_json(window, 4096)
    if (
        not isinstance(window, dict)
        or set(window) != {"split", "start", "end"}
        or window["split"] not in {"train", "validation"}
    ):
        raise DomainError(
            "invalid_evaluation_window",
            "Only exact configured train/validation windows may execute",
            422,
        )
    if window not in evaluation_windows(config):
        raise DomainError(
            "invalid_evaluation_window",
            "Evaluation window differs from deterministic immutable research plan",
            422,
        )
    return window


def begin_evaluation(
    session, attempt_id, generation, lease_token, *, candidate_key, code_sha256, window
):
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    if run.status == "cancel_requested":
        raise DomainError(
            "cancel_requested", "Cancellation prevents launching another sandbox"
        )
    _validated_window(run, window)
    if run.research_image_digest is None:
        raise DomainError(
            "image_digest_required",
            "Pin the verified evaluator image before registering a sandbox",
        )
    identifier = uuid4()
    item = Evaluation(
        id=identifier,
        run_id=run.id,
        attempt_id=attempt.id,
        generation=generation,
        candidate_key=_key(candidate_key),
        code_sha256=_sha(code_sha256),
        split=window["split"],
        window=window,
        window_sha256=payload_hash(window),
        image_digest=run.research_image_digest,
        sandbox_id=f"tradegrad-{identifier}",
        sandbox_stopped=False,
        status="registered",
    )
    session.add(item)
    session.flush()
    return item


def complete_evaluation(
    session,
    attempt_id,
    generation,
    lease_token,
    evaluation_id,
    *,
    outcome,
    sandbox_stopped,
):
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    item = session.scalar(
        select(Evaluation)
        .where(
            Evaluation.id == evaluation_id,
            Evaluation.run_id == run.id,
            Evaluation.attempt_id == attempt.id,
        )
        .with_for_update()
    )
    if not item:
        raise DomainError("not_found", "Registered evaluation not found", 404)
    encoded = bounded_json(outcome)
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if item.finished_at is not None:
        if (
            item.payload_sha256 != fingerprint
            or item.sandbox_stopped != sandbox_stopped
        ):
            raise DomainError(
                "immutable_evaluation", "Completed evaluation cannot be changed"
            )
        return item
    if not sandbox_stopped:
        item.error = "Sandbox exit could not be confirmed; recovery is blocked"
        append_event(
            session,
            run,
            "recovery_blocked",
            {"evaluation_id": str(item.id), "message": item.error},
            attempt.id,
        )
        return item
    if not isinstance(outcome, dict):
        raise DomainError(
            "invalid_research_payload", "Evaluation outcome must be an object", 422
        )
    status = outcome.get("status", "failed")
    if status not in {"succeeded", "failed", "cancelled", "interrupted"}:
        status = "failed"
    metrics = outcome.get("metrics")
    numbers = metrics.get("metrics", metrics) if isinstance(metrics, dict) else None
    score = outcome.get(
        "combined_score",
        metrics.get("combined_score") if isinstance(metrics, dict) else None,
    )
    required_metrics = {
        "sharpe_ratio",
        "annualized_return",
        "max_drawdown",
        "trade_per_day",
        "win_rate",
        "return_per_trade",
    }
    bundle_artifact = None
    reference = outcome.get("result_bundle")
    if isinstance(reference, dict):
        try:
            bundle_id = UUID(str(reference["artifact_id"]))
        except (KeyError, TypeError, ValueError):
            pass
        else:
            session.flush()
            bundle_artifact = session.get(Artifact, bundle_id)
    # The trusted runner validates and hash-publishes native V2 bytes. This
    # transaction additionally requires that exact durable artifact registration;
    # metrics alone can never manufacture successful output provenance.
    bound_bundle_artifact = (
        bundle_artifact is not None
        and bundle_artifact.run_id == run.id
        and bundle_artifact.attempt_id == attempt.id
        and bundle_artifact.name == f"evaluation-{item.id}-result-bundle.json"
        and bundle_artifact.media_type == "application/json"
        and bundle_artifact.sha256 == reference.get("sha256")
        and bundle_artifact.size_bytes == reference.get("size_bytes")
        and 0 < bundle_artifact.size_bytes <= 8 * 1_048_576
    )
    if status == "succeeded" and (
        not bound_bundle_artifact
        or outcome.get("image_digest") != item.image_digest
        or outcome.get("exit_code") != 0
        or not isinstance(numbers, dict)
        or set(numbers) != required_metrics
        or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in numbers.values()
        )
        or numbers["trade_per_day"] <= 0
        or set(metrics)
        - {"combined_score", "metrics", "market", "profile", "start_time", "end_time"}
        or isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
    ):
        status = "failed"
    item.status = status
    item.payload = json.loads(encoded)
    item.payload_sha256 = fingerprint
    item.error = (
        redact(str(outcome.get("error")))[:4000]
        if outcome.get("error")
        else (
            "Evaluator returned incomplete result evidence or invalid metrics"
            if status != outcome.get("status")
            else None
        )
    )
    item.sandbox_stopped = True
    item.finished_at = utcnow()
    append_event(
        session,
        run,
        "evaluation_completed",
        {
            "evaluation_id": str(item.id),
            "candidate_key": item.candidate_key,
            "split": item.split,
            "status": item.status,
            "payload_sha256": fingerprint,
        },
        attempt.id,
    )
    session.flush()
    return item


def _candidate(session, run, attempt, row):
    key = _key(row["candidate_id"])
    code = row["code"]
    if not isinstance(code, str) or not code or len(code.encode()) > MAX_CODE_BYTES:
        raise DomainError(
            "invalid_candidate", "Candidate code exceeds bounds or is empty", 422
        )
    sha = hashlib.sha256(code.encode()).hexdigest()
    if sha != row["code_sha256"]:
        raise DomainError("candidate_integrity", "Candidate code hash mismatch")
    parent = None
    if row.get("parent_id"):
        parent = session.scalar(
            select(Candidate).where(
                Candidate.run_id == run.id, Candidate.candidate_key == row["parent_id"]
            )
        )
        if not parent:
            raise DomainError(
                "candidate_lineage", "Candidate parent must already exist in this run"
            )
    values = {
        "candidate_key": key,
        "parent_id": parent.id if parent else None,
        "kind": row["kind"],
        "iteration": row["iteration"],
        "repair_index": row["repair_index"],
        "code": code,
        "code_sha256": sha,
    }
    if row["kind"] not in {"seed", "generated", "repair"} or any(
        type(values[k]) is not int or not 0 <= values[k] <= 10000
        for k in ("iteration", "repair_index")
    ):
        raise DomainError(
            "invalid_candidate", "Invalid candidate version metadata", 422
        )
    if (row["kind"] == "repair" and (parent is None or row["repair_index"] < 1)) or (
        row["kind"] == "seed" and parent is not None
    ):
        raise DomainError("candidate_lineage", "Invalid seed/repair ancestry")
    if row["kind"] == "seed" and (row["iteration"] != 0 or row["repair_index"] != 0):
        raise DomainError(
            "candidate_lineage", "Seed must be the initial unrepaired version"
        )
    if row["kind"] == "repair" and (
        row["iteration"] != parent.iteration
        or row["repair_index"] != parent.repair_index + 1
    ):
        raise DomainError(
            "candidate_lineage",
            "Repair must advance exactly one version of its same-iteration parent",
        )
    if row["kind"] == "generated" and (
        parent is None
        or row["repair_index"] != 0
        or row["iteration"] <= parent.iteration
    ):
        raise DomainError(
            "candidate_lineage",
            "Generated candidate must descend from an earlier iteration",
        )
    item = session.scalar(
        select(Candidate).where(
            Candidate.run_id == run.id, Candidate.candidate_key == key
        )
    )
    if item:
        if any(getattr(item, key) != value for key, value in values.items()):
            raise DomainError(
                "immutable_candidate", "Candidate version cannot be overwritten"
            )
        return item
    item = Candidate(
        id=uuid4(),
        run_id=run.id,
        attempt_id=attempt.id,
        generation=attempt.generation,
        **values,
    )
    session.add(item)
    session.flush()
    append_event(
        session,
        run,
        "candidate_created",
        {
            "candidate_id": str(item.id),
            "candidate_key": key,
            "parent_id": str(item.parent_id) if item.parent_id else None,
            "kind": item.kind,
            "code_sha256": sha,
        },
        attempt.id,
    )
    return item


def _model_call(session, run, attempt, row, checkpoint_key, manifest):
    config = execution_config(run)
    if (
        row.get("provider") != config.model.provider
        or row.get("model") != config.model.model
        or row.get("prompt_version") != config.prompt_version
    ):
        raise DomainError(
            "model_call_provenance_mismatch",
            "Model/provider/prompt must match the immutable research configuration",
        )
    if (
        not isinstance(row.get("parameters"), dict)
        or row["parameters"].get("temperature") != config.model.temperature
    ):
        raise DomainError(
            "model_call_provenance_mismatch",
            "Model parameters differ from the immutable research configuration",
        )
    encoded = bounded_json(row)
    key = _key(row["call_id"])
    if row.get("status") not in {"started", "completed", "failed"}:
        raise DomainError("invalid_model_call", "Invalid model call status", 422)
    response = row.get("response")
    response_sha = (
        hashlib.sha256(response.encode()).hexdigest()
        if isinstance(response, str)
        else None
    )
    usage = {
        key: row.get(key)
        for key in (
            "input_tokens",
            "output_tokens",
            "charged_tokens",
            "usage_source",
            "price",
        )
    }
    for name in ("input_tokens", "output_tokens", "charged_tokens"):
        if usage[name] is not None and (
            type(usage[name]) is not int or not 0 <= usage[name] <= 10**9
        ):
            raise DomainError("invalid_model_call", "Invalid model token usage", 422)
    latency = row.get("latency_seconds") or 0.0
    if (
        isinstance(latency, bool)
        or not isinstance(latency, (int, float))
        or not math.isfinite(latency)
        or not 0 <= latency <= 86400 * 7
    ):
        raise DomainError("invalid_model_call", "Invalid model latency", 422)
    # This record identifies the verified immutable completed_calls artifact, not a host path.
    inventory = next(
        entry for entry in manifest["files"] if entry["path"] == "completed_calls.json"
    )
    response_artifact = (
        {
            "checkpoint_manifest_sha256": hashlib.sha256(
                bounded_json(manifest)
            ).hexdigest(),
            "name": "completed_calls.json",
            "sha256": inventory["sha256"],
            "size_bytes": inventory["size_bytes"],
            "logical_call_id": key,
        }
        if response is not None
        else None
    )
    item = session.scalar(
        select(ModelCall).where(
            ModelCall.run_id == run.id, ModelCall.logical_call_id == key
        )
    )
    clean = json.loads(encoded)
    if item:
        prior, current = dict(item.payload), dict(clean)
        prior_adopted, current_adopted = (
            prior.pop("adopted", False),
            current.pop("adopted", False),
        )
        if (
            item.request_sha256 != row["request_sha256"]
            or prior_adopted
            and not current_adopted
        ):
            raise DomainError(
                "immutable_model_call",
                "Logical model call identity or adoption changed",
            )
        if item.status != "started" and prior != current:
            raise DomainError(
                "immutable_model_call",
                "Completed model response and usage cannot change",
            )
        if prior == current and prior_adopted == current_adopted:
            return item
    else:
        item = ModelCall(
            id=uuid4(),
            run_id=run.id,
            attempt_id=attempt.id,
            generation=attempt.generation,
            logical_call_id=key,
        )
        session.add(item)
    if len(row["provider"]) > 40 or (
        response is not None
        and (not isinstance(response, str) or len(response.encode()) > MAX_CODE_BYTES)
    ):
        raise DomainError(
            "invalid_model_call", "Model provider or response exceeds bounds", 422
        )
    item.provider = _key(row["provider"])
    item.model = _key(row["model"])
    item.prompt_version = _key(row["prompt_version"])
    item.status = row["status"]
    item.adopted = row.get("adopted") is True
    item.request_sha256 = _sha(row["request_sha256"])
    item.response_sha256 = response_sha
    if item.response_artifact is None:
        item.response_artifact = response_artifact
    item.usage = usage
    item.latency_seconds = latency
    item.error_category = (
        str(row["error_class"])[:80] if row.get("error_class") else None
    )
    item.payload = clean
    item.payload_sha256 = hashlib.sha256(encoded).hexdigest()
    session.flush()
    return item


def checkpoint_reference(checkpoint, settings):
    return CheckpointRef(
        str(safe_path(settings.storage_root, checkpoint.manifest_storage_key)),
        checkpoint.manifest_sha256,
        checkpoint.input_digest,
        checkpoint.schema_version,
    )


def _load_checkpoint(reference, config, settings, *, run_id, attempt_id):
    try:
        ref = CheckpointRef.from_value(reference)
        path = Path(ref.manifest_path)
        root = settings.storage_root.resolve()
        expected = (
            root / "runs" / str(run_id) / "attempts" / str(attempt_id) / "checkpoints"
        )
        if (
            not path.is_absolute()
            or not path.is_relative_to(expected)
            or len(path.relative_to(expected).parts) != 2
        ):
            raise ValueError("Checkpoint is outside its exact run/attempt namespace")
        key = str(path.relative_to(root))
        safe_path(root, key)
        state = CheckpointStore.load(ref, expected_input_digest=ref.input_digest)
        if state["inputs"]["config"] != config.model_dump(mode="json"):
            raise ValueError("Checkpoint config differs from immutable RunConfig")
        if state["inputs"]["initial_code_sha256"] != config.seed_strategy.sha256:
            raise ValueError("Checkpoint seed differs from immutable RunConfig")
        windows = list(execution_windows(config))
        if state["inputs"]["windows"] != windows:
            raise ValueError("Checkpoint windows differ or expose final holdout")
        manifest = json.loads(
            read_verified(root, key, ref.manifest_sha256, 1024 * 1024)
        )
        return ref, key, state, manifest
    except DomainError:
        raise
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise DomainError(
            "checkpoint_invalid", redact(str(error))[:2000], 409
        ) from None


def publish_checkpoint(
    session, attempt_id, generation, lease_token, reference, settings
):
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    config = execution_config(run)
    ref, key, state, manifest = _load_checkpoint(
        reference, config, settings, run_id=run.id, attempt_id=attempt.id
    )
    previous = session.scalar(
        select(ResearchCheckpoint).where(
            ResearchCheckpoint.run_id == run.id,
            ResearchCheckpoint.attempt_id == attempt.id,
            ResearchCheckpoint.manifest_sha256 == ref.manifest_sha256,
        )
    )
    if previous:
        return previous
    if (
        len(state.get("candidates", [])) > 22000
        or len(state.get("model_calls", [])) > 10000
    ):
        raise DomainError(
            "invalid_checkpoint", "Checkpoint record budget exceeded", 422
        )
    summary = {"stop_reason": state.get("stop_reason"), "candidates": {}}
    planned_windows = list(execution_windows(config))
    for row in state["candidates"]:
        item = _candidate(session, run, attempt, row)
        evaluation = row.get("evaluation") or {}
        physical_records = []
        for outcome in row.get("window_results", evaluation.get("windows", [])):
            identifier = outcome.get("evaluation_id")
            if identifier is None:
                raise DomainError(
                    "evaluation_provenance_missing",
                    "Checkpoint evaluation lacks its registered sandbox identity",
                )
            try:
                identifier = UUID(identifier)
            except (ValueError, TypeError):
                raise DomainError(
                    "invalid_evaluation",
                    "Invalid checkpoint evaluation identifier",
                    422,
                ) from None
            physical = session.scalar(
                select(Evaluation).where(
                    Evaluation.id == identifier, Evaluation.run_id == run.id
                )
            )
            if (
                not physical
                or physical.candidate_key != item.candidate_key
                or physical.code_sha256 != item.code_sha256
                or not physical.sandbox_stopped
                or physical.finished_at is None
                or physical.image_digest != run.research_image_digest
            ):
                raise DomainError(
                    "evaluation_provenance_mismatch",
                    "Checkpoint refers to an unverified or foreign evaluation",
                )
            if (
                outcome.get("window") != physical.window
                or outcome.get("split") != physical.split
            ):
                raise DomainError(
                    "evaluation_provenance_mismatch",
                    "Checkpoint evaluation window differs from registered provenance",
                )
            persisted = physical.payload or {}
            if outcome.get("metrics") != persisted.get("metrics"):
                raise DomainError(
                    "evaluation_provenance_mismatch",
                    "Checkpoint evaluation metrics differ from the immutable result",
                )
            envelope = persisted.get("metrics")
            score = persisted.get(
                "combined_score",
                envelope.get("combined_score") if isinstance(envelope, dict) else None,
            )
            if outcome.get("valid") and (
                physical.status != "succeeded" or outcome.get("combined_score") != score
            ):
                raise DomainError(
                    "evaluation_provenance_mismatch",
                    "Checkpoint claims success without a successful registered evaluation",
                )
            physical_records.append(physical)
            # A resumed checkpoint may contain previous-generation completed evaluations.
            if physical.candidate_id is None:
                physical.candidate_id = item.id
            elif physical.candidate_id != item.id:
                raise DomainError(
                    "evaluation_provenance_mismatch",
                    "Evaluation is bound to another candidate",
                )
        actual_windows = [physical.window for physical in physical_records]
        if actual_windows != planned_windows[: len(actual_windows)] or len(
            actual_windows
        ) > len(planned_windows):
            raise DomainError(
                "evaluation_provenance_mismatch",
                "Checkpoint outcomes must match the ordered deterministic evaluation plan",
            )
        complete = actual_windows == planned_windows
        valid = complete and all(
            physical.status == "succeeded" for physical in physical_records
        )
        train_scores = []
        for physical in physical_records:
            if physical.split == "train":
                payload = physical.payload or {}
                envelope = payload.get("metrics")
                train_scores.append(
                    payload.get(
                        "combined_score",
                        envelope.get("combined_score")
                        if isinstance(envelope, dict)
                        else None,
                    )
                )
        aggregate = None
        if valid and train_scores:
            count = max(1, math.ceil(len(train_scores) * config.optimizer.worst_ratio))
            aggregate = round(sum(sorted(train_scores)[:count]) / count, 2)
        if row.get("valid") and (
            not valid or evaluation.get("combined_score") != aggregate
        ):
            raise DomainError(
                "evaluation_provenance_mismatch",
                "Candidate validity or objective differs from complete verified evaluations",
            )
        summary["candidates"][item.candidate_key] = {
            "id": str(item.id),
            "evaluation_ids": [str(physical.id) for physical in physical_records],
            "complete": complete,
            "valid": valid and bool(row.get("valid")),
            "combined_score": aggregate,
        }
    bounded_json(summary)
    for row in state["model_calls"]:
        if row.get("input_digest") != ref.input_digest:
            raise DomainError(
                "model_call_provenance_mismatch",
                "Model call belongs to different immutable inputs",
            )
        _model_call(session, run, attempt, row, key, manifest)
    sequence = (
        session.scalar(
            select(func.max(ResearchCheckpoint.sequence)).where(
                ResearchCheckpoint.run_id == run.id
            )
        )
        or 0
    ) + 1
    record = ResearchCheckpoint(
        id=uuid4(),
        run_id=run.id,
        attempt_id=attempt.id,
        generation=generation,
        sequence=sequence,
        schema_version=ref.schema_version,
        manifest_storage_key=key,
        manifest_sha256=ref.manifest_sha256,
        input_digest=ref.input_digest,
        config_sha256=config.sha256,
        summary=summary,
        next_stage=_key(state["next_stage"]),
        iteration=state["iteration"],
        complete=True,
    )
    session.add(record)
    session.flush()
    run.latest_checkpoint_id = record.id
    append_event(
        session,
        run,
        "checkpoint_committed",
        {
            "checkpoint_id": str(record.id),
            "sequence": sequence,
            "manifest_sha256": record.manifest_sha256,
            "next_stage": record.next_stage,
        },
        attempt.id,
    )
    session.flush()
    return record


def latest_checkpoint_for_resume(session, run, settings):
    if not run.latest_checkpoint_id:
        raise DomainError(
            "checkpoint_required",
            "No complete database-published checkpoint is available for resume",
        )
    checkpoint = session.scalar(
        select(ResearchCheckpoint).where(
            ResearchCheckpoint.id == run.latest_checkpoint_id,
            ResearchCheckpoint.run_id == run.id,
            ResearchCheckpoint.complete.is_(True),
        )
    )
    if not checkpoint:
        raise DomainError("checkpoint_invalid", "Latest checkpoint pointer is invalid")
    ref = checkpoint_reference(checkpoint, settings)
    _load_checkpoint(
        ref,
        execution_config(run),
        settings,
        run_id=run.id,
        attempt_id=checkpoint.attempt_id,
    )
    return ref


def request_pause(session, run_id, owner_id):
    run = owned_research_run(session, run_id, owner_id, lock=True)
    if run.status in {"pause_requested", "paused"}:
        return run
    if run.status not in {"queued", "preparing", "running"}:
        raise DomainError(
            "invalid_transition",
            "Only queued, preparing or running research can be paused",
        )
    # A queued attempt still claims its outbox job and publishes an initial
    # complete checkpoint before fulfilling this pause request.
    run.status = "pause_requested"
    append_event(
        session, run, "stage_changed", {"status": run.status}, run.active_attempt_id
    )
    return run


def resume_research_run(session, run_id, owner_id, settings):
    advisory_lock(session, f"submit:{owner_id}")
    run = owned_research_run(session, run_id, owner_id, lock=True)
    if run.status not in {"paused", "failed", "cancelled", "interrupted"}:
        raise DomainError(
            "invalid_transition",
            "Only paused, failed, cancelled or interrupted research can resume",
        )
    if run.active_attempt_id:
        attempt = session.get(ExecutionAttempt, run.active_attempt_id)
        if attempt is None or not attempt.sandbox_stopped:
            raise DomainError(
                "sandbox_not_stopped",
                "Previous sandbox must be confirmed stopped before resume",
            )
    if session.scalar(
        select(Evaluation.id)
        .where(Evaluation.run_id == run.id, Evaluation.sandbox_stopped.is_(False))
        .limit(1)
    ):
        raise DomainError(
            "sandbox_not_stopped",
            "Every evaluation sandbox must be confirmed stopped before resume",
        )
    validate_model_selection(research_config(run).model, settings, owner_id)
    latest_checkpoint_for_resume(session, run, settings)
    active = session.scalar(
        select(func.count())
        .select_from(ResearchRun)
        .where(ResearchRun.owner_id == owner_id, ResearchRun.status.not_in(TERMINAL))
    )
    if active >= settings.max_active_runs_per_user:
        raise DomainError("run_quota", "Active run limit reached", 429)
    run.resume_checkpoint_id = run.latest_checkpoint_id
    run.generation += 1
    run.active_attempt_id = None
    run.status, run.error, run.result = "queued", None, None
    session.add(OutboxEvent(run_id=run.id, generation=run.generation))
    append_event(
        session,
        run,
        "stage_changed",
        {
            "status": "queued",
            "generation": run.generation,
            "resume_mode": "verified_checkpoint",
            "checkpoint_id": str(run.resume_checkpoint_id),
        },
    )
    return run


def finish_research_attempt(
    session,
    attempt_id,
    generation,
    lease_token,
    outcome,
    sandbox_stopped,
    artifacts=None,
):
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    config = execution_config(run)
    bounded_json(outcome)
    if not isinstance(outcome, dict):
        raise DomainError(
            "invalid_research_payload", "Research outcome must be an object", 422
        )
    outstanding = session.scalar(
        select(Evaluation.id)
        .where(
            Evaluation.attempt_id == attempt.id, Evaluation.sandbox_stopped.is_(False)
        )
        .limit(1)
    )
    if config.model.provider != "fixture":
        from backend.app.model_containment import model_processes_confirmed_stopped

        if not model_processes_confirmed_stopped(session, attempt.id):
            run.error = attempt.error = (
                "Recovery blocked: model process termination receipt is unconfirmed"
            )
            append_event(
                session, run, "recovery_blocked", {"message": run.error}, attempt.id
            )
            return run
    if not sandbox_stopped or outstanding:
        run.error = attempt.error = (
            "Recovery blocked: an evaluation sandbox termination is unconfirmed"
        )
        append_event(
            session, run, "recovery_blocked", {"message": run.error}, attempt.id
        )
        return run
    status = outcome.get("status", "failed")
    if run.status == "cancel_requested":
        status = "cancelled"
    if status == "paused" and (
        run.status != "pause_requested" or not run.latest_checkpoint_id
    ):
        raise DomainError(
            "checkpoint_required",
            "Pause requires a published complete checkpoint and a pending pause request",
        )
    if status not in TERMINAL:
        status = "failed"
    best_key = outcome.get("best_candidate_key") or outcome.get("best_candidate_id")
    best = (
        session.scalar(
            select(Candidate).where(
                Candidate.run_id == run.id, Candidate.candidate_key == best_key
            )
        )
        if best_key
        else None
    )
    score = outcome.get("combined_score")
    stop_reason = str(
        outcome.get("stop_reason") or ("paused" if status == "paused" else "unknown")
    )[:160]
    checkpoint = (
        session.scalar(
            select(ResearchCheckpoint).where(
                ResearchCheckpoint.id == run.latest_checkpoint_id,
                ResearchCheckpoint.run_id == run.id,
                ResearchCheckpoint.attempt_id == attempt.id,
                ResearchCheckpoint.complete.is_(True),
            )
        )
        if run.latest_checkpoint_id
        else None
    )
    recorded_best = (
        checkpoint.summary.get("candidates", {}).get(best_key, {}) if checkpoint else {}
    )
    valid_best = bool(
        best
        and recorded_best.get("id") == str(best.id)
        and recorded_best.get("valid")
        and recorded_best.get("complete")
        and recorded_best.get("combined_score") == score
    )
    if status == "paused" and checkpoint is None:
        raise DomainError(
            "checkpoint_required",
            "Pause requires a checkpoint published by this active attempt",
        )
    if status == "paused":
        pause_seq = session.scalar(
            select(func.max(RunEvent.seq)).where(
                RunEvent.run_id == run.id,
                RunEvent.type == "stage_changed",
                RunEvent.payload["status"].astext == "pause_requested",
            )
        )
        committed_seq = session.scalar(
            select(func.max(RunEvent.seq)).where(
                RunEvent.run_id == run.id,
                RunEvent.type == "checkpoint_committed",
                RunEvent.payload["checkpoint_id"].astext == str(checkpoint.id),
            )
        )
        if pause_seq is None or committed_seq is None or committed_seq <= pause_seq:
            raise DomainError(
                "checkpoint_required",
                "Pause must reach a complete checkpoint after the pause request",
            )
    if status == "succeeded" and (
        not valid_best
        or best is None
        or isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not run.latest_checkpoint_id
        or checkpoint.next_stage != "finished"
        or checkpoint.summary.get("stop_reason") != stop_reason
        or outcome.get("exit_code") != 0
    ):
        status = "failed"
        stop_reason = (
            "no_valid_candidate" if best is None else "invalid_optimization_result"
        )
    dataset = session.get(DatasetVersion, run.dataset_version_id)
    for artifact in artifacts or []:
        session.add(Artifact(run_id=run.id, attempt_id=attempt.id, **artifact))
    attempt.sandbox_stopped = True
    attempt.status = run.status = status
    attempt.exit_code = outcome.get("exit_code")
    attempt.error = run.error = (
        redact(str(outcome["error"]))[:4000] if outcome.get("error") else None
    )
    attempt.finished_at = utcnow()
    run.result = {
        "execution_mode": "optimization",
        "stop_reason": stop_reason,
        "best_candidate_id": str(best.id) if best else None,
        "best_candidate_key": best.candidate_key if best else None,
        "combined_score": score if status == "succeeded" else None,
        "metrics": {},
        "is_synthetic": dataset.is_synthetic,
        "evaluation_mode": "native_akquant",
        "result_trust": "native_strategy_and_engine_share_process",
        "image_digest": run.research_image_digest,
        "holdout_executed": False,
    }
    if config.schema_version == "tradegrad.portable.run.v1":
        run.result = {
            **run.result,
            "execution_mode": config.execution_mode,
            "evaluation_mode": "portable",
            "result_trust": "isolated_policy_trusted_akquant_engine"
            if status == "succeeded"
            else None,
            "ledger_origin": "trusted_engine" if status == "succeeded" else None,
            "runtime_certified": False,
            "external_framework_equivalence": "not_certified",
            "policy_image_digest": run.portable_policy_image_digest,
            "objective_definition": config.objective.definition_version,
        }
    append_event(
        session,
        run,
        "run_finished",
        {
            "status": status,
            "stop_reason": stop_reason,
            "best_candidate_id": str(best.id) if best else None,
            "error": run.error,
        },
        attempt.id,
    )
    session.flush()
    return run


def pin_research_image(session, attempt_id, generation, lease_token, image_digest):
    """Bind actual evaluator image independently from the build-input digest."""
    run, _ = fenced_attempt(session, attempt_id, generation, lease_token)
    research_config(run)
    if not isinstance(image_digest, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", image_digest
    ):
        raise DomainError(
            "invalid_image_digest",
            "Evaluator image must have an exact SHA-256 digest",
            422,
        )
    if run.research_image_digest and run.research_image_digest != image_digest:
        raise DomainError(
            "image_digest_mismatch", "Research resume cannot change the evaluator image"
        )
    run.research_image_digest = image_digest
    return run
