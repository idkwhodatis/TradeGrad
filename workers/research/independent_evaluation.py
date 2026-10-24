"""Single isolated native/portable execution; no optimizer, model or provider."""

import socket
import time
from uuid import UUID, uuid4

from backend.app.comparison_guard import (
    ComparisonGuardUnavailable,
    comparison_attempt_guard,
)
from backend.app.core.maintenance import guarded_worker
from backend.app.independent_services import (
    begin_independent_evaluation,
    complete_independent_evaluation,
    finish_independent_attempt,
    independent_config,
    validate_independent_inputs,
)
from backend.app.models import Artifact, Evaluation, ResearchRun
from backend.app.services import FenceLost, claim_attempt, fenced_attempt, mark_running
from backend.app.storage import safe_path
from packages.tradegrad_adapter.independent_config import NATIVE_SCHEMA
from workers.research.comparison import _confirm_query_closure
from workers.research.independent_staging import stage_independent_input
from workers.research.lease import AttemptLease
from workers.research.runtime_publication import (
    attach_runtime_reference,
    prepare_runtime_artifacts,
)
from workers.research.trace_publication import (
    attach_trace_reference,
    prepare_trace_artifacts,
)


def publish_independent_native_artifacts(settings, root, result):
    import hashlib

    from backend.app.core.atomic_storage import publish_bytes
    from packages.tradegrad_adapter.independent_results import validate_result_bundle
    from packages.tradegrad_adapter.results import canonical_json_bytes
    from workers.research.executor import publish_artifacts

    records = publish_artifacts(
        settings.storage_root,
        root / "artifacts",
        {key: value for key, value in result.items() if key != "bundle"},
        settings.max_artifact_bytes,
    )
    if result.get("status") == "succeeded":
        bundle = validate_result_bundle(result["bundle"])
        raw = canonical_json_bytes(bundle)
        if len(raw) > min(settings.max_artifact_bytes, 8 * 1024**2):
            raise ValueError("Independent result exceeds economic byte budget")
        path = root / "artifacts" / "result-bundle.json"
        sha = hashlib.sha256(raw).hexdigest()
        publish_bytes(path, raw, sha256=sha, durable_root=settings.storage_root)
        records.append(
            {
                "name": path.name,
                "media_type": "application/json",
                "storage_key": str(path.relative_to(settings.storage_root)),
                "sha256": sha,
                "size_bytes": len(raw),
            }
        )
    return records


def _stop(runner, portable, evaluation_id, attempt_id):
    if runner is None:
        return False
    stops = []
    if evaluation_id is not None:
        try:
            stops.append(runner.stop(str(evaluation_id)) is True)
        except Exception:  # noqa: BLE001 - unknown stop is never containment
            stops.append(False)
    try:
        stops.append(
            (
                runner.stop_attempt(str(attempt_id))
                if portable
                else runner.stop(str(attempt_id))
            )
            is True
        )
    except Exception:  # noqa: BLE001
        stops.append(False)
    return all(stops)


@guarded_worker
def execute_independent(run_id, generation, settings, factory):
    worker_id = f"independent:{socket.gethostname()}:{uuid4()}"
    with factory.begin() as session:
        attempt = claim_attempt(session, UUID(run_id), generation, worker_id, settings)
        if attempt is None:
            return {"status": "duplicate_or_stale"}
        attempt_id, token = attempt.id, attempt.lease_token
    try:
        with comparison_attempt_guard(
            settings.storage_root, UUID(run_id), attempt_id
        ) as guard:
            return _execute_guarded(
                run_id,
                generation,
                settings,
                factory,
                worker_id,
                attempt_id,
                token,
                guard,
            )
    except (ComparisonGuardUnavailable, FenceLost):
        return {
            "status": "recovery_blocked",
            "sandbox_stopped": False,
            "query_stopped": False,
        }


def _execute_guarded(
    run_id, generation, settings, factory, worker_id, attempt_id, token, guard
):
    from packages.research_trace.comparison import ComparisonControl
    from runtimes.evaluator.portable_runner import PortableDockerEvaluator
    from runtimes.evaluator.runner import DockerEvaluator
    from workers.research.diagnostic_parity_publication import (
        attach_independent_parity_reference,
        prepare_independent_parity_artifacts,
    )

    started = time.monotonic()
    lease = AttemptLease(factory, settings, attempt_id, generation, token, worker_id)
    runner, evaluation_id, control, config = None, None, None, None
    portable, stopped, committed = False, False, False
    outcome = {
        "status": "failed",
        "exit_code": None,
        "error": "Independent execution did not start",
    }
    lease.start()
    try:
        # Fresh fence occurs after the lifetime guard, before source reads/host SQL.
        with factory.begin() as session:
            ready = mark_running(session, attempt_id, generation, token)
            run = session.get(ResearchRun, UUID(run_id))
            config = independent_config(run)
            portable = config.schema_version != NATIVE_SCHEMA
            _, _, dataset, strategy, _ = validate_independent_inputs(
                session, run, settings
            )
            owner_id = run.owner_id
            manifest, data_key, code, policy_manifest, parameters = (
                dataset.manifest,
                dataset.storage_key,
                strategy.code,
                strategy.manifest,
                strategy.parameters,
            )
        control = ComparisonControl(
            cancel_event=lease.cancelled, timeout_seconds=config.max_runtime_seconds
        )
        runner = PortableDockerEvaluator() if portable else DockerEvaluator()
        images = runner.bind_environment(config.environment_digest)
        expected = (
            {
                "engine": config.research_image_digest,
                "policy": config.portable_policy_image_digest,
            }
            if portable
            else config.research_image_digest
        )
        if images != expected:
            raise ValueError(
                "Independent replay requires the actual original engine and policy images"
            )
        attempt_root = guard.path
        if portable:
            from workers.research.portable_optimization import (
                publish_portable_environment,
            )

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
        else:
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
                image_id=images,
            )
        with factory.begin() as session:
            fenced_attempt(session, attempt_id, generation, token)
            if ready and not lease.cancelled.is_set():
                evaluation_id = begin_independent_evaluation(
                    session, attempt_id, generation, token
                ).id
        if evaluation_id is None:
            outcome = {"status": "cancelled", "exit_code": None}
        else:
            root = safe_path(attempt_root, f"evaluations/{evaluation_id}")
            result = {
                "status": "failed",
                "exit_code": None,
                "error": "Independent sandbox did not start",
                "logs": {},
            }
            try:
                from backend.app.benchmark_services import prepare_for_window

                with factory.begin() as session:
                    benchmark_input = prepare_for_window(
                        session, owner_id, config, manifest, config.window, settings
                    )
                staged = stage_independent_input(
                    config=config,
                    manifest=manifest,
                    storage_root=settings.storage_root,
                    dataset_storage_key=data_key,
                    code=code,
                    policy_manifest=policy_manifest,
                    parameters=parameters,
                    target=root / "input",
                    max_bytes=settings.max_artifact_bytes,
                )
                remaining = config.max_runtime_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(
                        "Independent runtime budget exhausted before sandbox launch"
                    )
                runner.timeout = remaining
                control.check()
                if portable:
                    result = runner.run(
                        str(evaluation_id),
                        staged.engine_root,
                        staged.policy_root,
                        root / "output",
                        run_id=run_id,
                        attempt_id=str(attempt_id),
                        cancelled=lease.cancelled.is_set,
                        heartbeat=lease.pulse,
                        expected_config_sha256=config.sha256,
                    )
                else:
                    result = runner.evaluate(
                        input_dir=staged,
                        output_dir=root / "output",
                        run_id=run_id,
                        attempt_id=str(evaluation_id),
                        cancelled=lease.cancelled.is_set,
                        heartbeat=lease.pulse,
                        expected_config_sha256=config.sha256,
                    )
            except Exception as error:  # noqa: BLE001 - all paths converge on confirmed termination
                result = {
                    "status": "failed",
                    "exit_code": None,
                    "error": f"Independent execution stopped safely ({type(error).__name__})",
                    "logs": {},
                }
            finally:
                stopped = _stop(runner, portable, evaluation_id, attempt_id)
            if not stopped:
                raise RuntimeError(
                    "Independent sibling/sandbox termination is unconfirmed"
                )
            if lease.lost.is_set():
                raise FenceLost()
            result.update(
                execution_mode="independent_evaluation",
                image_digest=config.research_image_digest,
            )
            outcome = {
                "status": result.get("status", "failed"),
                "exit_code": 0
                if result.get("status") == "succeeded"
                else result.get("exit_code"),
                "error": result.get("error"),
                "evaluation_id": str(evaluation_id),
                "image_digest": config.research_image_digest,
            }
            try:
                if portable:
                    from workers.research.portable_optimization import (
                        publish_portable_artifacts,
                    )

                    records, bundle = publish_portable_artifacts(
                        storage_root=settings.storage_root,
                        artifact_root=root / "artifacts",
                        output_root=root / "output",
                        result=result,
                        config=config,
                        code_sha256=config.selected_strategy.sha256,
                        window=config.window,
                        images=images,
                        max_bytes=settings.max_artifact_bytes,
                        benchmark_input=benchmark_input,
                    )
                    outcome["policy_image_digest"] = config.portable_policy_image_digest
                    if bundle is not None:
                        result["bundle"] = bundle
                        outcome.update(
                            metrics=bundle["summary"],
                            combined_score=None,
                            reward_status="not_applicable",
                            result_trust=bundle["result_trust"],
                            ledger_origin=bundle["ledger_origin"],
                            runtime_certified=False,
                        )
                else:
                    if result.get("status") == "succeeded":
                        from packages.tradegrad_adapter.benchmarks import (
                            attach_comparison,
                        )

                        result["bundle"] = attach_comparison(
                            result["bundle"], benchmark_input
                        )
                        outcome["metrics"] = result["metrics"]
                    records = publish_independent_native_artifacts(
                        settings, root, result
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
                records.extend(
                    prepare_runtime_artifacts(
                        settings=settings,
                        config=config,
                        result=result,
                        records=records,
                        root=root,
                    )
                )
                records.extend(
                    prepare_independent_parity_artifacts(
                        factory=factory,
                        settings=settings,
                        config=config,
                        evaluation_id=evaluation_id,
                        result=result,
                        records=records,
                        root=root,
                        control=control,
                    )
                )
                control.check()
            except Exception as error:  # noqa: BLE001 - malformed publication remains failed, never zero-profit success
                outcome = {
                    "status": "failed",
                    "exit_code": None,
                    "evaluation_id": str(evaluation_id),
                    "image_digest": config.research_image_digest,
                    "error": f"Independent evidence publication failed ({type(error).__name__})",
                }
                records = []
            _confirm_query_closure(control)
            with factory.begin() as session:
                fenced_attempt(session, attempt_id, generation, token)
                guard.assert_owned(UUID(run_id), attempt_id)
                for record in records:
                    name = record["name"]
                    artifact = Artifact(
                        id=uuid4(),
                        run_id=UUID(run_id),
                        attempt_id=attempt_id,
                        **{**record, "name": f"evaluation-{evaluation_id}-{name}"},
                    )
                    session.add(artifact)
                    attach_trace_reference(outcome, artifact, name, result)
                    attach_runtime_reference(outcome, artifact, name, result)
                    attach_independent_parity_reference(outcome, artifact, name, result)
                    if name in {"result-bundle.json", "portable-result.json"}:
                        outcome["portable_result" if portable else "result_bundle"] = {
                            "artifact_id": str(artifact.id),
                            "sha256": artifact.sha256,
                            "size_bytes": artifact.size_bytes,
                        }
                complete_independent_evaluation(
                    session,
                    attempt_id,
                    generation,
                    token,
                    evaluation_id,
                    outcome=outcome,
                    sandbox_stopped=True,
                    settings=settings,
                )
    except Exception as error:  # noqa: BLE001 - containment/fence completion must run
        outcome = {
            "status": "failed",
            "exit_code": None,
            "error": f"Independent execution stopped safely ({type(error).__name__})",
        }
    finally:
        _confirm_query_closure(control)
        try:
            stopped = _stop(runner, portable, evaluation_id, attempt_id)
            if not lease.lost.is_set():
                with factory.begin() as session:
                    if stopped and evaluation_id is not None:
                        item = session.get(Evaluation, evaluation_id)
                        if item is not None and item.finished_at is None:
                            outcome = {
                                "status": "failed",
                                "exit_code": None,
                                "evaluation_id": str(evaluation_id),
                                "error": "Independent publication was incomplete",
                            }
                            complete_independent_evaluation(
                                session,
                                attempt_id,
                                generation,
                                token,
                                evaluation_id,
                                outcome=outcome,
                                sandbox_stopped=True,
                                settings=settings,
                            )
                    guard.assert_owned(UUID(run_id), attempt_id)
                    run = finish_independent_attempt(
                        session, attempt_id, generation, token, outcome, stopped
                    )
                    outcome["status"] = run.status
                committed = True
        except FenceLost:
            lease.lost.set()
        finally:
            lease.close()
            if lease.thread.is_alive():
                lease.thread.join()
    return {
        "status": "recovery_blocked"
        if not stopped
        else outcome["status"]
        if committed
        else "fence_lost",
        "sandbox_stopped": stopped,
        "query_stopped": True,
    }
