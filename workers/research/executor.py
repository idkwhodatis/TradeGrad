"""Trusted orchestration only; untrusted strategy execution stays inside the runner."""

import csv
import hashlib
import io
import json
import os
import socket
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backend.app.core.config import Settings, get_settings
from backend.app.core.db import create_db_engine, session_factory
from backend.app.models import (
    DatasetVersion,
    ResearchRun,
    StrategyVersion,
    WorkerHeartbeat,
    utcnow,
)
from backend.app.services import (
    FenceLost,
    claim_attempt,
    finish_attempt,
    heartbeat,
    mark_running,
)
from backend.app.storage import read_verified, redact, safe_path


def prepare_input(session, run: ResearchRun, attempt_id: UUID, settings: Settings):
    dataset = session.get(DatasetVersion, run.dataset_version_id)
    strategy = session.get(StrategyVersion, run.strategy_version_id)
    if (
        not dataset
        or not strategy
        or dataset.owner_id != run.owner_id
        or strategy.owner_id != run.owner_id
    ):
        raise ValueError("Run input ownership mismatch")
    return stage_input(run, dataset, strategy, attempt_id, settings)


def stage_input(
    run: ResearchRun,
    dataset: DatasetVersion,
    strategy: StrategyVersion,
    attempt_id: UUID,
    settings: Settings,
):
    """Pure file staging from trusted immutable records, separately testable offline."""
    manifest_bytes = json.dumps(
        dataset.manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    if (
        hashlib.sha256(manifest_bytes).hexdigest() != run.config["dataset_sha256"]
        or dataset.content_sha256 != run.config["dataset_sha256"]
    ):
        raise ValueError("Immutable dataset manifest changed")
    code = strategy.code.encode()
    if hashlib.sha256(code).hexdigest() != run.config["strategy_sha256"]:
        raise ValueError("Immutable strategy version changed")
    attempt_root = safe_path(settings.storage_root, f"runs/{run.id}/{attempt_id}")
    input_dir = attempt_root / "input"
    input_dir.mkdir(parents=True, mode=0o700)  # never reuse an attempt namespace
    total = 0
    for key, digest in dataset.manifest["files"].items():
        relative = Path(key)
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] not in {"data", "universe"}
            or relative.suffix != ".csv"
        ):
            raise ValueError("Dataset manifest contains an unsupported input path")
        data = read_verified(
            settings.storage_root,
            f"{dataset.storage_key}/{key}",
            digest,
            settings.max_artifact_bytes,
        )
        total += len(data)
        if total > 500_000_000:
            raise ValueError("Dataset exceeds the M1 input size budget")
        if relative.parts[0] == "data":
            reader = csv.DictReader(io.StringIO(data.decode()))
            out = io.StringIO()
            writer = csv.DictWriter(out, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                if run.config["start_date"] <= row["date"] < run.config["end_date"]:
                    writer.writerow(row)
            data = out.getvalue().encode()
        target = input_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (input_dir / "strategy.py").write_bytes(code)
    request = {
        key: run.config[key]
        for key in ("task", "market", "profile", "start_date", "end_date")
    }
    (input_dir / "request.json").write_text(json.dumps(request, sort_keys=True))
    for path in input_dir.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    input_dir.chmod(0o555)
    return input_dir, attempt_root / "output", attempt_root / "artifacts"


def publish_artifacts(root: Path, target: Path, outcome: dict, max_bytes: int):
    target.mkdir(parents=True, mode=0o700)
    files = {
        "provenance.json": (
            json.dumps(
                {
                    "schema_version": 1,
                    "execution_mode": outcome.get("execution_mode", "seed_evaluation"),
                    "evaluation_mode": "native_akquant",
                    "result_trust": "native_strategy_and_engine_share_process",
                    "image_digest": outcome.get("image_digest"),
                },
                sort_keys=True,
            ).encode(),
            "application/json",
        )
    }
    if isinstance(outcome.get("metrics"), dict):
        files["metrics.json"] = (
            json.dumps(
                outcome["metrics"], sort_keys=True, allow_nan=False, indent=2
            ).encode(),
            "application/json",
        )
    if isinstance(outcome.get("bundle"), dict):
        from packages.tradegrad_adapter.results import (
            canonical_json_bytes,
            validate_result_bundle,
        )

        validate_result_bundle(outcome["bundle"])
        files["result-bundle.json"] = (
            canonical_json_bytes(outcome["bundle"]),
            "application/json",
        )
    logs = outcome.get("logs") or {}
    if not isinstance(logs, dict):
        logs = {"output": str(logs)}
    text = "\n".join(f"[{name}]\n{redact(str(value))}" for name, value in logs.items())
    if outcome.get("error"):
        text += "\n[error]\n" + redact(str(outcome["error"]))
    files["logs.txt"] = (text.encode()[:max_bytes], "text/plain")
    artifacts = []
    for name, (content, media_type) in files.items():
        if len(content) > max_bytes:
            raise ValueError("Artifact exceeds output budget")
        path = target / name
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o444)
        artifacts.append(
            {
                "name": name,
                "media_type": media_type,
                "storage_key": str(path.relative_to(root.resolve())),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    return artifacts


def execute(run_id: str, generation: int, settings: Settings | None = None):
    from runtimes.evaluator.runner import DockerEvaluator

    settings = settings or get_settings()
    factory = session_factory(create_db_engine(settings))
    with factory() as session:
        current = session.get(ResearchRun, UUID(run_id))
        configured = (
            current is not None
            and current.config.get("schema_version") == "tradegrad.run.v1"
        )
    if configured:
        from workers.research.optimization import execute_configured

        return execute_configured(run_id, generation, settings, factory)
    worker_id = f"research:{socket.gethostname()}:{uuid4()}"
    with factory.begin() as session:
        attempt = claim_attempt(session, UUID(run_id), generation, worker_id, settings)
        if attempt is None:
            return {"status": "duplicate_or_stale"}
        attempt_id, token = attempt.id, attempt.lease_token
    runner = DockerEvaluator()
    done, cancelled, fenced = threading.Event(), threading.Event(), threading.Event()
    pulse_lock = threading.Lock()
    last_pulse = [0.0]

    def pulse(force=False):
        with pulse_lock:
            if not force and time.monotonic() - last_pulse[0] < 1:
                return
            try:
                with factory.begin() as session:
                    if heartbeat(session, attempt_id, generation, token, settings):
                        cancelled.set()
                    session.execute(
                        insert(WorkerHeartbeat)
                        .values(
                            worker_id=worker_id, role="research", heartbeat_at=utcnow()
                        )
                        .on_conflict_do_update(
                            index_elements=[WorkerHeartbeat.worker_id],
                            set_={"heartbeat_at": utcnow()},
                        )
                    )
                last_pulse[0] = time.monotonic()
            except Exception:
                # No optimistic execution through a database outage or stale lease.
                fenced.set()
                cancelled.set()
                raise

    def maintain_lease():
        while not done.wait(
            min(settings.heartbeat_seconds, settings.lease_seconds / 3, 2)
        ):
            try:
                pulse(force=True)
            except Exception:  # noqa: BLE001 - fail-closed background lease guard
                return

    thread = threading.Thread(target=maintain_lease, daemon=True)
    thread.start()
    artifacts = []
    outcome = {
        "status": "failed",
        "exit_code": None,
        "error": "Execution did not start",
        "logs": {},
    }
    try:
        with factory() as session:
            run = session.scalar(
                select(ResearchRun).where(ResearchRun.id == UUID(run_id))
            )
            input_dir, output_dir, artifact_dir = prepare_input(
                session, run, attempt_id, settings
            )
        with factory.begin() as session:
            ready = mark_running(session, attempt_id, generation, token)
        if ready and not cancelled.is_set():
            outcome = runner.evaluate(
                input_dir=input_dir,
                output_dir=output_dir,
                run_id=run_id,
                attempt_id=str(attempt_id),
                cancelled=cancelled.is_set,
                heartbeat=pulse,
            )
        else:
            outcome = {
                "status": "cancelled",
                "exit_code": None,
                "error": None,
                "logs": {},
            }
        if outcome.get("error"):
            outcome["error"] = redact(str(outcome["error"]))[:4000]
        if not fenced.is_set():
            artifacts = publish_artifacts(
                settings.storage_root,
                artifact_dir,
                outcome,
                settings.max_artifact_bytes,
            )
    except Exception as exc:  # noqa: BLE001 - terminate the sandbox for every orchestration failure
        # Never include exception values: they may carry database URLs/SQL parameters.
        outcome = {
            "status": "failed",
            "exit_code": None,
            "error": f"Execution stopped safely ({type(exc).__name__}); inspect server diagnostics",
            "logs": {},
        }
    finally:
        stopped = runner.stop(str(attempt_id))
        try:
            if not fenced.is_set():
                with factory.begin() as session:
                    finish_attempt(
                        session,
                        attempt_id,
                        generation,
                        token,
                        outcome,
                        sandbox_stopped=stopped,
                        artifacts=artifacts,
                    )
        except FenceLost:
            pass  # reconciliation now owns the attempt; no late result publication
        finally:
            done.set()
            thread.join(timeout=5)
    return {"status": outcome["status"], "sandbox_stopped": stopped}
