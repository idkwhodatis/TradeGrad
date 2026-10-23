"""Hosted-only real pause -> cold cleanup -> HTTP checkpoint resume acceptance.

Requires BOTH TRADEGRAD_REQUIRE_COLD_RECOVERY=1 and the existing research gate's
TRADEGRAD_REQUIRE_RESEARCH_INTEGRATION=1 plus its three runtimes/PG/Docker image.
Collection/opt-out is inert. Only the inherited explicitly synthetic dataset and
frozen model are allowed. No SQL writes simulate run/queue progress. Cleanup is
restricted to this harness's random private database, storage and pinned process
identities. The identity-safe process harness patch must be integrated first.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from test_research_optimization import (
    ResearchProcessHarness,
    logical_lineage,
    model_fingerprints,
    research_rows,
)
from test_research_optimization import (
    research_harness as research_harness,  # noqa: PLC0414 -- explicit gated fixture reuse
)
from test_worker_recovery import require

from backend.app.core.maintenance import MaintenanceBusy
from backend.app.operations_backup import BackupLimits, PostgresDatabase
from backend.app.operations_cold_retention import (
    ACKNOWLEDGEMENT,
    cold_retention,
    exclusive_maintenance,
)
from backend.app.operations_retention import _protected, _scan, _snapshot_manifest_keys
from backend.app.storage import safe_path
from packages.tradegrad_adapter.checkpoint import CheckpointRef, CheckpointStore
from packages.tradegrad_adapter.config import evaluation_windows
from packages.tradegrad_adapter.results import validate_result_bundle
from script.compose_bounds import alarm_deadline

# Smaller than the inherited absolute 240-second harness budget. All external
# activity below is additionally covered by that absolute wall-clock alarm.
INSPECTION_SECONDS = 20


@pytest.fixture
def cold_research_harness(request):
    # Dependency checks precede even the inherited fixture's runtime probes,
    # private database creation, sockets and service startup.
    require(
        os.getenv("TRADEGRAD_REQUIRE_RESEARCH_INTEGRATION") == "1",
        "Cold recovery also requires explicit real-research integration opt-in",
    )
    try:
        from script.ci_owned_group import signal_owned_group
    except ImportError:
        pytest.fail(
            "Identity-safe process helper must be integrated first", pytrace=False
        )
    require(
        callable(signal_owned_group)
        and callable(getattr(ResearchProcessHarness, "stop_processes", None)),
        "Identity-safe process harness must be integrated first",
    )
    return request.getfixturevalue("research_harness")


def _limits(h):
    return BackupLimits(
        max_files=10_000,
        max_file_bytes=64 * 1024 * 1024,
        max_total_bytes=256 * 1024 * 1024,
        timeout_seconds=max(1, int(h.remaining(INSPECTION_SECONDS))),
    )


def _pause_generated_writer(h, run_id, database):
    """Freeze only the pinned owned worker while testing refusal/admitting pause."""
    from script.ci_owned_group import signal_owned_group

    client = h.clients[0]
    detail = h.request(client, "GET", f"/research-runs/{run_id}").json()
    require(
        detail["status"] in {"queued", "preparing", "running"},
        "Run ended before the generated-evaluation pause boundary",
    )
    generated = {
        row["candidate_key"]
        for row in research_rows(h, run_id, "candidates")
        if row["kind"] == "generated"
    }
    evaluations = research_rows(h, run_id, "evaluations")
    active = next(
        (
            row
            for row in evaluations
            if row["candidate_key"] in generated
            and row["split"] == "train"
            and not row["sandbox_stopped"]
        ),
        None,
    )
    if active is None:
        return None
    state = h.runner._inspect(active["id"])
    if state is None or not state["State"]["Running"]:
        return None
    require(
        state["Config"]["Labels"]["io.tradegrad.run_id"] == run_id,
        "Generated evaluator belongs to another run",
    )
    require(len(h.workers) == 1, "Pause boundary requires one owned research worker")
    worker = h.workers[0]
    require(worker in h.expected_alive, "Pause target is not an owned live worker")
    signal_owned_group(worker, signal.SIGSTOP)
    try:
        detail = h.request(client, "GET", f"/research-runs/{run_id}").json()
        evaluations = research_rows(h, run_id, "evaluations")
        active = next(row for row in evaluations if row["id"] == active["id"])
        calls = research_rows(h, run_id, "model-calls")
        require(
            detail["status"] == "running"
            and detail["generation"] == 1
            and detail["active_attempt_id"]
            and detail["latest_checkpoint_id"]
            and not active["sandbox_stopped"],
            "Worker passed the intended first-generation pause boundary",
        )
        require(
            bool(calls)
            and all(
                row["provider"] == "fixture"
                and row["status"] == "completed"
                and row["response_sha256"]
                for row in calls
            ),
            "Pause requires completed frozen-model call receipts, not live SDK work",
        )
        # The real worker still holds its lifecycle lock even between SQL calls.
        # This is deliberately an INVALID quiescence claim: the lock must reject
        # it before scanning or mutating, rather than trusting the operator flag.
        with pytest.raises(MaintenanceBusy):
            cold_retention(
                database=database,
                storage_root=h.root / "storage",
                quiesced=True,
                limits=_limits(h),
            )
        requested = h.request(
            client, "POST", f"/research-runs/{run_id}/pause", status=202
        ).json()
        require(requested["status"] == "pause_requested", "HTTP pause was not admitted")
        result = {
            "attempt_id": detail["active_attempt_id"],
            "active_evaluation_id": active["id"],
            "calls": model_fingerprints(calls),
            "lineage": logical_lineage(research_rows(h, run_id, "candidates")),
        }
    finally:
        # No raw PID signal or poll/reap. Failure leaves ownership uncertain and
        # aborts; the hardened fixture cleanup still attempts pinned containment.
        signal_owned_group(worker, signal.SIGCONT)
    h.attempts.add(result["attempt_id"])
    h.attempts.update(row["id"] for row in evaluations)
    return result


def _paused_and_drained(h, run_id, old):
    detail = h.request(h.clients[0], "GET", f"/research-runs/{run_id}").json()
    if detail["status"] != "paused":
        require(
            detail["status"] in {"running", "pause_requested", "finalizing"},
            "Real worker failed to honor cooperative HTTP pause",
        )
        return None
    require(
        detail["generation"] == 1 and len(detail["attempts"]) == 1,
        "Pause created an unsolicited generation or attempt",
    )
    attempt = detail["attempts"][0]
    require(
        attempt["id"] == old["attempt_id"]
        and attempt["status"] == "paused"
        and attempt["sandbox_stopped"]
        and attempt["finished_at"],
        "Paused attempt lacks durable termination evidence",
    )
    evaluations = research_rows(h, run_id, "evaluations")
    require(
        len(evaluations) == 3
        and all(
            row["status"] == "succeeded"
            and row["sandbox_stopped"]
            and row["finished_at"]
            for row in evaluations
        ),
        "Pause did not preserve exactly seed train/validation and generated train",
    )
    require(
        all(h.runner._inspect(row["id"]) is None for row in evaluations)
        and h.runner._inspect(old["attempt_id"]) is None,
        "A paused attempt's physical Docker child is still present",
    )
    h.attempts.update(row["id"] for row in evaluations)
    return detail if _queue_drained(h) else None


def _queue_drained(h):
    with h.connection(private=True) as connection:
        # This is observation only. Procrastinate must acknowledge its own job.
        pending = connection.execute(
            "SELECT count(*) FROM procrastinate_jobs WHERE status IN ('todo','doing')"
        ).fetchone()[0]
        outbox = connection.execute(
            "SELECT (SELECT count(*) FROM outbox_events WHERE dispatched_at IS NULL)"
            " + (SELECT count(*) FROM data_outbox WHERE dispatched_at IS NULL)"
        ).fetchone()[0]
        # FrozenModel is in-process, so no external model SDK admission exists.
        # Unexpected admission is not excused by a terminal logical call record.
        models = connection.execute(
            "SELECT count(*) FROM run_events "
            "WHERE type IN ('model_process_admitted','model_process_stopped')"
        ).fetchone()[0]
        require(models == 0, "Frozen-only gate unexpectedly launched a model process")
    return pending == outbox == 0


def _storage_snapshot(h, database):
    """Verify every production reference/manifest and hash all current files."""
    limits = _limits(h)
    with exclusive_maintenance(database, h.root / "storage") as (locked, _):
        references = locked.source_snapshot(limits)
        deadline = time.monotonic() + h.remaining(INSPECTION_SECONDS)
        files, _, manifests = _scan(
            h.root / "storage",
            _snapshot_manifest_keys(references, limits),
            limits,
            deadline,
        )
        _protected(references, files, manifests, deadline)
    return references, {
        key: (entry["sha256"], entry["size_bytes"]) for key, entry in files.items()
    }


def _checkpoint(h, run_id, checkpoint_id):
    with h.connection(private=True) as connection:
        row = connection.execute(
            "SELECT c.manifest_storage_key, c.manifest_sha256, c.input_digest "
            "FROM research_checkpoints c WHERE c.run_id=%s AND c.id=%s AND c.complete",
            (run_id, checkpoint_id),
        ).fetchone()
    require(row is not None, "Exact authoritative checkpoint reference is absent")
    reference = CheckpointRef(
        str(safe_path(h.root / "storage", row[0])), row[1], row[2]
    )
    return reference, CheckpointStore.load(reference, expected_input_digest=row[2])


def _restart_services(h, commands):
    h.workers = []
    for label, arguments in commands:
        process = h.spawn(label, arguments)
        if label == "research-worker":
            h.workers.append(process)
    require(len(h.workers) == 1, "Restart did not preserve exactly one research worker")

    def ready():
        try:
            return (
                h.clients[0].get("/health/ready", timeout=h.remaining(3)).status_code
                == 200
            )
        except httpx.TransportError:
            return False

    h.until(ready, "Same owned API failed to restart after cold cleanup", timeout=15)


def _verify_artifacts(h, run_id, prior=None):
    rows = h.request(h.clients[0], "GET", f"/runs/{run_id}/artifacts").json()["data"]
    require(
        bool(rows), "Run did not publish downloadable physical evaluation artifacts"
    )
    inventory = {}
    for row in rows:
        data = h.request(
            h.clients[0], "GET", f"/artifacts/{row['id']}/download"
        ).content
        require(
            len(data) == row["size_bytes"]
            and hashlib.sha256(data).hexdigest() == row["sha256"],
            "Published artifact changed content or size",
        )
        inventory[row["id"]] = row
        if row["name"].endswith("result-bundle.json"):
            validate_result_bundle(json.loads(data))
    if prior:
        require(
            all(inventory.get(key) == value for key, value in prior.items()),
            "Resume removed or rewrote a pre-cleanup artifact reference",
        )
    return inventory


def _exercise_cold_resume(h):
    require(
        callable(getattr(h, "stop_processes", None)),
        "Identity-safe process harness is required",
    )
    database = PostgresDatabase(h.env["DATABASE_URL"])
    require(
        database.database_name == h.name,
        "Cold target is not the harness private database",
    )
    require(
        urlsplit(h.base_url).hostname == "127.0.0.1",
        "Resume API is not the harness's private loopback server",
    )
    dataset, universe = h.upload_fixture()
    run_id, config = h.submit_configured(dataset, universe)
    require(
        config.model.provider == "fixture", "Cold recovery must never use a live model"
    )
    old = h.until(
        lambda: _pause_generated_writer(h, run_id, database),
        "No real generated-candidate evaluation reached the cold-recovery pause boundary",
        timeout=100,
    )
    paused = h.until(
        lambda: _paused_and_drained(h, run_id, old),
        "Worker, evaluators or queue did not quiesce after HTTP pause",
        timeout=45,
    )
    old["checkpoint_id"] = paused["latest_checkpoint_id"]
    checkpoint = h.request(
        h.clients[0], "GET", f"/research-checkpoints/{old['checkpoint_id']}"
    ).json()
    reference, state = _checkpoint(h, run_id, old["checkpoint_id"])
    require(
        checkpoint["complete"]
        and checkpoint["next_stage"] == state["next_stage"] == "evaluate"
        and checkpoint["iteration"] == state["iteration"] == 1
        and checkpoint["config_sha256"] == config.sha256
        and state["inputs"]["config"] == config.model_dump(mode="json"),
        "Pause did not preserve the complete generated-candidate checkpoint",
    )
    old["evaluations"] = research_rows(h, run_id, "evaluations")
    old["artifacts"] = _verify_artifacts(h, run_id)
    require(
        model_fingerprints(research_rows(h, run_id, "model-calls")) == old["calls"],
        "Pause changed a completed model call",
    )
    commands = [
        (label, list(process.args[1:]))
        for label, process in h.processes
        if process in h.expected_alive
    ]
    require(
        sorted(label for label, _ in commands)
        == sorted(
            [
                "api",
                "dispatcher",
                "reconciler",
                "research-worker",
                "data-dispatcher",
                "data-worker",
            ]
        ),
        "Cold stop discovered an unknown or missing producer",
    )
    # Includes raw queue clients/API/dispatchers/reconciler, not just the worker.
    # The helper refuses identity uncertainty and proves group death before reap.
    h.stop_processes()
    require(
        not h.processes and not h.expected_alive, "Owned producers remain unconfirmed"
    )
    before_references, before_files = _storage_snapshot(h, database)
    require(
        any("/checkpoints/" in key for key in before_files)
        and any("/model_calls/" in key for key in before_files)
        and any(key.endswith("strategy.py") for key in before_files)
        and any(key.endswith("logs.txt") for key in before_files),
        "Retention fixture lacks real checkpoint, source, replay or log bytes",
    )
    uploads = [
        row["path"]
        for row in before_references["references"]
        if row["path"].startswith("uploads/") and row["path"].endswith(".csv")
    ]
    require(len(uploads) == 1, "Expected one real immutable uploaded CSV reference")
    upload = PurePosixPath(uploads[0])
    temporary_key = str(upload.parent / f".{upload.name}.{uuid4().hex}.tmp")
    temporary = safe_path(h.root / "storage", temporary_key)
    with temporary.open("xb") as stream:
        stream.write(
            b"abandoned atomic temporary, owned by hosted cold-recovery fixture\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
    # No utime or clock monkeypatch: explicit future test time ages only this
    # private inventory for policy classification, never business/database time.
    arguments = {
        "database": database,
        "storage_root": h.root / "storage",
        "quiesced": True,
        "now": datetime.now(UTC) + timedelta(days=31),
        "limits": _limits(h),
    }
    plan = cold_retention(**arguments)
    require(
        plan["dry_run"] and plan["deleted_files"] == 0,
        "Cold preview must not delete any file",
    )
    require(
        [entry["path"] for entry in plan["temporary_candidates"]] == [temporary_key],
        "Cold plan did not select only the one explicitly abandoned fixture",
    )
    require(
        plan["aged_run_logs_retained"] > 0,
        "Cold plan did not account for retained real configured-attempt logs",
    )
    receipt = h.root / "cold-resume-receipt.jsonl"
    report = cold_retention(
        **arguments,
        apply=True,
        expected_plan_sha256=plan["plan_sha256"],
        acknowledgement=ACKNOWLEDGEMENT,
        receipt_path=receipt,
    )
    require(
        report["status"] == "applied"
        and report["deleted_files"] == 1
        and not temporary.exists(),
        "Actual cold apply did not remove exactly the reviewed temporary",
    )
    after_references, after_files = _storage_snapshot(h, database)
    require(
        before_references == after_references and before_files == after_files,
        "Cold cleanup changed a published reference or any original stored byte",
    )
    journal = [json.loads(line) for line in receipt.read_text().splitlines()]
    require(
        [row["action"] for row in journal]
        == ["begin", "unlink_intent", "unlinked", "complete"]
        and journal[1]["path"] == journal[2]["path"] == temporary_key
        and receipt.stat().st_mode & 0o777 == 0o600,
        "Cold apply lacks the exact private durable completion receipt",
    )
    require(
        CheckpointStore.load(reference, expected_input_digest=reference.input_digest)
        == state,
        "Cold cleanup changed the exact resumable checkpoint state",
    )
    _restart_services(h, commands)
    require(
        h.request(
            h.clients[0], "GET", f"/research-checkpoints/{old['checkpoint_id']}"
        ).json()
        == checkpoint,
        "Restart changed the authoritative paused checkpoint record",
    )
    resumed = h.request(
        h.clients[0], "POST", f"/research-runs/{run_id}/resume", status=202
    ).json()
    require(
        resumed["generation"] == 2
        and resumed["resume_checkpoint_id"] == old["checkpoint_id"],
        "HTTP resume did not select the exact pre-cleanup checkpoint",
    )
    final = h.until(
        lambda: h.finished(run_id),
        "Real resumed worker did not finish after cleanup",
        timeout=80,
    )
    require(
        final["generation"] == 2
        and len(final["attempts"]) == 2
        and all(
            row["sandbox_stopped"] and row["finished_at"] for row in final["attempts"]
        )
        and final["attempts"][1]["resumed_checkpoint_id"] == old["checkpoint_id"]
        and final["config"] == paused["config"] == config.model_dump(mode="json")
        and final["research_image_digest"] == paused["research_image_digest"],
        "Resume changed immutable configuration, checkpoint lineage or stop evidence",
    )
    require(
        logical_lineage(research_rows(h, run_id, "candidates")) == old["lineage"]
        and model_fingerprints(research_rows(h, run_id, "model-calls")) == old["calls"],
        "Resume duplicated a model call or changed generated code/parent lineage",
    )
    evaluations = research_rows(h, run_id, "evaluations")
    fresh = [row for row in evaluations if row["generation"] == 2]
    require(
        len(fresh) == 1
        and fresh[0]["candidate_key"] == "candidate-0001"
        and fresh[0]["split"] == "validation"
        and fresh[0]["status"] == "succeeded"
        and fresh[0]["sandbox_stopped"]
        and fresh[0]["finished_at"]
        and fresh[0]["window"] in list(evaluation_windows(config))
        and all(
            next(row for row in evaluations if row["id"] == prior["id"]) == prior
            for prior in old["evaluations"]
        ),
        "Resume reran a completed window, rewrote history or omitted the unfinished validation",
    )
    h.attempts.update(row["id"] for row in evaluations)
    h.attempts.update(row["id"] for row in final["attempts"])
    _verify_artifacts(h, run_id, prior=old["artifacts"])
    latest = h.request(
        h.clients[0], "GET", f"/research-checkpoints/{final['latest_checkpoint_id']}"
    ).json()
    require(
        latest["complete"]
        and latest["generation"] == 2
        and latest["next_stage"] == "finished"
        and latest["config_sha256"] == config.sha256,
        "Resumed worker did not publish its own complete terminal checkpoint",
    )
    events = h.request(h.clients[0], "GET", f"/runs/{run_id}/events?follow=false").text
    terminal = [
        json.loads(line[6:])
        for line in events.splitlines()
        if line.startswith("data: ")
    ]
    require(
        [row["payload"]["status"] for row in terminal if row["type"] == "run_finished"]
        == ["paused", "succeeded"],
        "Cold resume lost or duplicated an attempt's terminal event",
    )
    h.until(
        lambda: _queue_drained(h),
        "Resumed worker did not acknowledge its real queue job",
        timeout=15,
    )
    require(
        all(h.runner._inspect(row["id"]) is None for row in evaluations),
        "A resumed physical Docker evaluator remains present",
    )
    _, terminal_state = _checkpoint(h, run_id, final["latest_checkpoint_id"])
    require(
        terminal_state["next_stage"] == "finished"
        and terminal_state["inputs"]["config"] == config.model_dump(mode="json"),
        "Terminal checkpoint bytes are not complete and bound to the original config",
    )
    h.stop_processes()
    final_references, final_files = _storage_snapshot(h, database)
    # New generation files/references may be added, never rewrite originals.
    require(
        all(final_files.get(key) == value for key, value in before_files.items())
        and all(
            row in final_references[group]
            for group in ("references", "datasets", "checkpoints")
            for row in before_references[group]
        ),
        "Resumed worker changed pre-cleanup storage or immutable references",
    )


@pytest.mark.skipif(
    os.getenv("TRADEGRAD_REQUIRE_COLD_RECOVERY") != "1",
    reason="Explicit hosted cold-cleanup/research-resume gate not enabled",
)
def test_real_paused_worker_resumes_exact_checkpoint_after_cold_cleanup(
    cold_research_harness,
):
    h = cold_research_harness
    try:
        with alarm_deadline(h.deadline):
            _exercise_cold_resume(h)
    except Exception as error:  # noqa: BLE001 -- never log DSN, HTTP data or provider material
        pytest.fail(
            f"Real cold-resume gate failed ({type(error).__name__})", pytrace=False
        )
