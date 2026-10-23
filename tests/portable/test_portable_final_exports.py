"""Pure portable final/export contracts with inert source and fake engine records.

No PostgreSQL, Docker, policy execution or AKQuant backtest occurs in this suite.
"""

import copy
import io
import secrets
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from test_engine import hold_bundle

from backend.app.models import Artifact, ResearchRun
from backend.app.portable_final_services import final_config
from backend.app.portable_final_views import (
    VerifiedPortableFinal,
    final_result_view,
    validate_portable_final_result,
)
from backend.app.portable_services import portable_score
from backend.app.research_services import payload_hash
from backend.app.services import DomainError
from packages.strategy_sdk import hash_json
from packages.tradegrad_adapter.config import RunConfig
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
from packages.tradegrad_adapter.portable_config import (
    PortableRunConfig,
    portable_evaluation_windows,
)
from packages.tradegrad_adapter.portable_final_config import (
    PortableFinalEvaluationConfig,
)
from packages.tradegrad_adapter.portable_results import (
    canonical_json_bytes,
    validate_portable_result,
)
from packages.tradegrad_adapter.strategy_package import (
    PORTABLE_ENV_SCHEMA,
    PackageError,
    PackageInput,
    build_strategy_package,
    canonical,
    sha256,
)
from workers.research.portable_optimization import LOCKS

CODE = b"raise AssertionError('THIS SOURCE IS INERT AND MUST NEVER RUN ON HOST')\n"
IMAGES = {"engine": "sha256:" + "a" * 64, "policy": "sha256:" + "b" * 64}


def final_evidence():
    bundle = hold_bundle()  # Fake broker/controller only, never policy Python.
    manifest = bundle["manifest"]
    params = {"top_n": 1}
    locks = {
        name: {
            "content": "fake-dependency==0.0.0\n",
            "sha256": sha256(b"fake-dependency==0.0.0\n"),
        }
        for name in LOCKS
    }
    build = {
        "schema_version": "tradegrad.portable-build-inputs.v1",
        "files": {name: row["sha256"] for name, row in locks.items()},
    }
    environment = {
        "schema_version": PORTABLE_ENV_SCHEMA,
        "environment_digest": hash_json(build),
        "image_ids": IMAGES,
        "build_inputs": build,
        "dependency_locks": locks,
        "provenance": "captured_at_execution",
    }
    dataset_manifest = {
        "schema_version": "synthetic-contract.v1",
        "is_synthetic": True,
        "files": {},
        "artifact_files": {"sessions.json": "c" * 64, "actions.json": "d" * 64},
        "listings": [],
    }
    manifest["code_sha256"] = sha256(CODE)
    manifest["parameters_sha256"] = hash_json(params)
    manifest["engine_profile"]["environment_sha256"] = environment["environment_digest"]
    manifest["reference_result_sha256"] = None
    source = PortableRunConfig(
        execution_mode="optimization",
        task="us_cross",
        strategy_kind="portfolio",
        dataset={"id": str(uuid4()), "sha256": hash_json(dataset_manifest)},
        seed_strategy={"id": manifest["strategy_id"], "sha256": sha256(CODE)},
        seed_manifest={
            "id": manifest["strategy_id"] + "/manifest",
            "sha256": hash_json(manifest),
        },
        seed_parameters={
            "id": manifest["strategy_id"] + "/parameters",
            "sha256": hash_json(params),
        },
        universe={
            "id": str(uuid4()),
            "sha256": manifest["universe"]["snapshot_sha256"],
        },
        windows={
            "train": {"start": "2024-01-01", "end": "2024-06-01"},
            "validation": {"start": "2024-06-01", "end": "2024-12-01"},
            "holdout": {
                "start": bundle["window"]["start"],
                "end": bundle["window"]["end"],
            },
        },
        warmup={
            "sessions": max(
                2, manifest["data"]["lookback_bars"], manifest["data"]["warmup_bars"]
            )
        },
        initial_cash=bundle["summary"]["initial_cash"],
        initial_state=bundle["final_state"],
        environment_digest=environment["environment_digest"],
    )
    selected_id = str(uuid4())
    config = PortableFinalEvaluationConfig(
        selection_id=uuid4(),
        split_version_id=uuid4(),
        split_sha256="e" * 64,
        selected_strategy={"id": selected_id, "sha256": sha256(CODE)},
        selected_manifest={
            "id": selected_id + "/manifest",
            "sha256": hash_json(manifest),
        },
        selected_parameters={
            "id": selected_id + "/parameters",
            "sha256": hash_json(params),
        },
        source_config=source,
        source_config_sha256=source.sha256,
        holdout=source.windows.holdout,
        environment_digest=source.environment_digest,
        max_runtime_seconds=300,
    )
    bundle["identity"].update(
        strategy_sha256=sha256(CODE),
        manifest_sha256=hash_json(manifest),
        parameters_sha256=hash_json(params),
        run_config_sha256=config.sha256,
    )
    validate_portable_result(bundle)
    content = canonical_json_bytes(bundle)
    owner_id, run_id, evaluation_id, attempt_id = uuid4(), uuid4(), uuid4(), uuid4()
    dataset = SimpleNamespace(
        id=UUID(source.dataset.id),
        owner_id=owner_id,
        content_sha256=source.dataset.sha256,
        manifest=dataset_manifest,
        is_synthetic=True,
        start_date=source.windows.train.start,
        end_date=config.holdout.end,
        market="US",
    )
    run = ResearchRun(
        id=run_id,
        owner_id=owner_id,
        request_sha256=config.sha256,
        config=config.model_dump(mode="json"),
        status="succeeded",
        generation=1,
        active_attempt_id=attempt_id,
        experiment_id=uuid4(),
        dataset_version_id=dataset.id,
        final_selection_id=config.selection_id,
        split_version_id=config.split_version_id,
        strategy_version_id=UUID(selected_id),
        research_image_digest=IMAGES["engine"],
        portable_policy_image_digest=IMAGES["policy"],
        result={"evaluation_id": str(evaluation_id)},
    )
    artifact = Artifact(
        id=uuid4(),
        run_id=run_id,
        attempt_id=attempt_id,
        name=f"evaluation-{evaluation_id}-portable-result.json",
        media_type="application/json",
        sha256=sha256(content),
        size_bytes=len(content),
        storage_key="fixture/result.json",
    )
    payload = {
        "status": "succeeded",
        "exit_code": 0,
        "image_digest": IMAGES["engine"],
        "policy_image_digest": IMAGES["policy"],
        "metrics": bundle["summary"],
        "combined_score": portable_score(bundle),
        "result_trust": bundle["result_trust"],
        "ledger_origin": "trusted_engine",
        "runtime_certified": False,
        "portable_result": {
            "artifact_id": str(artifact.id),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        },
    }
    window = {"split": "holdout", **config.holdout.model_dump(mode="json")}
    evaluation = SimpleNamespace(
        id=evaluation_id,
        run_id=run_id,
        attempt_id=attempt_id,
        generation=1,
        status="succeeded",
        sandbox_stopped=True,
        finished_at=datetime.now(UTC),
        window=window,
        split="holdout",
        window_sha256=payload_hash(window),
        code_sha256=sha256(CODE),
        image_digest=IMAGES["engine"],
        payload=payload,
        payload_sha256=payload_hash(payload),
    )
    universe = {
        "reference_sha256": source.universe.sha256,
        "serialization": "serialization_not_original",
        "manifest": {"members": []},
        "manifest_sha256": hash_json({"members": []}),
    }
    package = PackageInput(
        CODE,
        "portable",
        sha256(CODE),
        params,
        {
            "source_code_sha256": sha256(CODE),
            "source_config_sha256": source.sha256,
            "final_config_sha256": config.sha256,
        },
        source.model_dump(mode="json"),
        config.model_dump(mode="json"),
        dataset_manifest,
        universe,
        {"windows": source.windows.model_dump(mode="json")},
        environment,
        content,
        sha256(content),
        {"classification": "previously_exposed"},
        "2026-10-22T12:00:00+00:00",
        manifest,
    )
    return SimpleNamespace(
        config=config,
        source=source,
        bundle=bundle,
        content=content,
        run=run,
        dataset=dataset,
        artifact=artifact,
        evaluation=evaluation,
        package=package,
    )


def test_portable_final_is_distinct_exact_and_not_a_research_window():
    f = final_evidence()
    assert final_config(f.run) == f.config
    assert f.config.source_config.canonical_bytes() == f.source.canonical_bytes()
    assert all(
        row["split"] != "holdout" for row in portable_evaluation_windows(f.source)
    )
    for cls in (RunConfig, FinalEvaluationConfig, PortableRunConfig):
        with pytest.raises(ValidationError):
            cls.model_validate(f.config.model_dump(mode="json"))
    for key, value in (
        ("holdout", {"start": "2025-01-03", "end": "2025-01-07"}),
        ("source_config_sha256", "f" * 64),
        ("environment_digest", "f" * 64),
        ("allow_holdout", True),
        ("optimizer", {}),
    ):
        raw = f.config.model_dump(mode="json")
        raw[key] = value
        with pytest.raises(ValidationError):
            PortableFinalEvaluationConfig.model_validate(raw)


def test_persisted_final_and_bounded_trace_query_are_data_only():
    f = final_evidence()
    assert (
        validate_portable_final_result(
            f.evaluation, f.run, f.dataset, f.artifact, f.content
        )
        == f.bundle
    )
    result = VerifiedPortableFinal(
        f.evaluation, f.run, f.dataset, f.artifact, f.bundle, f.content
    )
    response = final_result_view(
        result,
        exposure_snapshot={"classification": "previously_exposed"},
        section="decision_trace",
        limit=1,
    )
    assert response.selection_feedback is False and response.runtime_certified is False
    assert response.config == f.config and len(response.rows) == 1
    with pytest.raises(DomainError):
        final_result_view(result, exposure_snapshot={}, limit=501)


@pytest.mark.parametrize(
    "mutation",
    [
        "code",
        "window",
        "owner",
        "dataset",
        "engine",
        "policy",
        "config",
        "terminal",
        "attempt",
        "summary",
        "payload_hash",
        "payload_status",
        "exit_code",
        "artifact_size",
        "artifact_hash",
    ],
)
def test_final_result_fail_closed_on_changed_identity(mutation):
    f = final_evidence()
    if mutation == "code":
        f.evaluation.code_sha256 = "f" * 64
    elif mutation == "window":
        f.evaluation.window["end"] = "2026-01-01"
    elif mutation == "owner":
        f.dataset.owner_id = uuid4()
    elif mutation == "dataset":
        f.dataset.content_sha256 = "f" * 64
    elif mutation == "engine":
        f.run.research_image_digest = "sha256:" + "f" * 64
    elif mutation == "policy":
        f.run.portable_policy_image_digest = "sha256:" + "f" * 64
    elif mutation == "config":
        f.run.request_sha256 = "f" * 64
    elif mutation == "terminal":
        f.run.status = "running"
    elif mutation == "attempt":
        f.run.active_attempt_id = uuid4()
    elif mutation == "summary":
        f.evaluation.payload["metrics"] = {}
    elif mutation == "payload_status":
        f.evaluation.payload["status"] = "failed"
        f.evaluation.payload_sha256 = payload_hash(f.evaluation.payload)
    elif mutation == "exit_code":
        f.evaluation.payload["exit_code"] = 1
        f.evaluation.payload_sha256 = payload_hash(f.evaluation.payload)
    elif mutation == "payload_hash":
        f.evaluation.payload_sha256 = "f" * 64
    elif mutation == "artifact_size":
        f.artifact.size_bytes += 1
    else:
        f.artifact.sha256 = "f" * 64
    with pytest.raises(DomainError):
        validate_portable_final_result(
            f.evaluation, f.run, f.dataset, f.artifact, f.content
        )


def test_portable_package_preserves_actual_contract_parameters_state_and_dual_images():
    f = final_evidence()
    a, b = build_strategy_package(f.package), build_strategy_package(f.package)
    assert a.archive == b.archive and a.archive_sha256 == sha256(a.archive)
    with zipfile.ZipFile(io.BytesIO(a.archive)) as archive:
        assert archive.read("strategy/strategy.py") == CODE
        assert archive.read("strategy/policy-manifest.json") == canonical(
            f.package.policy_manifest
        )
        state = __import__("json").loads(archive.read("strategy/state-contract.json"))
        assert state["initial_state"] == f.source.initial_state.model_dump(mode="json")
        env = __import__("json").loads(
            archive.read("environment/execution-environment.json")
        )
        assert env["image_ids"] == IMAGES
        assert len(
            [x for x in archive.namelist() if x.startswith("environment/locks/")]
        ) == len(LOCKS)
        assert archive.read("reference/decision-trace.json") == canonical(
            f.bundle["decision_trace"]
        )
        assert b"never import policy on the host" in archive.read("REPLAY.txt")
        assert not any(
            x.endswith(".log") or "preview.json" in x or "model" in x
            for x in archive.namelist()
        )
    assert (
        a.manifest["capabilities"]["external_framework_equivalence"]
        == "not_established"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "manifest",
        "parameters",
        "state",
        "config",
        "mode",
        "source",
        "result",
        "image",
        "lock",
    ],
)
def test_portable_package_rejects_ambiguous_or_changed_reference(mutation):
    f = final_evidence()
    value = copy.deepcopy(f.package)
    if mutation == "manifest":
        value.policy_manifest["data"]["warmup_bars"] += 1
    elif mutation == "parameters":
        value.parameters["top_n"] = 2
    elif mutation == "state":
        value.source_config["initial_state"]["schema_sha256"] = "f" * 64
    elif mutation == "config":
        value.final_config["source_config_sha256"] = "f" * 64
    elif mutation == "mode":
        value = replace(value, strategy_mode="portable_policy")
    elif mutation == "source":
        value = replace(value, strategy_code=CODE + b"#changed")
    elif mutation == "result":
        value = replace(value, result_content=b"{}")
    elif mutation == "image":
        value.environment_evidence["image_ids"]["policy"] = "latest"
    else:
        value.environment_evidence["dependency_locks"][LOCKS[0]]["content"] += "changed"
    with pytest.raises(PackageError):
        build_strategy_package(value)


def test_portable_final_http_auth_and_strict_contracts_without_database():
    from fastapi.testclient import TestClient

    from backend.app.core.config import Settings
    from backend.app.main import create_app

    app = create_app(
        Settings(
            database_url="postgresql://unused.invalid/not_connected",
            secret_key=secrets.token_urlsafe(48),
        )
    )
    # Parent registers the new router during integration. Avoid opening any DB.
    schema = app.openapi()
    path = "/api/v1/portable-final-evaluations"
    assert path in schema["paths"]
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        assert client.post(path + "/preflight").status_code == 401
        assert (
            client.get(path + "/evaluations/" + str(uuid4()) + "/result").status_code
            == 401
        )
    retry = schema["paths"][path + "/{run_id}/retry"]["post"]
    assert any(
        p["name"] == "Idempotency-Key" and p["required"] for p in retry["parameters"]
    )


def test_freeze_derives_only_code_hash_and_preserves_real_stored_contract():
    from test_staging import fixture

    from backend.app.experiment_services import portable_selected_metadata
    from packages.tradegrad_adapter.portable_prompts import template_source

    config, _dataset, _blobs, original_manifest, parameters = fixture()
    source = template_source("event")
    selected = source.replace(
        "rising = bars[-1].close > bars[-2].close",
        "rising = bars[-1].close >= bars[-2].close",
    )
    manifest = original_manifest.model_dump(mode="json")
    manifest["code_sha256"] = sha256(source.encode())
    raw = config.model_dump(mode="json")
    raw["seed_strategy"]["sha256"] = manifest["code_sha256"]
    raw["seed_manifest"]["sha256"] = hash_json(manifest)
    config = PortableRunConfig.model_validate(raw)
    strategy = SimpleNamespace(
        id=UUID(config.seed_strategy.id),
        mode="portable",
        code=source,
        content_sha256=manifest["code_sha256"],
        manifest=manifest,
        parameters=parameters,
    )
    candidate = SimpleNamespace(code=selected, code_sha256=sha256(selected.encode()))
    frozen, params = portable_selected_metadata(config, strategy, candidate)
    assert params == parameters and params is not parameters
    expected = copy.deepcopy(manifest)
    expected["code_sha256"] = candidate.code_sha256
    assert frozen == expected
    assert strategy.manifest == manifest and strategy.code == source
    for mutation in ("parameters", "state", "protected_source", "source_identity"):
        bad_strategy, bad_candidate = copy.deepcopy(strategy), copy.deepcopy(candidate)
        if mutation == "parameters":
            bad_strategy.parameters["quantity"] += 1
        elif mutation == "state":
            bad_strategy.manifest["state_schema"]["version"] += 1
        elif mutation == "protected_source":
            bad_candidate.code += "\nprint('protected outside edit')\n"
        else:
            bad_strategy.content_sha256 = "f" * 64
        with pytest.raises(DomainError):
            portable_selected_metadata(config, bad_strategy, bad_candidate)
