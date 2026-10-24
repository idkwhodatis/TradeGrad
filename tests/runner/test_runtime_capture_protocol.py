"""Real OS-pipe trace demultiplexing plus opt-in maintained native entrypoint.

No Docker/socket/kernel isolation proof is claimed by these local checks.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.recorder import Recorder
from packages.research_trace.samples import CONTROLLED_SOURCE_SHA256, TIMING_TAPE
from packages.research_trace.transport import (
    MAX_FRAME_BYTES,
    CaptureReceiver,
    CaptureWriter,
    NativeCaptureDemultiplexer,
)
from packages.strategy_sdk import hash_json
from packages.tradegrad_adapter.runtime_observation import capture_rng_state
from runtimes.evaluator.runner import decode_result_protocol
from runtimes.portable_policy.protocol import (
    decision_envelope,
    engine_audit_envelope,
    observation_envelope,
    policy_startup_envelope,
    read_frame,
    result_envelope,
    write_frame,
)

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "portable_pipe_fixtures", ROOT / "tests/portable/test_runner.py"
)
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


@pytest.mark.parametrize("fault", [None, "missing_end", "policy_injects_capture"])
def test_real_pipe_trusted_capture_channel_is_separate_and_required(
    tmp_path, monkeypatch, fault
):
    obj = fixture.runner(tmp_path, timeout=3, decision_timeout=2)
    monkeypatch.setattr(obj, "_attachment_exitcode", lambda process: process.returncode)
    obs, manifest = fixture.observation(), fixture.make_manifest()
    request = {
        "initial_state": fixture.STATE.model_dump(mode="json"),
        "input_identity": {
            "run_config_sha256": "f" * 64,
            "market_data_sha256": "a" * 64,
        },
        "window": {"start": "2025-01-03", "end": "2025-01-10"},
        "initial_cash": 1000.0,
    }
    bundle = {
        "manifest": manifest.model_dump(mode="json"),
        "window": {**request["window"], "interval": "half_open"},
        "summary": {"initial_cash": 1000.0},
        "decision_trace": [{}],
        "identity": {
            "strategy_sha256": manifest.code_sha256,
            "parameters_sha256": manifest.parameters_sha256,
            "manifest_sha256": hash_json(manifest),
            **request["input_identity"],
        },
        "final_state": fixture.STATE.model_dump(mode="json"),
    }
    monkeypatch.setitem(
        sys.modules,
        "packages.tradegrad_adapter.portable_results",
        SimpleNamespace(validate_portable_result=lambda v: v),
    )
    channels = {role: fixture.PipeProcess() for role in ("engine", "policy")}
    receiver = CaptureReceiver(tmp_path, CaptureConfig(), source="engine_observed")
    errors = []

    def engine():
        try:
            writer = CaptureWriter(
                CaptureConfig(),
                lambda value: write_frame(
                    channels["engine"].writer, value, MAX_FRAME_BYTES
                ),
            )
            write_frame(
                channels["engine"].writer,
                observation_envelope(fixture.EVALUATION, 0, obs),
            )
            read_frame(channels["engine"].reader)
            if fault != "missing_end":
                writer.finish(
                    Recorder(code_sha256="a" * 64, source="engine_observed").summary()
                )
            write_frame(
                channels["engine"].writer,
                engine_audit_envelope(fixture.EVALUATION, 1, fixture.empty_audit()),
            )
            write_frame(
                channels["engine"].writer,
                result_envelope(fixture.EVALUATION, 1, bundle),
            )
            channels["engine"].finish()
        except (OSError, EOFError, ValueError) as exc:
            errors.append(type(exc).__name__)

    def policy():
        try:
            write_frame(
                channels["policy"].writer,
                policy_startup_envelope(fixture.EVALUATION, capture_rng_state()),
            )
            incoming = read_frame(channels["policy"].reader)
            if fault == "policy_injects_capture":
                CaptureWriter(
                    CaptureConfig(),
                    lambda value: write_frame(
                        channels["policy"].writer, value, MAX_FRAME_BYTES
                    ),
                )
                return
            write_frame(
                channels["policy"].writer,
                decision_envelope(incoming, fixture.reply(obs)),
            )
            channels["policy"].reader.read(1)
            channels["policy"].finish()
        except (OSError, EOFError, ValueError) as exc:
            errors.append(type(exc).__name__)

    threads = [
        threading.Thread(target=target, daemon=True) for target in (engine, policy)
    ]
    for thread in threads:
        thread.start()
    try:
        args = (
            channels,
            fixture.EVALUATION,
            manifest,
            request,
            {"engine_stderr": bytearray(), "policy_stderr": bytearray()},
            time.monotonic() + 3,
            threading.Event(),
            [None],
            receiver,
        )
        if fault:
            with pytest.raises((ValueError, EOFError)):
                obj._relay(*args)
        else:
            assert obj._relay(*args) == bundle
            assert receiver.result()["research_capture"]["coverage"] == "not_collected"
            assert not errors
    finally:
        for channel in channels.values():
            channel.close()
        for thread in threads:
            thread.join(1)
        if fault:
            receiver.abort()


def test_actual_maintained_native_entrypoint_emits_separate_validated_trace(tmp_path):
    interpreter = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    if not interpreter:
        if os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1":
            pytest.fail(
                "Required actual native entrypoint needs selected engine Python"
            )
        pytest.skip("Actual maintained native entrypoint is opt-in")
    inputs, engine_output, host_output = (
        tmp_path / x for x in ("input", "engine-output", "host-output")
    )
    for path in (inputs, engine_output, host_output):
        path.mkdir()
    (inputs / "data").mkdir()
    source = ROOT / "seed_strategies/research_time.py"
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == CONTROLLED_SOURCE_SHA256["daily_timing"]
    )
    (inputs / "strategy.py").write_bytes(source.read_bytes())
    instrument = TIMING_TAPE.instruments[0]
    with (inputs / "data/INDEX.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for day, close, volume in zip(
            TIMING_TAPE.dates, instrument.closes, instrument.volumes
        ):
            writer.writerow(
                [
                    day + "T16:00:00-05:00",
                    close,
                    close + 0.1,
                    close - 0.1,
                    close,
                    volume,
                ]
            )
    config = CaptureConfig(mode="research")
    request = {
        "task": "us_time",
        "market": "US",
        "profile": "corrected_explicit_config",
        "start_date": "2024-01-02",
        "end_date": "2024-01-13",
        "evaluation_config": {
            "engine_overrides": {
                "initial_cash": 10000.0,
                "commission_rate": 0,
                "stamp_tax_rate": 0,
                "slippage": {"type": "percent", "value": 0},
            },
            "window": {"start": "2024-01-02", "end": "2024-01-13", "role": "train"},
            "warmup": {"sessions": 0, "history_policy": "source_cold_start"},
            "benchmark": None,
            "run_config_sha256": "a" * 64,
            "research_capture": config.model_dump(mode="json"),
        },
    }
    (inputs / "request.json").write_text(json.dumps(request))
    script = "from runtimes.evaluator.entrypoint import main; from pathlib import Path; import sys; main(Path(sys.argv[1]),Path(sys.argv[2]))"
    result = subprocess.run(
        [interpreter, "-c", script, str(inputs), str(engine_output)],
        cwd=ROOT,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "TRADEGRAD_DATA_WORKERS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode()
    receiver = CaptureReceiver(host_output, config, source="strategy_emitted")
    demux = NativeCaptureDemultiplexer(receiver, other_limit=16 * 1024 * 1024)
    for offset in range(0, len(result.stdout), 8192):
        demux.feed(result.stdout[offset : offset + 8192])
    metrics, bundle = decode_result_protocol(
        demux.finish(), host_output, expected_config_sha256="a" * 64
    )
    captured = receiver.result()
    assert metrics["metrics"]["trade_per_day"] > 0 and bundle["orders"]["rows"]
    assert captured["research_capture"]["decisions_saved"] > len(TIMING_TAPE.dates)
    assert captured["research_capture"]["source"] == "strategy_emitted"
    assert (
        bundle["identity"]["strategy_sha256"]
        == CONTROLLED_SOURCE_SHA256["daily_timing"]
    )
