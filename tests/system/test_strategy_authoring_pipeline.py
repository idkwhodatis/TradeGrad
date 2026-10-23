"""Hosted only: real API/PG/queue/native and paired-container authoring execution.

Uses existing explicit opt-in harnesses; collection starts no processes/sockets.
Never execute these locally when candidate/SDK subprocess execution is denied.
"""

import hashlib
import io
import json
import zipfile
from uuid import uuid4

import test_portable_research_pipeline as portable_system
import test_research_optimization as native_system
from test_worker_recovery import ROOT, require

from packages.tradegrad_adapter.config import MarketConfig, RunConfig
from packages.tradegrad_adapter.portable_final_config import (
    PortableFinalEvaluationConfig,
)
from packages.tradegrad_adapter.portable_results import validate_portable_result

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
            "execution_mode": "optimization",
            "windows": {
                "train": {"start": "2024-01-10", "end": "2024-02-01"},
                "validation": {"start": "2024-02-01", "end": "2024-02-15"},
                "holdout": {"start": "2024-02-15", "end": "2024-03-01"},
            },
        },
    ).json()
    # Final selection requires optimization provenance, but no model rewrite.
    config["optimizer"]["max_iterations"] = 0
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
    require(
        {evaluation["split"] for evaluation in evaluations} == {"train", "validation"},
        "Authored research must not expose holdout",
    )
    require(
        h.request(client, "GET", f"/portable-runs/{run['id']}/model-calls").json()[
            "data"
        ]
        == [],
        "Zero-iteration authored gate must not rewrite source",
    )
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

    selected = h.request(
        client,
        "POST",
        "/final-selections",
        status=201,
        json={
            "split_version_id": detail["split_version_id"],
            "source_run_id": run["id"],
            "source_candidate_id": detail["result"]["best_candidate_id"],
            "source_checkpoint_id": detail["latest_checkpoint_id"],
        },
    ).json()
    final_body = {"selection_id": selected["id"], "max_runtime_seconds": 90}
    final_preflight = h.request(
        client, "POST", "/portable-final-evaluations/preflight", json=final_body
    ).json()
    require(final_preflight["eligible"], "Authored final preflight failed")
    final_config = PortableFinalEvaluationConfig.model_validate(
        final_preflight["config"]
    )
    require(
        final_config.source_config.model_dump(mode="json") == config,
        "Authored frozen source config changed",
    )
    final = h.request(
        client,
        "POST",
        "/portable-final-evaluations",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json=final_body,
    ).json()

    def finished_final():
        value = h.request(
            client, "GET", f"/portable-final-evaluations/{final['id']}"
        ).json()
        require(
            value["status"] not in {"failed", "interrupted", "cancelled"},
            "Authored final did not succeed",
        )
        return value if value["status"] == "succeeded" else None

    final_detail = h.until(finished_final, "Authored final did not finish", timeout=95)
    images = {
        "engine": detail["research_image_digest"],
        "policy": detail["portable_policy_image_digest"],
    }
    require(
        final_detail["config"] == final_config.model_dump(mode="json")
        and final_detail["research_image_digest"] == images["engine"]
        and final_detail["portable_policy_image_digest"] == images["policy"],
        "Authored final config or isolated image identity changed",
    )
    holdouts = h.request(
        client, "GET", f"/portable-final-evaluations/{final['id']}/evaluations"
    ).json()["data"]
    require(len(holdouts) == 1, "Authored final must schedule exactly one holdout")
    holdout = holdouts[0]
    h.attempts.add(holdout["id"])
    require(
        holdout["status"] == "succeeded"
        and holdout["split"] == "holdout"
        and holdout["sandbox_stopped"],
        "Authored holdout lacked confirmed terminal success",
    )
    for role in ("engine", "policy"):
        require(
            h.runner._inspect_role(holdout["id"], role) is None,
            "Authored final sibling survived terminal commit",
        )
    result_path = f"/portable-final-evaluations/evaluations/{holdout['id']}/result"
    content = h.request(client, "GET", result_path + "/download").content
    bundle = validate_portable_result(content)
    require(
        bundle["identity"]["run_config_sha256"] == final_config.sha256
        and bundle["identity"]["strategy_sha256"]
        == selected["source_code_sha256"]
        == version["content_sha256"]
        and bundle["manifest"]["authoring"] == version["manifest"]["authoring"]
        and bundle["window"]
        == {**config["windows"]["holdout"], "interval": "half_open"},
        "Authored holdout lost source, declaration or frozen-window identity",
    )
    h.request(other, "GET", result_path, status=404)
    h.request(other, "GET", result_path + "/download", status=404)
    h.request(other, "GET", f"/portable-final-evaluations/{final['id']}", status=404)
    package = h.request(
        client,
        "POST",
        f"/final-selections/{selected['id']}/packages",
        status=201,
        json={"reference_evaluation_id": holdout["id"]},
    ).json()
    package_path = f"/strategy-packages/{package['id']}/download"
    archive = h.request(client, "GET", package_path).content
    require(
        hashlib.sha256(archive).hexdigest() == package["archive_sha256"],
        "Authored selection package checksum changed",
    )
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        source = zipped.read("strategy/strategy.py")
        require(
            source == draft["code"].encode("utf-8")
            and hashlib.sha256(source).hexdigest() == selected["source_code_sha256"],
            "Authored package did not preserve byte-exact custom source",
        )
        require(
            json.loads(zipped.read("strategy/policy-manifest.json"))
            == version["manifest"]
            and zipped.read("reference/result-bundle.json") == content,
            "Authored package changed the declaration or trusted reference bundle",
        )
        require(
            json.loads(zipped.read("environment/execution-environment.json"))[
                "image_ids"
            ]
            == images,
            "Authored package lost dual image identity",
        )
    h.request(other, "GET", package_path, status=404)
