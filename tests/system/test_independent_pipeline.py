"""Hosted-only I5 evidence: real HTTP, PostgreSQL, queue and isolated evaluators.

Require TRADEGRAD_REQUIRE_INDEPENDENT_INTEGRATION=1, TEST_DATABASE_URL (CREATEDB),
API_PYTHON, RESEARCH_PYTHON and DATA_PYTHON, and all three exact-head images.
Collection/opt-out starts no process, opens no socket and invokes no Docker.
There are no fake sessions, HTTP routes, model/provider calls, SQLite fallback,
or host candidate imports. Candidate strings below are inert until the production
native or portable policy container executes them. SQL is read-only apart from
inherited, verified disposable-database creation and cleanup.

Ordinary zero-iteration optimization is used ONLY to produce original source
leaves without model calls. Every diagnostic/control uses the distinct independent
API, config, worker and result contract, never an optimizer emulation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from uuid import uuid4

import pytest
from test_portable_research_pipeline import PortableProcessHarness
from test_research_optimization import ResearchProcessHarness, runtime_paths
from test_worker_recovery import ROOT, require

from packages.research_trace.samples import CONTROLLED_SOURCE_SHA256
from packages.research_trace.templates import native_contract
from packages.tradegrad_adapter.config import MarketConfig, RunConfig
from packages.tradegrad_adapter.independent_results import (
    validate_result_bundle as validate_independent_result,
)
from packages.tradegrad_adapter.portable_environment import portable_environment_digest
from packages.tradegrad_adapter.portable_results import validate_portable_result
from packages.tradegrad_adapter.results import validate_result_bundle
from packages.tradegrad_adapter.runtime_observation import validate_runtime_observation

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


@pytest.fixture(params=["native", "portable"])
def independent_harness(request, tmp_path):
    if os.getenv("TRADEGRAD_REQUIRE_INDEPENDENT_INTEGRATION") != "1":
        pytest.skip("Real independent PostgreSQL/Docker gate was not requested")
    database = os.getenv("TEST_DATABASE_URL")
    require(bool(database), "TEST_DATABASE_URL is mandatory for independent acceptance")
    h = None
    try:
        cls = (
            ResearchProcessHarness
            if request.param == "native"
            else PortableProcessHarness
        )
        h = cls(tmp_path, database, runtime_paths())
        # The native lifecycle case includes deliberate 20-second isolated import
        # delays and a second generation, not a faster fake cancellation branch.
        h.deadline = time.monotonic() + 600
        h.env.update(MODEL_PROFILES_ENABLED="false", MODEL_SYNTHETIC_TEST_MODE="false")
        if request.param == "portable":
            h.runner.bind_environment(portable_environment_digest())
        h.setup()
        yield h
    except Exception as error:  # noqa: BLE001 - never disclose credentials/environment/HTTP payloads
        pytest.fail(
            f"Real independent pipeline failed ({type(error).__name__})", pytrace=False
        )
    finally:
        if h is not None:
            h.cleanup()


def _create_native(h, dataset, universe, code, *, contract=None):
    client = h.clients[0]
    version = h.request(
        client,
        "POST",
        "/strategy-versions",
        status=201,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "name": "Synthetic independent gate source",
            "task": "cn_time",
            "code": code,
            "manifest": contract,
        },
    ).json()
    environment = h.request(client, "GET", "/research-capabilities").json()[
        "environment_digest"
    ]
    config = RunConfig.model_validate(
        {
            "execution_mode": "optimization",
            "task": "cn_time",
            "dataset": {"id": dataset["id"], "sha256": dataset["content_sha256"]},
            "universe": {"id": universe["id"], "sha256": universe["content_sha256"]},
            "seed_strategy": {"id": version["id"], "sha256": version["content_sha256"]},
            "windows": {
                "train": {"start": "2024-02-01", "end": "2024-03-15"},
                "validation": {"start": "2024-03-15", "end": "2024-04-20"},
                "holdout": {"start": "2024-04-22", "end": "2024-04-27"},
            },
            "warmup": {"sessions": 20},
            "market_profile": MarketConfig.for_market("CN"),
            "optimizer": {"max_iterations": 0},
            "budgets": {
                "max_model_calls": 0,
                "max_tokens": 0,
                "max_runtime_seconds": 180,
                "max_repairs": 0,
            },
            "research_capture": {"mode": "research"},
            "strategy_contract": contract,
            "environment_digest": environment,
        }
    ).model_dump(mode="json")
    run = h.request(
        client,
        "POST",
        "/research-runs",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json=config,
    ).json()
    return version, run, config


def _source(h, mode, *, slow=False, maintained=False):
    client = h.clients[0]
    if mode == "native":
        dataset, universe = h.upload_fixture()
        contract = None
        if maintained:
            code = (ROOT / "seed_strategies/research_time.py").read_text()
            require(
                hashlib.sha256(code.encode()).hexdigest()
                == CONTROLLED_SOURCE_SHA256["daily_timing"],
                "Maintained template exact source hash changed",
            )
            contract = native_contract(
                "daily_timing",
                parameters={"fast_window": 5, "slow_window": 20, "exit_buffer": 0.0},
            ).model_dump(mode="json")
        else:
            # Read and manipulate source text only. Both RNG values actually affect
            # submitted quantities, so captured but unused RNG would not suffice.
            code = (ROOT / "seed_strategies/cn_time.py").read_text()
            require(
                code.count("target_percent = 0.95 if") == 1,
                "Native fixture allocation seam changed; RNG must affect actual orders",
            )
            code = code.replace(
                "target_percent = 0.95 if", "target_percent = INITIAL_ALLOCATION if"
            )
            code = (
                "import random\nimport numpy as np\n"
                "INITIAL_ALLOCATION = 0.4 + 0.1 * random.random() + 0.1 * np.random.random()\n"
                + ("import time\ntime.sleep(20)\n" if slow else "")
                + code
            )
        version, run, config = _create_native(
            h, dataset, universe, code, contract=contract
        )
        detail = h.until(
            lambda: h.finished(run["id"]), "Original native source failed", timeout=180
        )
        route = "research-runs"
    else:
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
                "name": "Policy RNG bootstrap reference",
                "strategy_kind": "portfolio",
                **refs,
            },
        ).json()
        draft = h.request(
            client, "GET", f"/portable-strategies/{seed['id']}/source-draft"
        ).json()
        require(
            "value=1.0 / len(selected)" in draft["code"],
            "Portable fixture target seam changed",
        )
        draft["code"] = (
            "import random\nINITIAL_ALLOCATION = 0.4 + 0.2 * random.random()\n"
            + draft["code"].replace(
                "value=1.0 / len(selected)", "value=INITIAL_ALLOCATION / len(selected)"
            )
        )
        draft["name"] = "Synthetic actual policy RNG bootstrap"
        version = h.request(
            client,
            "POST",
            "/portable-strategy-versions",
            status=201,
            headers={"Idempotency-Key": uuid4().hex},
            json=draft,
        ).json()
        code = draft["code"]
        config = h.request(
            client,
            "POST",
            "/portable-runs/default-config",
            json={
                **refs,
                "seed_strategy": {
                    "id": version["id"],
                    "sha256": version["content_sha256"],
                },
                "execution_mode": "optimization",
                "windows": {
                    "train": {"start": "2024-01-10", "end": "2024-02-01"},
                    "validation": {"start": "2024-02-01", "end": "2024-02-15"},
                    "holdout": {"start": "2024-02-15", "end": "2024-03-01"},
                },
            },
        ).json()
        config["optimizer"]["max_iterations"] = 0
        config["budgets"].update(
            max_model_calls=0, max_tokens=0, max_runtime_seconds=180, max_repairs=0
        )
        config["research_capture"] = {"mode": "research"}
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
            "Original portable source failed",
            timeout=180,
        )
        route = "portable-runs"
    leaves = h.request(client, "GET", f"/{route}/{run['id']}/evaluations").json()[
        "data"
    ]
    require(
        len(leaves) == 2 and {v["split"] for v in leaves} == {"train", "validation"},
        "Source did not execute only its two planned non-holdout leaves",
    )
    leaf = next(v for v in leaves if v["split"] == "train")
    h.attempts.update(v["id"] for v in leaves)
    workbench = h.request(client, "GET", f"/evaluations/{leaf['id']}/workbench").json()
    pin = {
        "evaluation_id": leaf["id"],
        "execution_attempt_id": leaf["attempt_id"],
        "result_manifest_sha256": workbench["result_manifest_sha256"],
        "trace_manifest_sha256": workbench["trace_manifest_sha256"],
        "source_kind": "strategy_version",
        "source_id": version["id"],
        "code_sha256": version["content_sha256"],
        "spec_revision": None,
        "spec_sha256": None,
    }
    capability = h.request(
        client, "POST", "/independent-evaluations/capability", json=pin
    ).json()
    h.request(
        h.clients[1],
        "POST",
        "/independent-evaluations/capability",
        json=pin,
        status=404,
    )
    require(
        capability["can_execute"] and capability["can_control"],
        "Fresh source lacks independent execution capability",
    )
    require(
        capability["source_code"] == code, "Capability rewrote actual original code"
    )
    evidence = _evidence(h, leaf["id"], mode=mode, independent=False)
    fills = evidence["bundle"]["fills"]
    require(
        bool(fills if mode == "portable" else fills["rows"])
        and evidence["observation"]["audit"]["event_counts"].get("trade", 0) > 0,
        "Source must have actual fills, not vacuous RNG/economic equality",
    )
    require(
        evidence["observation"]["rng"]["restored_from_sha256"] is None,
        "Ordinary source was silently reseeded/restored",
    )
    return {
        "pin": pin,
        "code": code,
        "config": config,
        "detail": detail,
        "capability": capability,
        "leaf": leaf,
        "trace": h.request(
            client, "GET", f"/evaluations/{leaf['id']}/trace-manifest"
        ).json(),
        "evidence": evidence,
        "dataset": dataset,
        "universe": universe,
    }


def _evidence(h, evaluation_id, *, mode, independent):
    client, other = h.clients
    with h.connection(private=True) as connection:
        row = connection.execute(
            "SELECT e.payload, e.status, e.sandbox_stopped, e.attempt_id, e.run_id, "
            "a.sandbox_stopped FROM research_evaluations e "
            "JOIN execution_attempts a ON a.id=e.attempt_id WHERE e.id=%s",
            (evaluation_id,),
        ).fetchone()
    require(
        row is not None and row[1:3] == ("succeeded", True) and row[5],
        "Published evidence lacks real terminal leaf/attempt termination",
    )
    payload, attempt_id, run_id = row[0], str(row[3]), str(row[4])
    h.attempts.update((evaluation_id, attempt_id))
    keys = [
        "portable_result" if mode == "portable" else "result_bundle",
        "runtime_observation",
    ]
    if independent and payload.get("diagnostic_parity"):
        keys.append("diagnostic_parity")
    contents = {}
    for key in keys:
        ref = payload[key]
        contents[key] = h.request(
            client, "GET", f"/artifacts/{ref['artifact_id']}/download"
        ).content
        require(
            hashlib.sha256(contents[key]).hexdigest() == ref["sha256"]
            and len(contents[key]) == ref["size_bytes"],
            "Immutable result/observation/parity bytes differ from their registered ref",
        )
        h.request(other, "GET", f"/artifacts/{ref['artifact_id']}/download", status=404)
        with h.connection(private=True) as connection:
            registration = connection.execute(
                "SELECT run_id, attempt_id, sha256, size_bytes FROM artifacts WHERE id=%s",
                (ref["artifact_id"],),
            ).fetchone()
        require(
            tuple(map(str, registration[:2])) == (run_id, attempt_id)
            and registration[2:] == (ref["sha256"], ref["size_bytes"]),
            "Artifact registration crossed physical ownership",
        )
    bundle = json.loads(contents[keys[0]])
    if mode == "portable":
        validate_portable_result(contents[keys[0]])
        require(
            bundle["ledger_origin"] == "trusted_engine",
            "Policy output was substituted for trusted engine results",
        )
    elif independent:
        validate_independent_result(bundle)
        require(
            bundle["schema_version"] == "tradegrad.result-bundle.v2",
            "Independent native used legacy reward eligibility",
        )
    else:
        validate_result_bundle(bundle)
        require(
            bundle["schema_version"] == "tradegrad.result-bundle.v1",
            "Ordinary optimizer result protocol was changed",
        )
    observation = validate_runtime_observation(
        json.loads(contents["runtime_observation"]),
        expected_identity=bundle["identity"],
        expected_runtime_kind=mode,
    )
    audit = observation["audit"]
    require(
        audit is not None and audit["completed"] and not audit["loss_reasons"],
        "Actual public engine audit is missing, dropped or incomplete",
    )
    require(
        audit["event_counts"].get("started")
        == audit["event_counts"].get("finished")
        == 1
        and audit["event_counts"].get("equity", 0) > 0,
        "Audit lacks real start/account/finished receipts",
    )
    require(
        [event["seq"] for event in audit["events"]]
        == sorted({event["seq"] for event in audit["events"]}),
        "Audit reordered or reused public sequence numbers",
    )
    require(
        observation["rng"]["state"]["seam"] == "before_candidate_import.v1",
        "RNG was not captured at the actual candidate bootstrap seam",
    )
    require(
        (observation["rng"]["state"]["numpy_legacy"] is not None) == (mode == "native"),
        "Portable policy acquired a fake NumPy state or native omitted it",
    )
    if mode == "portable":
        for role in ("engine", "policy"):
            require(
                h.runner._inspect_role(evaluation_id, role) is None,
                "Terminal portable sibling was not removed",
            )
    else:
        require(
            h.runner._inspect(evaluation_id) is None,
            "Terminal native sandbox was not removed",
        )
    return {
        "payload": payload,
        "contents": contents,
        "bundle": bundle,
        "observation": observation,
    }


def _finished(h, run_id, expected="succeeded"):
    detail = h.request(h.clients[0], "GET", f"/independent-evaluations/{run_id}").json()
    if detail["status"] not in TERMINAL:
        return None
    require(
        detail["status"] == expected,
        "Independent terminal state differs from the expected real execution outcome",
    )
    require(
        all(v["sandbox_stopped"] and v["finished_at"] for v in detail["attempts"]),
        "Terminal independent attempt did not confirm sandbox termination",
    )
    require(
        detail["result_ready"] == (expected == "succeeded"),
        "Failed/cancelled execution became a usable result",
    )
    return detail


def _submit_diagnostic(h, source, capture):
    client, other = h.clients
    path = f"/evaluations/{source['pin']['evaluation_id']}/diagnostic-replays"
    body = {
        "source": source["pin"],
        "research_capture": capture,
        "max_runtime_seconds": 120,
    }
    h.request(
        client,
        "POST",
        path,
        status=409,
        headers={"Idempotency-Key": uuid4().hex},
        json={**body, "source": {**source["pin"], "result_manifest_sha256": "0" * 64}},
    )
    h.request(
        other,
        "POST",
        path,
        status=404,
        headers={"Idempotency-Key": uuid4().hex},
        json=body,
    )
    key = uuid4().hex
    run = h.request(
        client, "POST", path, status=202, headers={"Idempotency-Key": key}, json=body
    ).json()
    repeated = h.request(
        client, "POST", path, status=202, headers={"Idempotency-Key": key}, json=body
    ).json()
    require(
        run["id"] == repeated["id"]
        and run["config_sha256"] == repeated["config_sha256"],
        "Lost/repeated diagnostic submit created another execution",
    )
    require(
        run["config"]["execution_mode"] == "independent_evaluation"
        and run["purpose"] == "diagnostic",
        "Diagnostic was implemented as an optimizer run",
    )
    return run


def _assert_frozen_replay(h, source, replay, mode, *, matched=False):
    config = replay["config"]
    original = source["evidence"]
    pin = replay["result_pin"]
    require(
        pin is not None
        and pin["evaluation_id"] != source["pin"]["evaluation_id"]
        and pin["execution_attempt_id"] != source["pin"]["execution_attempt_id"],
        "Replay reused source physical identities",
    )
    require(
        config["source"] == source["pin"]
        and config["source_config"] == source["capability"]["original_config"],
        "Replay changed or nested the immutable source config",
    )
    require(
        config["source_config_sha256"]
        == config["original_config_sha256"]
        == source["capability"]["original_config_sha256"],
        "Replay discarded the original config hash",
    )
    require(
        config["reconstruction_window"]
        == {key: source["leaf"]["window"][key] for key in ("start", "end")},
        "Observation scope narrowed actual reconstruction",
    )
    require(
        config["source_config"]["dataset"] == source["config"]["dataset"]
        and config["source_config"]["universe"] == source["config"]["universe"],
        "Replay changed frozen data/universe",
    )
    require(
        config["research_image_digest"] == source["detail"]["research_image_digest"],
        "Replay used a different actual engine image",
    )
    if mode == "portable":
        require(
            config["portable_policy_image_digest"]
            == source["detail"]["portable_policy_image_digest"],
            "Replay used a different isolated policy image",
        )
    else:
        require(
            config["source_config"]["warmup"] == source["config"]["warmup"],
            "Replay replaced original warmup",
        )
    require(
        config["selected_strategy"]["sha256"] == source["pin"]["code_sha256"]
        and config["selected_strategy"]["id"] != source["pin"]["source_id"],
        "Replay did not create an exact-code immutable copy",
    )
    observed = _evidence(h, pin["evaluation_id"], mode=mode, independent=True)
    require(
        observed["observation"]["initialization"]
        == original["observation"]["initialization"],
        "Original effective window/warmup initialization was not restored",
    )
    require(
        config["rng_restore"]
        == original["observation"]["rng"]["state"]
        == observed["observation"]["rng"]["state"],
        "Replay did not restore actual pre-import RNG state",
    )
    require(
        config["runtime_observation_sha256"]
        == original["payload"]["runtime_observation"]["sha256"],
        "Replay RNG was not bound to original immutable sidecar",
    )
    require(
        observed["observation"]["rng"]["restored_from_sha256"]
        == original["observation"]["rng"]["state_sha256"],
        "Replay omitted actual RNG restoration receipt",
    )
    require(
        observed["bundle"]["identity"]["run_config_sha256"] == replay["config_sha256"],
        "Replay result bound itself to the old config rather than the independent request",
    )
    staged = (
        h.root
        / "storage"
        / "runs"
        / replay["id"]
        / "attempts"
        / pin["execution_attempt_id"]
        / "evaluations"
        / pin["evaluation_id"]
        / "input"
    )
    if mode == "native":
        request = json.loads((staged / "request.json").read_bytes())
        require(
            (staged / "strategy.py").read_text() == source["code"],
            "Native Docker mount changed immutable candidate bytes",
        )
        require(
            request["evaluation_config"]["result_policy"]
            == "independent_observation.v1",
            "Trusted native staging did not choose independent admission",
        )
        require(
            {"start": request["start_date"], "end": request["end_date"]}
            == config["reconstruction_window"],
            "Native Docker staging substituted the observation window",
        )
        require(
            request["runtime_observation"]["rng_restore"] == config["rng_restore"],
            "Native staged RNG differs from admitted immutable request",
        )
    else:
        request = json.loads((staged / "engine" / "request.json").read_bytes())
        require(
            (staged / "policy" / "policy.py").read_text() == source["code"],
            "Portable policy mount changed source bytes",
        )
        require(
            {key: request["window"][key] for key in ("start", "end")}
            == config["reconstruction_window"],
            "Portable engine staging substituted the observation window",
        )
        require(
            request["replay_rng_state"]
            == config["rng_restore"]
            == json.loads((staged / "policy" / "replay-rng-state.json").read_bytes()),
            "Engine and policy RNG bootstrap files do not agree",
        )
        require(
            {p.name for p in (staged / "engine").iterdir()}
            == {"request.json", "market.json"},
            "Candidate code entered the trusted portable engine mount",
        )
    require(
        all(not path.stat().st_mode & 0o222 for path in staged.rglob("*")),
        "Independent Docker inputs were left writable",
    )
    client, other = h.clients
    result_path = f"/independent-evaluations/evaluations/{pin['evaluation_id']}/result"
    params = {
        key: pin[key] for key in ("execution_attempt_id", "result_manifest_sha256")
    }
    view = h.request(client, "GET", result_path, params=params).json()
    require(
        view["reward_status"] == "not_applicable" and view["rows"],
        "Independent result API invented optimizer reward or omitted actual accounts",
    )
    require(
        h.request(client, "GET", result_path + "/download").content
        == observed["contents"][
            "portable_result" if mode == "portable" else "result_bundle"
        ],
        "Dedicated result download retargeted immutable bytes",
    )
    h.request(other, "GET", result_path, params=params, status=404)
    h.request(other, "GET", f"/independent-evaluations/{replay['id']}", status=404)
    h.request(
        client,
        "GET",
        result_path,
        params={**params, "result_manifest_sha256": "0" * 64},
        status=409,
    )
    trace = h.request(
        client, "GET", f"/evaluations/{pin['evaluation_id']}/trace-manifest"
    ).json()
    require(
        trace["context"]["trace_manifest_sha256"] == pin["trace_manifest_sha256"]
        and trace["context"]["result_manifest_sha256"] == pin["result_manifest_sha256"]
        and trace["manifest"]["capture"] == config["research_capture"],
        "Replay trace/result/capture pins changed",
    )
    h.request(
        other, "GET", f"/evaluations/{pin['evaluation_id']}/trace-manifest", status=404
    )
    parity_path = f"/independent-evaluations/{replay['id']}/parity"
    parity = h.request(client, "GET", parity_path).json()
    require(
        parity["report_sha256"] == observed["payload"]["diagnostic_parity"]["sha256"],
        "Parity API did not load exact fenced report artifact",
    )
    checks = {item["key"]: item["status"] for item in parity["checks"]}
    for key in (
        "source_identity",
        "fixed_conditions",
        "rng_reconstruction",
        "audit_coverage",
        "submitted_intents",
        "ordered_status_history",
        "fills",
        "position_trajectory",
        "account_trajectory",
        "result_metrics",
    ):
        require(
            checks[key] == "matched",
            f"Actual diagnostic semantic check did not match: {key}",
        )
    if matched:
        require(
            parity["outcome"] == "matched" and set(checks.values()) == {"matched"},
            "Maintained deterministic source did not verify all fourteen named checks",
        )
    else:
        require(
            checks["randomness_scope"] == "not_verifiable"
            and parity["outcome"] == "not_verifiable",
            "Custom stochastic source was falsely certified as covering all entropy",
        )
    events = h.request(
        client,
        "GET",
        parity_path + "/events",
        params={"report_sha256": parity["report_sha256"]},
    ).json()
    if matched:
        require(
            events["items"]
            and all(v["mapping"] == "verified_original_event" for v in events["items"]),
            "Matched maintained replay did not publish explicit original-event mappings",
        )
        require(
            all(
                v["original_pre_state_sha256"] and v["replay_pre_state_sha256"]
                for v in events["items"]
            ),
            "Verified mappings omitted actual decision-before state evidence",
        )
    else:
        require(
            all(v["mapping"] != "verified_original_event" for v in events["items"]),
            "Unverified diagnostic observations were promoted to original facts",
        )
    for pair in events["items"]:
        if pair["original_decision_id"] and pair["replay_decision_id"]:
            require(
                pair["original_decision_id"] != pair["replay_decision_id"],
                "Replay reused an original decision ID",
            )
    h.request(other, "GET", parity_path, status=404)
    h.request(
        client, "GET", parity_path, params={"report_sha256": "0" * 64}, status=409
    )
    require(
        h.request(
            client,
            "GET",
            f"/evaluations/{source['pin']['evaluation_id']}/trace-manifest",
        ).json()
        == source["trace"],
        "Diagnostic changed original immutable trace manifest or physical binding",
    )
    for key, content in original["contents"].items():
        ref = original["payload"][key]
        require(
            h.request(
                client, "GET", f"/artifacts/{ref['artifact_id']}/download"
            ).content
            == content,
            "Diagnostic changed original immutable result or RNG/audit evidence",
        )
    return observed


def _assert_queue_no_models_or_new_data(h, run_ids, import_count):
    with h.connection(private=True) as connection:
        require(
            connection.execute("SELECT count(*) FROM research_model_calls").fetchone()[
                0
            ]
            == 0,
            "Independent/source execution invoked a model",
        )
        require(
            connection.execute("SELECT count(*) FROM data_imports").fetchone()[0]
            == import_count,
            "Independent execution downloaded or imported new data",
        )
        for run_id in run_ids:
            rows = connection.execute(
                "SELECT o.generation, o.job_id, j.status FROM outbox_events o JOIN procrastinate_jobs j ON j.id=o.job_id WHERE o.run_id=%s ORDER BY o.generation",
                (run_id,),
            ).fetchall()
            require(
                rows
                and all(
                    v[1] is not None and v[2] in {"doing", "succeeded"} for v in rows
                ),
                "Independent execution bypassed actual durable queue",
            )
            generation = connection.execute(
                "SELECT generation FROM research_runs WHERE id=%s", (run_id,)
            ).fetchone()[0]
            require(
                [v[0] for v in rows] == list(range(1, generation + 1)),
                "Actual queue omitted or duplicated an independent generation",
            )
            exposures = connection.execute(
                "SELECT x.provenance, x.created_at, a.started_at FROM data_usage_exposures x "
                "JOIN execution_attempts a ON a.run_id=x.run_id "
                "AND a.generation=(x.provenance->>'generation')::integer "
                "WHERE x.run_id=%s AND x.purpose='independent_evaluation_admission'",
                (run_id,),
            ).fetchall()
            require(
                len(exposures) == generation
                and all(
                    value[0]["admission_before_dispatch"] and value[1] <= value[2]
                    for value in exposures
                ),
                "Independent generation dispatched without prior durable exposure admission",
            )
            checkpoints = connection.execute(
                "SELECT count(*) FROM research_checkpoints WHERE run_id=%s", (run_id,)
            ).fetchone()[0]
            require(
                checkpoints == 0,
                "Independent execution manufactured an optimizer checkpoint",
            )


def _import_count(h):
    with h.connection(private=True) as connection:
        return connection.execute("SELECT count(*) FROM data_imports").fetchone()[0]


def _cancelled_evidence(h, evaluation_id):
    with h.connection(private=True) as connection:
        return connection.execute(
            "SELECT e.status, e.sandbox_stopped, e.payload, e.payload_sha256, "
            "e.finished_at, e.attempt_id, a.status, a.sandbox_stopped, "
            "a.finished_at, a.generation FROM research_evaluations e "
            "JOIN execution_attempts a ON a.id=e.attempt_id WHERE e.id=%s",
            (evaluation_id,),
        ).fetchone()


@pytest.mark.parametrize("independent_harness", ["native", "portable"], indirect=True)
def test_real_original_rng_and_audit_survive_independent_replay(independent_harness):
    h = independent_harness
    mode = "portable" if isinstance(h, PortableProcessHarness) else "native"
    source = _source(h, mode, slow=mode == "native")
    before_imports = _import_count(h)
    capture = (
        {
            "mode": "research",
            "observation_window": {"start": "2024-02-20", "end": "2024-03-05"},
        }
        if mode == "native"
        else {"mode": "research"}
    )
    created = _submit_diagnostic(h, source, capture)
    replay = h.until(
        lambda: _finished(h, created["id"]),
        "Independent diagnostic did not complete",
        timeout=150,
    )
    _assert_frozen_replay(h, source, replay, mode)
    run_ids = [replay["id"]]
    if mode == "native":
        # Observe the actual owned Docker sandbox before cancelling, not a queued
        # no-op or a fabricated stop receipt. Source import deliberately sleeps.
        cancellation = _submit_diagnostic(h, source, capture)
        run_ids.append(cancellation["id"])

        def running():
            detail = h.request(
                h.clients[0], "GET", f"/independent-evaluations/{cancellation['id']}"
            ).json()
            require(
                detail["status"] not in TERMINAL,
                "Cancellation fixture finished before its real isolated boundary",
            )
            if detail["status"] != "running" or not detail["evaluation_id"]:
                return None
            state = h.runner._inspect(detail["evaluation_id"])
            if state is None or not state["State"]["Running"]:
                return None
            require(
                state["Config"]["Labels"]["io.tradegrad.run_id"] == detail["id"],
                "Cancellation target is not this owned run",
            )
            h.attempts.add(detail["evaluation_id"])
            return detail

        old = h.until(
            running, "Independent sandbox never reached running state", timeout=60
        )
        path = f"/independent-evaluations/{old['id']}"
        h.request(h.clients[1], "POST", path + "/cancel", status=404)
        h.request(h.clients[0], "POST", path + "/pause", status=409)
        h.request(
            h.clients[0],
            "POST",
            path + "/retry",
            status=409,
            headers={"Idempotency-Key": uuid4().hex},
        )
        h.request(h.clients[0], "POST", path + "/cancel", status=202)
        cancelled = h.until(
            lambda: _finished(h, old["id"], "cancelled"),
            "Cancellation did not confirm real sandbox stop",
            timeout=40,
        )
        require(
            h.runner._inspect(old["evaluation_id"]) is None
            and cancelled["result_pin"] is None,
            "Cancelled sandbox survived or became zero-profit evidence",
        )
        cancelled_evidence = _cancelled_evidence(h, old["evaluation_id"])
        require(
            cancelled_evidence is not None
            and cancelled_evidence[:2] == ("cancelled", True)
            and cancelled_evidence[2].get("status") == "cancelled"
            and not any(
                cancelled_evidence[2].get(key)
                for key in ("result_bundle", "portable_result", "diagnostic_parity")
            )
            and cancelled_evidence[3]
            and cancelled_evidence[4] is not None
            and cancelled_evidence[6:8] == ("cancelled", True)
            and cancelled_evidence[8] is not None
            and cancelled_evidence[9] == old["generation"],
            "Cancellation did not preserve terminal cancelled leaf and attempt evidence",
        )
        key = uuid4().hex
        h.request(
            h.clients[1],
            "POST",
            path + "/retry",
            status=404,
            headers={"Idempotency-Key": key},
        )
        retry = h.request(
            h.clients[0],
            "POST",
            path + "/retry",
            status=202,
            headers={"Idempotency-Key": key},
        ).json()
        duplicate = h.request(
            h.clients[0],
            "POST",
            path + "/retry",
            status=202,
            headers={"Idempotency-Key": key},
        ).json()
        require(
            retry["generation"] == duplicate["generation"] == old["generation"] + 1
            and retry["config_sha256"] == old["config_sha256"],
            "Retry mutated frozen inputs or duplicated a generation",
        )
        retried = h.until(
            lambda: _finished(h, old["id"]),
            "Independent retry did not restart original reconstruction",
            timeout=150,
        )
        require(
            retried["evaluation_id"] != old["evaluation_id"]
            and len(retried["attempts"]) == 2,
            "Retry reused an old leaf/attempt",
        )
        _assert_frozen_replay(h, source, retried, mode)
        require(
            _cancelled_evidence(h, old["evaluation_id"]) == cancelled_evidence,
            "Retry overwrote cancelled evidence",
        )
    _assert_queue_no_models_or_new_data(h, run_ids, before_imports)


@pytest.mark.parametrize("independent_harness", ["native"], indirect=True)
def test_real_three_arm_no_trade_failure_retry_and_unchanged_optimizer(
    independent_harness,
):
    h = independent_harness
    client, other = h.clients
    source = _source(h, "native")
    before_imports = _import_count(h)
    no_trade = source["code"].replace(
        "target_percent = INITIAL_ALLOCATION if fast_ma > slow_ma else 0.0",
        "target_percent = 0.0",
    )
    require(no_trade != source["code"], "No-trade fixture seam changed")
    failure = (
        "raise RuntimeError('deliberate isolated independent arm failure')\n"
        + source["code"]
    )
    body = {
        "source": source["pin"],
        "research_capture": {"mode": "research"},
        "max_runtime_seconds": 120,
        "name": "Explicit synthetic three-arm independent control",
        "hypothesis": "Suppressing target allocation removes actual orders without inventing optimizer reward.",
        "planned_change": "Reviewed code-only variants; not an isolated causal claim.",
        "arms": [
            {"name": "Exact baseline copy", "code": source["code"]},
            {
                "name": "No-trade allocation",
                "code": no_trade,
                "planned_change": "Set every target to zero.",
            },
            {
                "name": "Preserved execution failure",
                "code": failure,
                "planned_change": "Deliberately fail inside the isolated candidate import.",
            },
        ],
    }
    h.request(
        other,
        "POST",
        "/controlled-experiments",
        status=404,
        headers={"Idempotency-Key": uuid4().hex},
        json=body,
    )
    key = uuid4().hex
    created = h.request(
        client,
        "POST",
        "/controlled-experiments",
        status=202,
        headers={"Idempotency-Key": key},
        json=body,
    ).json()
    repeated = h.request(
        client,
        "POST",
        "/controlled-experiments",
        status=202,
        headers={"Idempotency-Key": key},
        json=body,
    ).json()
    require(
        created["id"] == repeated["id"]
        and created["protocol_sha256"] == source["capability"]["protocol_sha256"],
        "Control protocol was mutated or duplicate arms were scheduled",
    )

    def complete():
        value = h.request(
            client, "GET", f"/controlled-experiments/{created['id']}"
        ).json()
        return value if all(v["status"] in TERMINAL for v in value["arms"]) else None

    control = h.until(
        complete, "Three real isolated control arms did not finish", timeout=180
    )
    require(
        [v["status"] for v in control["arms"]] == ["succeeded", "succeeded", "failed"],
        "Failed arm was hidden or no-trade execution was rejected",
    )
    require(
        [v["interpretation"] for v in control["arms"]]
        == ["baseline", "not_individually_validated", "not_individually_validated"],
        "Reviewed code patches falsely became single-factor causal proof",
    )
    require(
        len({v["strategy_version_id"] for v in control["arms"]})
        == len({v["run_id"] for v in control["arms"]})
        == 3,
        "Control arms reused mutable strategy/run identities",
    )
    require(
        control["root_experiment_id"] == source["detail"]["experiment_id"]
        and control["parent_experiment_id"] == source["detail"]["experiment_id"]
        and control["experiment_id"] != source["detail"]["experiment_id"]
        and control["split_version_id"] != source["detail"]["split_version_id"],
        "Child control reused another experiment's split ownership",
    )
    require(
        control["exposure_snapshot"]["independent_unseen_holdout_established"] is False
        and control["exposure_snapshot"]["classification"]
        == source["capability"]["exposure_snapshot"]["classification"],
        "Fresh controlled family reset source exposure classification or claimed an unseen holdout",
    )
    h.request(other, "GET", f"/controlled-experiments/{control['id']}", status=404)
    with h.connection(private=True) as connection:
        definitions = connection.execute(
            "SELECT definition FROM split_versions WHERE id IN (%s,%s)",
            (control["split_version_id"], source["detail"]["split_version_id"]),
        ).fetchall()
    require(
        len(definitions) == 2 and definitions[0][0] == definitions[1][0],
        "Control changed exact frozen split definition",
    )
    successful = []
    for arm in control["arms"]:
        detail = h.until(
            lambda arm=arm: _finished(h, arm["run_id"], arm["status"]),
            "Control status did not match its physical run",
            timeout=10,
        )
        require(
            detail["config"]["protocol_sha256"] == control["protocol_sha256"]
            and detail["config"]["source_config"]
            == source["capability"]["original_config"],
            "Arm changed shared fixed conditions",
        )
        if arm["status"] == "succeeded":
            evidence = _evidence(
                h, arm["evaluation_id"], mode="native", independent=True
            )
            successful.append(evidence)
        else:
            require(
                arm["result_pin"] is None
                and arm["error"]
                and detail["result_pin"] is None,
                "Failed arm acquired a successful or zero-profit result",
            )
    bundle = successful[1]["bundle"]
    metrics = {v["name"]: v["value"] for v in bundle["metrics"]}
    require(
        not bundle["orders"]["rows"]
        and not bundle["fills"]["rows"]
        and not bundle["closed_trades"]["rows"]
        and bundle["equity"]["rows"],
        "No-trade execution fabricated orders/trades or lost real account observations",
    )
    require(
        metrics["combined_score"] is None
        and metrics["win_rate"] is None
        and metrics["return_per_trade"] is None
        and metrics["trade_per_day"] == 0,
        "Unavailable no-trade statistics or inapplicable reward became numeric success",
    )
    no_trade_pin = control["arms"][1]["result_pin"]
    query = h.request(
        client,
        "GET",
        f"/independent-evaluations/evaluations/{no_trade_pin['evaluation_id']}/result",
        params={
            "execution_attempt_id": no_trade_pin["execution_attempt_id"],
            "result_manifest_sha256": no_trade_pin["result_manifest_sha256"],
            "section": "closed_trades",
        },
    ).json()
    require(
        query["reward_status"] == "not_applicable"
        and query["total"] == 0
        and query["rows"] == [],
        "No-trade result query invented reward or trades",
    )
    failed = control["arms"][2]
    before = h.request(
        client, "GET", f"/independent-evaluations/{failed['run_id']}"
    ).json()
    h.request(
        client,
        "POST",
        f"/independent-evaluations/{failed['run_id']}/retry",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
    )
    after = h.until(
        lambda: _finished(h, failed["run_id"], "failed"),
        "Real failing arm retry did not terminate truthfully",
        timeout=90,
    )
    require(
        after["generation"] == before["generation"] + 1
        and after["evaluation_id"] != before["evaluation_id"]
        and after["config_sha256"] == before["config_sha256"],
        "Failed retry overwrote original evidence or changed frozen inputs",
    )
    _, legacy, _ = _create_native(h, source["dataset"], source["universe"], no_trade)

    def optimizer_terminal():
        detail = h.request(client, "GET", f"/research-runs/{legacy['id']}").json()
        return detail if detail["status"] in TERMINAL else None

    legacy_detail = h.until(
        optimizer_terminal, "Legacy no-trade optimizer did not terminate", timeout=120
    )
    require(
        legacy_detail["status"] == "failed",
        "Independent no-trade acceptance weakened optimizer reward eligibility",
    )
    legacy_leaves = h.request(
        client, "GET", f"/research-runs/{legacy['id']}/evaluations"
    ).json()["data"]
    require(
        legacy_leaves
        and all(
            v["status"] == "failed" and v["sandbox_stopped"] for v in legacy_leaves
        ),
        "Optimizer promoted a no-closed-trade leaf to success",
    )
    with h.connection(private=True) as connection:
        require(
            connection.execute(
                "SELECT count(*) FROM artifacts WHERE run_id=%s AND name LIKE '%%result-bundle.json'",
                (legacy["id"],),
            ).fetchone()[0]
            == 0,
            "Optimizer published a successful independent result through its legacy path",
        )
    _assert_queue_no_models_or_new_data(
        h, [v["run_id"] for v in control["arms"]], before_imports
    )


@pytest.mark.parametrize("independent_harness", ["native"], indirect=True)
def test_real_exact_maintained_template_diagnostic_verifies_all_semantics(
    independent_harness,
):
    h = independent_harness
    source = _source(h, "native", maintained=True)
    before_imports = _import_count(h)
    created = _submit_diagnostic(h, source, {"mode": "research"})
    replay = h.until(
        lambda: _finished(h, created["id"]),
        "Maintained deterministic diagnostic did not finish",
        timeout=150,
    )
    require(
        replay["config"]["selected_contract"] == source["config"]["strategy_contract"],
        "Diagnostic changed documented business parameters rather than preserving source",
    )
    _assert_frozen_replay(h, source, replay, "native", matched=True)
    _assert_queue_no_models_or_new_data(h, [replay["id"]], before_imports)
