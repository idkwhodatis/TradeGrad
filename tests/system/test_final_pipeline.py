"""Prepared, explicit opt-in REAL native final-evaluation acceptance.

Set TRADEGRAD_REQUIRE_FINAL_INTEGRATION=1 plus TEST_DATABASE_URL (CREATEDB),
API_PYTHON, RESEARCH_PYTHON and DATA_PYTHON. A Docker evaluator image built from
these exact reviewed inputs must already exist. Collection and opt-out create
no sockets/processes and make no Docker calls. This is NOT run by local unit
validation and never substitutes SQLite, fake sessions/runners, provider data,
live models or candidate execution on the host.

The inherited harness owns a fresh migrated PostgreSQL database and process
groups. Its existing cleanup discovers every physical evaluation UUID before
stopping containers. Source optimization uses deterministic frozen model replies;
final evaluation must execute only the frozen strategy, with zero model/archive
writes. All business mutations go through authenticated production HTTP routes.
"""

import csv
import hashlib
import io
import json
import os
import signal
import time
import zipfile
from uuid import uuid4

import pytest
from exploration_assertions import assert_mode_exploration
from test_research_optimization import ResearchProcessHarness, runtime_paths
from test_worker_recovery import require

from backend.app.storage import safe_path
from packages.tradegrad_adapter.config import MarketConfig, RunConfig
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
from packages.tradegrad_adapter.results import validate_result_bundle
from packages.tradegrad_adapter.strategy_package import validate_environment_evidence
from script.ci_owned_group import peek_owned_group, signal_owned_group, wait_owned_group
from script.ci_research_selection import selected_candidate

GATE_SECONDS = 420


class FinalProcessHarness(ResearchProcessHarness):
    def __init__(self, root, database_url, interpreters):
        super().__init__(root, database_url, interpreters)
        self.deadline = time.monotonic() + GATE_SECONDS

    def remaining(self, maximum):
        value = min(maximum, self.deadline - time.monotonic())
        require(value > 0, "Real final pipeline exceeded its 420-second budget")
        return value

    def submit_source(self, dataset, universe, benchmark=None):
        client = self.clients[0]
        strategies = self.request(client, "GET", "/strategy-versions").json()["data"]
        strategy = next(
            item for item in strategies if item["name"] == "Original cn_time seed"
        )
        capabilities = self.request(client, "GET", "/research-capabilities").json()
        config = RunConfig.model_validate(
            {
                "execution_mode": "optimization",
                "benchmark": benchmark,
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
                    "train": {"start": "2024-02-01", "end": "2024-03-01"},
                    "validation": {"start": "2024-03-01", "end": "2024-03-26"},
                    "holdout": {"start": "2024-03-26", "end": "2024-04-27"},
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

    def source_rows(self, run_id, resource):
        return self.request(
            self.clients[0], "GET", f"/research-runs/{run_id}/{resource}"
        ).json()["data"]

    def final_finished(self, run_id):
        value = self.request(
            self.clients[0], "GET", f"/final-evaluations/{run_id}"
        ).json()
        require(
            value["status"] not in {"failed", "cancelled", "interrupted"},
            "Actual final evaluation did not succeed",
        )
        return value if value["status"] == "succeeded" else None

    def stop_idle_workers(self):
        # Only verified process groups created by this harness may be stopped.
        for process in self.workers:
            require(
                any(item is process for _, item in self.processes),
                "Worker is not owned",
            )
            require(peek_owned_group(process) is None, "Owned worker already exited")
            require(
                os.getpgid(process.pid) == process.pid != os.getpgrp(),
                "Worker group is not exclusive",
            )
            self.expected_alive.discard(process)
            signal_owned_group(process, signal.SIGTERM)
            wait_owned_group(process, timeout=self.remaining(10))
        self.workers = []


@pytest.fixture
def final_harness(tmp_path):
    if os.getenv("TRADEGRAD_REQUIRE_FINAL_INTEGRATION") != "1":
        pytest.skip(
            "Real PostgreSQL/Docker final pipeline was not explicitly requested"
        )
    url = os.getenv("TEST_DATABASE_URL")
    require(bool(url), "TEST_DATABASE_URL is mandatory for real final acceptance")
    harness = None
    try:
        harness = FinalProcessHarness(tmp_path, url, runtime_paths())
        harness.setup()
        yield harness
    except Exception as error:  # noqa: BLE001 - do not expose credentials or full HTTP payloads
        pytest.fail(
            f"Real final pipeline failed ({type(error).__name__})", pytrace=False
        )
    finally:
        if harness is not None:
            harness.cleanup()


@pytest.mark.parametrize(
    "with_benchmark", [False, True], ids=["no-benchmark", "immutable-benchmark"]
)
def test_real_frozen_final_result_export_and_cancelled_retry(
    final_harness, with_benchmark
):
    h = final_harness
    client, other = h.clients
    dataset, universe = h.upload_fixture()
    benchmark = None
    if with_benchmark:
        benchmark_dataset, _ = h.upload_fixture(benchmark=True)
        benchmark = {
            "dataset": {
                "id": benchmark_dataset["id"],
                "sha256": benchmark_dataset["content_sha256"],
            },
            "listing_id": benchmark_dataset["manifest"]["listings"][0]["listing_id"],
        }
    source_id, source_config = h.submit_source(dataset, universe, benchmark)
    source = h.until(
        lambda: h.finished(source_id), "Source optimization did not finish", timeout=170
    )
    before = {
        name: h.source_rows(source_id, name)
        for name in ("candidates", "evaluations", "model-calls", "checkpoints")
    }
    require(
        before["model-calls"]
        and all(row["provider"] == "fixture" for row in before["model-calls"]),
        "Source did not use deterministic frozen model evidence",
    )
    require(
        all(row["split"] != "holdout" for row in before["evaluations"]),
        "Optimizer executed holdout",
    )
    # The HTTP result identifies the persisted Candidate UUID; the optimizer's
    # private key is a separate field and must agree, not be substituted.
    chosen = selected_candidate(source, before["candidates"])
    selected = h.request(
        client,
        "POST",
        "/final-selections",
        status=201,
        json={
            "split_version_id": source["split_version_id"],
            "source_run_id": source_id,
            "source_candidate_id": chosen["id"],
            "source_checkpoint_id": source["latest_checkpoint_id"],
        },
    ).json()
    body = {"selection_id": selected["id"], "max_runtime_seconds": 90}
    preflight = h.request(
        client, "POST", "/final-evaluations/preflight", json=body
    ).json()
    require(preflight["eligible"], "Frozen selection did not pass final preflight")
    final_config = FinalEvaluationConfig.model_validate(preflight["config"])
    require(final_config.source_config == source_config, "Final source config changed")
    require(
        final_config.selected_strategy.sha256 == chosen["code_sha256"],
        "Selected code identity changed",
    )
    key = uuid4().hex
    final = h.request(
        client,
        "POST",
        "/final-evaluations",
        status=202,
        headers={"Idempotency-Key": key},
        json=body,
    ).json()
    replay = h.request(
        client,
        "POST",
        "/final-evaluations",
        status=202,
        headers={"Idempotency-Key": key},
        json=body,
    ).json()
    require(replay["id"] == final["id"], "Final admission replay created another run")
    detail = h.until(
        lambda: h.final_finished(final["id"]),
        "Final worker did not finish",
        timeout=100,
    )
    require(
        detail["config"] == final_config.model_dump(mode="json"),
        "Worker changed final configuration",
    )
    require(
        detail["research_image_digest"] == source["research_image_digest"],
        "Final image differs from source image",
    )
    require(
        len(detail["attempts"]) == 1 and detail["attempts"][0]["sandbox_stopped"],
        "Final attempt termination is unconfirmed",
    )
    evaluations = h.request(
        client, "GET", f"/final-evaluations/{final['id']}/evaluations"
    ).json()["data"]
    require(len(evaluations) == 1, "Final run executed more than one physical holdout")
    item = evaluations[0]
    h.attempts.add(item["id"])
    require(
        item["id"] != detail["attempts"][0]["id"],
        "Physical evaluation reused coordinator identity",
    )
    require(
        item["split"] == "holdout"
        and item["status"] == "succeeded"
        and item["sandbox_stopped"],
        "Final physical evaluation is incomplete",
    )
    require(
        item["code_sha256"] == selected["source_code_sha256"],
        "Final evaluated different code",
    )
    require(
        item["image_digest"] == source["research_image_digest"],
        "Final physical image drifted",
    )
    require(
        h.runner._inspect(item["id"]) is None,
        "Final container survived terminal publication",
    )
    query = h.request(
        client, "GET", f"/final-evaluations/evaluations/{item['id']}/result"
    ).json()
    require(
        query["selection_feedback"] is False and query["split"] == "holdout",
        "Final results became optimizer feedback",
    )
    require(
        not query["exposure_snapshot"]["independent_unseen_holdout_established"],
        "Incomplete history was labeled unseen",
    )
    content = h.request(
        client, "GET", f"/final-evaluations/evaluations/{item['id']}/result/download"
    ).content
    reference = item["payload"]["result_bundle"]
    require(
        hashlib.sha256(content).hexdigest() == reference["sha256"],
        "Final download hash differs",
    )
    bundle = json.loads(content)
    validate_result_bundle(bundle)
    from workbench_assertions import assert_final_workbench

    assert_final_workbench(h, client, other, item, selected)
    assert_mode_exploration(
        h,
        client,
        other,
        mode="native_final",
        evaluation_id=item["id"],
        content=content,
        bundle=bundle,
        selection_id=query["selection_id"],
    )
    if with_benchmark:
        comparison = bundle["benchmark"]
        require(
            comparison["status"] == "available", "Selected benchmark result is absent"
        )
        require(
            comparison["input"]["reference"]
            == source_config.benchmark.model_dump(mode="json"),
            "Benchmark selection changed",
        )
        require(
            comparison["input"]["source_classification"] == "synthetic",
            "Synthetic benchmark label lost",
        )
        require(
            comparison["input"]["window"]
            == final_config.holdout.model_dump(mode="json"),
            "Holdout benchmark used another window",
        )
        require(
            query["benchmark"]["metrics"] == comparison["metrics"],
            "Persisted benchmark projection changed",
        )
        for evaluation in before["evaluations"]:
            if evaluation["status"] == "succeeded":
                research_result = h.request(
                    client, "GET", f"/research-evaluations/{evaluation['id']}/results"
                ).json()
                require(
                    research_result["benchmark"]["status"] == "available",
                    "Research benchmark missing",
                )
                require(
                    all(
                        row[0] < str(source_config.windows.holdout.start)
                        for row in research_result["benchmark"]["series"]["rows"]
                    ),
                    "Holdout benchmark leaked into research",
                )
    require(
        bundle["identity"]["run_config_sha256"] == final_config.sha256,
        "Final ResultBundle config differs",
    )
    require(
        bundle["identity"]["strategy_sha256"] == chosen["code_sha256"],
        "Final ResultBundle source differs",
    )
    require(bundle["window"]["role"] == "holdout", "Final ResultBundle is not holdout")
    require(bundle["model_usage"]["calls"] == [], "Final evaluator invoked a model")

    # Read exact mounted rows, not the full immutable dataset, without executing code.
    mount = safe_path(
        h.root / "storage",
        f"runs/{final['id']}/attempts/{item['attempt_id']}/evaluations/{item['id']}/input",
    )
    require(
        json.loads((mount / "request.json").read_bytes())["evaluation_config"][
            "benchmark"
        ]
        is None,
        "Benchmark prices were mounted into native strategy inputs",
    )
    with (mount / "data/INDEX.csv").open() as stream:
        dates = [row["date"] for row in csv.DictReader(stream)]
    require(
        sum(day < str(final_config.holdout.start) for day in dates)
        == source_config.warmup.sessions,
        "Final mount has missing or excess warmup",
    )
    require(
        all(day < str(final_config.holdout.end) for day in dates),
        "Final mount contains later rows",
    )
    require(
        not any(
            path.name in {"checkpoint.json", "state.json", "model-calls.json"}
            for path in mount.rglob("*")
        ),
        "Optimizer state was mounted into the final sandbox",
    )

    with h.connection(private=True) as connection:
        for table in (
            "research_candidates",
            "research_model_calls",
            "research_checkpoints",
        ):
            require(
                connection.execute(
                    f"SELECT count(*) FROM {table} WHERE run_id=%s", (final["id"],)
                ).fetchone()[0]
                == 0,
                "Final run wrote optimizer provenance",
            )
        evidence = connection.execute(
            "SELECT storage_key, sha256 FROM artifacts WHERE run_id=%s AND name='execution-environment.json'",
            (final["id"],),
        ).fetchall()
        require(len(evidence) == 1, "Final environment Artifact was not pinned")
        jobs = connection.execute(
            "SELECT j.status FROM outbox_events o JOIN procrastinate_jobs j ON j.id=o.job_id WHERE o.run_id=%s",
            (final["id"],),
        ).fetchall()
        require(
            len(jobs) == 1 and jobs[0][0] in {"doing", "succeeded"},
            "Final run bypassed durable queue",
        )
    environment_bytes = safe_path(h.root / "storage", evidence[0][0]).read_bytes()
    require(
        hashlib.sha256(environment_bytes).hexdigest() == evidence[0][1],
        "Pinned environment artifact changed",
    )
    validate_environment_evidence(
        json.loads(environment_bytes),
        final_config.environment_digest,
        source["research_image_digest"],
    )
    for name, records in before.items():
        require(
            h.source_rows(source_id, name) == records,
            "Final result changed source optimizer archive",
        )
    h.request(client, "GET", f"/research-runs/{final['id']}", status=409)
    h.request(other, "GET", f"/final-evaluations/{final['id']}", status=404)
    h.request(
        other, "GET", f"/final-evaluations/evaluations/{item['id']}/result", status=404
    )
    h.request(
        other, "GET", f"/artifacts/{reference['artifact_id']}/download", status=404
    )

    package_body = {"reference_evaluation_id": item["id"]}
    package = h.request(
        client,
        "POST",
        f"/final-selections/{selected['id']}/packages",
        status=201,
        json=package_body,
    ).json()
    same_package = h.request(
        client,
        "POST",
        f"/final-selections/{selected['id']}/packages",
        status=200,
        json=package_body,
    ).json()
    require(
        same_package["id"] == package["id"],
        "Package replay created another immutable export",
    )
    archive = h.request(
        client, "GET", f"/strategy-packages/{package['id']}/download"
    ).content
    require(
        hashlib.sha256(archive).hexdigest() == package["archive_sha256"],
        "Package archive checksum differs",
    )
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        require(
            hashlib.sha256(zipped.read("strategy/strategy.py")).hexdigest()
            == chosen["code_sha256"],
            "Export changed frozen code",
        )
        require(
            zipped.read("reference/result-bundle.json") == content,
            "Export changed exact final evidence",
        )
        captured_environment = json.loads(environment_bytes)
        require(
            json.loads(zipped.read("environment/execution-environment.json"))
            == {
                key: value
                for key, value in captured_environment.items()
                if key != "dependency_locks"
            },
            "Export reconstructed a different environment",
        )
        for name, record in captured_environment["dependency_locks"].items():
            lock = zipped.read("environment/locks/" + name)
            require(
                lock == record["content"].encode("utf-8")
                and hashlib.sha256(lock).hexdigest() == record["sha256"],
                "Export changed a separately stored dependency lock",
            )
    h.request(other, "GET", f"/strategy-packages/{package['id']}/download", status=404)

    # Cancel a second admission before any worker claims it, then retry via HTTP.
    # The permanent exposure must survive cancellation; generation two is a new
    # physical job, not a continuation or optimizer feedback stage.
    h.stop_idle_workers()
    cancelled = h.request(
        client,
        "POST",
        "/final-evaluations",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json=body,
    ).json()
    cancelled = h.request(
        client, "POST", f"/final-evaluations/{cancelled['id']}/cancel", status=202
    ).json()
    require(
        cancelled["status"] == "cancelled", "Unclaimed final admission did not cancel"
    )
    retried = h.request(
        client,
        "POST",
        f"/final-evaluations/{cancelled['id']}/retry",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
    ).json()
    require(
        retried["generation"] == 2 and retried["config"] == final["config"],
        "Retry changed frozen selection/config",
    )
    h.workers = h.start_workers()
    h.until(
        lambda: h.final_finished(retried["id"]),
        "Retried final did not finish",
        timeout=100,
    )
    retried_evidence = h.request(
        client, "GET", f"/final-evaluations/{retried['id']}/evaluations"
    ).json()["data"]
    require(
        len(retried_evidence) == 1 and retried_evidence[0]["generation"] == 2,
        "Cancelled admission launched or retry reused generation",
    )
    require(
        retried_evidence[0]["id"] != item["id"], "Retry reused physical evaluation UUID"
    )
    with h.connection(private=True) as connection:
        admissions = connection.execute(
            "SELECT source_key FROM data_usage_exposures WHERE run_id=%s AND purpose='final_evaluation_admission'",
            (retried["id"],),
        ).fetchall()
        require(
            {row[0] for row in admissions}
            == {f"final:{retried['id']}:1", f"final:{retried['id']}:2"},
            "Cancellation or retry erased permanent exposure evidence",
        )
