"""Pure evidence and fake-transaction checks, not real DB/container integration."""

import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_engine import hold_bundle

from backend.app.models import Artifact
from backend.app.portable_services import portable_score
from backend.app.portable_views import OwnedPortableResult, portable_result_view
from backend.app.services import DomainError
from packages.strategy_sdk import hash_json
from packages.tradegrad_adapter.portable_config import PortableRunConfig
from packages.tradegrad_adapter.portable_prompts import template_source
from packages.tradegrad_adapter.portable_results import (
    canonical_json_bytes,
    validate_portable_result,
)
from workers.research.portable_optimization import publish_portable_artifacts

IMAGES = {"engine": "sha256:" + "a" * 64, "policy": "sha256:" + "b" * 64}


def evidence():
    bundle = hold_bundle()  # Inert trusted controller/fake broker; no policy execution.
    manifest = bundle["manifest"]
    code = template_source("portfolio")
    manifest["code_sha256"] = hashlib.sha256(code.encode()).hexdigest()
    config = PortableRunConfig(
        task="us_cross",
        strategy_kind="portfolio",
        dataset={"id": str(uuid4()), "sha256": "a" * 64},
        seed_strategy={
            "id": manifest["strategy_id"],
            "sha256": manifest["code_sha256"],
        },
        seed_manifest={
            "id": manifest["strategy_id"] + "/manifest",
            "sha256": hash_json(manifest),
        },
        seed_parameters={
            "id": manifest["strategy_id"] + "/parameters",
            "sha256": manifest["parameters_sha256"],
        },
        universe={
            "id": str(uuid4()),
            "sha256": manifest["universe"]["snapshot_sha256"],
        },
        windows={
            "train": {
                "start": bundle["window"]["start"],
                "end": bundle["window"]["end"],
            }
        },
        warmup={"sessions": 2},
        initial_cash=bundle["summary"]["initial_cash"],
        initial_state=bundle["final_state"],
        environment_digest=manifest["engine_profile"]["environment_sha256"],
    )
    bundle["identity"].update(
        strategy_sha256=manifest["code_sha256"],
        manifest_sha256=hash_json(manifest),
        run_config_sha256=config.sha256,
    )
    validate_portable_result(bundle)
    row = SimpleNamespace(
        id=uuid4(),
        code_sha256=manifest["code_sha256"],
        window={"split": "train", **config.windows.train.model_dump(mode="json")},
    )
    return bundle, config, row


def test_exact_success_requires_runner_file_and_all_image_identities(tmp_path):
    bundle, config, row = evidence()
    output = tmp_path / "output"
    output.mkdir()
    content = canonical_json_bytes(bundle)
    (output / "portable-result.json").write_bytes(content)
    result = {
        "status": "succeeded",
        "bundle": bundle,
        "image_digests": IMAGES,
        "result_trust": bundle["result_trust"],
        "ledger_origin": "trusted_engine",
        "runtime_certified": False,
        "logs": {},
    }
    records, verified = publish_portable_artifacts(
        storage_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        output_root=output,
        result=result,
        config=config,
        code_sha256=row.code_sha256,
        window=row.window,
        images=IMAGES,
        max_bytes=8 * 1_048_576,
    )
    assert verified == bundle and len(records) == 1
    assert (tmp_path / records[0]["storage_key"]).read_bytes() == content
    assert records[0]["sha256"] == hashlib.sha256(content).hexdigest()
    assert portable_score(bundle) == round(
        bundle["summary"]["total_return_pct"] - bundle["summary"]["max_drawdown_pct"], 8
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "file_missing",
        "file_changed",
        "engine_image",
        "policy_image",
        "false_trust",
        "certified",
        "source",
        "config",
        "window",
    ],
)
def test_artifact_publisher_rejects_unbound_or_forged_evidence(tmp_path, mutation):
    bundle, config, row = evidence()
    output = tmp_path / "output"
    output.mkdir()
    content = canonical_json_bytes(bundle)
    if mutation != "file_missing":
        (output / "portable-result.json").write_bytes(
            content if mutation != "file_changed" else b"{}"
        )
    result = {
        "status": "succeeded",
        "bundle": bundle,
        "image_digests": dict(IMAGES),
        "result_trust": bundle["result_trust"],
        "ledger_origin": "trusted_engine",
        "runtime_certified": False,
        "logs": {},
    }
    if mutation in {"engine_image", "policy_image"}:
        result["image_digests"][mutation.split("_")[0]] = "sha256:" + "c" * 64
    if mutation == "false_trust":
        result["ledger_origin"] = "policy"
    if mutation == "certified":
        result["runtime_certified"] = True
    if mutation == "source":
        row.code_sha256 = "f" * 64
    if mutation == "config":
        config = config.model_copy(update={"environment_digest": "f" * 64})
    if mutation == "window":
        row.window["start"] = "2025-01-01"
    with pytest.raises((ValueError, DomainError)):
        publish_portable_artifacts(
            storage_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
            output_root=output,
            result=result,
            config=config,
            code_sha256=row.code_sha256,
            window=row.window,
            images=IMAGES,
            max_bytes=8 * 1_048_576,
        )
    assert not (tmp_path / "artifacts" / "portable-result.json").exists()


def test_failed_runner_cannot_publish_trusted_result(tmp_path):
    bundle, config, row = evidence()
    records, verified = publish_portable_artifacts(
        storage_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        output_root=tmp_path / "missing",
        result={
            "status": "failed",
            "bundle": bundle,
            "result_trust": bundle["result_trust"],
            "logs": {},
        },
        config=config,
        code_sha256=row.code_sha256,
        window=row.window,
        images=IMAGES,
        max_bytes=8 * 1_048_576,
    )
    assert records == [] and verified is None


@pytest.mark.parametrize(
    "section",
    [
        "account_series",
        "positions",
        "orders",
        "fills",
        "closed_trades",
        "native_metrics",
        "decision_trace",
        "logical_orders",
    ],
)
def test_bounded_typed_result_sections_and_honest_provenance(section):
    bundle, config, row = evidence()
    run = SimpleNamespace(
        id=uuid4(),
        config=config.model_dump(mode="json"),
        research_image_digest=IMAGES["engine"],
        portable_policy_image_digest=IMAGES["policy"],
    )
    result = OwnedPortableResult(
        row,
        SimpleNamespace(sha256="a" * 64),
        bundle,
        canonical_json_bytes(bundle),
        run,
        True,
    )
    view = portable_result_view(result, section, 0, 1)
    assert len(view.rows) <= 1 and view.count == len(bundle[section])
    assert view.runtime_certified is False and view.is_synthetic is True
    assert view.image_digest != view.policy_image_digest
    with pytest.raises((ValueError, DomainError)):
        portable_result_view(result, section, 0, 501)


@pytest.mark.parametrize("failed", [False, True])
def test_completion_checks_registered_artifact_and_removes_failed_trust(
    tmp_path, monkeypatch, failed
):
    from backend.app import portable_services as services

    bundle, config, row = evidence()
    run_id, attempt_id = uuid4(), uuid4()
    row.run_id, row.attempt_id, row.finished_at = run_id, attempt_id, None
    row.candidate_key, row.split, row.image_digest = (
        "candidate-0000",
        "train",
        IMAGES["engine"],
    )
    run = SimpleNamespace(
        id=run_id,
        config=config.model_dump(mode="json"),
        research_image_digest=IMAGES["engine"],
        portable_policy_image_digest=IMAGES["policy"],
    )
    attempt = SimpleNamespace(id=attempt_id)
    data = canonical_json_bytes(bundle)
    (tmp_path / "result.json").write_bytes(data)
    artifact = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        attempt_id=attempt_id,
        name=f"evaluation-{row.id}-portable-result.json",
        media_type="application/json",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        storage_key="result.json",
    )
    session = SimpleNamespace(
        scalar=lambda query: row,
        get=lambda model, key: (
            artifact if model is Artifact and key == artifact.id else None
        ),
        flush=lambda: None,
    )
    monkeypatch.setattr(services, "fenced_attempt", lambda *a: (run, attempt))
    monkeypatch.setattr(services, "append_event", lambda *a: None)
    payload = {
        "status": "succeeded",
        "exit_code": 0,
        "image_digest": IMAGES["engine"],
        "policy_image_digest": IMAGES["policy"],
        "portable_result": {
            "artifact_id": str(artifact.id),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        },
        "metrics": bundle["summary"],
        "combined_score": portable_score(bundle),
        "result_trust": bundle["result_trust"],
        "ledger_origin": "trusted_engine",
        "runtime_certified": False,
    }
    if failed:
        payload["metrics"] = {"forged": 10}
    services.complete_portable_evaluation(
        session,
        attempt_id,
        1,
        uuid4(),
        row.id,
        outcome=payload,
        sandbox_stopped=True,
        settings=SimpleNamespace(
            storage_root=tmp_path, max_artifact_bytes=8 * 1_048_576
        ),
    )
    assert row.status == ("failed" if failed else "succeeded")
    if failed:
        assert not {
            "result_trust",
            "ledger_origin",
            "portable_result",
            "metrics",
            "combined_score",
        } & set(row.payload)
    else:
        assert row.payload == payload


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "file",
        "policy_image",
        "trust",
        "exit_code",
        "owner_dataset",
        "artifact_attempt",
    ],
)
def test_persisted_result_read_rechecks_hash_identity_and_success(
    tmp_path, monkeypatch, mutation
):
    from datetime import UTC, datetime

    from backend.app import portable_views as views
    from backend.app.models import DatasetVersion

    bundle, config, row = evidence()
    owner = uuid4()
    run = SimpleNamespace(
        id=uuid4(),
        config=config.model_dump(mode="json"),
        dataset_version_id=UUID(config.dataset.id),
        research_image_digest=IMAGES["engine"],
        portable_policy_image_digest=IMAGES["policy"],
    )
    row.run_id, row.attempt_id, row.status, row.sandbox_stopped, row.finished_at = (
        run.id,
        uuid4(),
        "succeeded",
        True,
        datetime.now(UTC),
    )
    row.image_digest = IMAGES["engine"]
    content = canonical_json_bytes(bundle)
    (tmp_path / "portable.json").write_bytes(content)
    artifact = SimpleNamespace(
        id=uuid4(),
        run_id=run.id,
        attempt_id=row.attempt_id,
        name=f"evaluation-{row.id}-portable-result.json",
        media_type="application/json",
        storage_key="portable.json",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    dataset = SimpleNamespace(
        owner_id=owner, content_sha256=config.dataset.sha256, is_synthetic=True
    )
    row.payload = {
        "status": "succeeded",
        "exit_code": 0,
        "image_digest": IMAGES["engine"],
        "policy_image_digest": IMAGES["policy"],
        "result_trust": bundle["result_trust"],
        "ledger_origin": "trusted_engine",
        "runtime_certified": False,
        "metrics": bundle["summary"],
        "combined_score": portable_score(bundle),
        "portable_result": {
            "artifact_id": str(artifact.id),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        },
    }
    session = SimpleNamespace(
        scalar=lambda query: row,
        get=lambda model, key: dataset if model is DatasetVersion else artifact,
    )
    monkeypatch.setattr(views, "owned_portable_run", lambda *args: run)
    if mutation == "file":
        (tmp_path / "portable.json").write_bytes(b"{}")
    if mutation == "policy_image":
        row.payload["policy_image_digest"] = "sha256:" + "c" * 64
    if mutation == "trust":
        row.payload["result_trust"] = "self_reported"
    if mutation == "exit_code":
        row.payload["exit_code"] = 1
    if mutation == "owner_dataset":
        dataset.owner_id = uuid4()
    if mutation == "artifact_attempt":
        artifact.attempt_id = uuid4()
    settings = SimpleNamespace(storage_root=tmp_path, max_artifact_bytes=8 * 1_048_576)
    if mutation is None:
        result = views.load_owned_portable_result(session, row.id, owner, settings)
        assert result.bundle == bundle and result.is_synthetic is True
    else:
        with pytest.raises(DomainError, match="Portable"):
            views.load_owned_portable_result(session, row.id, owner, settings)


def test_pins_are_dual_actual_ids_and_once_set(monkeypatch):
    from backend.app import portable_services as services

    _, config, _ = evidence()
    run = SimpleNamespace(
        config=config.model_dump(mode="json"),
        research_image_digest=None,
        portable_policy_image_digest=None,
    )
    monkeypatch.setattr(services, "fenced_attempt", lambda *a: (run, None))
    assert services.pin_portable_images(None, uuid4(), 1, uuid4(), IMAGES) is run
    assert services.pin_portable_images(None, uuid4(), 1, uuid4(), dict(IMAGES)) is run
    with pytest.raises(DomainError, match="change"):
        services.pin_portable_images(
            None, uuid4(), 1, uuid4(), IMAGES | {"policy": "sha256:" + "c" * 64}
        )
    with pytest.raises(DomainError, match="actual"):
        services.pin_portable_images(
            None, uuid4(), 1, uuid4(), {"engine": "tag", "policy": "tag"}
        )


def test_research_registration_rejects_holdout_before_record_or_create(monkeypatch):
    from backend.app import portable_services as services

    _, config, row = evidence()
    run = SimpleNamespace(
        config=config.model_dump(mode="json"),
        status="running",
        research_image_digest=IMAGES["engine"],
        portable_policy_image_digest=IMAGES["policy"],
    )
    monkeypatch.setattr(
        services, "fenced_attempt", lambda *a: (run, SimpleNamespace(id=uuid4()))
    )
    with pytest.raises(DomainError, match="train/validation"):
        services.begin_portable_evaluation(
            None,
            uuid4(),
            1,
            uuid4(),
            candidate_key="candidate-0000",
            code_sha256=row.code_sha256,
            window=row.window | {"split": "holdout"},
        )


def test_publisher_preserves_actual_runner_stderr_keys_bounded_and_redacted(tmp_path):
    _bundle, config, row = evidence()
    diagnostic = uuid4().hex
    logs = {
        "engine_stderr": "engine failed\npassword=" + diagnostic + "\n" + "x" * 70000,
        "policy_stderr": "policy failed\nBearer " + diagnostic,
    }
    records, verified = publish_portable_artifacts(
        storage_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        output_root=tmp_path / "absent",
        result={"status": "failed", "logs": logs},
        config=config,
        code_sha256=row.code_sha256,
        window=row.window,
        images=IMAGES,
        max_bytes=8 * 1_048_576,
    )
    assert verified is None and {item["name"] for item in records} == {
        "engine.log",
        "policy.log",
    }
    for item in records:
        content = (tmp_path / item["storage_key"]).read_bytes()
        assert content and len(content) <= 65536
        assert diagnostic.encode() not in content and b"[REDACTED]" in content
        assert hashlib.sha256(content).hexdigest() == item["sha256"]
