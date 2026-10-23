"""Fenced portable research. Policy source is data; only sibling Docker runs it.

Both modes use the actual upstream TradeTGD controller. Seed mode executes zero
revisions. Fixture responses preserve source and prove orchestration, not model
quality. Operator profiles reuse the native bounded SDK process and receipt
boundary; they are disabled by default. No host policy import or native fallback.
"""

from __future__ import annotations

import hashlib
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from backend.app.core.atomic_storage import publish_bytes
from backend.app.core.maintenance import guarded_worker
from backend.app.models import Artifact, DatasetVersion, ResearchRun, StrategyVersion
from backend.app.portable_services import (
    begin_portable_evaluation,
    complete_portable_evaluation,
    pin_portable_images,
    portable_config,
    portable_score,
    verify_portable_bundle_binding,
)
from backend.app.research_services import (
    finish_research_attempt,
    latest_checkpoint_for_resume,
    publish_checkpoint,
)
from backend.app.services import (
    FenceLost,
    append_event,
    claim_attempt,
    fenced_attempt,
    mark_running,
)
from backend.app.storage import read_verified, redact, safe_path
from packages.tradegrad_adapter.portable_config import portable_evaluation_windows
from packages.tradegrad_adapter.portable_environment import (
    ROOT,
    portable_environment_digest,
    portable_environment_manifest,
)
from packages.tradegrad_adapter.portable_prompts import (
    validate_policy_source,
    validate_user_policy_source,
)
from packages.tradegrad_adapter.portable_results import canonical_json_bytes
from packages.tradegrad_adapter.strategy_package import (
    canonical,
    capture_portable_environment_evidence,
)
from workers.research.lease import AttemptLease
from workers.research.optimization import safe_event_payload
from workers.research.portable_staging import (
    stage_portable_input,
    validate_portable_binding,
)

LOCKS = (
    "runtimes/evaluator/requirements.lock",
    "runtimes/portable_engine/calendar-requirements.lock",
    "runtimes/portable_policy/requirements.lock",
    "runtimes/research/requirements.lock",
    "runtimes/data/requirements.lock",
    "backend/requirements.lock",
)


def publish_portable_environment(
    *,
    factory,
    settings,
    attempt_root,
    run_id,
    attempt_id,
    generation,
    token,
    config,
    images,
):
    evidence = capture_portable_environment_evidence(
        config.environment_digest, images, portable_environment_manifest(), LOCKS, ROOT
    )
    content = canonical(evidence)
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("Portable environment evidence exceeds byte bounds")
    digest = hashlib.sha256(content).hexdigest()
    path = attempt_root / "execution-environment.json"
    publish_bytes(path, content, sha256=digest, durable_root=settings.storage_root)
    with factory.begin() as session:
        fenced_attempt(session, attempt_id, generation, token)
        session.add(
            Artifact(
                id=uuid4(),
                run_id=UUID(run_id),
                attempt_id=attempt_id,
                name="execution-environment.json",
                media_type="application/json",
                storage_key=str(path.relative_to(settings.storage_root)),
                sha256=digest,
                size_bytes=len(content),
            )
        )
    return evidence


def publish_portable_artifacts(
    *,
    storage_root,
    artifact_root,
    output_root,
    result,
    config,
    code_sha256,
    window,
    images,
    max_bytes,
    benchmark_input=None,
):
    """Verify exact runner bytes; optionally derive a separately hashed comparison."""
    records = []
    artifact_root = Path(artifact_root)
    artifact_root.mkdir(parents=True, mode=0o700)
    if result.get("status") == "succeeded":
        if (
            result.get("image_digests") != images
            or result.get("result_trust") != "isolated_policy_trusted_akquant_engine"
            or result.get("ledger_origin") != "trusted_engine"
            or result.get("runtime_certified") is not False
        ):
            raise ValueError(
                "Runner did not supply verified sibling execution provenance"
            )
        data = canonical_json_bytes(result["bundle"])
        if len(data) > min(max_bytes, 8 * 1_048_576):
            raise ValueError("Portable result exceeds artifact byte bounds")
        digest = hashlib.sha256(data).hexdigest()
        existing = read_verified(
            output_root, "portable-result.json", digest, min(max_bytes, 8 * 1_048_576)
        )
        if existing != data:
            raise ValueError("Runner output differs from returned portable bundle")
        bundle = verify_portable_bundle_binding(
            data,
            config,
            SimpleNamespace(code_sha256=code_sha256, window=window),
            allow_pending_benchmark=True,
        )
        from packages.tradegrad_adapter.benchmarks import (
            attach_comparison,
            require_comparison_binding,
        )

        bundle = attach_comparison(bundle, benchmark_input)
        require_comparison_binding(bundle, config)
        data = canonical_json_bytes(bundle)
        if len(data) > min(max_bytes, 8 * 1_048_576):
            raise ValueError("Benchmark result exceeds artifact byte bounds")
        digest = hashlib.sha256(data).hexdigest()
        path = artifact_root / "portable-result.json"
        publish_bytes(path, data, sha256=digest, durable_root=storage_root)
        records.append(
            {
                "name": "portable-result.json",
                "media_type": "application/json",
                "storage_key": str(path.relative_to(storage_root)),
                "sha256": digest,
                "size_bytes": len(data),
            }
        )
    else:
        bundle = None
    # Logs are bounded diagnostic text, never trusted accounting evidence.
    for role in ("engine", "policy"):
        text = result.get("logs", {}).get(f"{role}_stderr", "")
        if not isinstance(text, str):
            raise TypeError("Portable log must be text")
        data = redact(text).encode()[: min(max_bytes, 65536)]
        if data:
            digest = hashlib.sha256(data).hexdigest()
            path = artifact_root / f"{role}.log"
            publish_bytes(path, data, sha256=digest, durable_root=storage_root)
            records.append(
                {
                    "name": path.name,
                    "media_type": "text/plain",
                    "storage_key": str(path.relative_to(storage_root)),
                    "sha256": digest,
                    "size_bytes": len(data),
                }
            )
    return records, bundle


def _confirmed_stop(runner, evaluation_id):
    try:
        return runner.stop(evaluation_id) is True
    except Exception:  # noqa: BLE001 - unknown containment cannot become success
        return False


@guarded_worker
def execute_portable_configured(run_id: str, generation: int, settings, factory):
    from packages.tradegrad_adapter.model_calls import ModelProviderFailure
    from packages.tradegrad_adapter.model_profiles import (
        ModelProfileError,
        resolve_profile,
    )
    from packages.tradegrad_adapter.optimizer import run_optimization
    from runtimes.evaluator.portable_runner import PortableDockerEvaluator
    from workers.research.model_provider import create_model

    worker_id = f"portable:{socket.gethostname()}:{uuid4()}"
    with factory.begin() as session:
        attempt = claim_attempt(session, UUID(run_id), generation, worker_id, settings)
        if attempt is None:
            return {"status": "duplicate_or_stale"}
        attempt_id, token = attempt.id, attempt.lease_token
    runner = PortableDockerEvaluator()
    lease = AttemptLease(factory, settings, attempt_id, generation, token, worker_id)
    active_ids = set()
    images = None
    model = None
    finish_receipt_committed = False
    deadline = [0.0]
    outcome = {
        "status": "failed",
        "stop_reason": "setup_failed",
        "error": "Portable research did not start",
    }

    def control():
        lease.pulse(force=True)
        if lease.lost.is_set():
            raise FenceLost()
        if lease.cancelled.is_set():
            return "cancel"
        with factory() as session:
            run = session.get(ResearchRun, UUID(run_id))
            return "pause" if run.status == "pause_requested" else None

    def event(kind, payload):
        if kind == "budget_updated" and "remaining_runtime_seconds" in payload:
            deadline[0] = time.monotonic() + max(
                0.0, float(payload["remaining_runtime_seconds"])
            )
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

    def committed(reference):
        with factory.begin() as session:
            publish_checkpoint(
                session, attempt_id, generation, token, reference, settings
            )

    def model_boundary(phase, call_id):
        from backend.app.model_containment import (
            admit_model_process,
            confirm_model_process_stopped,
        )

        if phase not in {"admitted", "stopped"}:
            raise ValueError("Unknown model process boundary")
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
                resolve_profile(config.model, settings, model_owner_id)
            operation(session, attempt_id, generation, token, call_id)

    lease.start()
    try:
        with factory() as session:
            run = session.get(ResearchRun, UUID(run_id))
            benchmark_owner_id = run.owner_id
            config = portable_config(run)
            from backend.app.workspace_policy import enforce_model_policy

            enforce_model_policy(session, run.owner_id, config.model)
            resolve_profile(config.model, settings, run.owner_id)
            model_owner_id = run.owner_id
            if config.environment_digest != portable_environment_digest():
                raise ValueError("Portable reviewed environment changed")
            dataset = session.get(DatasetVersion, run.dataset_version_id)
            strategy = session.get(StrategyVersion, run.strategy_version_id)
            if (
                not dataset
                or not strategy
                or dataset.owner_id != run.owner_id
                or strategy.owner_id != run.owner_id
                or strategy.mode != "portable"
                or dataset.content_sha256 != config.dataset.sha256
                or hashlib.sha256(strategy.code.encode()).hexdigest()
                != config.seed_strategy.sha256
            ):
                raise ValueError(
                    "Portable immutable input identity or ownership differs"
                )
            user_authored = bool((strategy.manifest or {}).get("authoring"))
            if user_authored:
                validate_user_policy_source(strategy.code, config.strategy_kind)
                if config.optimizer.max_iterations > 0:
                    validate_policy_source(
                        strategy.code, config.strategy_kind, baseline=strategy.code
                    )
            else:
                validate_policy_source(strategy.code, config.strategy_kind)
            validate_portable_binding(config, strategy.manifest, strategy.parameters)
            seed_code, manifest, data_key, policy_manifest, parameters = (
                strategy.code,
                dataset.manifest,
                dataset.storage_key,
                strategy.manifest,
                strategy.parameters,
            )
            resume = (
                latest_checkpoint_for_resume(session, run, settings)
                if run.resume_checkpoint_id
                else None
            )
        images = runner.bind_environment(config.environment_digest)
        with factory.begin() as session:
            pin_portable_images(session, attempt_id, generation, token, images)
        attempt_root = safe_path(
            settings.storage_root, f"runs/{run_id}/attempts/{attempt_id}"
        )
        attempt_root.mkdir(parents=True, mode=0o700)
        publish_portable_environment(
            factory=factory,
            settings=settings,
            attempt_root=attempt_root,
            run_id=run_id,
            attempt_id=attempt_id,
            generation=generation,
            token=token,
            config=config,
            images=images,
        )
        with factory.begin() as session:
            ready = mark_running(session, attempt_id, generation, token)
        deadline[0] = time.monotonic() + config.budgets.max_runtime_seconds

        def evaluate(code, candidate_key, window):
            lease.pulse(force=True)
            sha = hashlib.sha256(code.encode()).hexdigest()
            with factory.begin() as session:
                physical = begin_portable_evaluation(
                    session,
                    attempt_id,
                    generation,
                    token,
                    candidate_key=candidate_key,
                    code_sha256=sha,
                    window=window,
                )
                evaluation_id = str(physical.id)
            # The physical UUID is COMMITTED before source checks, staging or create.
            active_ids.add(evaluation_id)
            root = safe_path(attempt_root, f"evaluations/{evaluation_id}")
            result = {
                "status": "failed",
                "error": "Portable evaluation did not start",
                "logs": {},
            }
            started = time.monotonic()
            benchmark_input = None
            try:
                if user_authored:
                    validate_user_policy_source(code, config.strategy_kind)
                    if code != seed_code or config.optimizer.max_iterations > 0:
                        validate_policy_source(
                            code, config.strategy_kind, baseline=seed_code
                        )
                else:
                    validate_policy_source(code, config.strategy_kind)
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
                staged = stage_portable_input(
                    config=config,
                    manifest=manifest,
                    storage_root=settings.storage_root,
                    dataset_storage_key=data_key,
                    code=code,
                    policy_manifest=policy_manifest,
                    parameters=parameters,
                    target=root / "input",
                    max_bytes=settings.max_artifact_bytes,
                    window=window,
                )
                remaining = deadline[0] - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Research runtime budget exhausted before sibling launch"
                    )
                runner.timeout = min(120.0, remaining)
                result = runner.run(
                    evaluation_id,
                    staged.engine_root,
                    staged.policy_root,
                    root / "output",
                    run_id=run_id,
                    attempt_id=str(attempt_id),
                    cancelled=lease.cancelled.is_set,
                    heartbeat=lease.pulse,
                    expected_config_sha256=config.sha256,
                )
            except Exception as error:  # noqa: BLE001 - all exits still contain both roles
                result = {
                    "status": "failed",
                    "error": f"Portable evaluation stopped safely ({type(error).__name__})",
                    "logs": {},
                }
            finally:
                stopped = _confirmed_stop(runner, evaluation_id)
                if stopped:
                    active_ids.discard(evaluation_id)
            if not stopped:
                lease.lost.set()
                lease.cancelled.set()
                raise RuntimeError("Portable sibling termination is unconfirmed")
            if lease.lost.is_set():
                raise FenceLost()
            small = {
                "status": result.get("status", "failed"),
                "error": result.get("error"),
                "exit_code": 0 if result.get("status") == "succeeded" else None,
                "evaluation_id": evaluation_id,
                "image_digest": images["engine"],
                "policy_image_digest": images["policy"],
                "duration_seconds": time.monotonic() - started,
            }
            try:
                records, bundle = publish_portable_artifacts(
                    storage_root=settings.storage_root,
                    artifact_root=root / "artifacts",
                    output_root=root / "output",
                    result=result,
                    config=config,
                    code_sha256=sha,
                    window=window,
                    images=images,
                    max_bytes=settings.max_artifact_bytes,
                    benchmark_input=benchmark_input,
                )
                if bundle:
                    small.update(
                        metrics=bundle["summary"],
                        combined_score=portable_score(bundle),
                        result_trust=bundle["result_trust"],
                        ledger_origin=bundle["ledger_origin"],
                        runtime_certified=False,
                    )
                with factory.begin() as session:
                    fenced_attempt(session, attempt_id, generation, token)
                    for record in records:
                        item = Artifact(
                            id=uuid4(),
                            run_id=UUID(run_id),
                            attempt_id=attempt_id,
                            **{
                                **record,
                                "name": f"evaluation-{evaluation_id}-{record['name']}",
                            },
                        )
                        session.add(item)
                        if record["name"] == "portable-result.json":
                            small["portable_result"] = {
                                "artifact_id": str(item.id),
                                "sha256": item.sha256,
                                "size_bytes": item.size_bytes,
                            }
                    completed = complete_portable_evaluation(
                        session,
                        attempt_id,
                        generation,
                        token,
                        UUID(evaluation_id),
                        outcome=small,
                        sandbox_stopped=True,
                        settings=settings,
                    )
                    durable = dict(completed.payload)
                return durable
            except FenceLost:
                raise
            except Exception as error:  # noqa: BLE001 - durable failure after confirmed stop
                failed = {
                    "status": "failed",
                    "exit_code": None,
                    "error": f"Portable artifact publication failed ({type(error).__name__})",
                    "evaluation_id": evaluation_id,
                    "image_digest": images["engine"],
                    "policy_image_digest": images["policy"],
                }
                with factory.begin() as session:
                    completed = complete_portable_evaluation(
                        session,
                        attempt_id,
                        generation,
                        token,
                        UUID(evaluation_id),
                        outcome=failed,
                        sandbox_stopped=True,
                        settings=settings,
                    )
                    return dict(completed.payload)

        if ready and not lease.cancelled.is_set():
            model = create_model(
                config, settings, model_owner_id, seed_code, boundary=model_boundary
            )
            result = run_optimization(
                config=config.model_dump(mode="json"),
                initial_code=seed_code,
                windows=list(portable_evaluation_windows(config)),
                evaluate=evaluate,
                root=attempt_root,
                model=model,
                event=event,
                checkpoint_committed=committed,
                control=control,
                resume=resume,
            )
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
                "image_digest": images["engine"],
                "policy_image_digest": images["policy"],
                "model_mode": "frozen_fixture"
                if config.model.provider == "fixture"
                else "operator_profile",
                "exit_code": 0,
            }
        else:
            outcome = {"status": "cancelled", "stop_reason": "cancelled"}
    except Exception as error:  # noqa: BLE001 - redact credentials/path-bearing errors
        outcome = {
            "status": "failed",
            "stop_reason": error.code
            if isinstance(error, ModelProfileError)
            else error.error_class
            if isinstance(error, ModelProviderFailure)
            else "orchestration_failed",
            "error": f"Portable research stopped safely ({type(error).__name__})",
        }
    finally:
        stopped = False
        try:
            stops = [_confirmed_stop(runner, item) for item in active_ids]
            try:
                stops.append(runner.stop_attempt(str(attempt_id)) is True)
            except Exception:  # noqa: BLE001 - independent group cleanup remains fail closed
                stops.append(False)
            # Model close runs even if a sibling could not stop. A missing local
            # receipt is independently checked by finish_research_attempt.
            try:
                model_stopped = (
                    model is None or getattr(model, "close", lambda: True)() is True
                )
            except Exception:  # noqa: BLE001 - an unknown SDK stop blocks recovery
                model_stopped = False
            stopped = all(stops) and model_stopped
            if not lease.lost.is_set():
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
            lease.close()
    return {
        "status": outcome["status"]
        if stopped and finish_receipt_committed
        else "fence_lost"
        if stopped
        else "recovery_blocked",
        "sandbox_stopped": stopped,
    }
