"""Prepared real PG/API/queue/dual-container final/export acceptance; collect only.

Requires TRADEGRAD_REQUIRE_PORTABLE_FINAL_INTEGRATION=1, TEST_DATABASE_URL
(CREATEDB), distinct API_PYTHON/RESEARCH_PYTHON/DATA_PYTHON and both current pinned
images. No sockets, processes or Docker calls occur at collection or opt-out.
Never substitute fakes, host policies, paid models or live market/trade services.
"""

import hashlib
import io
import json
import os
import zipfile
from uuid import uuid4

import pytest
from exploration_assertions import assert_mode_exploration
from test_final_pipeline import FinalProcessHarness
from test_portable_research_pipeline import PortableProcessHarness
from test_research_optimization import runtime_paths
from test_worker_recovery import require

from backend.app.storage import safe_path
from packages.tradegrad_adapter.portable_environment import portable_environment_digest
from packages.tradegrad_adapter.portable_final_config import (
    PortableFinalEvaluationConfig,
)
from packages.tradegrad_adapter.portable_results import validate_portable_result
from packages.tradegrad_adapter.strategy_package import validate_environment_evidence


@pytest.fixture
def portable_final_harness(tmp_path):
    if os.getenv("TRADEGRAD_REQUIRE_PORTABLE_FINAL_INTEGRATION") != "1":
        pytest.skip(
            "Real portable final/export acceptance was not explicitly requested"
        )
    url = os.getenv("TEST_DATABASE_URL")
    require(
        bool(url), "TEST_DATABASE_URL is required for real portable final acceptance"
    )
    harness = None
    try:
        harness = PortableProcessHarness(tmp_path, url, runtime_paths())
        harness.runner.bind_environment(portable_environment_digest())
        harness.setup()
        yield harness
    except Exception as error:  # noqa: BLE001 - no credentials/HTTP bodies in failure reports
        pytest.fail(
            f"Real portable final pipeline failed ({type(error).__name__})",
            pytrace=False,
        )
    finally:
        if harness is not None:
            harness.cleanup()


@pytest.mark.parametrize(
    "with_benchmark", [False, True], ids=["no-benchmark", "immutable-benchmark"]
)
@pytest.mark.parametrize("kind,market", [("event", "CN"), ("portfolio", "US")])
def test_real_portable_freeze_final_reference_export_and_retry(
    portable_final_harness, kind, market, with_benchmark
):
    h = portable_final_harness
    client, other = h.clients
    dataset, universe = h.import_fixture(market)
    refs = {
        "dataset": {"id": dataset["id"], "sha256": dataset["content_sha256"]},
        "universe": {"id": universe["id"], "sha256": universe["content_sha256"]},
    }
    strategy = h.request(
        client,
        "POST",
        "/portable-strategies",
        status=201,
        json={"name": "Portable final gate", "strategy_kind": kind, **refs},
    ).json()
    body = h.request(
        client,
        "POST",
        "/portable-runs/default-config",
        json={
            **refs,
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
    if with_benchmark:
        benchmark_dataset, _ = h.import_fixture(market)
        body["benchmark"] = {
            "definition_version": "daily_close_observed_anchor.v1",
            "dataset": {
                "id": benchmark_dataset["id"],
                "sha256": benchmark_dataset["content_sha256"],
            },
            "listing_id": benchmark_dataset["manifest"]["listings"][0]["listing_id"],
            "price_field": "close",
        }
    body["optimizer"]["max_iterations"] = 1
    body["budgets"].update(
        max_model_calls=10, max_tokens=100000, max_runtime_seconds=180, max_repairs=0
    )
    source = h.request(
        client,
        "POST",
        "/portable-runs",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json=body,
    ).json()
    source = h.until(
        lambda: h.finished_portable(source["id"]),
        "Portable source did not succeed",
        timeout=220,
    )
    source_id = source["id"]
    before = {
        name: h.request(client, "GET", f"/portable-runs/{source_id}/{name}").json()[
            "data"
        ]
        for name in ("candidates", "evaluations", "model-calls", "checkpoints")
    }
    require(
        before["model-calls"]
        and all(row["provider"] == "fixture" for row in before["model-calls"]),
        "Source used online model or lacks fixture evidence",
    )
    require(
        all(row["split"] != "holdout" for row in before["evaluations"]),
        "Optimization exposed holdout",
    )
    selected = h.request(
        client,
        "POST",
        "/final-selections",
        status=201,
        json={
            "split_version_id": source["split_version_id"],
            "source_run_id": source_id,
            "source_candidate_id": source["result"]["best_candidate_id"],
            "source_checkpoint_id": source["latest_checkpoint_id"],
        },
    ).json()
    final_body = {"selection_id": selected["id"], "max_runtime_seconds": 90}
    preflight = h.request(
        client, "POST", "/portable-final-evaluations/preflight", json=final_body
    ).json()
    require(preflight["eligible"], "Portable final preflight failed")
    config = PortableFinalEvaluationConfig.model_validate(preflight["config"])
    require(
        config.source_config.model_dump(mode="json") == body,
        "Frozen source config changed",
    )
    key = uuid4().hex
    final = h.request(
        client,
        "POST",
        "/portable-final-evaluations",
        status=202,
        headers={"Idempotency-Key": key},
        json=final_body,
    ).json()
    replay = h.request(
        client,
        "POST",
        "/portable-final-evaluations",
        status=202,
        headers={"Idempotency-Key": key},
        json=final_body,
    ).json()
    require(replay["id"] == final["id"], "Replay duplicated final admission")

    def finished(run_id):
        value = h.request(client, "GET", f"/portable-final-evaluations/{run_id}").json()
        require(
            value["status"] not in {"failed", "interrupted", "cancelled"},
            "Portable final did not succeed",
        )
        return value if value["status"] == "succeeded" else None

    detail = h.until(
        lambda: finished(final["id"]), "Portable final did not finish", timeout=100
    )
    require(detail["config"] == config.model_dump(mode="json"), "Final config changed")
    images = {
        "engine": source["research_image_digest"],
        "policy": source["portable_policy_image_digest"],
    }
    require(
        detail["research_image_digest"] == images["engine"]
        and detail["portable_policy_image_digest"] == images["policy"],
        "Final sibling image identity changed",
    )
    evaluations = h.request(
        client, "GET", f"/portable-final-evaluations/{final['id']}/evaluations"
    ).json()["data"]
    require(len(evaluations) == 1, "Final scheduled more than one holdout")
    item = evaluations[0]
    h.attempts.add(item["id"])
    require(
        item["status"] == "succeeded"
        and item["split"] == "holdout"
        and item["sandbox_stopped"],
        "Holdout did not durably finish",
    )
    for role in ("engine", "policy"):
        require(
            h.runner._inspect_role(item["id"], role) is None,
            "Final sibling survived terminal commit",
        )
    result_path = f"/portable-final-evaluations/evaluations/{item['id']}/result"
    view = h.request(
        client, "GET", result_path, params={"section": "decision_trace", "limit": 1}
    ).json()
    require(
        view["selection_feedback"] is False and view["runtime_certified"] is False,
        "Portable capability label overstated",
    )
    require(
        view["exposure_snapshot"]["independent_unseen_holdout_established"] is False,
        "Final labeled independently unseen",
    )
    content = h.request(client, "GET", result_path + "/download").content
    bundle = validate_portable_result(content)
    assert_mode_exploration(
        h,
        client,
        other,
        mode="portable_final",
        evaluation_id=item["id"],
        content=content,
        bundle=bundle,
        selection_id=view["selection_id"],
    )
    if with_benchmark:
        comparison = bundle["benchmark"]
        require(comparison["status"] == "available", "Portable benchmark result absent")
        require(
            comparison["input"]["reference"] == body["benchmark"],
            "Portable benchmark selection changed",
        )
        require(
            comparison["input"]["window"] == config.holdout.model_dump(mode="json"),
            "Portable benchmark scored window changed",
        )
        require(
            view["benchmark"]["metrics"] == comparison["metrics"],
            "Portable benchmark projection changed",
        )
        for evaluation in before["evaluations"]:
            if evaluation["status"] == "succeeded":
                research_result = h.request(
                    client,
                    "GET",
                    f"/portable-evaluations/{evaluation['id']}/results",
                ).json()
                require(
                    research_result["benchmark"]["status"] == "available",
                    "Portable research benchmark missing",
                )
                require(
                    all(
                        row[0] < body["windows"]["holdout"]["start"]
                        for row in research_result["benchmark"]["series"]["rows"]
                    ),
                    "Holdout benchmark entered portable research",
                )
    require(
        bundle["identity"]["run_config_sha256"] == config.sha256
        and bundle["identity"]["strategy_sha256"] == selected["source_code_sha256"],
        "Final result identity drifted",
    )
    mount = safe_path(
        h.root / "storage",
        f"runs/{final['id']}/attempts/{item['attempt_id']}/evaluations/{item['id']}/input",
    )
    market_input = json.loads((mount / "engine" / "market.json").read_bytes())
    dates = [row["session_date"] for row in market_input["sessions"]]
    require(
        all(day < str(config.holdout.end) for day in dates),
        "Final engine saw post-holdout data",
    )
    require(
        sum(day < str(config.holdout.start) for day in dates)
        == config.source_config.warmup.sessions,
        "Final warmup differs",
    )
    require(
        {path.name for path in (mount / "policy").iterdir()}
        == {"policy.py", "parameters.json", "manifest.json"},
        "Policy received raw market or optimizer data",
    )
    require(
        not (mount / "engine" / "policy.py").exists(),
        "Trusted engine mount contains policy source",
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
                "Final wrote optimizer provenance",
            )
        environment = connection.execute(
            "SELECT storage_key, sha256 FROM artifacts WHERE run_id=%s AND name='execution-environment.json'",
            (final["id"],),
        ).fetchall()
    require(len(environment) == 1, "Missing final environment capture")
    env_bytes = safe_path(h.root / "storage", environment[0][0]).read_bytes()
    require(
        hashlib.sha256(env_bytes).hexdigest() == environment[0][1],
        "Environment evidence changed",
    )
    validate_environment_evidence(
        json.loads(env_bytes), config.environment_digest, images
    )
    for name, records in before.items():
        require(
            h.request(client, "GET", f"/portable-runs/{source_id}/{name}").json()[
                "data"
            ]
            == records,
            "Final modified source optimizer archive",
        )
    h.request(other, "GET", result_path, status=404)
    h.request(other, "GET", result_path + "/download", status=404)
    h.request(other, "GET", f"/portable-final-evaluations/{final['id']}", status=404)
    package_body = {"reference_evaluation_id": item["id"]}
    package_path = f"/final-selections/{selected['id']}/packages"
    package = h.request(
        client, "POST", package_path, status=201, json=package_body
    ).json()
    require(
        h.request(client, "POST", package_path, status=200, json=package_body).json()[
            "id"
        ]
        == package["id"],
        "Package replay duplicated export",
    )
    archive = h.request(
        client, "GET", f"/strategy-packages/{package['id']}/download"
    ).content
    require(
        hashlib.sha256(archive).hexdigest() == package["archive_sha256"],
        "Archive hash differs",
    )
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        require(
            hashlib.sha256(zipped.read("strategy/strategy.py")).hexdigest()
            == selected["source_code_sha256"],
            "Selected code changed on export",
        )
        require(
            zipped.read("reference/result-bundle.json") == content,
            "Reference export changed engine bytes",
        )
        require(
            "strategy/policy-manifest.json" in zipped.namelist()
            and "strategy/state-contract.json" in zipped.namelist(),
            "Portable contract missing",
        )
        require(
            json.loads(zipped.read("environment/execution-environment.json"))[
                "image_ids"
            ]
            == images,
            "Export lost dual image identity",
        )
        require("REPLAY.txt" in zipped.namelist(), "Replay instructions missing")
    h.request(other, "GET", f"/strategy-packages/{package['id']}/download", status=404)
    FinalProcessHarness.stop_idle_workers(h)
    cancelled = h.request(
        client,
        "POST",
        "/portable-final-evaluations",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json=final_body,
    ).json()
    cancelled = h.request(
        client,
        "POST",
        f"/portable-final-evaluations/{cancelled['id']}/cancel",
        status=202,
    ).json()
    require(cancelled["status"] == "cancelled", "Unclaimed final did not cancel")
    retry_key = uuid4().hex
    retried = h.request(
        client,
        "POST",
        f"/portable-final-evaluations/{cancelled['id']}/retry",
        status=202,
        headers={"Idempotency-Key": retry_key},
    ).json()
    require(
        retried["generation"] == 2 and retried["config"] == final["config"],
        "Retry changed config or reused generation",
    )
    require(
        h.request(
            client,
            "POST",
            f"/portable-final-evaluations/{cancelled['id']}/retry",
            status=202,
            headers={"Idempotency-Key": retry_key},
        ).json()["generation"]
        == 2,
        "Retry replay duplicated admission",
    )
    h.workers = h.start_workers()
    h.until(
        lambda: finished(retried["id"]),
        "Retried portable final did not complete",
        timeout=100,
    )
    with h.connection(private=True) as connection:
        admissions = connection.execute(
            "SELECT source_key FROM data_usage_exposures WHERE run_id=%s AND purpose='final_evaluation_admission'",
            (retried["id"],),
        ).fetchall()
    require(
        {row[0] for row in admissions}
        == {f"final:{retried['id']}:1", f"final:{retried['id']}:2"},
        "Cancellation/retry erased permanent owner exposure",
    )
