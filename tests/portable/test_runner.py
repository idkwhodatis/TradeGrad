"""Offline transport/lifecycle tests using inert records and fake Docker only.

No policy source, bootstrap, AKQuant backtest, Docker socket or network is run.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import struct
import sys
import threading
import time
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from packages.strategy_sdk import (
    AccountView,
    BarView,
    DataRequirements,
    EngineProfile,
    ListingView,
    PortfolioReply,
    PriceAssumptions,
    StateCodec,
    StateSchema,
    StrategyManifest,
    TargetPortfolio,
    UniverseAssumptions,
    build_observation,
    canonical_json,
    hash_json,
)
from packages.tradegrad_adapter.runtime_observation import (
    NativeAuditObserver,
    capture_rng_state,
)
from runtimes.evaluator.portable_runner import (
    PortableDockerEvaluator,
    safe_policy_manifest,
    validate_inputs,
)
from runtimes.evaluator.runner import IsolationUnavailable
from runtimes.portable_policy.protocol import (
    MAX_REPLY_BYTES,
    decision_envelope,
    decode,
    engine_audit_envelope,
    frame,
    observation_envelope,
    policy_startup_envelope,
    read_frame,
    result_envelope,
    validate_request,
    validate_response,
    write_frame,
)

EVALUATION = str(UUID(int=701))
ATTEMPT = str(UUID(int=702))
RUN = str(UUID(int=703))
LISTING = str(UUID(int=704))
DIGEST = "d" * 64
STATE = StateCodec(StateSchema(name="test", version=1, fields=())).encode({})


def make_manifest():
    return StrategyManifest(
        strategy_kind="portfolio",
        strategy_id=str(UUID(int=705)),
        code_sha256=hashlib.sha256(
            b"raise AssertionError('NEVER RUN ON HOST')\n"
        ).hexdigest(),
        parameters_sha256=hash_json({}),
        market="US",
        currency="USD",
        calendar="XNYS",
        timezone="America/New_York",
        data=DataRequirements(lookback_bars=2, warmup_bars=1, max_listings=1),
        universe=UniverseAssumptions(
            semantics="user_fixed",
            snapshot_sha256="a" * 64,
            survivorship_bias="possible",
        ),
        prices=PriceAssumptions(actions_snapshot_sha256="b" * 64),
        state_schema=StateSchema(name="test", version=1, fields=()),
        engine_profile=EngineProfile(
            profile_sha256="c" * 64, environment_sha256=DIGEST
        ),
        reference_result_sha256="e" * 64,
    )


def staged(tmp_path):
    engine, policy = tmp_path / "engine", tmp_path / "policy"
    engine.mkdir()
    policy.mkdir()
    manifest = make_manifest()
    market = b"{}"  # deep calendar/market validation is separately engine-owned
    request = {
        "schema_version": "portable-engine-request.v1",
        "manifest": manifest.model_dump(mode="json"),
        "window": {"start": "2025-01-03", "end": "2025-01-10"},
        "initial_cash": 1000.0,
        "initial_state": STATE.model_dump(mode="json"),
        "input_identity": {
            "run_config_sha256": "f" * 64,
            "market_data_sha256": hashlib.sha256(market).hexdigest(),
        },
    }
    (engine / "request.json").write_bytes(canonical_json(request))
    (engine / "market.json").write_bytes(market)
    (policy / "policy.py").write_bytes(b"raise AssertionError('NEVER RUN ON HOST')\n")
    (policy / "parameters.json").write_bytes(b"{}")
    (policy / "manifest.json").write_bytes(
        canonical_json(safe_policy_manifest(manifest))
    )
    return engine, policy, request, manifest


def observation(manifest=None):
    manifest = manifest or make_manifest()
    now = datetime(2025, 1, 3, 21, tzinfo=UTC)
    return build_observation(
        manifest=manifest,
        decision_time=now,
        next_open_time=datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
        session_date=date(2025, 1, 3),
        listings=[
            ListingView(
                listing_id=LISTING,
                instrument_id=LISTING,
                symbol="AAPL",
                mic="XNAS",
                currency="USD",
                calendar="XNYS",
                timezone="America/New_York",
                effective_from=date(2020, 1, 1),
                known_at=datetime(2020, 1, 1, tzinfo=UTC),
                lot_size=1,
            )
        ],
        history=[
            BarView(
                listing_id=LISTING,
                session_date=date(2025, 1, 3),
                completed_at=now,
                available_at=now,
                open=10.0,
                high=12.0,
                low=9.0,
                close=11.0,
                volume=1000.0,
            )
        ],
        account=AccountView(as_of=now, currency="USD", cash=1000.0, equity=1000.0),
        state=STATE,
    )


def reply(obs):
    return PortfolioReply(
        decision=TargetPortfolio(
            signal_id="hold",
            effective_at=obs.next_open_time,
            expires_at=None,
            action="hold",
            basis="target_weight",
            targets=(),
            omitted_positions="preserve",
        ),
        next_state=STATE,
    )


def runner(tmp_path, **kwargs):
    return PortableDockerEvaluator(
        docker_binary="fake-docker-never-executed",
        state_dir=tmp_path / "runner",
        **kwargs,
    )


def empty_audit():
    """Explicit incomplete inert audit, never represented as real engine events."""
    return NativeAuditObserver().finish()


def test_safe_manifest_removes_all_future_fingerprints():
    manifest = make_manifest()
    safe = safe_policy_manifest(manifest)
    assert safe["universe"]["snapshot_sha256"] == "0" * 64
    assert safe["prices"]["actions_snapshot_sha256"] == "0" * 64
    assert safe["engine_profile"]["profile_sha256"] == "0" * 64
    assert safe["engine_profile"]["environment_sha256"] == "0" * 64
    assert safe["reference_result_sha256"] is None
    assert safe["code_sha256"] == manifest.code_sha256
    assert manifest.universe.snapshot_sha256 == "a" * 64


def test_disjoint_mounts_and_exact_hashes(tmp_path):
    engine, policy, request, _ = staged(tmp_path)
    assert validate_inputs(engine, policy, "f" * 64)[2] == request
    (engine / "policy.py").write_text("# forbidden even inert")
    with pytest.raises(ValueError, match="never code"):
        validate_inputs(engine, policy)
    (engine / "policy.py").unlink()
    (policy / "data.json").write_text("{}")
    with pytest.raises(ValueError, match="only code"):
        validate_inputs(engine, policy)


@pytest.mark.parametrize(
    "target", ["market", "code", "params", "unsafe_manifest", "config"]
)
def test_staging_identity_tamper_rejected(tmp_path, target):
    engine, policy, _, manifest = staged(tmp_path)
    if target == "market":
        (engine / "market.json").write_text('{"x":1}')
    if target == "code":
        (policy / "policy.py").write_text("# changed")
    if target == "params":
        (policy / "parameters.json").write_text('{"x":1}')
    if target == "unsafe_manifest":
        (policy / "manifest.json").write_bytes(canonical_json(manifest))
    with pytest.raises(ValueError):
        validate_inputs(engine, policy, "e" * 64 if target == "config" else "f" * 64)


def test_symlinks_and_nonregular_inputs_rejected(tmp_path):
    engine, policy, _, _ = staged(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(engine, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        validate_inputs(link, policy)
    (policy / "policy.py").unlink()
    os.mkfifo(policy / "policy.py")
    with pytest.raises(ValueError, match="regular"):
        validate_inputs(engine, policy)


def test_cli_security_role_labels_and_no_shared_mount(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1234)
    monkeypatch.setattr(os, "getgid", lambda: 1234)
    obj = runner(tmp_path)
    obj.images = {"engine": "sha256:" + "a" * 64, "policy": "sha256:" + "b" * 64}
    for role in ("engine", "policy"):
        command = obj._create_role_command(
            role, tmp_path / role, RUN, ATTEMPT, EVALUATION
        )
        for flag in (
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--interactive",
            "--log-driver=none",
        ):
            assert flag in command
        assert command[-1] == obj.images[role]
        assert command.count("--mount") == 1
        assert (
            command[command.index("--mount") + 1]
            == f"type=bind,src={tmp_path / role},dst=/input,readonly"
        )
        assert not any(
            "docker.sock" in part or "credential" in part.lower() for part in command
        )
        assert f"io.tradegrad.evaluation_id={EVALUATION}" in command
        assert f"io.tradegrad.attempt_id={ATTEMPT}" in command
        assert f"io.tradegrad.role={role}" in command
        assert command[command.index("--user") + 1] == "1234:1234"


def test_root_controller_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(IsolationUnavailable, match="non-root"):
        runner(tmp_path)._create_role_command(
            "engine", tmp_path, RUN, ATTEMPT, EVALUATION
        )


def test_images_pinned_and_labels_verified(tmp_path, monkeypatch):
    obj = runner(tmp_path)
    monkeypatch.setattr(obj, "_preflight", lambda: None)
    ids = {
        obj.images["engine"]: "sha256:" + "1" * 64,
        obj.images["policy"]: "sha256:" + "2" * 64,
    }

    def command(*args, **kwargs):
        if args[-1] == "{{.Id}}":
            return SimpleNamespace(
                returncode=0, stdout=ids.get(args[2], args[2]), stderr=""
            )
        role = "engine" if args[2] == "sha256:" + "1" * 64 else "policy"
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "io.tradegrad.environment_digest": DIGEST,
                    "io.tradegrad.portable_role": role,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(obj, "_command", command)
    assert obj.bind_environment(DIGEST) == {
        "engine": "sha256:" + "1" * 64,
        "policy": "sha256:" + "2" * 64,
    }
    with pytest.raises(IsolationUnavailable, match="frozen"):
        obj.bind_environment("e" * 64)


def test_group_stop_attempts_both_even_unknown_first(tmp_path, monkeypatch):
    obj = runner(tmp_path)
    calls = []

    def inspect(evaluation_id, role, **kwargs):
        calls.append(role)
        if role == "engine":
            raise RuntimeError("unknown daemon outcome")

    monkeypatch.setattr(obj, "_inspect_role", inspect)
    assert obj.stop(EVALUATION) is False
    assert calls == ["engine", "policy"]
    assert (obj.state_dir / f"{EVALUATION}.stopped").exists()


def test_group_stop_fence_failure_still_stops_both(tmp_path, monkeypatch):
    obj = runner(tmp_path)
    calls = []
    monkeypatch.setattr(
        obj, "_fence", lambda _: (_ for _ in ()).throw(OSError("disk full"))
    )
    monkeypatch.setattr(obj, "_stop_role", lambda _, role: calls.append(role) or True)
    assert obj.stop(EVALUATION) is False
    assert calls == ["engine", "policy"]


def test_partial_create_cleanup_and_no_result(tmp_path, monkeypatch):
    engine, policy, _, _ = staged(tmp_path)
    obj = runner(tmp_path)
    monkeypatch.setattr(obj, "bind_environment", lambda _: obj.images)
    monkeypatch.setattr(obj, "_inspect_role", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        obj, "_create_role_command", lambda role, *args: ["create", role]
    )
    stopped = []

    def command(*args, **kwargs):
        if args == ("create", "policy"):
            return SimpleNamespace(returncode=0, stdout="1" * 64)
        raise IsolationUnavailable("engine create timed out; unknown outcome")

    monkeypatch.setattr(obj, "_command", command)
    monkeypatch.setattr(obj, "_stop_role", lambda _, role: stopped.append(role) or True)
    result = obj.run(
        EVALUATION, engine, policy, tmp_path / "output", run_id=RUN, attempt_id=ATTEMPT
    )
    assert result["status"] == "failed"
    assert stopped == ["engine", "policy"]
    assert result["bundle"] is None
    assert not list((tmp_path / "output").iterdir())


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b"[]",
        b"{}junk",
        b"\xff",
        b'{"x":9007199254740992}',
        b'{"x":' + b"[" * 100 + b"0" + b"]" * 100 + b"}",
    ],
)
def test_strict_frame_json_rejects_hostile_values(raw):
    with pytest.raises(ValueError):
        decode(raw)


@pytest.mark.parametrize("size", [0, MAX_REPLY_BYTES + 1, 2**32 - 1])
def test_frame_header_rejected_before_body_read(size):
    with pytest.raises(ValueError):
        read_frame(io.BytesIO(struct.pack("!I", size)), MAX_REPLY_BYTES)


def test_fragmented_frames_and_short_reads():
    class Fragmented(io.BytesIO):
        def read(self, count=-1):
            return super().read(min(count, 1))

    request = observation_envelope(EVALUATION, 0, observation())
    assert read_frame(Fragmented(frame(request))) == request
    with pytest.raises(EOFError):
        read_frame(io.BytesIO(frame(request)[:-1]))
    stream = io.BytesIO()
    write_frame(stream, request)
    assert stream.getvalue() == frame(request)


@pytest.mark.parametrize(
    "key,value",
    [
        ("sequence", 1),
        ("sequence", True),
        ("request_id", "wrong"),
        ("expected_state_sha256", "0" * 64),
        ("evaluation_id", RUN),
        ("kind", "result"),
    ],
)
def test_response_binding_rejects_replay_and_forgery(key, value):
    request = observation_envelope(EVALUATION, 0, observation())
    response = decision_envelope(request, reply(observation()))
    response[key] = value
    with pytest.raises(ValueError):
        validate_response(response, request)


def test_request_state_and_sequence_binding():
    request = observation_envelope(EVALUATION, 0, observation())
    assert validate_request(request, EVALUATION, 0) == request
    bad = copy.deepcopy(request)
    bad["expected_state_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        validate_request(bad, EVALUATION, 0)


class PipeProcess:
    """Inert endpoint backed by real OS pipes, never a candidate subprocess."""

    def __init__(self):
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        error_read, error_write = os.pipe()
        self.stdin = os.fdopen(input_write, "wb", buffering=0)
        self.stdout = os.fdopen(output_read, "rb", buffering=0)
        self.stderr = os.fdopen(error_read, "rb", buffering=0)
        self.reader = os.fdopen(input_read, "rb", buffering=0)
        self.writer = os.fdopen(output_write, "wb", buffering=0)
        self.errors = os.fdopen(error_write, "wb", buffering=0)
        self.returncode = None

    def poll(self):
        return self.returncode

    def finish(self):
        self.writer.close()
        self.errors.close()
        self.returncode = 0

    def close(self):
        for stream in (
            self.stdin,
            self.stdout,
            self.stderr,
            self.reader,
            self.writer,
            self.errors,
        ):
            if not stream.closed:
                stream.close()


def test_real_pipe_relay_normalizes_and_only_accepts_engine_result(
    tmp_path, monkeypatch
):
    obj = runner(tmp_path, timeout=2, decision_timeout=1)
    monkeypatch.setattr(obj, "_attachment_exitcode", lambda process: process.returncode)
    obs = observation()
    manifest = make_manifest()
    request = {
        "initial_state": STATE.model_dump(mode="json"),
        "input_identity": {
            "run_config_sha256": "f" * 64,
            "market_data_sha256": "a" * 64,
        },
    }
    request["window"] = {"start": "2025-01-03", "end": "2025-01-10"}
    request["initial_cash"] = 1000.0
    result = {
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
        "final_state": STATE.model_dump(mode="json"),
    }
    # Fake only the exporter, not framing or relay state machine. Actual schema
    # validation has dedicated tests under test_engine.py.
    monkeypatch.setitem(
        sys.modules,
        "packages.tradegrad_adapter.portable_results",
        SimpleNamespace(validate_portable_result=lambda value: value),
    )
    channels = {role: PipeProcess() for role in ("engine", "policy")}
    captured = []

    def engine():
        write_frame(channels["engine"].writer, observation_envelope(EVALUATION, 0, obs))
        captured.append(read_frame(channels["engine"].reader))
        write_frame(
            channels["engine"].writer,
            engine_audit_envelope(EVALUATION, 1, empty_audit()),
        )
        write_frame(channels["engine"].writer, result_envelope(EVALUATION, 1, result))
        channels["engine"].finish()

    def policy():
        write_frame(
            channels["policy"].writer,
            policy_startup_envelope(EVALUATION, capture_rng_state()),
        )
        incoming = read_frame(channels["policy"].reader)
        write_frame(channels["policy"].writer, decision_envelope(incoming, reply(obs)))
        assert channels["policy"].reader.read(1) == b""
        channels["policy"].finish()

    threads = [
        threading.Thread(target=target, daemon=True) for target in (engine, policy)
    ]
    for thread in threads:
        thread.start()
    try:
        output = obj._relay(
            channels,
            EVALUATION,
            manifest,
            request,
            {"engine_stderr": bytearray(), "policy_stderr": bytearray()},
            time.monotonic() + 2,
            threading.Event(),
            [None],
        )
        assert output == result
        assert captured[0]["reply"] == reply(obs).model_dump(mode="json")
    finally:
        for item in channels.values():
            item.close()
        for thread in threads:
            thread.join(1)


@pytest.mark.parametrize(
    "failure",
    [
        "unsolicited",
        "forged_result",
        "wrong_id",
        "duplicate",
        "oversize",
        "stderr_flood",
        "blocked_read",
        "blocked_write",
    ],
)
def test_relay_hostile_and_stalled_inert_endpoints_fail_bounded(
    tmp_path, monkeypatch, failure
):
    import runtimes.evaluator.portable_runner as module

    obj = runner(tmp_path, timeout=1, decision_timeout=0.15, log_bytes=128)
    monkeypatch.setattr(obj, "_attachment_exitcode", lambda process: process.returncode)
    obs = observation()
    request = {"initial_state": STATE.model_dump(mode="json")}
    monkeypatch.setitem(
        sys.modules,
        "packages.tradegrad_adapter.portable_results",
        SimpleNamespace(validate_portable_result=lambda value: value),
    )
    channels = {role: PipeProcess() for role in ("engine", "policy")}
    inbound = observation_envelope(EVALUATION, 0, obs)
    if failure == "unsolicited":
        write_frame(channels["policy"].writer, {"kind": "result"})
    else:
        write_frame(
            channels["policy"].writer,
            policy_startup_envelope(EVALUATION, capture_rng_state()),
        )
        if failure == "stderr_flood":
            channels["policy"].errors.write(b"x" * 129)
        elif failure != "blocked_read":
            write_frame(channels["engine"].writer, inbound)
    if failure == "blocked_write":
        original = module.frame
        monkeypatch.setattr(
            module,
            "frame",
            lambda value, maximum: original(value, maximum) + b" " * 1_048_576,
        )
    if failure in {"forged_result", "wrong_id", "duplicate", "oversize"}:
        # Precompute bytes but deliver only once the host's request is read.
        def send():
            incoming = read_frame(channels["policy"].reader)
            value = decision_envelope(incoming, reply(obs))
            if failure == "forged_result":
                value = result_envelope(EVALUATION, 0, {"result_trust": "forged"})
            if failure == "wrong_id":
                value["evaluation_id"] = RUN
            if failure == "oversize":
                channels["policy"].writer.write(struct.pack("!I", MAX_REPLY_BYTES + 1))
            else:
                write_frame(channels["policy"].writer, value)
                if failure == "duplicate":
                    write_frame(channels["policy"].writer, value)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
    else:
        thread = None
    started = time.monotonic()
    try:
        with pytest.raises((ValueError, IsolationUnavailable)):
            obj._relay(
                channels,
                EVALUATION,
                make_manifest(),
                request,
                {"engine_stderr": bytearray(), "policy_stderr": bytearray()},
                started + 1,
                threading.Event(),
                [None],
            )
        assert time.monotonic() - started < 0.8
    finally:
        for item in channels.values():
            item.close()
        if thread:
            thread.join(1)


def valid_inspect(obj, role, root):
    return {
        "Image": obj.images[role],
        "Config": {"User": f"{os.getuid()}:{os.getgid()}"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "NetworkMode": "none",
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "PidMode": "",
            "Devices": [],
            "DeviceRequests": None,
            "Memory": obj.memory_mb * 1024 * 1024,
            "MemorySwap": obj.memory_mb * 1024 * 1024,
            "PidsLimit": obj.pids,
            "NanoCpus": int(obj.cpus * 1e9),
        },
        "Mounts": [
            {"Destination": "/input", "Source": str(root), "Type": "bind", "RW": False}
        ],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "image",
        "mount",
        "extra_mount",
        "network",
        "privileged",
        "readonly",
        "pids",
        "user",
    ],
)
def test_created_container_inspect_independently_verifies_boundary(
    tmp_path, monkeypatch, mutation
):
    obj = runner(tmp_path)
    value = valid_inspect(obj, "engine", tmp_path)
    monkeypatch.setattr(obj, "_inspect_role", lambda *args, **kwargs: value)
    obj._verify_role_configuration(EVALUATION, "engine", tmp_path, ATTEMPT)
    if mutation == "image":
        value["Image"] = "sha256:" + "e" * 64
    if mutation == "mount":
        value["Mounts"][0]["Source"] = "/wrong-root"
    if mutation == "extra_mount":
        value["Mounts"].append({"Destination": "/secret", "Type": "bind", "RW": False})
    if mutation == "network":
        value["HostConfig"]["NetworkMode"] = "host"
    if mutation == "privileged":
        value["HostConfig"]["Privileged"] = True
    if mutation == "readonly":
        value["HostConfig"]["ReadonlyRootfs"] = False
    if mutation == "pids":
        value["HostConfig"]["PidsLimit"] = 0
    if mutation == "user":
        value["Config"]["User"] = "0:0"
    with pytest.raises(IsolationUnavailable):
        obj._verify_role_configuration(EVALUATION, "engine", tmp_path, ATTEMPT)


def test_attach_subprocess_starts_its_own_process_group(tmp_path, monkeypatch):
    import runtimes.evaluator.portable_runner as module

    obj = runner(tmp_path)
    captured = {}

    def popen(args, **kwargs):
        captured.update(args=args, **kwargs)
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    obj._launch(EVALUATION, "policy")
    assert captured["start_new_session"] is True
    assert captured["args"][-4:] == [
        "start",
        "--attach",
        "--interactive",
        obj.container_name(EVALUATION, "policy"),
    ]
    assert set(captured["env"]) == {"PATH", "LANG"}


def test_attach_unreaped_groups_killed_even_after_leader_exit_and_first_error(
    tmp_path, monkeypatch
):
    obj = runner(tmp_path)
    processes = [
        SimpleNamespace(pid=123, returncode=None, wait=lambda **_: 0),
        SimpleNamespace(pid=124, returncode=None, wait=lambda **_: 0),
    ]
    obj._attachments[EVALUATION] = processes
    calls = []

    def observe(kind, pid, flags):
        assert kind == os.P_PID
        assert flags & os.WNOWAIT
        calls.append(("observe", pid))
        return (
            SimpleNamespace(si_code=os.CLD_EXITED, si_status=0) if pid == 123 else None
        )

    def killpg(pid, sig):
        calls.append(("signal" if sig else "probe", pid))
        if sig == 0:
            raise ProcessLookupError
        if pid == 123:
            raise PermissionError("first group uncertain")

    monkeypatch.setattr(os, "waitid", observe)
    monkeypatch.setattr(os, "killpg", killpg)
    assert obj._kill_attachments(EVALUATION) is False
    assert calls == [
        ("observe", 123),
        ("signal", 123),
        ("probe", 123),
        ("observe", 124),
        ("signal", 124),
        ("probe", 124),
    ]


@pytest.mark.parametrize("reaped", [True, False])
def test_attachment_lost_identity_is_never_signalled(tmp_path, monkeypatch, reaped):
    obj = runner(tmp_path)
    obj._attachments[EVALUATION] = [
        SimpleNamespace(pid=123, returncode=0 if reaped else None)
    ]
    calls = []

    def observe(*args):
        raise ChildProcessError

    def probe(pid, sig):
        calls.append((pid, sig))
        assert sig == 0, "Never signal a reaped or no-longer-owned process identity"
        raise PermissionError

    monkeypatch.setattr(os, "waitid", observe)
    monkeypatch.setattr(os, "killpg", probe)
    assert obj._kill_attachments(EVALUATION) is False
    assert calls == ([(123, 0)] if reaped else [])


@pytest.mark.parametrize(
    "code,status,expected",
    [(os.CLD_EXITED, 0, 0), (os.CLD_EXITED, 9, 9), (os.CLD_KILLED, 9, -9)],
)
def test_attachment_observation_preserves_pid_until_cleanup(
    tmp_path, monkeypatch, code, status, expected
):
    obj = runner(tmp_path)
    process = SimpleNamespace(pid=123, returncode=None)

    def observe(kind, pid, flags):
        assert (kind, pid, flags) == (
            os.P_PID,
            123,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
        return SimpleNamespace(si_code=code, si_status=status)

    monkeypatch.setattr(os, "waitid", observe)
    assert obj._attachment_exitcode(process) == expected
    assert process.returncode is None


def test_attachment_observation_fails_if_ownership_was_lost(tmp_path, monkeypatch):
    obj = runner(tmp_path)
    monkeypatch.setattr(
        os, "waitid", lambda *args: (_ for _ in ()).throw(ChildProcessError())
    )
    with pytest.raises(IsolationUnavailable, match="identity no longer reserved"):
        obj._attachment_exitcode(SimpleNamespace(pid=123, returncode=None))


def test_relay_never_reaps_process_identity_before_teardown():
    import inspect

    source = inspect.getsource(PortableDockerEvaluator._relay)
    assert ".poll(" not in source and ".wait(" not in source
    assert "_attachment_exitcode" in source


def test_child_enumeration_recovers_partial_groups_by_attempt(tmp_path, monkeypatch):
    obj = runner(tmp_path)
    second = str(UUID(int=999))
    (obj.state_dir / f"{EVALUATION}.portable-group.json").write_text(
        json.dumps({"evaluation_id": EVALUATION, "attempt_id": ATTEMPT, "run_id": RUN})
    )
    monkeypatch.setattr(
        obj,
        "_command",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=second + "\n" + second + "\n"
        ),
    )
    inspections = []
    monkeypatch.setattr(
        obj,
        "_inspect_role",
        lambda identity, role, **kwargs: (
            inspections.append((identity, role, kwargs["attempt_id"])) or None
        ),
    )
    assert obj.children(ATTEMPT) == sorted([EVALUATION, second])
    assert set(inspections) == {
        (identity, role, ATTEMPT)
        for identity in (EVALUATION, second)
        for role in ("engine", "policy")
    }


def test_attempt_stop_does_not_short_circuit_other_groups(tmp_path, monkeypatch):
    obj = runner(tmp_path)
    ids = [EVALUATION, str(UUID(int=999))]
    monkeypatch.setattr(obj, "children", lambda _: ids)
    stopped = []
    monkeypatch.setattr(
        obj, "stop", lambda identity: stopped.append(identity) or identity != EVALUATION
    )
    assert obj.stop_attempt(ATTEMPT) is False
    assert stopped == ids


def test_unknown_daemon_enumeration_still_contains_locally_registered_group(
    tmp_path, monkeypatch
):
    obj = runner(tmp_path)
    (obj.state_dir / f"{EVALUATION}.portable-group.json").write_text(
        json.dumps({"evaluation_id": EVALUATION, "attempt_id": ATTEMPT, "run_id": RUN})
    )
    monkeypatch.setattr(
        obj,
        "_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            IsolationUnavailable("daemon timeout")
        ),
    )
    stopped = []
    monkeypatch.setattr(obj, "stop", lambda identity: stopped.append(identity) or True)
    assert obj.stop_attempt(ATTEMPT) is False
    assert stopped == [EVALUATION]


def test_unknown_stop_prevents_even_valid_engine_result_publication(
    tmp_path, monkeypatch
):
    engine, policy, _, _ = staged(tmp_path)
    obj = runner(tmp_path)
    monkeypatch.setattr(obj, "bind_environment", lambda _: obj.images)
    inspections = [0]

    def inspect(*args, **kwargs):
        inspections[0] += 1
        return (
            None
            if inspections[0] <= 2
            else {"State": {"Running": False, "ExitCode": 0}}
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
    monkeypatch.setattr(
        obj, "_relay", lambda *args: {"summary": {"initial_cash": 1000.0}}
    )
    monkeypatch.setattr(obj, "stop", lambda _: False)
    result = obj.run(
        EVALUATION, engine, policy, tmp_path / "output", run_id=RUN, attempt_id=ATTEMPT
    )
    assert result["status"] == "interrupted"
    assert result["bundle"] is None
    assert result["result_trust"] is None
    assert not list((tmp_path / "output").iterdir())


def test_independent_watchdog_contains_siblings_while_heartbeat_blocks(
    tmp_path, monkeypatch
):
    engine, policy, _, _ = staged(tmp_path)
    obj = runner(tmp_path, timeout=0.15, decision_timeout=0.1)
    monkeypatch.setattr(obj, "bind_environment", lambda _: obj.images)
    monkeypatch.setattr(obj, "_inspect_role", lambda *args, **kwargs: None)
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
    killed, release = threading.Event(), threading.Event()
    stops = []

    def stop_role(_, role):
        stops.append(role)
        if "engine" in stops and "policy" in stops:
            killed.set()
        return True

    monkeypatch.setattr(obj, "_stop_role", stop_role)

    def relay(*args):
        assert killed.wait(1), "watchdog must not depend on a blocked heartbeat"
        raise IsolationUnavailable("watchdog enforced deadline")

    monkeypatch.setattr(obj, "_relay", relay)
    started = time.monotonic()
    try:
        result = obj.run(
            EVALUATION,
            engine,
            policy,
            tmp_path / "output",
            run_id=RUN,
            attempt_id=ATTEMPT,
            heartbeat=lambda: release.wait(2),
        )
        assert result["status"] == "failed"
        assert time.monotonic() - started < 0.8
        assert {"engine", "policy"} <= set(stops)
    finally:
        release.set()


def test_concurrent_attachment_stops_signal_reserved_leader_only_once(
    tmp_path, monkeypatch
):
    obj = runner(tmp_path)
    entered, release = threading.Event(), threading.Event()
    process = SimpleNamespace(pid=123, returncode=None)
    calls = []

    def wait(**kwargs):
        entered.set()
        assert release.wait(1)
        process.returncode = -9
        return -9

    process.wait = wait
    obj._attachments[EVALUATION] = [process]
    monkeypatch.setattr(os, "waitid", lambda *args: None)

    def killpg(pid, sig):
        calls.append(sig)
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", killpg)
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(obj._kill_attachments(EVALUATION))
        )
        for _ in range(2)
    ]
    try:
        threads[0].start()
        assert entered.wait(1)
        threads[1].start()
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(2)
    assert all(not thread.is_alive() for thread in threads)
    assert results == [True, True]
    assert calls == [9, 0, 0]
