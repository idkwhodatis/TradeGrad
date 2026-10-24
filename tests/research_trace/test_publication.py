"""Pure publication and historical configuration contracts, not database claims."""

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.artifact_metadata import _native_request
from backend.app.models import Evaluation, ResearchRun
from packages.research_trace.binding import make_binding
from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.recorder import Recorder
from packages.research_trace.transport import CaptureReceiver, CaptureWriter
from packages.tradegrad_adapter.config import RunConfig, canonical_bytes
from packages.tradegrad_adapter.portable_config import PortableRunConfig
from tests.portable.test_config import config_values
from tests.tradegrad.test_config_windows import _bundle, _config
from workers.research.optimization import stage_research_input
from workers.research.trace_publication import (
    attach_trace_reference,
    prepare_trace_artifacts,
)


@pytest.mark.parametrize("portable", [False, True])
def test_omitted_capture_preserves_historical_bytes_and_explicit_is_immutable(portable):
    config = (
        PortableRunConfig.model_validate(config_values()) if portable else _config()
    )
    before = config.canonical_bytes()
    assert b"research_capture" not in before
    cls = type(config)
    assert cls.model_validate_json(before).canonical_bytes() == before
    raw = config.model_dump(mode="json") | {"research_capture": None}
    assert cls.model_validate(raw).canonical_bytes() == before
    explicit = cls.model_validate(raw | {"research_capture": {"mode": "research"}})
    assert explicit.sha256 != config.sha256
    assert explicit.research_capture.mode == "research"
    assert cls.model_validate_json(explicit.canonical_bytes()) == explicit


def test_native_staging_and_metadata_reconstruct_identical_capture_request(tmp_path):
    config, manifest, files = _bundle(second=False)
    config = RunConfig.model_validate(
        config.model_dump(mode="json") | {"research_capture": {"mode": "research"}}
    )
    source = tmp_path / "source"
    for name, data in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    window = {"split": "train", **config.windows.train.model_dump(mode="json")}
    target = stage_research_input(
        config=config,
        manifest=manifest,
        storage_root=tmp_path,
        dataset_storage_key="source",
        code="class Strategy: pass\n",
        window=window,
        target=tmp_path / "input",
        max_bytes=8 * 1024 * 1024,
    )
    actual = json.loads((target / "request.json").read_bytes())
    expected = _native_request(
        SimpleNamespace(config=config.model_dump(mode="json")),
        SimpleNamespace(window=window),
    )
    assert actual == expected
    assert actual["evaluation_config"]["research_capture"]["mode"] == "research"


def _publication(tmp_path):
    config = _config(
        dataset={"id": str(uuid4()), "sha256": "a" * 64},
        universe={"id": str(uuid4()), "sha256": "c" * 64},
    )
    evaluation = SimpleNamespace(
        id=uuid4(),
        run_id=uuid4(),
        attempt_id=uuid4(),
        code_sha256="d" * 64,
        candidate_id=None,
    )
    run = SimpleNamespace(
        id=evaluation.run_id,
        strategy_version_id=uuid4(),
        research_image_digest="sha256:" + "f" * 64,
        portable_policy_image_digest=None,
    )
    root = tmp_path / "evaluation"
    output = root / "output"
    output.mkdir(parents=True)
    (root / "artifacts").mkdir()
    receiver = CaptureReceiver(output, CaptureConfig(), source="strategy_emitted")
    writer = CaptureWriter(CaptureConfig(), receiver.accept)
    recorder = Recorder(
        code_sha256=evaluation.code_sha256,
        config=CaptureConfig(),
        on_record=writer.append,
    )
    writer.finish(recorder.summary())
    result = {
        "status": "succeeded",
        "bundle": {"identity": {"evaluation_config_sha256": "e" * 64}},
        **receiver.result(),
    }
    records = [{"name": "result-bundle.json", "sha256": "b" * 64}]

    @contextmanager
    def factory():
        class Session:
            def get(self, model, identifier):
                assert identifier == (evaluation.id if model is Evaluation else run.id)
                assert model in {Evaluation, ResearchRun}
                return evaluation if model is Evaluation else run

        yield Session()

    return (
        {
            "factory": factory,
            "settings": SimpleNamespace(storage_root=tmp_path),
            "config": config,
            "evaluation_id": evaluation.id,
            "result": result,
            "records": records,
            "root": root,
        },
        run,
        evaluation,
    )


def test_empty_capture_is_bound_not_fabricated_and_pointer_is_payload_evidence(
    tmp_path,
):
    values, run, evaluation = _publication(tmp_path)
    written = prepare_trace_artifacts(**values)
    assert [item["name"] for item in written] == ["trace-manifest.json"]
    raw = (tmp_path / written[0]["storage_key"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == written[0]["sha256"]
    manifest = json.loads(raw)
    assert manifest["coverage"] == "not_collected"
    assert manifest["decisions_saved"] == 0
    binding = make_binding(
        run=run,
        evaluation=evaluation,
        config=values["config"],
        bundle=values["result"]["bundle"],
        result_sha256="b" * 64,
    )
    assert manifest["binding"] == binding.model_dump(mode="json")
    artifact = SimpleNamespace(id=uuid4(), **written[0])
    outcome = {}
    attach_trace_reference(outcome, artifact, artifact.name, values["result"])
    assert outcome["trace_manifest"]["sha256"] == written[0]["sha256"]
    assert outcome["research_capture"] == values["result"]["research_capture"]
    assert canonical_bytes(outcome)


@pytest.mark.parametrize(
    "change", ["path", "hash", "partial", "config", "result_missing"]
)
def test_capture_cannot_rebind_or_publish_partial_transport(tmp_path, change):
    values, _, _ = _publication(tmp_path)
    if change == "path":
        values["result"]["research_capture_path"] = str(tmp_path / "elsewhere.gz")
    elif change == "hash":
        values["result"]["research_capture_sha256"] = "0" * 64
    elif change == "partial":
        del values["result"]["research_capture_size_bytes"]
    elif change == "config":
        values["result"]["research_capture"]["capture"]["mode"] = "research"
    else:
        values["records"] = []
    with pytest.raises(ValueError):
        prepare_trace_artifacts(**values)


def test_preincrement_absence_is_not_a_manufactured_trace(tmp_path):
    values, _, _ = _publication(tmp_path)
    values["result"] = {"status": "succeeded"}
    assert prepare_trace_artifacts(**values) == []


def test_hosted_gate_requires_capture_engine_and_postgres_checks():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[2] / ".github/workflows/v1-validation.yml"
    ).read_text()
    assert '"feat/strategy-research-v1"' in text
    assert 'TRADEGRAD_NATIVE_TEST_REQUIRED: "1"' in text
    assert 'TRADEGRAD_TRACE_POSTGRES_REQUIRED: "1"' in text
    assert "pytest tests/research_trace -q" in text
    assert "v1-research-trace-contracts-junit.xml" in text
