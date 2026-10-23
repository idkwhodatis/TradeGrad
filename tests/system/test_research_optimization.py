"""Opt-in REAL PostgreSQL + Docker configured-research boundary acceptance.

TRADEGRAD_REQUIRE_RESEARCH_INTEGRATION=1, TEST_DATABASE_URL (CREATEDB),
API_PYTHON, RESEARCH_PYTHON and DATA_PYTHON are mandatory. The image must already
be built from the same reviewed inputs. Collection/opt-out opens no sockets,
starts no processes and makes no Docker calls. There is no SQLite, fake-session,
fake-runner, live-model, host-candidate execution or provider-network fallback.

The only market input is explicitly synthetic, filtered to pinned calendar
sessions by the separate data runtime. Upload, immutable universe creation,
research submission, worker execution and results all use production boundaries.
SQL below is read-only, except the inherited disposable-database lifecycle. The
recovery gate SIGKILLs only its verified owned research worker process group.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from test_worker_recovery import ROOT, ProcessHarness, require

from backend.app.storage import safe_path
from packages.tradegrad_adapter.checkpoint import CheckpointRef, CheckpointStore
from packages.tradegrad_adapter.config import (
    MarketConfig,
    RunConfig,
    evaluation_windows,
)
from packages.tradegrad_adapter.results import validate_result_bundle
from script.ci_owned_group import (
    admit_owned_group,
    peek_owned_group,
    signal_owned_group,
    signal_owned_process,
    wait_owned_group,
)

GATE_SECONDS = 240


def runtime_paths():
    paths, prefixes = {}, {}
    for key in ("API_PYTHON", "RESEARCH_PYTHON", "DATA_PYTHON"):
        value = os.getenv(key)
        require(bool(value), f"{key} is required for the real research smoke")
        path = Path(value).absolute()
        require(path.is_file() and os.access(path, os.X_OK), f"{key} is not executable")
        probe = subprocess.run(
            [str(path), "-I", "-c", "import sys; print(sys.prefix)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        require(probe.returncode == 0, f"{key} probe failed")
        paths[key], prefixes[key] = str(path), probe.stdout.strip()
    require(
        len(set(prefixes.values())) == 3,
        "API, research and data must use distinct runtime environments",
    )
    return paths


class ResearchProcessHarness(ProcessHarness):
    def __init__(self, root, database_url, interpreters):
        super().__init__(root, database_url)
        self.interpreters = interpreters
        self.deadline = time.monotonic() + GATE_SECONDS
        self.env.update(DATA_PROVIDER_MODE="fixture", DATA_WORKER_CONCURRENCY="1")

    def remaining(self, maximum):
        result = min(maximum, self.deadline - time.monotonic())
        require(result > 0, "Real research smoke exceeded its 240-second budget")
        return result

    def spawn(self, label, arguments, *, persistent=True, extra_env=None):
        runtime = "API_PYTHON"
        if arguments[:2] == ["-m", "workers.data"]:
            runtime = "DATA_PYTHON"
        elif arguments[:3] == ["-m", "workers.research", "worker"]:
            runtime = "RESEARCH_PYTHON"
        process = subprocess.Popen(
            [self.interpreters[runtime], *arguments],
            cwd=ROOT,
            env={**self.env, **(extra_env or {})},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.processes.append((label, process))
        admit_owned_group(process)
        if persistent:
            self.expected_alive.add(process)
        return process

    def start_workers(self):
        return [self.spawn("research-worker", ["-m", "workers.research", "worker"])]

    def upload_fixture(self, *, benchmark=False):
        for role in ("dispatcher", "worker"):
            self.spawn(f"data-{role}", ["-m", "workers.data", role])
        # Only trusted fixture/calendar processing runs here, never candidate code.
        program = """import csv, io, sys
import exchange_calendars as calendars
sessions = {str(day.date()) for day in calendars.get_calendar("XSHG").sessions_in_range("2024-01-02", "2024-04-26")}
with open(sys.argv[1], newline="") as source:
    reader = csv.DictReader(source)
    writer = csv.DictWriter(sys.stdout, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(row for row in reader if row["date"] in sessions)
"""
        fixture = subprocess.run(
            [
                self.interpreters["DATA_PYTHON"],
                "-c",
                program,
                str(ROOT / "tests/baseline/fixtures/CN/INDEX.csv"),
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=self.remaining(15),
            check=False,
        )
        require(
            fixture.returncode == 0 and bool(fixture.stdout),
            "Synthetic pinned-calendar fixture preparation failed",
        )
        mapping = {
            "name": "Synthetic configured research smoke, NOT market evidence",
            "symbol": "600001" if benchmark else "600000",
            "market": "CN",
            "mic": "XSHG",
            "currency": "CNY",
            "timezone": "Asia/Shanghai",
            "calendar": "XSHG",
            "start_date": "2024-01-02",
            "end_date": "2024-04-27",
            "columns": {
                "session_date": "date",
                **{
                    key: key
                    for key in ("open", "high", "low", "close", "volume", "amount")
                },
            },
            "volume_unit": "shares",
            "price_semantics": {
                "native_mode": "user_declared_synthetic",
                "split_basis": "unadjusted",
                "dividend_basis": "unadjusted",
                "note": "Synthetic engine fixture, not market prices",
            },
            "unknown_flags": [],
        }
        client = self.clients[0]
        imported = self.request(
            client,
            "POST",
            "/data/uploads",
            status=202,
            headers={"Idempotency-Key": uuid4().hex},
            data={"mapping": json.dumps(mapping)},
            files={"file": ("synthetic.csv", fixture.stdout, "text/csv")},
        ).json()

        def ready():
            record = self.request(
                client, "GET", f"/data/imports/{imported['id']}"
            ).json()
            require(
                record["status"] not in {"failed", "partial", "cancelled"},
                "Synthetic upload did not produce a complete dataset",
            )
            return record if record["status"] == "ready" else None

        record = self.until(
            ready,
            "Actual data worker did not publish the uploaded snapshot",
            timeout=40,
        )
        dataset_id = record["dataset_version_id"]
        dataset = next(
            item
            for item in self.request(client, "GET", "/datasets").json()["data"]
            if item["id"] == dataset_id
        )
        coverage = self.request(
            client, "GET", f"/datasets/{dataset_id}/coverage"
        ).json()
        members = coverage["universe"]["members"]
        universe = self.request(
            client,
            "POST",
            "/data/universes",
            status=201,
            json={
                "name": "Synthetic fixed research smoke universe",
                "state": "user_fixed",
                "as_of": datetime.now(UTC).isoformat(),
                "members": members,
                "evidence": [],
            },
        ).json()
        return dataset, universe

    def submit_configured(self, dataset, universe):
        client = self.clients[0]
        strategies = self.request(client, "GET", "/strategy-versions").json()["data"]
        strategy = next(
            item for item in strategies if item["name"] == "Original cn_time seed"
        )
        capabilities = self.request(client, "GET", "/research-capabilities").json()
        config = RunConfig.model_validate(
            {
                "execution_mode": "optimization",
                "task": "cn_time",
                "dataset": {"id": dataset["id"], "sha256": dataset["content_sha256"]},
                "seed_strategy": {
                    "id": strategy["id"],
                    "sha256": strategy["content_sha256"],
                },
                "universe": {
                    "id": universe["id"],
                    "sha256": universe["content_sha256"],
                },
                "windows": {
                    "train": {"start": "2024-02-01", "end": "2024-03-15"},
                    "validation": {"start": "2024-03-15", "end": "2024-04-20"},
                    "holdout": {"start": "2024-04-22", "end": "2024-04-27"},
                },
                "warmup": {"sessions": 20},
                "market_profile": MarketConfig.for_market("CN").model_dump(mode="json"),
                "optimizer": {"max_iterations": 1},
                "budgets": {
                    "max_model_calls": 10,
                    "max_tokens": 100000,
                    "max_runtime_seconds": 150,
                    "max_repairs": 0,
                },
                "environment_digest": capabilities["environment_digest"],
            }
        )
        run = self.request(
            client,
            "POST",
            "/research-runs",
            status=202,
            headers={"Idempotency-Key": uuid4().hex},
            json=config.model_dump(mode="json"),
        ).json()
        self.runs.append({"id": run["id"], "client": client, "config": config})
        return run["id"], config

    def finished(self, run_id):
        value = self.request(self.clients[0], "GET", f"/research-runs/{run_id}").json()
        require(
            value["status"] not in {"failed", "interrupted", "cancelled", "paused"},
            "Configured research did not succeed through the real worker",
        )
        return value if value["status"] == "succeeded" else None

    def cleanup(self):
        # Stop owned producers before discovering every registered evaluator UUID.
        # Otherwise the inherited M1 cleanup knows only coordinator attempt IDs.
        errors = []
        try:
            self.stop_processes(initial_signal=signal.SIGKILL)
        except RuntimeError:
            errors.append("owned process/session cleanup is unconfirmed")
        if self.created:
            try:
                with self.connection(private=True) as connection:
                    exists = connection.execute(
                        "SELECT to_regclass('research_evaluations')"
                    ).fetchone()[0]
                    if exists:
                        self.attempts.update(
                            str(row[0])
                            for row in connection.execute(
                                "SELECT id FROM research_evaluations"
                            )
                        )
            except Exception:  # noqa: BLE001 - always continue owned process/database cleanup
                errors.append("could not discover owned evaluation sandbox identities")
        super().cleanup()
        require(not errors, "; ".join(errors))


@pytest.fixture
def research_harness(tmp_path):
    if os.getenv("TRADEGRAD_REQUIRE_RESEARCH_INTEGRATION") != "1":
        pytest.skip(
            "Real PostgreSQL/Docker research smoke was not explicitly requested"
        )
    url = os.getenv("TEST_DATABASE_URL")
    require(bool(url), "TEST_DATABASE_URL is mandatory for real research acceptance")
    harness = None
    try:
        harness = ResearchProcessHarness(tmp_path, url, runtime_paths())
        harness.setup()
        yield harness
    except Exception as error:  # noqa: BLE001 - suppress credentials, environment mappings and HTTP payloads
        pytest.fail(
            f"Real configured research smoke failed ({type(error).__name__})",
            pytrace=False,
        )
    finally:
        if harness is not None:
            harness.cleanup()


def test_real_api_queue_docker_results_and_checkpoint(research_harness):
    h = research_harness
    dataset, universe = h.upload_fixture()
    run_id, config = h.submit_configured(dataset, universe)
    detail = h.until(
        lambda: h.finished(run_id), "Actual research worker did not finish", timeout=160
    )
    client, other = h.clients
    require(
        detail["execution_mode"] == "optimization",
        "Legacy fixed-seed route was substituted",
    )
    require(
        detail["result"]["best_candidate_id"] is not None,
        "No usable best candidate was published",
    )
    require(
        detail["latest_checkpoint_id"] is not None,
        "No durable checkpoint pointer was published",
    )
    require(
        detail["research_image_digest"].startswith("sha256:"),
        "Actual immutable image ID was not pinned",
    )
    require(
        len(detail["attempts"]) == 1 and detail["attempts"][0]["sandbox_stopped"],
        "Attempt sandbox termination is unconfirmed",
    )
    h.attempts.add(detail["attempts"][0]["id"])

    candidates = h.request(client, "GET", f"/research-runs/{run_id}/candidates").json()[
        "data"
    ]
    evaluations = h.request(
        client, "GET", f"/research-runs/{run_id}/evaluations"
    ).json()["data"]
    calls = h.request(client, "GET", f"/research-runs/{run_id}/model-calls").json()[
        "data"
    ]
    checkpoints = h.request(
        client, "GET", f"/research-runs/{run_id}/checkpoints"
    ).json()["data"]
    require(
        len(candidates) == 2 and len(evaluations) == 4,
        "Real optimization did not evaluate seed plus generated candidate",
    )
    require(
        bool(calls) and all(row["provider"] == "fixture" for row in calls),
        "Frozen model calls were not durably recorded",
    )
    planned = list(evaluation_windows(config))
    for evaluation in evaluations:
        h.attempts.add(evaluation["id"])
        require(
            evaluation["window"] in planned and evaluation["split"] != "holdout",
            "Evaluation escaped immutable train/validation plan",
        )
        require(
            evaluation["status"] == "succeeded" and evaluation["sandbox_stopped"],
            "A physical evaluation lacks successful confirmed termination",
        )
        require(
            evaluation["image_digest"] == detail["research_image_digest"],
            "Physical evaluator image drifted",
        )
        record = h.request(client, "GET", f"/evaluations/{evaluation['id']}").json()
        reference = record["payload"].get("result_bundle")
        require(bool(reference), "Physical evaluation omitted its native ResultBundle")
        content = h.request(
            client, "GET", f"/artifacts/{reference['artifact_id']}/download"
        ).content
        require(
            hashlib.sha256(content).hexdigest() == reference["sha256"],
            "Downloaded ResultBundle hash differs",
        )
        bundle = json.loads(content)
        validate_result_bundle(bundle)
        require(
            bundle["identity"]["run_config_sha256"] == config.sha256,
            "ResultBundle config identity differs",
        )
        h.request(other, "GET", f"/evaluations/{evaluation['id']}", status=404)
        h.request(
            other, "GET", f"/artifacts/{reference['artifact_id']}/download", status=404
        )
    latest = next(
        row for row in checkpoints if row["id"] == detail["latest_checkpoint_id"]
    )
    require(
        latest["complete"]
        and latest["next_stage"] == "finished"
        and latest["config_sha256"] == config.sha256,
        "Latest checkpoint is incomplete or unbound",
    )
    h.request(client, "GET", f"/research-checkpoints/{latest['id']}")
    h.request(other, "GET", f"/research-checkpoints/{latest['id']}", status=404)
    stream = h.request(client, "GET", f"/runs/{run_id}/events?follow=false").text
    events = [
        json.loads(line[6:])
        for line in stream.splitlines()
        if line.startswith("data: ")
    ]
    require(
        sum(event["type"] == "run_finished" for event in events) == 1,
        "Duplicate or missing final durable SSE event",
    )
    require(
        len({event["seq"] for event in events}) == len(events),
        "SSE sequence numbers were reused",
    )
    with h.connection(private=True) as connection:
        jobs = connection.execute(
            "SELECT j.status FROM outbox_events o JOIN procrastinate_jobs j ON j.id=o.job_id WHERE o.run_id=%s",
            (run_id,),
        ).fetchall()
        require(
            len(jobs) == 1 and jobs[0][0] in {"doing", "succeeded"},
            "Run bypassed the actual durable queue",
        )
        pointer = connection.execute(
            "SELECT c.manifest_storage_key, c.manifest_sha256, c.input_digest "
            "FROM research_runs r JOIN research_checkpoints c ON c.id=r.latest_checkpoint_id "
            "WHERE r.id=%s AND c.id=%s AND c.complete=true",
            (run_id, latest["id"]),
        ).fetchone()
        require(
            pointer is not None,
            "HTTP checkpoint pointer is not durably bound to the run",
        )
    manifest = safe_path(h.root / "storage", pointer[0])
    state = CheckpointStore.load(
        CheckpointRef(str(manifest), pointer[1], pointer[2]),
        expected_input_digest=latest["input_digest"],
    )
    require(
        state["next_stage"] == "finished", "Published checkpoint bytes are not terminal"
    )
    require(
        state["inputs"]["config"] == config.model_dump(mode="json"),
        "Checkpoint immutable config differs from submitted HTTP payload",
    )
    require(
        state["inputs"]["windows"] == planned,
        "Checkpoint includes an unplanned or holdout window",
    )
    h.request(other, "GET", f"/research-runs/{run_id}", status=404)


def research_rows(harness, run_id, resource):
    return harness.request(
        harness.clients[0], "GET", f"/research-runs/{run_id}/{resource}"
    ).json()["data"]


def model_fingerprints(rows):
    """Stable durable call identities, not latency, process or fixture call counts."""
    require(
        len({row["logical_call_id"] for row in rows}) == len(rows),
        "A logical model call has duplicate database records",
    )
    return {
        row["logical_call_id"]: {
            field: row[field]
            for field in (
                "id",
                "request_sha256",
                "response_sha256",
                "payload_sha256",
                "status",
                "usage",
            )
        }
        for row in rows
    }


def logical_lineage(rows):
    keys = {row["id"]: row["candidate_key"] for row in rows}
    return sorted(
        (
            row["candidate_key"],
            keys.get(row["parent_id"]),
            row["kind"],
            row["iteration"],
            row["repair_index"],
            row["code_sha256"],
        )
        for row in rows
    )


def kill_owned_worker_at_generated_evaluation(harness, run_id):
    """Observe durable generation, then stop only this harness's actual worker.

    SIGSTOP closes the observation/publication race while evidence is captured.
    Docker containers run independently. SIGKILL then prevents Python cleanup;
    only the production reconciler can acknowledge the orphan's termination.
    """
    h = harness
    client = h.clients[0]
    detail = h.request(client, "GET", f"/research-runs/{run_id}").json()
    require(
        detail["status"] not in {"succeeded", "failed", "cancelled", "interrupted"},
        "Configured run ended before the generated-evaluation crash boundary",
    )
    candidates = research_rows(h, run_id, "candidates")
    generated = {
        row["candidate_key"] for row in candidates if row["kind"] == "generated"
    }
    if not generated:
        return None
    calls = research_rows(h, run_id, "model-calls")
    if not calls or not all(
        row["status"] == "completed" and row["response_sha256"] for row in calls
    ):
        return None
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
    require(len(h.workers) == 1, "Fault injection requires one owned research worker")
    process = h.workers[0]
    require(
        process in h.expected_alive and peek_owned_group(process) is None,
        "Owned research worker exited before SIGKILL",
    )
    require(
        any(item is process for _, item in h.processes),
        "Worker PID was not created by this harness",
    )
    group = os.getpgid(process.pid)
    require(
        group == process.pid and group != os.getpgrp(),
        "Worker process group is not exclusively owned",
    )
    signal_owned_process(process, signal.SIGSTOP)
    try:
        # Worker threads cannot publish while this committed snapshot is captured.
        detail = h.request(client, "GET", f"/research-runs/{run_id}").json()
        calls = research_rows(h, run_id, "model-calls")
        evaluations = research_rows(h, run_id, "evaluations")
        require(
            detail["status"] == "running" and detail["generation"] == 1,
            "Worker passed the intended first-generation crash boundary",
        )
        require(
            detail["latest_checkpoint_id"] is not None,
            "No durable pre-crash checkpoint exists",
        )
        active = next(row for row in evaluations if row["id"] == active["id"])
        require(
            not active["sandbox_stopped"],
            "Generated evaluation completed before the owned-worker crash",
        )
        require(
            all(
                row["status"] == "completed" and row["response_sha256"] for row in calls
            ),
            "Crash boundary contains an unknown model response",
        )
        checkpoint = h.request(
            client, "GET", f"/research-checkpoints/{detail['latest_checkpoint_id']}"
        ).json()
        require(
            checkpoint["next_stage"] == "evaluate" and checkpoint["iteration"] == 1,
            "Checkpoint did not preserve generated candidate evaluation stage",
        )
        snapshot = {
            "attempt_id": detail["active_attempt_id"],
            "checkpoint_id": detail["latest_checkpoint_id"],
            "active_evaluation_id": active["id"],
            "evaluations": evaluations,
            "calls": model_fingerprints(calls),
            "lineage": logical_lineage(research_rows(h, run_id, "candidates")),
        }
    finally:
        # The PID/group were just verified and remain owned. No broad process scan.
        h.expected_alive.discard(process)
        signal_owned_group(process, signal.SIGKILL)
        require(
            wait_owned_group(process, timeout=h.remaining(5)) == -signal.SIGKILL,
            "Owned research worker was not killed by SIGKILL",
        )
        # Reaped PIDs are never signaled again by later harness cleanup.
        h.processes = [
            (label, item) for label, item in h.processes if item is not process
        ]
    h.attempts.update(row["id"] for row in evaluations)
    h.attempts.add(snapshot["attempt_id"])
    return snapshot


def test_real_sigkill_reconciles_and_resumes_generated_candidate_without_model_replay(
    research_harness,
):
    h = research_harness
    dataset, universe = h.upload_fixture()
    reference_id, reference_config = h.submit_configured(dataset, universe)
    reference = h.until(
        lambda: h.finished(reference_id),
        "Uninterrupted reference optimization did not finish",
        timeout=100,
    )
    reference_lineage = logical_lineage(research_rows(h, reference_id, "candidates"))
    run_id, config = h.submit_configured(dataset, universe)
    require(
        config == reference_config,
        "Recovery fixture changed the immutable reference configuration",
    )
    old = h.until(
        lambda: kill_owned_worker_at_generated_evaluation(h, run_id),
        "No actual generated-candidate Docker evaluation reached the crash boundary",
        timeout=75,
    )
    client = h.clients[0]
    h.request(client, "POST", f"/research-runs/{run_id}/resume", status=409)

    def reconciled():
        detail = h.request(client, "GET", f"/research-runs/{run_id}").json()
        if detail["status"] != "interrupted":
            require(
                detail["status"] == "running",
                "Unexpected state while waiting for production reconciliation",
            )
            return None
        require(
            detail["generation"] == 1 and len(detail["attempts"]) == 1,
            "Reconciliation created an unsolicited execution attempt",
        )
        attempt = detail["attempts"][0]
        require(
            attempt["id"] == old["attempt_id"]
            and attempt["status"] == "interrupted"
            and attempt["sandbox_stopped"]
            and attempt["finished_at"],
            "Old coordinator was not durably fenced and stopped",
        )
        require(
            detail["latest_checkpoint_id"] == old["checkpoint_id"],
            "Recovery guessed or advanced the authoritative checkpoint pointer",
        )
        evaluations = research_rows(h, run_id, "evaluations")
        require(
            all(row["sandbox_stopped"] for row in evaluations),
            "Reconciler left a registered evaluator unconfirmed",
        )
        interrupted = next(
            row for row in evaluations if row["id"] == old["active_evaluation_id"]
        )
        require(
            interrupted["status"] == "interrupted" and interrupted["finished_at"],
            "Killed generated evaluator was not durably interrupted",
        )
        require(
            all(h.runner._inspect(row["id"]) is None for row in evaluations),
            "An old physical Docker child survived reconciliation",
        )
        require(
            h.runner._inspect(old["attempt_id"]) is None,
            "Old coordinator-named sandbox survived reconciliation",
        )
        return detail

    h.until(
        reconciled,
        "Production reconciler did not contain the killed worker's children",
        timeout=25,
    )
    require(
        model_fingerprints(research_rows(h, run_id, "model-calls")) == old["calls"],
        "Reconciliation modified completed model calls",
    )
    resumed = h.request(
        client, "POST", f"/research-runs/{run_id}/resume", status=202
    ).json()
    require(
        resumed["generation"] == 2
        and resumed["resume_checkpoint_id"] == old["checkpoint_id"],
        "HTTP resume did not bind generation 2 to the verified old pointer",
    )
    h.workers = h.start_workers()
    final = h.until(
        lambda: h.finished(run_id),
        "Resumed configured optimization did not finish",
        timeout=80,
    )
    require(
        final["generation"] == 2 and len(final["attempts"]) == 2,
        "Resume produced a duplicate or missing attempt",
    )
    require(
        all(row["sandbox_stopped"] for row in final["attempts"]),
        "A resumed coordinator has unconfirmed termination",
    )
    require(
        final["attempts"][1]["resumed_checkpoint_id"] == old["checkpoint_id"],
        "New attempt did not consume the selected checkpoint",
    )
    require(
        final["config"] == reference["config"] == config.model_dump(mode="json"),
        "Immutable config drifted across recovery",
    )
    require(
        final["research_image_digest"] == reference["research_image_digest"],
        "Recovered run changed evaluator image",
    )
    for field in ("best_candidate_key", "combined_score", "stop_reason"):
        require(
            final["result"][field] == reference["result"][field],
            f"Resumed {field} differs from uninterrupted optimization",
        )
    final_lineage = logical_lineage(research_rows(h, run_id, "candidates"))
    require(
        final_lineage == old["lineage"] == reference_lineage,
        "Recovery changed generated candidate code or parent lineage",
    )
    calls = research_rows(h, run_id, "model-calls")
    require(
        model_fingerprints(calls) == old["calls"],
        "Resume replayed, duplicated or changed a completed model call/response",
    )
    require(
        len(calls) == len(research_rows(h, reference_id, "model-calls")),
        "Recovery charged an extra logical model call",
    )
    evaluations = research_rows(h, run_id, "evaluations")
    old_ids = {row["id"] for row in old["evaluations"]}
    fresh = [row for row in evaluations if row["generation"] == 2]
    require(
        len(fresh) == 2 and {row["id"] for row in fresh}.isdisjoint(old_ids),
        "Resume reused old physical evaluation UUIDs or omitted an unfinished window",
    )
    require(
        all(row["status"] == "succeeded" and row["sandbox_stopped"] for row in fresh),
        "Fresh physical evaluations did not succeed and stop",
    )
    require(
        all(row["candidate_key"] == "candidate-0001" for row in fresh),
        "Resume unnecessarily reevaluated the completed seed",
    )
    require(
        {json.dumps(row["window"], sort_keys=True) for row in fresh}
        == {json.dumps(row, sort_keys=True) for row in evaluation_windows(config)},
        "Recovery executed unplanned windows",
    )
    for prior in old["evaluations"]:
        current = next(row for row in evaluations if row["id"] == prior["id"])
        if prior["sandbox_stopped"]:
            require(current == prior, "Resume rewrote a completed physical evaluation")
        require(
            h.runner._inspect(prior["id"]) is None,
            "An original Docker child reappeared after resume",
        )
    h.attempts.update(row["id"] for row in evaluations)
    h.attempts.update(row["id"] for row in final["attempts"])
    latest = h.request(
        client, "GET", f"/research-checkpoints/{final['latest_checkpoint_id']}"
    ).json()
    require(
        latest["complete"]
        and latest["generation"] == 2
        and latest["next_stage"] == "finished"
        and latest["config_sha256"] == config.sha256,
        "Resumed attempt did not publish its own complete terminal checkpoint",
    )
    stream = h.request(client, "GET", f"/runs/{run_id}/events?follow=false").text
    events = [
        json.loads(line[6:])
        for line in stream.splitlines()
        if line.startswith("data: ")
    ]
    terminal = [event for event in events if event["type"] == "run_finished"]
    require(
        len(terminal) == 2
        and [event["payload"]["status"] for event in terminal]
        == ["interrupted", "succeeded"],
        "Recovery must publish one final SSE event per actual attempt",
    )
