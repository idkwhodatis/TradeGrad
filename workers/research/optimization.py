"""Fenced research orchestration. Candidate code is data, never host-executed.

Every evaluator has a durable UUID registered before container creation. The DB
checkpoint pointer is the only resume authority; each new attempt first publishes
its own checkpoint before new model calls. Operator model profiles are pinned.
"""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy.dialects.postgresql import insert

from backend.app.core.config import Settings
from backend.app.core.maintenance import guarded_worker
from backend.app.models import (
    Artifact,
    DatasetVersion,
    ResearchRun,
    StrategyVersion,
    WorkerHeartbeat,
    utcnow,
)
from backend.app.research_services import (
    begin_evaluation,
    complete_evaluation,
    finish_research_attempt,
    latest_checkpoint_for_resume,
    pin_research_image,
    publish_checkpoint,
    research_config,
)
from backend.app.services import (
    FenceLost,
    append_event,
    claim_attempt,
    fenced_attempt,
    heartbeat,
    mark_running,
)
from backend.app.storage import read_verified, redact, safe_path
from packages.tradegrad_adapter.config import (
    RunConfig,
    canonical_bytes,
    evaluation_windows,
)
from packages.tradegrad_adapter.environment import environment_digest
from packages.tradegrad_adapter.windows import DatasetWindowReport, stage_dataset_window
from workers.research.runtime_publication import (
    attach_runtime_reference,
    prepare_runtime_artifacts,
)
from workers.research.trace_publication import (
    attach_trace_reference,
    prepare_trace_artifacts,
)


def stage_research_input(
    *,
    config: RunConfig,
    manifest: dict,
    storage_root: Path,
    dataset_storage_key: str,
    code: str,
    window: dict,
    target: Path,
    max_bytes: int,
) -> Path:
    """Reverify immutable data and emit only this window plus declared history."""
    staged = stage_dataset_window(
        config,
        window["split"],
        manifest,
        lambda name: read_verified(
            storage_root,
            f"{dataset_storage_key}/{name}",
            manifest["files"][name],
            max_bytes,
        ),
        evaluation_window=window,
    )
    if not code or len(code.encode()) > 1_048_576:
        raise ValueError("Candidate code exceeds the bounded text contract")
    target.mkdir(
        parents=True, mode=0o700
    )  # no reuse after a partial or complete attempt
    for name, content in staged.files.items():
        path = safe_path(target, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
    request = {
        "task": config.task,
        "market": config.market_profile.market,
        "profile": config.profile,
        "start_date": window["start"],
        "end_date": window["end"],
        "evaluation_config": {
            "engine_overrides": config.market_profile.engine_kwargs(),
            "window": {
                "start": window["start"],
                "end": window["end"],
                "role": window["split"],
            },
            "warmup": {
                "sessions": config.warmup.sessions,
                "history_policy": config.warmup.history_policy,
            },
            "benchmark": None,
            "run_config_sha256": config.sha256,
        },
    }
    if config.research_capture is not None:
        request["evaluation_config"]["research_capture"] = (
            config.research_capture.model_dump(mode="json")
        )
    if config.strategy_contract is not None:
        request["evaluation_config"]["strategy_contract"] = (
            config.strategy_contract.model_dump(mode="json")
        )
    (target / "strategy.py").write_bytes(code.encode())
    (target / "request.json").write_bytes(canonical_bytes(request))
    # Preflight evidence is private orchestration metadata, not candidate input.
    (target.parent / "window-report.json").write_bytes(
        canonical_bytes(
            TypeAdapter(DatasetWindowReport).dump_python(staged.report, mode="json")
        )
    )
    for path in target.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    target.chmod(0o555)
    return target


def safe_event_payload(kind: str, payload: dict) -> dict:
    """SSE summaries exclude source, prompts, responses and host filesystem paths."""
    allowed = {
        "stage",
        "iteration",
        "candidate_id",
        "parent_id",
        "kind",
        "code_sha256",
        "call_id",
        "provider",
        "model",
        "usage_source",
        "input_tokens",
        "output_tokens",
        "charged_tokens",
        "latency_seconds",
        "error_class",
        "model_calls",
        "tokens",
        "elapsed_seconds",
        "remaining_runtime_seconds",
        "fallback_parent_id",
    }
    return {
        key: redact(str(value))[:256] if isinstance(value, str) else value
        for key, value in payload.items()
        if key in allowed and (value is None or type(value) in {str, bool, int, float})
    }


def confirm_stop(runner, evaluation_id: str) -> bool:
    """Unknown teardown failures are never confirmation and cannot strand leases."""
    try:
        return runner.stop(evaluation_id) is True
    except Exception:  # noqa: BLE001 - continue containing every owned child
        return False


@guarded_worker
def execute_configured(run_id: str, generation: int, settings: Settings, factory):
    # Imports are deliberately lazy: API/dispatcher/M1 tests do not load research SDKs.
    from packages.tradegrad_adapter.model_calls import ModelProviderFailure
    from packages.tradegrad_adapter.model_profiles import ModelProfileError
    from packages.tradegrad_adapter.optimizer import run_optimization
    from runtimes.evaluator.runner import DockerEvaluator
    from workers.research.executor import publish_artifacts
    from workers.research.model_provider import create_model

    worker_id = f"research:{socket.gethostname()}:{uuid4()}"
    with factory.begin() as session:
        attempt = claim_attempt(session, UUID(run_id), generation, worker_id, settings)
        if attempt is None:
            return {"status": "duplicate_or_stale"}
        attempt_id, token = attempt.id, attempt.lease_token
    runner = DockerEvaluator()
    done, cancelled, fenced = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    last_pulse, control_state, evaluation_deadline = [0.0], [None], [0.0]
    evaluation_ids = set()
    image_id = None
    finish_receipt_committed = False
    model = None

    def pulse(force=False):
        with lock:
            if not force and time.monotonic() - last_pulse[0] < 1:
                return
            try:
                with factory.begin() as session:
                    if heartbeat(session, attempt_id, generation, token, settings):
                        cancelled.set()
                    run = session.get(ResearchRun, UUID(run_id))
                    control_state[0] = (
                        "cancel"
                        if cancelled.is_set()
                        else "pause"
                        if run.status == "pause_requested"
                        else None
                    )
                    session.execute(
                        insert(WorkerHeartbeat)
                        .values(
                            worker_id=worker_id,
                            role="research",
                            heartbeat_at=utcnow(),
                        )
                        .on_conflict_do_update(
                            index_elements=[WorkerHeartbeat.worker_id],
                            set_={"heartbeat_at": utcnow()},
                        )
                    )
                last_pulse[0] = time.monotonic()
            except Exception:
                fenced.set()
                cancelled.set()
                raise

    def maintain_lease():
        while not done.wait(
            min(settings.heartbeat_seconds, settings.lease_seconds / 3, 2)
        ):
            try:
                pulse(force=True)
            except Exception:  # noqa: BLE001 - failure revokes execution authority
                return

    def control():
        pulse(force=True)
        if fenced.is_set():
            raise FenceLost()
        return control_state[0]

    def event(kind, payload):
        if kind == "budget_updated" and "remaining_runtime_seconds" in payload:
            evaluation_deadline[0] = time.monotonic() + max(
                0.0, float(payload["remaining_runtime_seconds"])
            )
        # These events are published by the fenced durable services instead.
        if kind in {
            "checkpoint_committed",
            "candidate_created",
            "evaluation_completed",
            "run_finished",
        }:
            return
        with factory.begin() as session:
            run, _ = fenced_attempt(session, attempt_id, generation, token)
            append_event(
                session, run, kind, safe_event_payload(kind, payload), attempt_id
            )

    def model_boundary(phase, call_id):
        from backend.app.model_containment import (
            admit_model_process,
            confirm_model_process_stopped,
        )

        operation = (
            admit_model_process
            if phase == "admitted"
            else confirm_model_process_stopped
        )
        with factory.begin() as session:
            if phase == "admitted":
                from backend.app.services import advisory_lock
                from backend.app.workspace_policy import enforce_model_policy

                advisory_lock(session, f"submit:{model_owner_id}")
                enforce_model_policy(session, model_owner_id, config.model)
            operation(session, attempt_id, generation, token, call_id)

    def committed(reference):
        with factory.begin() as session:
            publish_checkpoint(
                session, attempt_id, generation, token, reference, settings
            )

    thread = threading.Thread(target=maintain_lease, daemon=True)
    thread.start()
    outcome = {
        "status": "failed",
        "error": "Research did not start",
        "stop_reason": "setup_failed",
    }
    try:
        with factory() as session:
            run = session.get(ResearchRun, UUID(run_id))
            benchmark_owner_id = run.owner_id
            config = research_config(run)
            if config.execution_mode != "optimization":
                raise ValueError("This worker requires optimization mode")
            from backend.app.workspace_policy import enforce_model_policy
            from packages.tradegrad_adapter.model_profiles import resolve_profile

            enforce_model_policy(session, run.owner_id, config.model)
            resolve_profile(config.model, settings, run.owner_id)
            model_owner_id = run.owner_id
            if config.environment_digest != environment_digest():
                raise ValueError(
                    "Research environment differs from immutable RunConfig"
                )
            dataset = session.get(DatasetVersion, run.dataset_version_id)
            strategy = session.get(StrategyVersion, run.strategy_version_id)
            if (
                not dataset
                or not strategy
                or dataset.owner_id != run.owner_id
                or strategy.owner_id != run.owner_id
            ):
                raise ValueError("Research input ownership mismatch")
            if (
                hashlib.sha256(strategy.code.encode()).hexdigest()
                != config.seed_strategy.sha256
            ):
                raise ValueError("Immutable seed strategy changed")
            from packages.tradegrad_adapter.native_contract import (
                validate_native_binding,
            )

            validate_native_binding(
                config.strategy_contract, strategy.manifest, strategy.parameters
            )
            seed_code, manifest, data_key = (
                strategy.code,
                dataset.manifest,
                dataset.storage_key,
            )
            resume = (
                latest_checkpoint_for_resume(session, run, settings)
                if run.resume_checkpoint_id
                else None
            )
        image_id = runner.bind_environment(config.environment_digest)
        with factory.begin() as session:
            pin_research_image(session, attempt_id, generation, token, image_id)
            ready = mark_running(session, attempt_id, generation, token)
        attempt_root = safe_path(
            settings.storage_root, f"runs/{run_id}/attempts/{attempt_id}"
        )
        attempt_root.mkdir(parents=True, mode=0o700)
        from workers.research.environment_evidence import publish_native_environment

        publish_native_environment(
            factory=factory,
            storage_root=settings.storage_root,
            attempt_root=attempt_root,
            run_id=run_id,
            attempt_id=attempt_id,
            generation=generation,
            token=token,
            expected_environment_digest=config.environment_digest,
            image_id=image_id,
        )
        evaluation_deadline[0] = time.monotonic() + float(
            config.budgets.max_runtime_seconds
        )

        def evaluate(code, candidate_key, window):
            pulse(force=True)
            with factory.begin() as session:
                physical = begin_evaluation(
                    session,
                    attempt_id,
                    generation,
                    token,
                    candidate_key=candidate_key,
                    code_sha256=hashlib.sha256(code.encode()).hexdigest(),
                    window=window,
                )
                evaluation_id = physical.id
            # Commit identity before any staging or container creation.
            evaluation_ids.add(str(evaluation_id))
            root = safe_path(attempt_root, f"evaluations/{evaluation_id}")
            result = {
                "status": "failed",
                "exit_code": None,
                "error": "Evaluation did not start",
                "logs": {},
            }
            started = time.monotonic()
            try:
                from backend.app.benchmark_services import prepare_for_window

                with factory() as benchmark_session:
                    benchmark_input = prepare_for_window(
                        benchmark_session,
                        benchmark_owner_id,
                        config,
                        manifest,
                        window,
                        settings,
                    )
                input_dir = stage_research_input(
                    config=config,
                    manifest=manifest,
                    storage_root=settings.storage_root,
                    dataset_storage_key=data_key,
                    code=code,
                    window=window,
                    target=root / "input",
                    max_bytes=settings.max_artifact_bytes,
                )
                remaining = evaluation_deadline[0] - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Research runtime budget exhausted before sandbox launch"
                    )
                runner.timeout = min(120.0, remaining)
                result = runner.evaluate(
                    input_dir=input_dir,
                    output_dir=root / "output",
                    run_id=run_id,
                    attempt_id=str(evaluation_id),
                    cancelled=cancelled.is_set,
                    heartbeat=pulse,
                    expected_config_sha256=config.sha256,
                )
            except Exception as error:  # noqa: BLE001 - no exception skips sandbox termination
                result = {
                    "status": "failed",
                    "exit_code": None,
                    "error": f"Evaluation stopped safely ({type(error).__name__})",
                    "logs": {},
                }
            finally:
                stopped = confirm_stop(runner, str(evaluation_id))
                if stopped:
                    evaluation_ids.discard(str(evaluation_id))
            if not stopped:
                fenced.set()
                cancelled.set()
                raise RuntimeError("Evaluation sandbox termination is unconfirmed")
            if fenced.is_set():
                raise FenceLost()
            result["execution_mode"] = "optimization"
            result["image_digest"] = image_id
            try:
                if result.get("status") == "succeeded":
                    from packages.tradegrad_adapter.benchmarks import attach_comparison

                    result["bundle"] = attach_comparison(
                        result["bundle"], benchmark_input
                    )
                records = publish_artifacts(
                    settings.storage_root,
                    root / "artifacts",
                    result,
                    settings.max_artifact_bytes,
                )
                records.extend(
                    prepare_runtime_artifacts(
                        config=config,
                        result=result,
                        records=records,
                        root=root,
                        settings=settings,
                    )
                )
                records.extend(
                    prepare_trace_artifacts(
                        factory=factory,
                        settings=settings,
                        config=config,
                        evaluation_id=evaluation_id,
                        result=result,
                        records=records,
                        root=root,
                    )
                )
            except Exception as error:  # noqa: BLE001 - preserve confirmed stop even if artifact disk fails
                failed = {
                    "status": "failed",
                    "exit_code": None,
                    "error": f"Artifact publication failed ({type(error).__name__})",
                    "evaluation_id": str(evaluation_id),
                    "image_digest": image_id,
                }
                with factory.begin() as session:
                    complete_evaluation(
                        session,
                        attempt_id,
                        generation,
                        token,
                        evaluation_id,
                        outcome=failed,
                        sandbox_stopped=True,
                    )
                return failed
            small = {
                key: result.get(key)
                for key in ("status", "exit_code", "error", "metrics", "image_digest")
            }
            small.update(
                evaluation_id=str(evaluation_id),
                duration_seconds=time.monotonic() - started,
            )
            with factory.begin() as session:
                fenced_attempt(session, attempt_id, generation, token)
                for record in records:
                    name = record["name"]
                    artifact = Artifact(
                        id=uuid4(),
                        run_id=UUID(run_id),
                        attempt_id=attempt_id,
                        **{**record, "name": f"evaluation-{evaluation_id}-{name}"},
                    )
                    session.add(artifact)
                    attach_trace_reference(small, artifact, name, result)
                    attach_runtime_reference(small, artifact, name, result)
                    if name == "result-bundle.json":
                        small["result_bundle"] = {
                            "artifact_id": str(artifact.id),
                            "sha256": artifact.sha256,
                            "size_bytes": artifact.size_bytes,
                        }
                complete_evaluation(
                    session,
                    attempt_id,
                    generation,
                    token,
                    evaluation_id,
                    outcome=small,
                    sandbox_stopped=True,
                )
            return small

        if ready and not cancelled.is_set():
            model = create_model(
                config, settings, model_owner_id, seed_code, boundary=model_boundary
            )
            result = run_optimization(
                config=config.model_dump(mode="json"),
                initial_code=seed_code,
                windows=list(evaluation_windows(config)),
                evaluate=evaluate,
                root=attempt_root,
                model=model,
                event=event,
                checkpoint_committed=committed,
                control=control,
                resume=resume,
            )
            outcome = result.to_dict()
            best = next(
                (
                    row
                    for row in result.candidates
                    if row["candidate_id"] == result.best_candidate_id
                ),
                None,
            )
            outcome = {
                "status": result.status,
                "stop_reason": result.stop_reason,
                "best_candidate_id": result.best_candidate_id,
                "combined_score": best["evaluation"]["combined_score"]
                if best
                else None,
                "image_digest": image_id,
                "model_mode": "frozen_fixture"
                if config.model.provider == "fixture"
                else "operator_profile",
                "exit_code": 0,
            }
        else:
            outcome = {
                "status": "cancelled",
                "stop_reason": "cancelled",
                "image_digest": image_id,
            }
    except Exception as error:  # noqa: BLE001 - no exception values/DB credentials in events
        outcome = {
            "status": "failed",
            "stop_reason": error.code
            if isinstance(error, ModelProfileError)
            else error.error_class
            if isinstance(error, ModelProviderFailure)
            else "orchestration_failed",
            "error": f"Research stopped safely ({type(error).__name__})",
            "image_digest": image_id,
        }
    finally:
        stopped = False
        try:
            # Only active/unconfirmed children remain; confirmed, durably fenced IDs
            # cannot restart and must not cause unbounded teardown work on long runs.
            stop_results = [
                confirm_stop(runner, item)
                for item in [*evaluation_ids, str(attempt_id)]
            ]
            model_stopped = (
                model is None or getattr(model, "close", lambda: True)() is True
            )
            stopped = all(stop_results) and model_stopped
            if not fenced.is_set():
                with factory.begin() as session:
                    final_run = finish_research_attempt(
                        session,
                        attempt_id,
                        generation,
                        token,
                        outcome,
                        sandbox_stopped=stopped,
                    )
                    outcome["status"] = final_run.status
                finish_receipt_committed = True
        except FenceLost:
            pass
        finally:
            done.set()
            thread.join(timeout=5)
    return {
        "status": outcome["status"]
        if stopped and finish_receipt_committed
        else "fence_lost"
        if stopped
        else "recovery_blocked",
        "sandbox_stopped": stopped,
    }
