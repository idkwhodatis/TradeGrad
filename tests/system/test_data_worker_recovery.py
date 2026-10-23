"""Hosted M2 gate: kill an actual data worker after durable checkpoints exist.

Requires TRADEGRAD_REQUIRE_DATA_PROCESS_RECOVERY=1, TEST_DATABASE_URL (CREATEDB),
DATA_PYTHON (the separate locked data runtime), and the prebuilt Docker evaluator.
SDK-schema fixtures are synthetic, not live-provider evidence. All business writes
go through the API, actual queue workers, or the real dispatcher. SQL is read-only.
The existing M1 harness supplies disposable ownership, HTTP, process and cleanup
interfaces unchanged. Collection and opt-out never start sockets or containers.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from test_worker_recovery import ROOT, ProcessHarness, require

from script.ci_owned_group import (
    admit_owned_group,
    peek_owned_group,
    signal_owned_group,
    wait_owned_group,
)

START, END = "2024-01-02", "2024-07-01"
BLOCK_DAYS = 7
BLOCK_COUNT = 2 * math.ceil(
    (date.fromisoformat(END) - date.fromisoformat(START)).days / BLOCK_DAYS
)
LEASE_SECONDS = 30
DISPATCH_SECONDS = 2
QUEUE_STALE_SECONDS = 120  # Production maximum; pruning can clear worker_id sooner.
# Setup 60s, initial checkpoint 30s, lease recovery 40s, resumed 52 paced blocks
# 80s, original Docker seed 60s, final verification 30s. Queue cleanup overlaps.
GATE_SECONDS = 300


def dedicated_data_python():
    value = os.getenv("DATA_PYTHON")
    require(bool(value), "DATA_PYTHON is mandatory for the required data recovery gate")
    executable = Path(value).absolute()
    require(
        executable.is_file() and os.access(executable, os.X_OK),
        "DATA_PYTHON must identify an executable dedicated data runtime",
    )
    # Venv executables may resolve to the same base binary: compare sys.prefix,
    # not resolved symlink paths. This probe imports no SDK and exposes no env.
    probe = subprocess.run(
        [
            str(executable),
            "-I",
            "-c",
            (
                "import importlib.util,json,sys;print(json.dumps({'prefix':sys.prefix,"
                "'ready':all(importlib.util.find_spec(x) is not None for x in "
                "('pandas','pyarrow','exchange_calendars','yfinance','procrastinate'))}))"
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
    require(probe.returncode == 0, "Dedicated data runtime probe failed")
    metadata = json.loads(probe.stdout)
    require(
        metadata["prefix"] != sys.prefix and metadata["ready"],
        "DATA_PYTHON must be a separate installed data environment, not the API interpreter",
    )
    return str(executable)


def fingerprint(path):
    require(
        path.is_file() and not path.is_symlink(), "Expected an owned regular artifact"
    )
    before = path.stat()
    content = path.read_bytes()
    after = path.stat()
    require(
        (before.st_ino, before.st_size, before.st_mtime_ns)
        == (after.st_ino, after.st_size, after.st_mtime_ns),
        "Artifact changed while its immutable fingerprint was recorded",
    )
    require(after.st_mode & 0o222 == 0, "Published artifact is writable")
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "inode": after.st_ino,
    }


class DataProcessHarness(ProcessHarness):
    def __init__(self, root, database_url, data_python):
        super().__init__(root, database_url)
        self.deadline = time.monotonic() + GATE_SECONDS
        self.data_python = data_python
        self.env.update(
            DATA_PROVIDER_MODE="fixture",
            DATA_LEASE_SECONDS=str(LEASE_SECONDS),
            DATA_BLOCK_DAYS=str(BLOCK_DAYS),
            DATA_WORKER_CONCURRENCY="1",
        )

    def remaining(self, maximum):
        remaining = min(maximum, self.deadline - time.monotonic())
        require(remaining > 0, "Data process recovery exceeded its 300-second budget")
        return remaining

    def spawn_data(self, role):
        require(role in {"worker", "dispatcher"}, "Unsupported data runtime role")
        process = subprocess.Popen(
            [self.data_python, "-m", "workers.data", role],
            cwd=ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.processes.append((f"data-{role}", process))
        admit_owned_group(process)
        self.expected_alive.add(process)
        return process

    def rows(self, query, parameters=()):
        with (
            self.connection(private=True) as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return cursor.execute(query, parameters).fetchall()

    def import_detail(self):
        return self.request(
            self.clients[0], "GET", f"/data/imports/{self.import_id}"
        ).json()

    def import_row(self):
        rows = self.rows(
            "SELECT owner_id, status, generation, lease_token, lease_expires_at, "
            "dataset_version_id, now() AS observed_at FROM data_imports WHERE id=%s",
            (self.import_id,),
        )
        require(len(rows) == 1, "Durable import identity disappeared")
        require(str(rows[0]["owner_id"]) == self.owner_id, "Import owner changed")
        return rows[0]

    def blocks(self):
        return self.rows(
            "SELECT id, symbol, start_date, end_date, status, attempts, storage_key, "
            "content_sha256, updated_at FROM data_blocks WHERE import_id=%s "
            "ORDER BY symbol, start_date",
            (self.import_id,),
        )

    def queue_jobs(self):
        rows = self.rows(
            "SELECT o.generation, o.job_id, o.dispatched_at, j.status, j.args, j.attempts "
            "FROM data_outbox o LEFT JOIN procrastinate_jobs j ON j.id=o.job_id "
            "WHERE o.import_id=%s ORDER BY o.generation",
            (self.import_id,),
        )
        for row in rows:
            if row["job_id"] is not None:
                require(
                    row["args"]
                    == {"import_id": self.import_id, "generation": row["generation"]},
                    "Queue task identity/generation crossed",
                )
        return rows

    def dataset_ids(self):
        return {
            str(row["id"])
            for row in self.rows(
                "SELECT id FROM dataset_versions WHERE owner_id=%s", (self.owner_id,)
            )
        }

    def checkpoints(self, blocks):
        snapshots = {}
        for block in blocks:
            if block["status"] != "ready":
                continue
            key = f"data-checkpoints/{self.owner_id}/{self.import_id}/{block['content_sha256']}.json"
            require(
                block["storage_key"] == key,
                "Checkpoint escaped its owner/import namespace",
            )
            snapshot = {**block, "file": fingerprint(self.root / "storage" / key)}
            require(
                snapshot["file"]["sha256"] == block["content_sha256"],
                "Checkpoint hash mismatch",
            )
            require(block["attempts"] >= 1, "Checkpoint has no recorded fetch attempt")
            snapshots[str(block["id"])] = snapshot
        return snapshots

    def verify_reuse(self, old):
        current = self.checkpoints(self.blocks())
        require(
            all(current.get(key) == value for key, value in old.items()),
            "Previously ready checkpoint was refetched, replaced or modified",
        )

    def checkpoint_ready(self):
        detail = self.import_detail()
        require(
            detail["status"] not in {"ready", "failed", "partial", "cancelled"},
            "Import ended before the actual crash",
        )
        blocks = self.blocks()
        if not any(block["status"] == "ready" for block in blocks):
            return False
        require(
            len(blocks) == BLOCK_COUNT, "Fixture listing/date block plan is incomplete"
        )
        require(
            any(block["status"] != "ready" for block in blocks),
            "Nothing remained to resume",
        )
        jobs = self.queue_jobs()
        require(
            len(jobs) == 1 and jobs[0]["status"] == "doing",
            "Import did not run through the actual queue",
        )
        self.original_job_id = jobs[0]["job_id"]
        self.original_job_attempts = jobs[0]["attempts"]
        lease = self.import_row()
        require(
            lease["generation"] == 1
            and lease["lease_token"] is not None
            and lease["lease_expires_at"] > lease["observed_at"],
            "Original import has no valid live worker lease",
        )
        self.original_token = lease["lease_token"]
        require(
            all(
                block["attempts"] == 1 for block in blocks if block["status"] == "ready"
            ),
            "Fresh checkpoint required an unexpected retry",
        )
        return self.checkpoints(blocks)

    def crash_data_worker(self):
        process = self.data_worker
        require(
            process in self.expected_alive and peek_owned_group(process) is None,
            "Owned data worker exited before SIGKILL",
        )
        group = os.getpgid(process.pid)
        require(
            group == process.pid and group != os.getpgrp(),
            "Data worker process group is not exclusively owned",
        )
        self.expected_alive.remove(process)
        self.killed_at = time.monotonic()
        signal_owned_group(process, signal.SIGKILL)
        require(
            wait_owned_group(process, timeout=self.remaining(5)) == -signal.SIGKILL,
            "Data worker was not killed with SIGKILL",
        )
        # The exact group received SIGKILL and its leader is reaped. Do not retain
        # its PID for later cleanup, where PID reuse is possible.
        self.processes = [
            (label, owned) for label, owned in self.processes if owned is not process
        ]
        lease = self.import_row()
        require(
            lease["generation"] == 1
            and lease["status"] == "fetching"
            and lease["lease_token"] == self.original_token,
            "Import stopped being the original active generation at the crash",
        )
        self.crashed_lease_expires_at = lease["lease_expires_at"]
        # No SIGTERM, graceful task completion, helper execute(), or SQL lease edits.
        self.alive()

    def recovered(self, snapshots):
        detail = self.import_detail()
        row = self.import_row()
        require(
            detail["dataset_version_id"] is None
            and self.dataset_ids() == self.initial_datasets,
            "Dead/stale worker published a dataset",
        )
        self.verify_reuse(snapshots)
        if row["generation"] == 1:
            require(
                row["status"] == "fetching"
                and row["lease_token"] == self.original_token,
                "Unexplained pre-recovery state change",
            )
            return False
        # HTTP and SQL are independent reads. If the dispatcher committed between
        # them, wait for the next consistent observation instead of flaking.
        if detail["generation"] != row["generation"]:
            return False
        require(
            row["generation"] == 2
            and row["status"] == "requested"
            and row["lease_token"] is None
            and row["lease_expires_at"] is None
            and detail["progress"].get("recovered_after_worker_loss") is True,
            "Actual dispatcher did not fence and recover the expired generation",
        )
        require(
            row["observed_at"] >= self.crashed_lease_expires_at,
            "Dispatcher recovered an unexpired data lease",
        )
        jobs = self.queue_jobs()
        require(
            len(jobs) == 2,
            "Recovery duplicated or omitted the durable outbox generation",
        )
        require(
            jobs[0]["job_id"] == self.original_job_id,
            "Recovery replaced original queue history",
        )
        return jobs[1]["dispatched_at"] is not None and jobs[1]["status"] == "todo"

    def ready(self):
        detail = self.import_detail()
        require(
            detail["status"] not in {"partial", "failed", "cancelled"},
            "Resumed fixture import failed or lost coverage",
        )
        require(
            detail["generation"] == 2,
            "Resumed import unexpectedly acquired another generation",
        )
        return detail if detail["status"] == "ready" else False

    def dataset_snapshot(self, dataset_id):
        dataset = self.request(self.clients[0], "GET", f"/datasets/{dataset_id}").json()
        records = self.rows(
            "SELECT owner_id, storage_key FROM dataset_versions WHERE id=%s",
            (dataset_id,),
        )
        key = f"datasets/{self.owner_id}/{dataset_id}"
        require(
            len(records) == 1
            and str(records[0]["owner_id"]) == self.owner_id
            and records[0]["storage_key"] == key,
            "Dataset owner/storage namespace crossed",
        )
        directory = self.root / "storage" / key
        require(
            directory.is_dir() and not directory.is_symlink(),
            "Immutable dataset was not atomically published",
        )
        manifest = dataset["manifest"]
        require(
            dataset["source"] == "yfinance"
            and dataset["is_synthetic"]
            and dataset["start_date"] == START
            and dataset["end_date"] == END
            and manifest["ready"]
            and manifest["evaluation_compatibility"]["compatible"]
            and {item["symbol"] for item in manifest["listings"]} == {"AAPL", "MSFT"}
            and all(item["evidence"] == "fixture" for item in manifest["provenance"]),
            "Published fixture identity, coverage or synthetic disclosure changed",
        )
        require(
            len(manifest["coverage"]) == 2
            and all(
                item["status"] == "complete" and not item["missing_sessions"]
                for item in manifest["coverage"]
            ),
            "Recovered snapshot lacks complete per-listing coverage",
        )
        files = {}
        for path in directory.rglob("*"):
            require(not path.is_symlink(), "Published dataset contains a symlink")
            if path.is_file():
                files[path.relative_to(directory).as_posix()] = fingerprint(path)
        require(
            files["manifest.json"]["sha256"] == dataset["content_sha256"],
            "Immutable manifest hash mismatch",
        )
        require(
            json.loads((directory / "manifest.json").read_bytes()) == manifest,
            "API manifest differs from immutable storage",
        )
        expected = {**manifest["files"], **manifest["artifact_files"]}
        for pages in manifest["preview_files"].values():
            expected.update({page["path"]: page["sha256"] for page in pages})
        require(
            all(
                files.get(name, {}).get("sha256") == digest
                for name, digest in expected.items()
            ),
            "Published dataset artifact hash mismatch",
        )
        return {"dataset": dataset, "files": files}

    def evaluate_original_seed(self, dataset):
        strategies = self.request(self.clients[0], "GET", "/strategy-versions").json()[
            "data"
        ]
        candidates = [
            item
            for item in strategies
            if item["name"] == "Original us_cross seed" and item["task"] == "us_cross"
        ]
        require(len(candidates) == 1, "Original US cross-sectional seed is missing")
        strategy = candidates[0]
        require(
            strategy["content_sha256"]
            == hashlib.sha256(
                (ROOT / "seed_strategies/us_cross.py").read_bytes()
            ).hexdigest(),
            "Original US seed was modified",
        )
        config = {
            "task": "us_cross",
            "profile": "corrected_explicit_config",
            "dataset_version_id": dataset["id"],
            "strategy_version_id": strategy["id"],
            "start_date": START,
            "end_date": END,
        }
        response = self.request(
            self.clients[0],
            "POST",
            "/runs",
            status=202,
            json=config,
            headers={"Idempotency-Key": str(uuid4())},
        ).json()
        run = {"id": response["id"], "client": self.clients[0], "config": config}
        self.runs.append(run)
        self.until(
            self.succeeded,
            "Original US seed did not complete in real Docker",
            timeout=60,
        )
        detail = self.detail(run)
        require(
            len(detail["attempts"]) == 1 and detail["generation"] == 1,
            "Original US seed needed an unexpected retry",
        )
        attempt = detail["attempts"][0]
        self.attempts.add(attempt["id"])
        require(
            attempt["status"] == "succeeded"
            and attempt["exit_code"] == 0
            and attempt["sandbox_stopped"],
            "Real Docker attempt did not succeed and stop",
        )
        require(
            detail["dataset_version_id"] == dataset["id"]
            and detail["config"]["dataset_sha256"] == dataset["content_sha256"]
            and detail["result"]["evaluation_mode"] == "native_akquant"
            and detail["result"]["is_synthetic"]
            and detail["result"]["image_digest"] == self.runner._image_id(),
            "Evaluation did not use the recovered snapshot and real pinned Docker image",
        )
        source = self.root / "storage" / "runs" / run["id"] / attempt["id"] / "input"
        require(
            fingerprint(source / "strategy.py")["sha256"] == strategy["content_sha256"],
            "Docker input did not retain the exact original US seed",
        )
        for name, digest in dataset["manifest"]["files"].items():
            require(
                fingerprint(source / name)["sha256"] == digest,
                "Recovered data changed during evaluator staging",
            )
        artifacts = self.request(
            self.clients[0], "GET", f"/runs/{run['id']}/artifacts"
        ).json()["data"]
        require(
            {item["name"] for item in artifacts}
            == {"metrics.json", "logs.txt", "provenance.json"},
            "Real evaluation artifacts are incomplete",
        )
        for artifact in artifacts:
            require(
                artifact["run_id"] == run["id"]
                and artifact["attempt_id"] == attempt["id"],
                "Artifact run/attempt ownership crossed",
            )
            path = f"/artifacts/{artifact['id']}/download"
            content = self.request(self.clients[0], "GET", path).content
            require(
                hashlib.sha256(content).hexdigest() == artifact["sha256"]
                and len(content) == artifact["size_bytes"],
                "Downloaded artifact hash/size mismatch",
            )
            self.request(self.clients[1], "GET", path, status=404)
            if artifact["name"] == "metrics.json":
                metrics = json.loads(content)
                require(
                    metrics["metrics"] == detail["result"]["metrics"]
                    and metrics["market"] == "US"
                    and metrics["profile"] == config["profile"],
                    "Real seed metrics do not belong to the imported US snapshot",
                )
        self.request(self.clients[1], "GET", f"/runs/{run['id']}", status=404)
        require(
            self.runner._inspect(attempt["id"]) is None,
            "Completed evaluator container survived cleanup",
        )

    def stale_job_finished(self):
        jobs = self.queue_jobs()
        require(
            len(jobs) == 2 and jobs[0]["job_id"] == self.original_job_id,
            "Stale redelivery changed queue generation history",
        )
        require(
            all(item["status"] not in {"failed", "aborted"} for item in jobs),
            "Actual data queue failed during stale redelivery",
        )
        if any(item["status"] != "succeeded" for item in jobs):
            return False
        require(
            jobs[0]["attempts"] > self.original_job_attempts,
            "Orphaned queue job was not actually redelivered",
        )
        return True


@pytest.fixture
def data_process_harness(tmp_path):
    if os.getenv("TRADEGRAD_REQUIRE_DATA_PROCESS_RECOVERY") != "1":
        pytest.skip("Hosted data process recovery gate was not explicitly requested")
    harness = None
    try:
        deadline = time.monotonic() + GATE_SECONDS
        data_python = dedicated_data_python()
        value = os.getenv("TEST_DATABASE_URL")
        require(bool(value), "TEST_DATABASE_URL is required for the data process gate")
        harness = DataProcessHarness(tmp_path, value, data_python)
        harness.deadline = deadline
        harness.setup()
        harness.owner_id = harness.request(
            harness.clients[0], "GET", "/users/me"
        ).json()["id"]
        harness.initial_datasets = harness.dataset_ids()
        harness.spawn_data("dispatcher")
        harness.data_worker = harness.spawn_data("worker")
        yield harness
    except Exception as exc:  # noqa: BLE001 - do not reflect subprocess env or credentials
        pytest.fail(
            f"Data process harness failed ({type(exc).__name__})", pytrace=False
        )
    finally:
        if harness is not None:
            harness.cleanup()


def test_sigkill_reuses_checkpoints_then_original_us_seed_runs_in_real_docker(
    data_process_harness,
):
    harness = data_process_harness
    response = harness.request(
        harness.clients[0],
        "POST",
        "/data/imports",
        status=202,
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "name": "SIGKILL recovery SDK-schema fixture, NOT live market data",
            "provider": "yfinance",
            "market": "US",
            "symbols": ["AAPL", "MSFT"],
            "start_date": START,
            "end_date": END,
            "price_mode": "provider_native",
        },
    ).json()
    harness.import_id = response["id"]
    harness.request(
        harness.clients[1], "GET", f"/data/imports/{harness.import_id}", status=404
    )
    observed = harness.until(
        harness.checkpoint_ready,
        "No durable checkpoint existed before the actual worker crash",
        timeout=30,
    )
    harness.crash_data_worker()
    harness.verify_reuse(observed)
    interrupted_blocks = harness.blocks()
    prior = harness.checkpoints(interrupted_blocks)
    require(
        0 < len(prior) < BLOCK_COUNT,
        "Crash did not leave both durable and unfinished blocks",
    )
    harness.until(
        lambda: harness.recovered(prior),
        "Actual dispatcher did not recover the expired import lease",
        timeout=LEASE_SECONDS + 5 * DISPATCH_SECONDS,
    )
    harness.data_worker = harness.spawn_data("worker")
    ready = harness.until(
        harness.ready,
        "Resumed data worker did not publish a complete immutable snapshot",
        timeout=BLOCK_COUNT + 28,
    )
    harness.verify_reuse(prior)
    completed_blocks = {str(block["id"]): block for block in harness.blocks()}
    require(
        len(completed_blocks) == BLOCK_COUNT
        and set(completed_blocks) == {str(block["id"]) for block in interrupted_blocks}
        and all(
            completed_blocks[str(block["id"])]["status"] == "ready"
            and completed_blocks[str(block["id"])]["attempts"]
            == block["attempts"] + (block["status"] != "ready")
            for block in interrupted_blocks
        ),
        "Resumed block plan was duplicated, incomplete, or refetched ready blocks",
    )
    dataset_id = ready["dataset_version_id"]
    require(
        dataset_id is not None
        and harness.dataset_ids() == harness.initial_datasets | {dataset_id},
        "Recovery published zero or multiple dataset versions",
    )
    snapshot = harness.dataset_snapshot(dataset_id)
    for suffix in ("", "/coverage"):
        harness.request(
            harness.clients[1], "GET", f"/datasets/{dataset_id}{suffix}", status=404
        )
    listing_id = snapshot["dataset"]["manifest"]["listings"][0]["listing_id"]
    harness.request(
        harness.clients[1],
        "GET",
        f"/datasets/{dataset_id}/preview",
        status=404,
        params={"listing_id": listing_id},
    )
    harness.evaluate_original_seed(snapshot["dataset"])
    # Actual dispatcher requeues generation 1 by its 120s stale-worker threshold,
    # or earlier if the replacement worker prunes its dead queue-worker record.
    # Its actual stale-generation redelivery must be a fenced no-op.
    remaining_stall = (
        QUEUE_STALE_SECONDS
        + 5 * DISPATCH_SECONDS
        - (time.monotonic() - harness.killed_at)
    )
    harness.until(
        harness.stale_job_finished,
        "Actual dispatcher did not reclaim/redeliver the orphaned queue job",
        timeout=max(1, remaining_stall),
    )
    harness.verify_reuse(prior)
    require(
        harness.ready()["dataset_version_id"] == dataset_id,
        "Stale queue delivery replaced the completed generation",
    )
    require(
        harness.dataset_ids() == harness.initial_datasets | {dataset_id},
        "Stale queue delivery published another dataset",
    )
    require(
        harness.dataset_snapshot(dataset_id) == snapshot,
        "Evaluation or stale redelivery modified the immutable snapshot",
    )
