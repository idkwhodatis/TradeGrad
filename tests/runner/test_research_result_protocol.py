"""Bounded M3 transport with a real unchanged-original native result.

These are offline protocol/supervisor tests, not Docker isolation evidence.
"""

import base64
import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from packages.tradegrad_adapter.results import (
    MAX_WIRE_BYTES,
    RESULT_PREFIX_V2,
    canonical_json_bytes,
    export_native_result,
    make_envelope,
)
from runtimes.evaluator.runner import (
    RESULT_PREFIX,
    DockerEvaluator,
    _without_protocol,
    decode_result_protocol,
)

ROOT = Path(__file__).parents[2]
CONFIG_HASH = "a" * 64


@pytest.fixture(scope="module")
def native_protocol(tmp_path_factory):
    from src.evaluate import run_backtest

    root = tmp_path_factory.mktemp("native-protocol-original-seed")
    seed = ROOT / "seed_strategies/us_cross.py"
    captured = {}

    def capture(result, metrics, kwargs):
        identity = {
            "strategy_sha256": hashlib.sha256(seed.read_bytes()).hexdigest(),
            "run_config_sha256": CONFIG_HASH,
        }
        bundle = export_native_result(
            result,
            metrics,
            identity=identity,
            window={
                "start": "2024-01-02",
                "end": "2024-04-27",
                "interval": "half_open",
            },
            engine_kwargs=kwargs,
            currency="USD",
        )
        captured.update(bundle=bundle, metrics=metrics, identity=identity)

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TRADEGRAD_DATA_WORKERS", "1")
        patch.setenv("AKQUANT_DATA_CACHE", str(root / "cache"))
        run_backtest(
            str(seed),
            str(root / "result"),
            str(ROOT / "tests/baseline/fixtures/US_list"),
            str(ROOT / "tests/baseline/fixtures/US"),
            "2024-01-02",
            "2024-04-26",
            "US",
            result_callback=capture,
        )
    captured["data"] = make_envelope(captured["metrics"], captured["bundle"])
    assert 1_048_576 < len(captured["data"]) < MAX_WIRE_BYTES
    captured["wire"] = RESULT_PREFIX_V2.encode() + base64.b64encode(captured["data"])
    captured["v1"] = RESULT_PREFIX.encode() + base64.b64encode(
        canonical_json_bytes(captured["metrics"])
    )
    return captured


def test_real_large_bundle_persists_exact_bounded_artifacts(native_protocol, tmp_path):
    metrics, bundle = decode_result_protocol(
        native_protocol["wire"],
        tmp_path,
        expected_config_sha256=CONFIG_HASH,
        expected_identity=native_protocol["identity"],
    )
    assert metrics == native_protocol["metrics"]
    assert bundle == native_protocol["bundle"]
    assert (tmp_path / "result-bundle.json").read_bytes() == canonical_json_bytes(
        bundle
    )
    assert (tmp_path / "metrics.json").read_bytes() == canonical_json_bytes(metrics)
    assert {p.name for p in tmp_path.iterdir()} == {
        "metrics.json",
        "result-bundle.json",
    }


@pytest.mark.parametrize("wire", ["oversize_encoded", "oversize_decoded", "bad_base64"])
def test_oversize_and_invalid_wire_fail_before_persist(tmp_path, wire):
    if wire == "oversize_encoded":
        data = RESULT_PREFIX_V2.encode() + b"A" * (4 * ((MAX_WIRE_BYTES + 2) // 3) + 4)
    elif wire == "oversize_decoded":
        data = RESULT_PREFIX_V2.encode() + base64.b64encode(b" " * (MAX_WIRE_BYTES + 1))
    else:
        data = RESULT_PREFIX_V2.encode() + b"not/base64???"
    with pytest.raises(ValueError):
        decode_result_protocol(data, tmp_path, expected_config_sha256=CONFIG_HASH)
    assert not list(tmp_path.iterdir())


def test_v1_is_refused_for_configured_research(native_protocol, tmp_path):
    with pytest.raises(ValueError, match="complete V2"):
        decode_result_protocol(
            native_protocol["v1"], tmp_path, expected_config_sha256=CONFIG_HASH
        )
    assert not list(tmp_path.iterdir())


def test_v1_old_inputs_remain_supported(native_protocol, tmp_path):
    metrics, bundle = decode_result_protocol(native_protocol["v1"], tmp_path)
    assert metrics == native_protocol["metrics"]
    assert bundle is None
    assert not (tmp_path / "result-bundle.json").exists()


@pytest.mark.parametrize("other", ["v1", "wire"])
def test_duplicate_or_mixed_protocol_records_are_rejected(
    native_protocol, tmp_path, other
):
    with pytest.raises(ValueError, match="duplicated"):
        decode_result_protocol(
            native_protocol["wire"] + b"\n" + native_protocol[other], tmp_path
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "mutation", ["bundle", "hash", "unknown_output_path", "config", "full_identity"]
)
def test_tampered_or_misbound_native_result_is_rejected(
    native_protocol, tmp_path, mutation
):
    value = json.loads(native_protocol["data"])
    expected_hash, expected_identity = CONFIG_HASH, native_protocol["identity"]
    if mutation == "bundle":
        value["bundle"]["orders"]["rows"][0][0] = "tampered-order"
    elif mutation == "hash":
        value["bundle_sha256"] = "0" * 64
    elif mutation == "unknown_output_path":
        value["output_path"] = "../../input/strategy.py"
    elif mutation == "config":
        expected_hash = "b" * 64
    else:
        expected_identity = copy.deepcopy(expected_identity)
        expected_identity["strategy_sha256"] = "b" * 64
    wire = RESULT_PREFIX_V2.encode() + base64.b64encode(canonical_json_bytes(value))
    with pytest.raises(ValueError):
        decode_result_protocol(
            wire,
            tmp_path,
            expected_config_sha256=expected_hash,
            expected_identity=expected_identity,
        )
    assert not list(tmp_path.iterdir())


def test_wire_is_removed_from_ordinary_logs(native_protocol):
    stdout = b"before\n" + native_protocol["wire"] + b"\nafter\n"
    assert _without_protocol(stdout) == "before\nafter"
    assert _without_protocol(native_protocol["v1"] + b"\nwarning") == "warning"


@pytest.mark.parametrize("target", ["metrics.json", "result-bundle.json", "directory"])
def test_protocol_never_follows_output_symlink_into_inputs(
    native_protocol, tmp_path, target
):
    inputs, output = tmp_path / "input", tmp_path / "output"
    inputs.mkdir()
    sentinel = inputs / "strategy.py"
    sentinel.write_bytes(b"immutable original input")
    if target == "directory":
        output.symlink_to(inputs, target_is_directory=True)
    else:
        output.mkdir()
        (output / target).symlink_to(sentinel)
    with pytest.raises((ValueError, OSError)):
        decode_result_protocol(
            native_protocol["wire"], output, expected_config_sha256=CONFIG_HASH
        )
    assert sentinel.read_bytes() == b"immutable original input"
    assert {p.name for p in inputs.iterdir()} == {"strategy.py"}


@pytest.mark.parametrize("target", ["metrics.json", "result-bundle.json"])
def test_protocol_never_overwrites_existing_artifact(native_protocol, tmp_path, target):
    original = tmp_path / target
    original.write_bytes(b"previous immutable result")
    with pytest.raises((ValueError, OSError)):
        decode_result_protocol(
            native_protocol["wire"], tmp_path, expected_config_sha256=CONFIG_HASH
        )
    assert original.read_bytes() == b"previous immutable result"


def test_bound_m3_image_extends_only_bounded_wire_log_allowance(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1001)
    runner = DockerEvaluator(
        state_dir=tmp_path / "state", docker_binary="/not/a/docker"
    )
    args = (
        "sha256:" + "b" * 64,
        tmp_path / "input",
        tmp_path / "output",
        str(uuid4()),
        str(uuid4()),
    )
    legacy = runner._create_command(*args)
    assert "--log-opt=max-size=2097152" in legacy
    monkeypatch.setattr(runner, "_preflight", lambda: None)
    monkeypatch.setattr(runner, "_image_id", lambda: args[0])
    monkeypatch.setattr(
        runner,
        "_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, CONFIG_HASH + "\n", ""
        ),
    )
    assert runner.bind_environment(CONFIG_HASH) == args[0]
    assert runner.wire_log_allowance == 4 * ((MAX_WIRE_BYTES + 2) // 3) + 128
    configured = runner._create_command(*args)
    max_size = next(
        value for value in configured if value.startswith("--log-opt=max-size=")
    )
    assert (
        max_size == f"--log-opt=max-size={runner.log_bytes + runner.wire_log_allowance}"
    )
    assert runner.log_bytes == 2 * 1024 * 1024
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
    ):
        assert flag in configured


@pytest.mark.parametrize("payload", [b'{"metrics":{}}', b" " * (1_048_576 + 1)])
def test_invalid_v1_metrics_do_not_publish_partial_artifact(tmp_path, payload):
    wire = RESULT_PREFIX.encode() + base64.b64encode(payload)
    with pytest.raises(ValueError):
        decode_result_protocol(wire, tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "malformation",
    [
        "state_null",
        "state_list",
        "state_string",
        "state_missing_running",
        "running_string",
        "running_number",
        "exit_code_string",
        "exit_code_boolean",
        "config_null",
        "labels_null",
        "labels_list",
        "labels_string",
        "labels_boolean",
        "top_level_null",
        "top_level_object",
        "empty_list",
        "multiple_objects",
    ],
)
def test_stop_unknown_inspect_shapes_fail_closed_without_actions(
    tmp_path, monkeypatch, malformation
):
    attempt = str(uuid4())
    valid = {
        "Config": {
            "Labels": {
                "io.tradegrad.managed": "evaluator-v1",
                "io.tradegrad.attempt_id": attempt,
            }
        },
        "State": {"Running": False, "ExitCode": 0, "OOMKilled": False},
    }
    payload = [copy.deepcopy(valid)]
    target = payload[0]
    if malformation == "state_null":
        target["State"] = None
    elif malformation == "state_list":
        target["State"] = []
    elif malformation == "state_string":
        target["State"] = "running"
    elif malformation == "state_missing_running":
        target["State"] = {"ExitCode": 0}
    elif malformation == "running_string":
        target["State"]["Running"] = "false"
    elif malformation == "running_number":
        target["State"]["Running"] = 0
    elif malformation == "exit_code_string":
        target["State"]["ExitCode"] = "0"
    elif malformation == "exit_code_boolean":
        target["State"]["ExitCode"] = False
    elif malformation == "config_null":
        target["Config"] = None
    elif malformation.startswith("labels_"):
        target["Config"]["Labels"] = {
            "labels_null": None,
            "labels_list": ["untrusted"],
            "labels_string": "not-a-mapping",
            "labels_boolean": True,
        }[malformation]
    elif malformation == "top_level_null":
        payload = None
    elif malformation == "top_level_object":
        payload = valid
    elif malformation == "empty_list":
        payload = []
    elif malformation == "multiple_objects":
        payload = [valid, copy.deepcopy(valid)]
    calls = []

    def command(*args, **kwargs):
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    runner = DockerEvaluator(
        state_dir=tmp_path / "state", docker_binary="/not/a/docker"
    )
    monkeypatch.setattr(runner, "_command", command)
    assert runner.stop(attempt) is False
    assert calls == ["inspect"]


@pytest.mark.parametrize("wrong_label", ["", "unversioned", "b" * 64])
def test_bind_environment_mismatch_never_pins_or_expands_allowance(
    tmp_path, monkeypatch, wrong_label
):
    from runtimes.evaluator.runner import IsolationUnavailable

    runner = DockerEvaluator(
        image="candidate:mutable",
        state_dir=tmp_path / "state",
        docker_binary="/not/a/docker",
    )
    image_id = "sha256:" + "d" * 64
    monkeypatch.setattr(runner, "_preflight", lambda: None)
    monkeypatch.setattr(runner, "_image_id", lambda: image_id)
    observed = []

    def command(*args, **kwargs):
        observed.append(args)
        return subprocess.CompletedProcess(args, 0, wrong_label + "\n", "")

    monkeypatch.setattr(runner, "_command", command)
    with pytest.raises(IsolationUnavailable, match="frozen build inputs"):
        runner.bind_environment(CONFIG_HASH)
    assert observed == [
        (
            "image",
            "inspect",
            image_id,
            "--format",
            '{{index .Config.Labels "io.tradegrad.environment_digest"}}',
        )
    ]
    assert runner.image == "candidate:mutable"
    assert runner.wire_log_allowance == 0


def test_bind_environment_pins_inspected_image_not_mutable_tag(tmp_path, monkeypatch):
    runner = DockerEvaluator(
        image="candidate:mutable",
        state_dir=tmp_path / "state",
        docker_binary="/not/a/docker",
    )
    image_id = "sha256:" + "e" * 64
    monkeypatch.setattr(runner, "_preflight", lambda: None)
    monkeypatch.setattr(runner, "_image_id", lambda: image_id)
    observed = []

    def command(*args, **kwargs):
        observed.append(args)
        return subprocess.CompletedProcess(args, 0, CONFIG_HASH + "\n", "")

    monkeypatch.setattr(runner, "_command", command)
    assert runner.bind_environment(CONFIG_HASH) == image_id
    assert runner.image == image_id
    assert observed[0][2] == image_id
    assert "candidate:mutable" not in observed[0]
