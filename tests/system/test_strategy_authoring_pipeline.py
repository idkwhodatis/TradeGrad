"""Hosted only: real API/PG/queue/native and paired-container authoring execution.

Uses existing explicit opt-in harnesses; collection starts no processes/sockets.
Never execute these locally when candidate/SDK subprocess execution is denied.
"""

import json
from uuid import uuid4

import test_portable_research_pipeline as portable_system
import test_research_optimization as native_system
from test_worker_recovery import ROOT, require

from packages.tradegrad_adapter.config import MarketConfig, RunConfig

research_harness = native_system.research_harness
portable_harness = portable_system.portable_harness


def test_native_same_source_different_parameters_change_actual_engine_orders(
    research_harness,
):
    h = research_harness
    client = h.clients[0]
    dataset, universe = h.upload_fixture()
    source = (
        (ROOT / "seed_strategies/cn_time.py")
        .read_text()
        .replace(
            "target_percent = 0.95 if",
            'target_percent = self.tradegrad_parameters["allocation"] if',
        )
    )
    environment = h.request(client, "GET", "/research-capabilities").json()[
        "environment_digest"
    ]
    results, versions = [], []
    for allocation in (0.2, 0.8):
        contract = {
            "required_fields": ["open", "high", "low", "close", "volume", "amount"],
            "lookback_bars": 20,
            "warmup_bars": 20,
            "requires_shorting": False,
            "parameters": [
                {
                    "name": "allocation",
                    "kind": "number",
                    "value": allocation,
                    "minimum": 0.1,
                    "maximum": 0.9,
                }
            ],
        }
        version = h.request(
            client,
            "POST",
            "/strategy-versions",
            status=201,
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "name": "Parameter-consumption gate",
                "task": "cn_time",
                "code": source,
                "manifest": contract,
            },
        ).json()
        versions.append(version)
        config = RunConfig.model_validate(
            {
                "execution_mode": "optimization",
                "task": "cn_time",
                "dataset": {"id": dataset["id"], "sha256": dataset["content_sha256"]},
                "universe": {
                    "id": universe["id"],
                    "sha256": universe["content_sha256"],
                },
                "seed_strategy": {
                    "id": version["id"],
                    "sha256": version["content_sha256"],
                },
                "strategy_contract": version["manifest"],
                "windows": {
                    "train": {"start": "2024-02-01", "end": "2024-03-15"},
                    "validation": {"start": "2024-03-15", "end": "2024-04-20"},
                },
                "warmup": {"sessions": 20},
                "market_profile": MarketConfig.for_market("CN"),
                "optimizer": {"max_iterations": 0},
                "environment_digest": environment,
            }
        )
        h.request(
            client,
            "POST",
            "/research-runs/preflight",
            json=config.model_dump(mode="json"),
        )
        run = h.request(
            client,
            "POST",
            "/research-runs",
            status=202,
            headers={"Idempotency-Key": uuid4().hex},
            json=config.model_dump(mode="json"),
        ).json()
        h.runs.append({"id": run["id"], "client": client, "config": config})
        h.until(
            lambda run_id=run["id"]: h.finished(run_id),
            "Declared-parameter run did not succeed",
            timeout=120,
        )
        evaluations = h.request(
            client, "GET", f"/research-runs/{run['id']}/evaluations"
        ).json()["data"]
        require(
            len(evaluations) == 2,
            "Zero-iteration run must evaluate train and validation",
        )
        records = []
        for evaluation in evaluations:
            h.attempts.add(evaluation["id"])
            require(
                evaluation["sandbox_stopped"] and evaluation["status"] == "succeeded",
                "Evaluator lacked confirmed terminal success",
            )
            detail = h.request(client, "GET", f"/evaluations/{evaluation['id']}").json()
            reference = detail["payload"]["result_bundle"]
            bundle = h.request(
                client, "GET", f"/artifacts/{reference['artifact_id']}/download"
            ).json()
            require(
                bundle["identity"]["run_config_sha256"] == config.sha256,
                "Parameters were not hash bound",
            )
            columns = [column["name"] for column in bundle["fills"]["columns"]]
            quantities = [
                row[columns.index("quantity")] for row in bundle["fills"]["rows"]
            ]
            require(bool(quantities), "Consumption gate requires actual nonempty fills")
            records.append((evaluation["split"], quantities))
        results.append(json.dumps(sorted(records), sort_keys=True))
    require(
        versions[0]["content_sha256"] == versions[1]["content_sha256"]
        and versions[0]["id"] != versions[1]["id"],
        "Expected identical source with distinct immutable parameter versions",
    )
    require(
        results[0] != results[1], "Parameter changes did not change actual engine fills"
    )


def test_custom_portable_source_outside_reference_scaffold_runs_in_both_containers(
    portable_harness,
):
    h = portable_harness
    client, other = h.clients
    dataset, universe = h.import_fixture("US")
    refs = {
        "dataset": {"id": dataset["id"], "sha256": dataset["content_sha256"]},
        "universe": {"id": universe["id"], "sha256": universe["content_sha256"]},
    }
    seed = h.request(
        client,
        "POST",
        "/portable-strategies",
        status=201,
        json={
            "name": "Reference for custom derivation",
            "strategy_kind": "portfolio",
            **refs,
        },
    ).json()
    draft = h.request(
        client, "GET", f"/portable-strategies/{seed['id']}/source-draft"
    ).json()
    # Changing outside the old reference markers demonstrates whole-source authoring.
    # It remains just inert text until the production policy container executes it.
    draft["code"] = (
        "# Custom full user source, not reference-template identity\n" + draft["code"]
    )
    draft["name"] = "Custom portable authored gate"
    version = h.request(
        client,
        "POST",
        "/portable-strategy-versions",
        status=201,
        headers={"Idempotency-Key": uuid4().hex},
        json=draft,
    ).json()
    h.request(other, "GET", f"/portable-strategies/{version['id']}", status=404)
    config = h.request(
        client,
        "POST",
        "/portable-runs/default-config",
        json={
            **refs,
            "seed_strategy": {"id": version["id"], "sha256": version["content_sha256"]},
            "execution_mode": "seed_evaluation",
            "windows": {
                "train": {"start": "2024-01-10", "end": "2024-02-01"},
                "validation": {"start": "2024-02-01", "end": "2024-02-15"},
            },
        },
    ).json()
    preflight = h.request(
        client, "POST", "/portable-runs/preflight", json=config
    ).json()
    require(
        preflight["is_synthetic"],
        "Custom policy gate must not imply upstream provider evidence",
    )
    run = h.request(
        client,
        "POST",
        "/portable-runs",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json=config,
    ).json()
    detail = h.until(
        lambda: h.finished_portable(run["id"]),
        "Custom full policy did not finish",
        timeout=160,
    )
    require(
        detail["research_image_digest"] and detail["portable_policy_image_digest"],
        "Both isolated images must be recorded",
    )
    evaluations = h.request(
        client, "GET", f"/portable-runs/{run['id']}/evaluations"
    ).json()["data"]
    require(len(evaluations) == 2, "Expected exact train/validation evaluations")
    for evaluation in evaluations:
        h.attempts.add(evaluation["id"])
        require(
            evaluation["status"] == "succeeded" and evaluation["sandbox_stopped"],
            "Paired-container cleanup unconfirmed",
        )
        view = h.request(
            client,
            "GET",
            f"/portable-evaluations/{evaluation['id']}/results",
            params={"section": "decision_trace", "limit": 1},
        ).json()
        require(
            view["count"] > 0 and view["ledger_origin"] == "trusted_engine",
            "Custom policy must return decisions, never fabricate the ledger",
        )
