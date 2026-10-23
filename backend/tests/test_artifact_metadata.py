"""Temporary checksum-valid files and fake persistence, never host execution."""

import hashlib
import json
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.app import result_access, storage
from backend.app.models import (
    Candidate,
    DatasetVersion,
    Evaluation,
    ExecutionAttempt,
    ResearchRun,
    StrategyVersion,
    UniverseVersion,
)
from backend.app.services import DomainError
from backend.tests.final_result_fixtures import synthetic_final_result
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
from tests.tradegrad.test_strategy_package import package_input


@pytest.fixture
def metadata_context(tmp_path, monkeypatch):
    result = synthetic_final_result()
    result.run.status = "cancelled"
    result.dataset.start_date = date(2024, 1, 1)
    result.dataset.end_date = date(2024, 12, 31)
    code = b"# inert source: metrics, combined_score and summary are ordinary strings\nraise AssertionError('NEVER EXECUTE SOURCE ON HOST')\n"
    digest = hashlib.sha256(code).hexdigest()
    source = SimpleNamespace(
        id=result.run.strategy_version_id,
        owner_id=result.run.owner_id,
        content_sha256=digest,
        code=code.decode(),
    )
    result.run.config["selected_strategy"]["sha256"] = digest
    result.evaluation.code_sha256 = digest
    universe_ref = result.run.config["source_config"]["universe"]
    universe = SimpleNamespace(
        id=UUID(universe_ref["id"]),
        owner_id=result.run.owner_id,
        content_sha256=universe_ref["sha256"],
    )
    objects = {
        ResearchRun: result.run,
        Evaluation: result.evaluation,
        StrategyVersion: source,
        Candidate: None,
        DatasetVersion: result.dataset,
        UniverseVersion: universe,
    }
    calls = []
    session = SimpleNamespace(get=lambda model, identifier: objects[model])
    monkeypatch.setattr(
        result_access, "record_exposure", lambda session, **values: calls.append(values)
    )

    def write(name, content, *, prefixed=True):
        result.artifact.name = (
            f"evaluation-{result.evaluation.id}-{name}" if prefixed else name
        )
        result.artifact.media_type = (
            "text/x-python" if name == "strategy.py" else "application/json"
        )
        result.artifact.storage_key = "metadata/fixture"
        result.artifact.sha256 = hashlib.sha256(content).hexdigest()
        result.artifact.size_bytes = len(content)
        path = tmp_path / result.artifact.storage_key
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        return content

    return SimpleNamespace(
        result=result,
        source=source,
        code=code,
        calls=calls,
        session=session,
        write=write,
        settings=SimpleNamespace(
            storage_root=tmp_path, max_artifact_bytes=8 * 1_048_576
        ),
    )


def load(h, owner=None):
    return result_access.load_result_aware_artifact(
        h.session, h.result.artifact, owner or h.result.run.owner_id, h.settings
    )


def provenance(h):
    return {
        "schema_version": 1,
        "execution_mode": "final_evaluation",
        "evaluation_mode": "native_akquant",
        "result_trust": "native_strategy_and_engine_share_process",
        "image_digest": h.result.run.research_image_digest,
    }


@pytest.mark.parametrize(
    "name",
    ["provenance.json", "execution-environment.json", "request.json", "strategy.py"],
)
@pytest.mark.parametrize("prefixed", [False, True])
def test_complete_result_under_metadata_or_source_alias_is_never_released(
    metadata_context, name, prefixed
):
    h = metadata_context
    h.write(name, h.result.content, prefixed=prefixed)
    # Reproduce the defence-in-depth audit using the real stored bundle bytes
    # and a valid registered hash, not a mocked file-return function.
    assert (
        storage.read_verified(
            h.settings.storage_root,
            h.result.artifact.storage_key,
            h.result.artifact.sha256,
            h.settings.max_artifact_bytes,
        )
        == h.result.content
    )
    with pytest.raises(DomainError) as error:
        load(h)
    assert error.value.code == "artifact_metadata_invalid"
    assert not h.calls


@pytest.mark.parametrize("prefixed", [False, True])
def test_exact_bounded_provenance_and_source_remain_downloadable(
    metadata_context, prefixed
):
    h = metadata_context
    for name, content in (
        ("provenance.json", json.dumps(provenance(h)).encode()),
        ("strategy.py", h.code),
    ):
        h.write(name, content, prefixed=prefixed)
        assert load(h) == content
    assert not h.calls


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_metrics",
        "nested_result",
        "wrong_mode",
        "wrong_image",
        "duplicate_key",
        "nonfinite",
        "oversized",
    ],
)
def test_reserved_provenance_requires_exact_shape_not_just_no_top_level_metrics(
    metadata_context, mutation
):
    h = metadata_context
    value = provenance(h)
    if mutation == "extra_metrics":
        value["metrics"] = {"return": 7}
    elif mutation == "nested_result":
        value["extra"] = {"equity": {"rows": [[1, 7]]}}
    elif mutation == "wrong_mode":
        value["execution_mode"] = "optimization"
    elif mutation == "wrong_image":
        value["image_digest"] = "sha256:" + "0" * 64
    elif mutation == "nonfinite":
        value["schema_version"] = float("nan")
    elif mutation == "oversized":
        value["extra"] = "x" * 256_001
    content = json.dumps(value).encode()
    if mutation == "duplicate_key":
        content = b'{"metrics":{"return":7},' + content[1:]
        content = content[:-1] + b',"metrics":null}'
    h.write("provenance.json", content)
    with pytest.raises(DomainError) as error:
        load(h)
    assert error.value.code == "artifact_metadata_invalid"
    assert not h.calls


@pytest.mark.parametrize(
    "mutation", ["owner", "id", "source_hash", "source_bytes", "selected_hash"]
)
def test_source_exclusion_requires_exact_owned_immutable_source(
    metadata_context, mutation
):
    h = metadata_context
    h.write("strategy.py", h.code)
    if mutation in {"owner", "id"}:
        setattr(h.source, "owner_id" if mutation == "owner" else "id", uuid4())
    elif mutation == "source_hash":
        h.source.content_sha256 = "0" * 64
    elif mutation == "source_bytes":
        h.source.code += "# changed"
    else:
        h.result.run.config["selected_strategy"]["sha256"] = "0" * 64
    with pytest.raises(DomainError) as error:
        load(h)
    assert error.value.code == "artifact_metadata_invalid"
    assert not h.calls


@pytest.mark.parametrize("mutation", [None, "run_id", "id", "code_sha256"])
def test_optimizer_candidate_source_has_its_own_exact_run_binding(
    metadata_context, mutation
):
    h = metadata_context
    h.result.run.config = h.result.run.config["source_config"]
    h.result.evaluation.candidate_id = uuid4()
    candidate = SimpleNamespace(
        id=h.result.evaluation.candidate_id,
        run_id=h.result.run.id,
        code_sha256=h.result.evaluation.code_sha256,
        code=h.code.decode(),
    )
    original_get = h.session.get
    h.session.get = lambda model, identifier: (
        candidate if model is Candidate else original_get(model, identifier)
    )
    content = h.write("strategy.py", h.code)
    if mutation:
        setattr(candidate, mutation, "0" * 64 if mutation == "code_sha256" else uuid4())
        with pytest.raises(DomainError) as error:
            load(h)
        assert error.value.code == "artifact_metadata_invalid"
    else:
        assert load(h) == content
    assert not h.calls


def test_execution_environment_uses_existing_exact_shape_and_digest_validator(
    metadata_context,
):
    h = metadata_context
    environment = package_input.__wrapped__().environment_evidence
    h.result.run.config["environment_digest"] = environment["environment_digest"]
    h.result.run.research_image_digest = environment["image_id"]
    content = h.write(
        "execution-environment.json", json.dumps(environment).encode(), prefixed=False
    )
    assert load(h) == content
    environment["metrics"] = {"return": 7}
    h.write(
        "execution-environment.json", json.dumps(environment).encode(), prefixed=False
    )
    with pytest.raises(DomainError) as error:
        load(h)
    assert error.value.code == "artifact_metadata_invalid"
    assert not h.calls


def test_native_request_is_bound_to_exact_reviewed_input_and_rejects_appended_results(
    metadata_context,
):
    h = metadata_context
    config = FinalEvaluationConfig.model_validate(h.result.run.config)
    source = config.source_config
    value = {
        "task": source.task,
        "market": source.market_profile.market,
        "profile": source.profile,
        "start_date": str(config.holdout.start),
        "end_date": str(config.holdout.end),
        "evaluation_config": {
            "engine_overrides": source.market_profile.engine_kwargs(),
            "window": {
                "start": str(config.holdout.start),
                "end": str(config.holdout.end),
                "role": "holdout",
            },
            "warmup": {
                "sessions": source.warmup.sessions,
                "history_policy": source.warmup.history_policy,
            },
            "benchmark": None,
            "run_config_sha256": config.sha256,
        },
    }
    content = h.write("request.json", json.dumps(value).encode())
    assert load(h) == content
    value["evaluation_config"]["metrics"] = {"return": 7}
    h.write("request.json", json.dumps(value).encode())
    with pytest.raises(DomainError) as error:
        load(h)
    assert error.value.code == "artifact_metadata_invalid"
    assert not h.calls


def test_foreign_owner_never_opens_metadata_file(metadata_context, monkeypatch):
    h = metadata_context
    h.write("provenance.json", json.dumps(provenance(h)).encode())
    monkeypatch.setattr(
        storage, "read_verified", lambda *args: pytest.fail("Foreign metadata read")
    )
    with pytest.raises(DomainError) as error:
        load(h, uuid4())
    assert error.value.code == "not_found" and not h.calls


@pytest.mark.parametrize("name", ["engine.log", "policy.log"])
def test_actual_portable_diagnostic_names_remain_conservative_downloads(
    metadata_context, name
):
    h = metadata_context
    content = h.write(name, b"synthetic diagnostic text")
    assert load(h) == content
    assert len(h.calls) == 1
    assert "diagnostic" in h.calls[0]["provenance"]["coverage"]


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "missing_attempt",
        "wrong_attempt",
        "wrong_run",
        "old_generation",
        "different_active",
        "running",
        "failed_attempt",
        "nonzero_exit",
        "not_stopped",
        "unfinished",
        "no_result",
        "wrong_mode",
        "wrong_image",
        "invalid_image",
        "spoofed_research_image",
    ],
)
def test_legacy_provenance_uses_exact_completed_seed_attempt_not_research_pin(
    metadata_context, mutation
):
    h = metadata_context
    run = h.result.run
    digest = "sha256:" + "a" * 64
    run.config = {"task": "cn_time", "market": "CN", "profile": "source_compatible"}
    run.status, run.generation = "succeeded", 2
    run.active_attempt_id = h.result.artifact.attempt_id
    run.research_image_digest = None
    run.result = {
        "execution_mode": "seed_evaluation",
        "evaluation_mode": "native_akquant",
        "result_trust": "native_strategy_and_engine_share_process",
        "image_digest": digest,
    }
    attempt = SimpleNamespace(
        id=run.active_attempt_id,
        run_id=run.id,
        generation=2,
        status="succeeded",
        exit_code=0,
        sandbox_stopped=True,
        finished_at="2026-10-23T00:00:00Z",
    )
    original_get = h.session.get
    h.session.get = lambda model, identifier: (
        (None if mutation == "missing_attempt" else attempt)
        if model is ExecutionAttempt
        else original_get(model, identifier)
    )
    value = {
        "schema_version": 1,
        "execution_mode": "seed_evaluation",
        "evaluation_mode": "native_akquant",
        "result_trust": "native_strategy_and_engine_share_process",
        "image_digest": digest,
    }
    if mutation == "wrong_attempt":
        attempt.id = uuid4()
    elif mutation == "wrong_run":
        attempt.run_id = uuid4()
    elif mutation == "old_generation":
        attempt.generation = 1
    elif mutation == "different_active":
        run.active_attempt_id = uuid4()
    elif mutation == "running":
        run.status = "running"
    elif mutation == "failed_attempt":
        attempt.status = "failed"
    elif mutation == "nonzero_exit":
        attempt.exit_code = 1
    elif mutation == "not_stopped":
        attempt.sandbox_stopped = False
    elif mutation == "unfinished":
        attempt.finished_at = None
    elif mutation == "no_result":
        run.result = None
    elif mutation == "wrong_mode":
        run.result["execution_mode"] = "optimization"
    elif mutation == "wrong_image":
        value["image_digest"] = "sha256:" + "b" * 64
    elif mutation == "invalid_image":
        value["image_digest"] = run.result["image_digest"] = "not-an-image"
    elif mutation == "spoofed_research_image":
        value["image_digest"] = run.research_image_digest = "sha256:" + "c" * 64
    content = h.write("provenance.json", json.dumps(value).encode(), prefixed=False)
    if mutation is None:
        assert load(h) == content
    else:
        with pytest.raises(DomainError) as error:
            load(h)
        assert error.value.code == "artifact_metadata_invalid"
    assert not h.calls
