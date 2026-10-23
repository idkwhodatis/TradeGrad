"""Prepared REAL PostgreSQL/API/queue/two-container portable pipeline gate.

Explicitly opt in with TRADEGRAD_REQUIRE_PORTABLE_RESEARCH_INTEGRATION=1 and
TEST_DATABASE_URL (CREATEDB), API_PYTHON, RESEARCH_PYTHON, DATA_PYTHON. Both
portable images must already be built from the exact current reviewed inputs.
Collecting or opting out opens no sockets, starts no processes, and never invokes
Docker. Offline controller tests are NOT evidence that this gate has passed.
Only frozen model responses and the explicitly synthetic raw-price data provider
are allowed. Candidate policy execution stays in the production policy container.
"""

import hashlib
import json
import os
import signal
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_research_optimization import ResearchProcessHarness, runtime_paths
from test_worker_recovery import require

from packages.tradegrad_adapter.portable_config import (
    PortableRunConfig,
    portable_evaluation_windows,
)
from packages.tradegrad_adapter.portable_environment import portable_environment_digest
from packages.tradegrad_adapter.portable_results import validate_portable_result
from packages.tradegrad_adapter.strategy_package import validate_environment_evidence
from runtimes.evaluator.portable_runner import PortableDockerEvaluator


class PortableProcessHarness(ResearchProcessHarness):
    def __init__(self, root, database_url, interpreters):
        super().__init__(root, database_url, interpreters)
        self.deadline = time.monotonic() + 420
        self.runner = PortableDockerEvaluator(state_dir=root / "runner")
        self.data_started = False

    def remaining(self, maximum):
        remaining = min(maximum, self.deadline - time.monotonic())
        require(remaining > 0, "Portable system gate exceeded its 420-second bound")
        return remaining

    def stop_idle_research_workers(self):
        # Called before this test submits research. Only our recorded groups qualify.
        for process in self.workers:
            require(
                any(owned is process for _, owned in self.processes),
                "Worker is not harness-owned",
            )
            require(
                process.poll() is None
                and os.getpgid(process.pid) == process.pid != os.getpgrp(),
                "Research worker has no exclusive owned process group",
            )
            self.expected_alive.discard(process)
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=self.remaining(10))
        self.workers = []

    def import_fixture(self, market):
        if not self.data_started:
            for role in ("dispatcher", "worker"):
                self.spawn(f"data-{role}", ["-m", "workers.data", role])
            self.data_started = True
        client = self.clients[0]
        record = self.request(
            client,
            "POST",
            "/data/imports",
            status=202,
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "provider": "portable_fixture",
                "name": "Explicit synthetic portable pipeline gate",
                "market": market,
                "symbols": ["600000"] if market == "CN" else ["AAPL", "MSFT"],
                "start_date": "2024-01-02",
                "end_date": "2024-03-01",
                "price_mode": "raw",
            },
        ).json()

        def complete():
            value = self.request(client, "GET", f"/data/imports/{record['id']}").json()
            require(
                value["status"] not in {"failed", "partial", "cancelled"},
                "Synthetic data worker failed",
            )
            return value if value["status"] == "ready" else None

        imported = self.until(
            complete, "Synthetic data import did not complete", timeout=70
        )
        dataset = self.request(
            client, "GET", f"/datasets/{imported['dataset_version_id']}"
        ).json()
        require(dataset["is_synthetic"], "Artificial fixture lost its synthetic label")
        universe = self.request(
            client,
            "POST",
            "/data/universes",
            status=201,
            json={
                "name": "Explicit fixed synthetic portable universe",
                "state": "user_fixed",
                "as_of": datetime.now(UTC).isoformat(),
                "members": [
                    {"listing_id": value, "member_from": None, "member_to": None}
                    for value in imported["listing_ids"]
                ],
                "evidence": [],
            },
        ).json()
        return dataset, universe

    def finished_portable(self, run_id):
        detail = self.request(self.clients[0], "GET", f"/portable-runs/{run_id}").json()
        require(
            detail["status"] not in {"failed", "cancelled", "interrupted", "paused"},
            "Real portable worker did not succeed",
        )
        return detail if detail["status"] == "succeeded" else None


@pytest.fixture
def portable_harness(tmp_path):
    if os.getenv("TRADEGRAD_REQUIRE_PORTABLE_RESEARCH_INTEGRATION") != "1":
        pytest.skip(
            "Real portable PostgreSQL/Docker pipeline gate was not explicitly requested"
        )
    url = os.getenv("TEST_DATABASE_URL")
    require(bool(url), "TEST_DATABASE_URL is required for the real portable gate")
    harness = None
    try:
        harness = PortableProcessHarness(tmp_path, url, runtime_paths())
        harness.runner.bind_environment(portable_environment_digest())
        harness.setup()
        yield harness
    except Exception as error:  # noqa: BLE001 - never expose credentials or HTTP request bodies
        pytest.fail(
            f"Real portable pipeline failed ({type(error).__name__})", pytrace=False
        )
    finally:
        if harness is not None:
            harness.cleanup()


@pytest.mark.parametrize("kind,market", [("event", "CN"), ("portfolio", "US")])
def test_real_portable_api_queue_both_containers_lineage_results_and_ownership(
    portable_harness, kind, market
):
    h = portable_harness
    client, other = h.clients
    dataset, universe = h.import_fixture(market)
    dataset_ref = {"id": dataset["id"], "sha256": dataset["content_sha256"]}
    universe_ref = {"id": universe["id"], "sha256": universe["content_sha256"]}
    strategy = h.request(
        client,
        "POST",
        "/portable-strategies",
        status=201,
        json={
            "name": f"Portable {kind} real gate",
            "strategy_kind": kind,
            "dataset": dataset_ref,
            "universe": universe_ref,
        },
    ).json()
    h.request(other, "GET", f"/portable-strategies/{strategy['id']}", status=404)
    body = h.request(
        client,
        "POST",
        "/portable-runs/default-config",
        json={
            "dataset": dataset_ref,
            "universe": universe_ref,
            "seed_strategy": {
                "id": strategy["id"],
                "sha256": strategy["content_sha256"],
            },
            "execution_mode": "optimization",
            "windows": {
                "train": {"start": "2024-01-10", "end": "2024-02-01"},
                "validation": {"start": "2024-02-01", "end": "2024-02-15"},
                "holdout": {"start": "2024-02-15", "end": "2024-03-01"},
            },
        },
    ).json()
    body["optimizer"]["max_iterations"] = 1
    body["budgets"].update(
        max_model_calls=10, max_tokens=100000, max_runtime_seconds=180, max_repairs=0
    )
    config = PortableRunConfig.model_validate(body)
    preflight = h.request(client, "POST", "/portable-runs/preflight", json=body).json()
    require(
        preflight["config_sha256"] == config.sha256 and preflight["is_synthetic"],
        "Preflight lost exact config or data evidence",
    )
    h.request(
        client,
        "POST",
        "/research-runs",
        status=422,
        headers={"Idempotency-Key": uuid4().hex},
        json=body,
    )
    h.stop_idle_research_workers()
    key = uuid4().hex
    run = h.request(
        client,
        "POST",
        "/portable-runs",
        status=202,
        headers={"Idempotency-Key": key},
        json=body,
    ).json()
    replay = h.request(
        client,
        "POST",
        "/portable-runs",
        status=202,
        headers={"Idempotency-Key": key},
        json=body,
    ).json()
    require(replay["id"] == run["id"], "Portable submit was not idempotent")
    run_id = run["id"]

    h.request(other, "POST", f"/portable-runs/{run_id}/pause", status=404)
    h.request(client, "POST", f"/portable-runs/{run_id}/pause", status=202)
    h.workers = h.start_workers()

    def paused():
        value = h.request(client, "GET", f"/portable-runs/{run_id}").json()
        require(
            value["status"] not in {"failed", "cancelled", "interrupted", "succeeded"},
            "Portable checkpoint pause failed",
        )
        return value if value["status"] == "paused" else None

    pause_receipt = h.until(
        paused, "Portable worker did not commit pause and sibling cleanup", timeout=100
    )
    require(
        pause_receipt["latest_checkpoint_id"] is not None
        and all(attempt["sandbox_stopped"] for attempt in pause_receipt["attempts"]),
        "Pause lacks durable checkpoint or confirmed sibling termination",
    )
    checkpoint = h.request(
        client, "GET", f"/portable-checkpoints/{pause_receipt['latest_checkpoint_id']}"
    ).json()
    require(
        checkpoint["complete"]
        and checkpoint["next_stage"] == "evaluate"
        and checkpoint["iteration"] == 0,
        "Queued pause did not capture the initial physical checkpoint phase",
    )
    before_calls = h.request(
        client, "GET", f"/portable-runs/{run_id}/model-calls"
    ).json()["data"]
    require(
        not before_calls, "Initial-checkpoint gate unexpectedly consumed model calls"
    )
    # This real gate proves durable initial-state recovery, NOT completed-call replay.
    # Completed frozen-response replay and RNG/memory equality are offline tests.
    h.request(other, "POST", f"/portable-runs/{run_id}/resume", status=404)
    resumed = h.request(
        client, "POST", f"/portable-runs/{run_id}/resume", status=202
    ).json()
    require(
        resumed["generation"] == pause_receipt["generation"] + 1,
        "Resume did not fence a new generation",
    )
    detail = h.until(
        lambda: h.finished_portable(run_id),
        "Portable queue/worker did not complete",
        timeout=220,
    )
    require(
        detail["latest_checkpoint_id"] is not None
        and detail["result"]["best_candidate_id"] is not None,
        "Missing durable best candidate/checkpoint",
    )
    require(
        detail["research_image_digest"].startswith("sha256:")
        and detail["portable_policy_image_digest"].startswith("sha256:"),
        "Both immutable image IDs were not pinned",
    )
    require(
        detail["result"]["runtime_certified"] is False,
        "One smoke gate cannot certify a runtime",
    )
    h.request(other, "GET", f"/portable-runs/{run_id}", status=404)
    h.request(client, "GET", f"/research-runs/{run_id}", status=404)
    rows = {}
    for resource in ["candidates", "evaluations", "model-calls", "checkpoints"]:
        rows[resource] = h.request(
            client, "GET", f"/portable-runs/{run_id}/{resource}"
        ).json()["data"]
        h.request(other, "GET", f"/portable-runs/{run_id}/{resource}", status=404)
    require(
        len(rows["candidates"]) == 2 and len(rows["evaluations"]) == 4,
        "Actual TradeTGD did not evaluate seed and generated candidate",
    )
    require(
        bool(rows["model-calls"])
        and all(row["provider"] == "fixture" for row in rows["model-calls"]),
        "Unexpected online model or missing durable responses",
    )
    require(
        all(
            row["window"] in portable_evaluation_windows(config)
            and row["split"] != "holdout"
            for row in rows["evaluations"]
        ),
        "Optimization escaped train/validation isolation",
    )
    for evaluation in rows["evaluations"]:
        h.attempts.add(evaluation["id"])
        require(
            evaluation["status"] == "succeeded" and evaluation["sandbox_stopped"],
            "Physical sibling termination was not confirmed",
        )
        path = f"/portable-evaluations/{evaluation['id']}/results"
        view = h.request(
            client, "GET", path, params={"section": "decision_trace", "limit": 1}
        ).json()
        require(
            view["count"] > 0 and len(view["rows"]) == 1 and view["is_synthetic"],
            "Bounded trusted result view incomplete",
        )
        require(
            view["image_digest"] == detail["research_image_digest"]
            and view["policy_image_digest"] == detail["portable_policy_image_digest"],
            "Result images drifted",
        )
        content = h.request(client, "GET", path + "/download").content
        bundle = validate_portable_result(content)
        require(
            bundle["identity"]["run_config_sha256"] == config.sha256,
            "Downloaded result config differs",
        )
        require(
            hashlib.sha256(content).hexdigest()
            == hashlib.sha256(
                json.dumps(
                    bundle,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest(),
            "Result bytes are not canonical",
        )
        h.request(other, "GET", path, status=404)
        h.request(other, "GET", path + "/download", status=404)
        require(
            h.runner.stop(evaluation["id"]),
            "Physical siblings remain after completed receipt",
        )
    with h.connection(private=True) as connection:
        env = connection.execute(
            "SELECT id FROM artifacts WHERE run_id=%s AND name='execution-environment.json'",
            (run_id,),
        ).fetchall()
    require(
        len(detail["attempts"]) == 2
        and all(attempt["sandbox_stopped"] for attempt in detail["attempts"]),
        "Resume did not clean both fenced attempts",
    )
    require(
        len(env) == 2, "Each generation must capture its exact execution environment"
    )
    for record in env:
        environment = json.loads(
            h.request(client, "GET", f"/artifacts/{record[0]}/download").content
        )
        validate_environment_evidence(
            environment,
            config.environment_digest,
            {
                "engine": detail["research_image_digest"],
                "policy": detail["portable_policy_image_digest"],
            },
        )
