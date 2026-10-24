"""Execute one exact frozen portable holdout, without optimizer or model calls."""

import socket
import time
from uuid import UUID, uuid4

from backend.app.core.maintenance import guarded_worker
from backend.app.models import Artifact, Evaluation, ResearchRun
from backend.app.portable_final_services import (
    begin_final_evaluation,
    complete_final_evaluation,
    final_config,
    finish_final_attempt,
    validate_final_inputs,
)
from backend.app.portable_services import portable_score
from backend.app.services import FenceLost, claim_attempt, fenced_attempt, mark_running
from backend.app.storage import safe_path
from workers.research.lease import AttemptLease
from workers.research.portable_final_staging import stage_portable_final_input
from workers.research.portable_optimization import (
    _confirmed_stop,
    publish_portable_artifacts,
    publish_portable_environment,
)
from workers.research.runtime_publication import (
    attach_runtime_reference,
    prepare_runtime_artifacts,
)
from workers.research.trace_publication import (
    attach_trace_reference,
    prepare_trace_artifacts,
)


@guarded_worker
def execute_portable_final(run_id, generation, settings, factory):
    from runtimes.evaluator.portable_runner import PortableDockerEvaluator

    worker_id = f"portable-final:{socket.gethostname()}:{uuid4()}"
    with factory.begin() as session:
        attempt = claim_attempt(session, UUID(run_id), generation, worker_id, settings)
        if attempt is None:
            return {"status": "duplicate_or_stale"}
        attempt_id, token = attempt.id, attempt.lease_token
    lease = AttemptLease(factory, settings, attempt_id, generation, token, worker_id)
    runner, evaluation_id, images = None, None, None
    stopped, committed = False, False
    started = time.monotonic()
    outcome = {
        "status": "failed",
        "exit_code": None,
        "error": "Portable final did not start",
    }
    try:
        runner = PortableDockerEvaluator()
        lease.start()
        with factory() as session:
            run = session.get(ResearchRun, UUID(run_id))
            _, _, dataset, strategy, _ = validate_final_inputs(session, run, settings)
            benchmark_owner_id = run.owner_id
            config = final_config(run)
            manifest, data_key, code, policy_manifest, parameters = (
                dataset.manifest,
                dataset.storage_key,
                strategy.code,
                strategy.manifest,
                strategy.parameters,
            )
            expected_images = {
                "engine": run.research_image_digest,
                "policy": run.portable_policy_image_digest,
            }
        images = runner.bind_environment(config.environment_digest)
        if images != expected_images:
            raise ValueError("Final execution cannot change either frozen source image")
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
        lease.pulse(force=True)
        with factory.begin() as session:
            ready = mark_running(session, attempt_id, generation, token)
            if ready and not lease.cancelled.is_set():
                evaluation = begin_final_evaluation(
                    session, attempt_id, generation, token
                )
                evaluation_id = evaluation.id
                window = dict(evaluation.window)
        if evaluation_id is None:
            outcome = {"status": "cancelled", "exit_code": None}
        else:
            # The physical evaluation UUID has committed before either sibling create.
            root = safe_path(attempt_root, f"evaluations/{evaluation_id}")
            result = {
                "status": "failed",
                "error": "Portable final did not start",
                "logs": {},
            }
            benchmark_input = None
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
                staged = stage_portable_final_input(
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
                    raise TimeoutError("Final budget exhausted before sibling launch")
                runner.timeout = remaining
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
            except Exception as error:  # noqa: BLE001 - all exits stop both sibling roles
                result = {
                    "status": "failed",
                    "error": f"Portable final stopped safely ({type(error).__name__})",
                    "logs": {},
                }
            finally:
                stopped = _confirmed_stop(runner, str(evaluation_id))
            if not stopped:
                raise RuntimeError("Portable final sibling termination is unconfirmed")
            if lease.lost.is_set():
                raise FenceLost()
            outcome = {
                "status": result.get("status", "failed"),
                "error": result.get("error"),
                "exit_code": 0 if result.get("status") == "succeeded" else None,
                "evaluation_id": str(evaluation_id),
                "image_digest": images["engine"],
                "policy_image_digest": images["policy"],
            }
            try:
                records, bundle = publish_portable_artifacts(
                    storage_root=settings.storage_root,
                    artifact_root=root / "artifacts",
                    output_root=root / "output",
                    result=result,
                    config=config,
                    code_sha256=config.selected_strategy.sha256,
                    window=window,
                    images=images,
                    max_bytes=settings.max_artifact_bytes,
                    benchmark_input=benchmark_input,
                )
                if bundle is not None:
                    result["bundle"] = bundle
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
                if bundle:
                    outcome.update(
                        metrics=bundle["summary"],
                        combined_score=portable_score(bundle),
                        result_trust=bundle["result_trust"],
                        ledger_origin=bundle["ledger_origin"],
                        runtime_certified=False,
                    )
            except Exception as error:  # noqa: BLE001 - artifact failure remains durable failed evidence
                records = []
                outcome = {
                    "status": "failed",
                    "exit_code": None,
                    "evaluation_id": str(evaluation_id),
                    "error": f"Portable final artifact publication failed ({type(error).__name__})",
                    "image_digest": images["engine"],
                    "policy_image_digest": images["policy"],
                }
            with factory.begin() as session:
                fenced_attempt(session, attempt_id, generation, token)
                for record in records:
                    artifact = Artifact(
                        id=uuid4(),
                        run_id=UUID(run_id),
                        attempt_id=attempt_id,
                        **{
                            **record,
                            "name": f"evaluation-{evaluation_id}-{record['name']}",
                        },
                    )
                    session.add(artifact)
                    attach_trace_reference(outcome, artifact, record["name"], result)
                    attach_runtime_reference(outcome, artifact, record["name"], result)
                    if record["name"] == "portable-result.json":
                        outcome["portable_result"] = {
                            "artifact_id": str(artifact.id),
                            "sha256": artifact.sha256,
                            "size_bytes": artifact.size_bytes,
                        }
                completed = complete_final_evaluation(
                    session,
                    attempt_id,
                    generation,
                    token,
                    evaluation_id,
                    outcome=outcome,
                    sandbox_stopped=True,
                    settings=settings,
                )
                outcome = dict(completed.payload)
    except Exception as error:  # noqa: BLE001 - finalization must run even after lease/launch failures
        outcome = {
            "status": "failed",
            "exit_code": None,
            "error": f"Portable final stopped safely ({type(error).__name__})",
        }
    finally:
        try:
            stops = (
                [_confirmed_stop(runner, str(evaluation_id))]
                if runner is not None and evaluation_id
                else []
            )
            try:
                stops.append(
                    runner is not None and runner.stop_attempt(str(attempt_id)) is True
                )
            except Exception:  # noqa: BLE001 - unknown group cleanup cannot become success
                stops.append(False)
            stopped = all(stops)
            if not lease.lost.is_set():
                with factory.begin() as session:
                    if stopped and evaluation_id is not None:
                        row = session.get(Evaluation, evaluation_id)
                        if row is not None and row.finished_at is None:
                            outcome = {
                                "status": "failed",
                                "exit_code": None,
                                "evaluation_id": str(evaluation_id),
                                "error": "Portable final evidence publication was incomplete",
                            }
                            complete_final_evaluation(
                                session,
                                attempt_id,
                                generation,
                                token,
                                evaluation_id,
                                outcome=outcome,
                                sandbox_stopped=True,
                                settings=settings,
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
                committed = True
        except FenceLost:
            lease.lost.set()
        finally:
            lease.close()
    return {
        "status": "recovery_blocked"
        if not stopped
        else outcome["status"]
        if committed
        else "fence_lost",
        "sandbox_stopped": stopped,
    }
