"""A frozen candidate, one holdout execution, no optimizer or model invocation."""

import socket
import time
from uuid import UUID, uuid4

from backend.app.final_evaluation_services import (
    begin_final_evaluation,
    complete_final_evaluation,
    final_config,
    finish_final_attempt,
    validate_final_inputs,
)
from backend.app.models import Artifact, Evaluation, ResearchRun
from backend.app.services import FenceLost, claim_attempt, fenced_attempt, mark_running
from backend.app.storage import safe_path
from workers.research.environment_evidence import publish_native_environment
from workers.research.final_staging import stage_final_input
from workers.research.lease import AttemptLease
from workers.research.optimization import confirm_stop


def execute_final(run_id, generation, settings, factory):
    from runtimes.evaluator.runner import DockerEvaluator
    from workers.research.executor import publish_artifacts

    worker_id = f"final:{socket.gethostname()}:{uuid4()}"
    with factory.begin() as session:
        attempt = claim_attempt(session, UUID(run_id), generation, worker_id, settings)
        if attempt is None:
            return {"status": "duplicate_or_stale"}
        attempt_id, token = attempt.id, attempt.lease_token
    started = time.monotonic()
    runner = None
    lease = AttemptLease(factory, settings, attempt_id, generation, token, worker_id)
    evaluation_id, image_id = None, None
    outcome = {
        "status": "failed",
        "exit_code": None,
        "error": "Final evaluation did not start",
    }
    stopped, finished = False, False
    try:
        runner = DockerEvaluator()
        lease.start()
        with factory() as session:
            run = session.get(ResearchRun, UUID(run_id))
            _, _, dataset, strategy, _ = validate_final_inputs(session, run, settings)
            benchmark_owner_id = run.owner_id
            config = final_config(run)
            manifest, data_key, code = (
                dataset.manifest,
                dataset.storage_key,
                strategy.code,
            )
            expected_image = run.research_image_digest
        image_id = runner.bind_environment(config.environment_digest)
        if image_id != expected_image:
            raise ValueError("Final evaluation image differs from frozen source image")
        attempt_root = safe_path(
            settings.storage_root, f"runs/{run_id}/attempts/{attempt_id}"
        )
        attempt_root.mkdir(parents=True, mode=0o700)
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
        lease.pulse(force=True)
        with factory.begin() as session:
            ready = mark_running(session, attempt_id, generation, token)
            if ready and not lease.cancelled.is_set():
                item = begin_final_evaluation(session, attempt_id, generation, token)
                evaluation_id = item.id
                window = {"split": "holdout", **config.holdout.model_dump(mode="json")}
        if evaluation_id is None:
            outcome = {"status": "cancelled", "exit_code": None}
        else:
            # The committed UUID is recovery authority before staging or creation.
            root = safe_path(attempt_root, f"evaluations/{evaluation_id}")
            result = {
                "status": "failed",
                "exit_code": None,
                "error": "Final evaluation did not start",
                "logs": {},
            }
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
                input_dir = stage_final_input(
                    config=config,
                    manifest=manifest,
                    storage_root=settings.storage_root,
                    dataset_storage_key=data_key,
                    code=code,
                    target=root / "input",
                    max_bytes=settings.max_artifact_bytes,
                )
                remaining = config.max_runtime_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(
                        "Final runtime budget exhausted before sandbox launch"
                    )
                runner.timeout = remaining
                result = runner.evaluate(
                    input_dir=input_dir,
                    output_dir=root / "output",
                    run_id=run_id,
                    attempt_id=str(evaluation_id),
                    cancelled=lease.cancelled.is_set,
                    heartbeat=lease.pulse,
                    expected_config_sha256=config.sha256,
                )
            except Exception as error:  # noqa: BLE001 - no failure skips containment/fenced completion
                result = {
                    "status": "failed",
                    "exit_code": None,
                    "error": f"Final execution stopped safely ({type(error).__name__})",
                    "logs": {},
                }
            finally:
                stopped = confirm_stop(runner, str(evaluation_id))
            if not stopped:
                raise RuntimeError("Final sandbox termination is unconfirmed")
            if lease.lost.is_set():
                raise FenceLost()
            result.update(execution_mode="final_evaluation", image_digest=image_id)
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
            except Exception as error:  # noqa: BLE001 - no failure skips containment/fenced completion
                result = {
                    "status": "failed",
                    "exit_code": None,
                    "image_digest": image_id,
                    "error": f"Final artifact publication failed ({type(error).__name__})",
                }
                records = []
            outcome = {
                key: result.get(key)
                for key in ("status", "exit_code", "error", "metrics", "image_digest")
            }
            outcome["evaluation_id"] = str(evaluation_id)
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
                    if name == "result-bundle.json":
                        outcome["result_bundle"] = {
                            "artifact_id": str(artifact.id),
                            "sha256": artifact.sha256,
                            "size_bytes": artifact.size_bytes,
                        }
                complete_final_evaluation(
                    session,
                    attempt_id,
                    generation,
                    token,
                    evaluation_id,
                    outcome=outcome,
                    sandbox_stopped=True,
                )
    except Exception as error:  # noqa: BLE001 - no failure skips containment/fenced completion
        outcome = {
            "status": "failed",
            "exit_code": None,
            "image_digest": image_id,
            "error": f"Final evaluation stopped safely ({type(error).__name__})",
        }
    finally:
        try:
            stops = (
                [
                    confirm_stop(runner, identifier)
                    for identifier in ([str(evaluation_id)] if evaluation_id else [])
                    + [str(attempt_id)]
                ]
                if runner is not None
                else [False]
            )
            stopped = all(stops)
            if not lease.lost.is_set():
                with factory.begin() as session:
                    if stopped and evaluation_id is not None:
                        item = session.get(Evaluation, evaluation_id)
                        if item is not None and item.finished_at is None:
                            outcome = {
                                "status": "failed",
                                "exit_code": None,
                                "image_digest": image_id,
                                "error": "Final result publication was incomplete",
                                "evaluation_id": str(evaluation_id),
                            }
                            complete_final_evaluation(
                                session,
                                attempt_id,
                                generation,
                                token,
                                evaluation_id,
                                outcome=outcome,
                                sandbox_stopped=True,
                            )
                    run = finish_final_attempt(
                        session,
                        attempt_id,
                        generation,
                        token,
                        outcome,
                        sandbox_stopped=stopped,
                    )
                    outcome["status"] = run.status
                # A successful evaluator result is not a committed terminal run.
                # In particular, losing the final fence must never report success.
                finished = True
        except FenceLost:
            lease.lost.set()
        finally:
            lease.close()
    return {
        "status": (
            "recovery_blocked"
            if not stopped
            else outcome["status"]
            if finished
            else "fence_lost"
        ),
        "sandbox_stopped": stopped,
    }
