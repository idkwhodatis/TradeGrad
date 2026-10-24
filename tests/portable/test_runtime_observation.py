"""Portable bootstrap RNG and real-pipe transport, not Docker isolation proof.

Only the exact SHA-pinned maintained SDK reference is executed in the subprocess
check. All adversarial payloads are inert framed data, never host candidate code.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import random
import struct
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.strategy_sdk import (
    AccountView,
    BarView,
    ListingView,
    StateSnapshot,
    build_observation,
    canonical_json,
    hash_json,
    parse_manifest,
)
from packages.strategy_sdk.base import utc_timestamp
from packages.tradegrad_adapter.runtime_observation import capture_rng_state
from runtimes.evaluator.portable_runner import validate_inputs
from runtimes.evaluator.runner import IsolationUnavailable
from runtimes.portable_policy.protocol import (
    MAX_STARTUP_BYTES,
    decision_envelope,
    engine_audit_envelope,
    frame,
    observation_envelope,
    policy_startup_envelope,
    read_frame,
    result_envelope,
    validate_engine_audit,
    validate_policy_startup,
    write_frame,
)
from tests.portable import test_runner as fixture
from tests.portable.test_isolation import stage

ROOT = Path(__file__).resolve().parents[2]
REFERENCE_SHA256 = "3823b73eac5ab7205140da097526d67ced4c3f069c7df105b20684d9da9424f4"


def test_replay_staging_requires_identical_bounded_policy_artifact(tmp_path):
    engine, policy, request, _ = fixture.staged(tmp_path)
    request["replay_rng_state"] = capture_rng_state()
    (engine / "request.json").write_bytes(canonical_json(request))
    with pytest.raises(ValueError, match="RNG artifact"):
        validate_inputs(engine, policy)
    path = policy / "replay-rng-state.json"
    path.write_bytes(canonical_json(request["replay_rng_state"]))
    assert validate_inputs(engine, policy)[2] == request
    changed = copy.deepcopy(request["replay_rng_state"])
    changed["python"]["state"][0] ^= 1
    path.write_bytes(canonical_json(changed))
    with pytest.raises(ValueError, match="state mismatch"):
        validate_inputs(engine, policy)
    path.write_bytes(b" " * (MAX_STARTUP_BYTES + 1))
    with pytest.raises(ValueError, match="oversized"):
        validate_inputs(engine, policy)
    del request["replay_rng_state"]
    (engine / "request.json").write_bytes(canonical_json(request))
    with pytest.raises(ValueError, match="only code"):
        validate_inputs(engine, policy)


@pytest.mark.parametrize(
    "fault", ["identity", "kind", "extra", "numpy", "digest", "seam"]
)
def test_startup_schema_is_strict_and_python_only(fault):
    value = policy_startup_envelope(fixture.EVALUATION, capture_rng_state())
    if fault == "identity":
        value["evaluation_id"] = fixture.RUN
    elif fault == "kind":
        value["kind"] = "decision"
    elif fault == "extra":
        value["trusted"] = True
    elif fault == "numpy":
        value["rng_state"]["numpy_legacy"] = {}
    elif fault == "digest":
        value["restored_from_sha256"] = "not-a-digest"
    else:
        value["rng_state"]["seam"] = "after_candidate_import"
    with pytest.raises(ValueError):
        validate_policy_startup(value, fixture.EVALUATION)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "duplicate",
        "late",
        "engine_origin",
        "restore_mismatch",
        "restore_digest",
        "oversize",
        "missing_audit",
        "duplicate_audit",
        "policy_audit",
    ],
)
def test_startup_is_single_bound_policy_only_before_economic_frames(
    tmp_path, monkeypatch, fault
):
    obj = fixture.runner(tmp_path, timeout=2, decision_timeout=0.5)
    monkeypatch.setattr(obj, "_attachment_exitcode", lambda process: process.returncode)
    monkeypatch.setitem(
        sys.modules,
        "packages.tradegrad_adapter.portable_results",
        SimpleNamespace(validate_portable_result=lambda v: v),
    )
    obs, manifest = fixture.observation(), fixture.make_manifest()
    rng = capture_rng_state()
    request = {
        "initial_state": fixture.STATE.model_dump(mode="json"),
        "input_identity": {
            "run_config_sha256": "f" * 64,
            "market_data_sha256": "a" * 64,
        },
        "window": {"start": "2025-01-03", "end": "2025-01-10"},
        "initial_cash": 1000.0,
        "replay_rng_state": rng,
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
    startup = policy_startup_envelope(
        fixture.EVALUATION, rng, restored_from_sha256=hash_json(rng)
    )
    if fault == "restore_mismatch":
        startup = copy.deepcopy(startup)
        startup["rng_state"]["python"]["state"][0] ^= 1
    elif fault == "restore_digest":
        startup["restored_from_sha256"] = None
    channels = {role: fixture.PipeProcess() for role in ("engine", "policy")}
    errors = []

    def engine():
        try:
            if fault == "engine_origin":
                write_frame(channels["engine"].writer, startup)
            write_frame(
                channels["engine"].writer,
                observation_envelope(fixture.EVALUATION, 0, obs),
            )
            read_frame(channels["engine"].reader)
            if fault != "missing_audit":
                write_frame(
                    channels["engine"].writer,
                    engine_audit_envelope(fixture.EVALUATION, 1, fixture.empty_audit()),
                )
            if fault == "duplicate_audit":
                write_frame(
                    channels["engine"].writer,
                    engine_audit_envelope(fixture.EVALUATION, 1, fixture.empty_audit()),
                )
            write_frame(
                channels["engine"].writer,
                result_envelope(fixture.EVALUATION, 1, bundle),
            )
            channels["engine"].finish()
        except (OSError, ValueError, EOFError) as exc:
            errors.append(type(exc).__name__)

    def policy():
        try:
            if fault == "oversize":
                channels["policy"].writer.write(
                    struct.pack("!I", MAX_STARTUP_BYTES + 1)
                )
                return
            if fault == "missing":
                return
            write_frame(channels["policy"].writer, startup)
            if fault == "duplicate":
                write_frame(channels["policy"].writer, startup)
            incoming = read_frame(channels["policy"].reader)
            if fault == "policy_audit":
                write_frame(
                    channels["policy"].writer,
                    engine_audit_envelope(fixture.EVALUATION, 0, fixture.empty_audit()),
                )
            if fault == "late":
                write_frame(channels["policy"].writer, startup)
            write_frame(
                channels["policy"].writer,
                decision_envelope(incoming, fixture.reply(obs)),
            )
            channels["policy"].reader.read(1)
            channels["policy"].finish()
        except (OSError, ValueError, EOFError) as exc:
            errors.append(type(exc).__name__)

    threads = [
        threading.Thread(target=target, daemon=True) for target in (engine, policy)
    ]
    for thread in threads:
        thread.start()
    received = {}
    try:
        args = (
            channels,
            fixture.EVALUATION,
            manifest,
            request,
            {"engine_stderr": bytearray(), "policy_stderr": bytearray()},
            time.monotonic() + 2,
            threading.Event(),
            [None],
            None,
            received,
        )
        if fault:
            with pytest.raises((ValueError, EOFError, IsolationUnavailable)):
                obj._relay(*args)
        else:
            assert obj._relay(*args) == bundle
            assert received == startup
            assert not errors
    finally:
        for channel in channels.values():
            channel.close()
        for thread in threads:
            thread.join(1)


def test_exact_maintained_policy_bootstrap_capture_restore_and_decision(
    tmp_path, monkeypatch
):
    source = ROOT / "packages/strategy_sdk/templates/event_trend.py"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == REFERENCE_SHA256
    monkeypatch.setenv("PORTABLE_ENVIRONMENT_DIGEST", "d" * 64)
    engine, policy, request = stage(tmp_path)
    manifest = parse_manifest(canonical_json(request["manifest"]))
    market = json.loads((engine / "market.json").read_bytes())
    session = market["sessions"][0]
    observation = build_observation(
        manifest=manifest,
        decision_time=utc_timestamp(session["close_time"]),
        next_open_time=utc_timestamp(session["next_open_time"]),
        session_date=date.fromisoformat(session["session_date"]),
        listings=[ListingView.model_validate(row) for row in market["listings"]],
        history=[
            BarView.model_validate(row)
            for row in market["bars"]
            if row["session_date"] == session["session_date"]
        ],
        account=AccountView(
            as_of=utc_timestamp(session["close_time"]),
            currency="USD",
            cash=100000.0,
            equity=100000.0,
        ),
        state=StateSnapshot.model_validate(request["initial_state"]),
        warmup=True,
    )
    script = """
import hashlib, random, sys
from pathlib import Path
from runtimes.portable_policy.entrypoint import main
source=Path(sys.argv[1])/'policy.py'
assert hashlib.sha256(source.read_bytes()).hexdigest()==sys.argv[3]
random.seed(int(sys.argv[2]))
main(Path(sys.argv[1]))
assert 'numpy' not in sys.modules
"""

    def invoke(seed):
        result = subprocess.run(
            [sys.executable, "-c", script, str(policy), str(seed), REFERENCE_SHA256],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "TRADEGRAD_EVALUATION_ID": fixture.EVALUATION,
            },
            input=frame(observation_envelope(fixture.EVALUATION, 0, observation)),
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode()
        wire = io.BytesIO(result.stdout)
        startup, decision = read_frame(wire), read_frame(wire)
        assert wire.read() == b""
        validate_policy_startup(startup, fixture.EVALUATION)
        return startup, decision

    original, first_decision = invoke(13)
    assert original["restored_from_sha256"] is None
    assert original["rng_state"]["python"]["state"] == list(
        random.Random(13).getstate()[1]
    )
    (policy / "replay-rng-state.json").write_bytes(
        canonical_json(original["rng_state"])
    )
    replayed, second_decision = invoke(999)
    assert replayed["rng_state"] == original["rng_state"]
    assert replayed["restored_from_sha256"] == hash_json(original["rng_state"])
    assert first_decision == second_decision


def test_engine_audit_wire_preserves_int64_ns_and_rejects_tamper():
    from packages.tradegrad_adapter.runtime_observation import NativeAuditObserver

    observer = NativeAuditObserver()
    observer(
        {
            "run_id": "inert-stream",
            "seq": 1,
            "ts": 1_740_000_000_000_000_123,
            "event_type": "started",
            "symbol": None,
            "level": "info",
            "payload": {},
        }
    )
    audit = observer.finish()
    value = engine_audit_envelope(fixture.EVALUATION, 0, audit)
    decoded = read_frame(io.BytesIO(frame(value)))
    assert validate_engine_audit(decoded, fixture.EVALUATION, 0) == audit
    assert audit["events"][0]["ts"] == 1_740_000_000_000_000_123
    for field, change in (
        ("sha256", "0" * 64),
        ("sequence", True),
        ("evaluation_id", fixture.RUN),
        ("chunks", ["!"]),
        ("chunks", ["A" * 50000]),
    ):
        bad = {**decoded, field: change}
        with pytest.raises(ValueError):
            validate_engine_audit(bad, fixture.EVALUATION, 0)


ACTUAL_ENGINE_SCRIPT = r"""
import json, sys
from packages.tradegrad_adapter.runtime_observation import NativeAuditObserver
from runtimes.portable_engine.runtime import execute
from packages.strategy_sdk import CancelIntent, Target
from tests.portable import engine_fixtures as f
scenario=sys.argv[2]
request,data=f.fixture(kind='event' if scenario in ('cancel','failure') else 'portfolio',listing_count=2)
if scenario=='partial':
    for bar in data['bars']: bar['volume']=20.0
if scenario=='reject':
    for bar in data['bars']:
        if bar['session_date']>data['sessions'][0]['session_date']:
            bar['open'],bar['high']=2000.0,2001.0
inputs=f.changed_input(request,data)
observer=NativeAuditObserver() if sys.argv[1]=='audit' else None
def exchange(obs):
    if scenario=='failure': raise ValueError('trusted test callback failure')
    if scenario=='cancel':
        if obs.session_date==inputs.sessions[0].session_date:
            return f.event_reply(obs,(f.order(obs,order_type='limit',limit_price=1.0),))
        return f.event_reply(obs,tuple(CancelIntent(signal_id='cancel:'+str(obs.session_date),order_id=o.order_id,listing_id=o.listing_id,effective_at=obs.next_open_time,expires_at=None) for o in obs.open_orders))
    return f.portfolio_reply(obs, 'rebalance' if scenario!='zero' and obs.session_date==inputs.sessions[0].session_date else 'hold', (Target(listing_id=f.L1,value=10),))
error=None
try: bundle=execute(inputs,exchange,runtime_observer=observer)
except ValueError as exc: bundle=None; error=type(exc).__name__
ids={} if bundle is None else {r['order_id']:'order:'+str(i) for i,r in enumerate(bundle['orders'])}
if bundle: ids.update({r['fill_id']:'fill:'+str(i) for i,r in enumerate(bundle['fills'])})
def normalized(v):
    if isinstance(v,str): return ids.get(v,v)
    if isinstance(v,list): return [normalized(x) for x in v]
    if isinstance(v,dict): return {k:normalized(x) for k,x in v.items()}
    return v
print('PORTABLE_AUDIT_RESULT:'+json.dumps({'bundle':normalized(bundle),'audit':observer.finish() if observer else None,'error':error},sort_keys=True,allow_nan=False))
"""


@pytest.mark.parametrize(
    "scenario", ["filled", "partial", "reject", "cancel", "zero", "failure"]
)
def test_actual_portable_engine_audit_records_ordered_transitions_without_economic_change(
    scenario,
):
    from packages.tradegrad_adapter.runtime_observation import validate_audit

    interpreter = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    if not interpreter:
        if os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1":
            pytest.fail(
                "Required portable audit test needs selected native interpreter"
            )
        pytest.skip("Actual pinned AKQuant portable audit test is opt-in")
    results = []
    for mode in ("off", "audit"):
        result = subprocess.run(
            [interpreter, "-c", ACTUAL_ENGINE_SCRIPT, mode, scenario],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT),
                "TRADEGRAD_PORTABLE_ENGINE_CONTAINER": "1",
                "TRADEGRAD_DATA_WORKERS": "1",
                "OPENBLAS_NUM_THREADS": "1",
            },
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode()
        record = next(
            line
            for line in result.stdout.decode().splitlines()
            if line.startswith("PORTABLE_AUDIT_RESULT:")
        )
        results.append(json.loads(record.split(":", 1)[1]))
    assert results[0]["bundle"] == results[1]["bundle"]
    assert results[0]["error"] == results[1]["error"]
    audit = validate_audit(results[1]["audit"])
    if scenario == "failure":
        assert results[1]["bundle"] is None and results[1]["error"] == "ValueError"
        assert audit["completed"] is False
        return
    assert audit["completed"] and not audit["loss_reasons"]
    orders = [event for event in audit["events"] if event["event_type"] == "order"]
    expected = {
        "filled": ["New", "Filled"],
        "partial": ["New", "PartiallyFilled", "Filled"],
        "reject": ["New", "Rejected"],
        "cancel": ["New", "Cancelled"],
        "zero": [],
    }[scenario]
    assert [event["payload"]["status"] for event in orders] == expected
    if orders:
        assert len({event["payload"]["order_id"] for event in orders}) == 1
    trades = [event for event in audit["events"] if event["event_type"] == "trade"]
    if scenario in {"filled", "partial"}:
        assert len(trades) == (2 if scenario == "partial" else 1)
        assert all(
            event["payload"]["order_id"] == orders[0]["payload"]["order_id"]
            for event in trades
        )
    else:
        assert trades == []


@pytest.mark.parametrize(
    "output_bytes,tamper_market",
    [(64 * 1024 * 1024, False), (100, False), (64 * 1024 * 1024, True)],
)
def test_runtime_sidecar_is_published_only_after_verified_sibling_teardown(
    tmp_path, monkeypatch, output_bytes, tamper_market
):
    from packages.research_trace.contracts import CaptureConfig
    from packages.research_trace.recorder import Recorder
    from packages.research_trace.transport import CaptureWriter
    from packages.tradegrad_adapter import runtime_observation as runtime

    engine, policy, request, manifest = fixture.staged(tmp_path)
    market = {"sessions": [{"session_date": "2025-01-02"}]}
    raw = canonical_json(market)
    request["input_identity"]["market_data_sha256"] = hashlib.sha256(raw).hexdigest()
    (engine / "market.json").write_bytes(raw)
    (engine / "request.json").write_bytes(canonical_json(request))
    obj = fixture.runner(tmp_path, output_bytes=output_bytes)
    monkeypatch.setattr(obj, "bind_environment", lambda _: obj.images)
    inspections = 0

    def inspect(*args, **kwargs):
        nonlocal inspections
        inspections += 1
        return (
            None if inspections <= 2 else {"State": {"Running": False, "ExitCode": 0}}
        )

    monkeypatch.setattr(obj, "_inspect_role", inspect)
    monkeypatch.setattr(
        obj, "_create_role_command", lambda role, *args: ["create", role]
    )
    monkeypatch.setattr(obj, "_verify_role_configuration", lambda *args: None)
    monkeypatch.setattr(
        obj,
        "_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="a" * 64),
    )
    monkeypatch.setattr(
        obj,
        "_launch",
        lambda *args: SimpleNamespace(
            stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO()
        ),
    )
    stopped = []
    monkeypatch.setattr(obj, "stop", lambda _: stopped.append(True) or True)
    identity = {"strategy_sha256": manifest.code_sha256, **request["input_identity"]}

    def relay(*args):
        receiver, startup, audit = args[8:]
        if output_bytes == 100:
            # Isolate the sidecar/economic combined bound from the separately
            # tested trace receiver's earlier zero-compressed-budget rejection.
            monkeypatch.setattr(receiver, "result", dict)
        else:
            writer = CaptureWriter(CaptureConfig(), receiver.accept)
            writer.finish(
                Recorder(
                    code_sha256=manifest.code_sha256, source="engine_observed"
                ).summary()
            )
        startup.update(policy_startup_envelope(fixture.EVALUATION, capture_rng_state()))
        audit.update(fixture.empty_audit())
        if tamper_market:
            (engine / "market.json").write_bytes(
                b'{"sessions":[{"session_date":"2000-01-01"}]}'
            )
        return {"identity": identity, "summary": {"initial_cash": 1000.0}}

    monkeypatch.setattr(obj, "_relay", relay)
    original = runtime.write_runtime_observation

    def write(root, value):
        assert stopped, (
            "No sidecar may be published while either sibling may remain alive"
        )
        return original(root, value)

    monkeypatch.setattr(runtime, "write_runtime_observation", write)

    def invoke():
        return obj.run(
            fixture.EVALUATION,
            engine,
            policy,
            tmp_path / "output",
            run_id=fixture.RUN,
            attempt_id=fixture.ATTEMPT,
        )

    if output_bytes == 100 or tamper_market:
        with pytest.raises(
            ValueError,
            match="market input changed" if tamper_market else "budget exceeded",
        ):
            invoke()
        assert stopped
        assert not (tmp_path / "output" / "runtime-observation.json").exists()
        assert not (tmp_path / "output" / "portable-result.json").exists()
        return
    result = invoke()
    assert result["status"] == "succeeded"
    metadata = result["runtime_observation"]
    path = tmp_path / "output" / metadata["path"]
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == metadata["sha256"]
    assert len(data) == metadata["bytes"] and path.stat().st_mode & 0o777 == 0o600
    value = runtime.validate_runtime_observation(
        json.loads(data), expected_identity=identity
    )
    assert value["initialization"]["window"] == {
        **request["window"],
        "warmup_start": "2025-01-02",
    }
    assert value["audit"]["completed"] is False
    assert value["rng"]["restored_from_sha256"] is None


def test_staging_writer_includes_only_requested_replay_state_and_preserves_default(
    tmp_path,
):
    from packages.tradegrad_adapter.portable_windows import (
        stage_portable_dataset_window,
    )
    from tests.portable.test_staging import SOURCE
    from tests.portable.test_staging import fixture as staging_fixture
    from workers.research.portable_staging import _write_portable_inputs

    config, dataset, blobs, manifest, parameters = staging_fixture()
    staged = stage_portable_dataset_window(config, "train", dataset, blobs.__getitem__)
    rng = capture_rng_state()
    for name, replay in (("ordinary", None), ("diagnostic", rng)):
        result = _write_portable_inputs(
            config=config,
            staged=staged,
            seed_manifest=manifest,
            parameters=parameters,
            code=SOURCE,
            target=tmp_path / name,
            run_config_sha256=config.sha256,
            replay_rng_state=replay,
        )
        actual = validate_inputs(result.engine_root, result.policy_root, config.sha256)[
            2
        ]
        if replay is None:
            assert "replay_rng_state" not in actual
            assert not (result.policy_root / "replay-rng-state.json").exists()
        else:
            assert actual["replay_rng_state"] == rng
            path = result.policy_root / "replay-rng-state.json"
            assert json.loads(path.read_bytes()) == rng
            assert path.stat().st_mode & 0o777 == 0o444
