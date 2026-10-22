"""Pure-local supervisor regressions, not evidence of actual Docker isolation.

All Docker calls in these tests are replaced with explicit test doubles. The
real-container M1 acceptance gate must still be exercised on a supported host.
"""

import base64
import io
import json
import secrets
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from runtimes.evaluator import runner as runner_module
from runtimes.evaluator.runner import (
    RESULT_PREFIX,
    DockerEvaluator,
    IsolationUnavailable,
    _metrics,
)


def _payload():
    return json.loads(
        (Path(__file__).parents[1] / "baseline/expected.json").read_text()
    )["cn_time"]


class _DeferredCaptureThread:
    """Deterministically model a reader that is scheduled only during drain."""

    def __init__(self, *, target, args, daemon):
        self.target, self.args = target, args
        self.finished = False

    def start(self):
        pass

    def join(self, timeout=None):
        if not self.finished:
            self.target(*self.args)
            self.finished = True

    def is_alive(self):
        return not self.finished


class _FinishedLogProcess:
    returncode = 0

    def __init__(self, wire):
        self.stdout = io.BytesIO(wire)
        self.stderr = io.BytesIO()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_late_log_drain_cannot_succeed_after_exceeding_budget(tmp_path, monkeypatch):
    budget = 1024
    record = (
        RESULT_PREFIX.encode()
        + base64.b64encode(json.dumps(_payload()).encode())
        + b"\n"
    )
    assert len(record) < budget
    wire = record + b"x" * (budget * 3)
    evaluator = DockerEvaluator(
        docker_binary="/unused/docker", state_dir=tmp_path / "state", log_bytes=budget
    )
    inputs = tmp_path / "input"
    inputs.mkdir()
    inspect_results = iter([None, {"State": {"Running": False, "ExitCode": 0}}])
    monkeypatch.setattr(evaluator, "_image_id", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(evaluator, "_preflight", lambda: None)
    monkeypatch.setattr(evaluator, "_inspect", lambda _: next(inspect_results))
    monkeypatch.setattr(evaluator, "_create_command", lambda *a: ["create"])
    monkeypatch.setattr(
        evaluator,
        "_command",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "a" * 64, ""),
    )
    monkeypatch.setattr(evaluator, "stop", lambda _: True)
    real_thread = threading.Thread

    def make_thread(*args, **kwargs):
        if kwargs.get("target").__name__ == "capture":
            return _DeferredCaptureThread(**kwargs)
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(runner_module.threading, "Thread", make_thread)
    observed_log_commands = []

    def logs(command, **kwargs):
        observed_log_commands.append(command)
        return _FinishedLogProcess(wire)

    monkeypatch.setattr(runner_module.subprocess, "Popen", logs)
    result = evaluator.evaluate(inputs, tmp_path / "output", str(uuid4()), str(uuid4()))

    assert result["status"] == "failed"
    assert result["metrics"] is None
    assert "log" in result["error"].lower()
    assert len(result["logs"]["stdout"].encode()) <= budget
    # The streaming path must use the same isolated CLI config/host as commands.
    assert observed_log_commands[0][: len(evaluator.prefix)] == evaluator.prefix


@pytest.mark.parametrize("location", ["top_level", "metrics"])
def test_nonfinite_extra_json_cannot_cross_result_boundary(tmp_path, location):
    payload = _payload()
    destination = payload if location == "top_level" else payload["metrics"]
    destination["untrusted_extra"] = float("nan")
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(payload))
    try:
        accepted = _metrics(path)
    except ValueError:
        return
    # Either strict rejection or canonical omission is safe; propagation is not.
    json.dumps(accepted, allow_nan=False)
    assert "untrusted_extra" not in (
        accepted if location == "top_level" else accepted["metrics"]
    )


@pytest.mark.parametrize("field", ["combined_score", "trade_per_day"])
def test_large_integer_is_schema_failure_not_uncaught_overflow(tmp_path, field):
    payload = _payload()
    destination = payload if field == "combined_score" else payload["metrics"]
    destination[field] = 10**400
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        _metrics(path)


def test_cli_uses_explicit_empty_config_and_local_host(tmp_path, monkeypatch):
    proxy_password = secrets.token_hex(32)
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    (ambient / "config.json").write_text(
        json.dumps(
            {
                "currentContext": "untrusted-remote",
                "proxies": {
                    "default": {
                        "httpProxy": f"http://user:{proxy_password}@proxy.invalid"
                    }
                },
            }
        )
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKER_CONFIG", str(ambient))
    monkeypatch.setenv("DOCKER_CONTEXT", "untrusted-remote")
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.invalid:2375")
    monkeypatch.setenv("TRADEGRAD_DOCKER_HOST", "unix:///tmp/tradegrad-test.sock")
    monkeypatch.setenv("OPENAI_API_KEY", secrets.token_hex(32))
    evaluator = DockerEvaluator(
        docker_binary="/unused/docker", state_dir=tmp_path / "state"
    )
    captured = []

    def command(args, **kwargs):
        captured.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runner_module.subprocess, "run", command)
    evaluator._command("version")
    argv, kwargs = captured[0]
    config = Path(argv[argv.index("--config") + 1])
    assert config != ambient
    assert json.loads((config / "config.json").read_text()) == {}
    assert argv[argv.index("--host") + 1] == "unix:///tmp/tradegrad-test.sock"
    assert set(kwargs["env"]) == {"PATH", "LANG"}
    assert proxy_password not in json.dumps(kwargs["env"])


def test_candidate_tmpfs_has_byte_and_inode_budgets(tmp_path, monkeypatch):
    evaluator = DockerEvaluator(
        docker_binary="/unused/docker", state_dir=tmp_path / "state"
    )
    monkeypatch.setattr(runner_module.os, "getuid", lambda: 1001)
    arguments = evaluator._create_command(
        "sha256:" + "a" * 64,
        tmp_path / "input",
        tmp_path / "output",
        str(uuid4()),
        str(uuid4()),
    )
    mounts = [
        arguments[index + 1]
        for index, value in enumerate(arguments)
        if value == "--tmpfs"
    ]
    for destination, inode_limit in (("/tmp:", 4096), ("/output:", 1024)):
        mount = next(value for value in mounts if value.startswith(destination))
        assert f"nr_inodes={inode_limit}" in mount
        assert "size=67108864" in mount
        assert all(option in mount for option in ("noexec", "nosuid", "nodev"))


def test_created_container_is_removed_before_stop_is_confirmed(tmp_path, monkeypatch):
    evaluator = DockerEvaluator(
        docker_binary="/unused/docker", state_dir=tmp_path / "state"
    )
    attempt = str(uuid4())
    commands = []
    removed = False

    def inspect(_):
        return None if removed else {"State": {"Running": False, "Status": "created"}}

    def command(*args, **kwargs):
        nonlocal removed
        commands.append(args)
        if args[0] == "rm" and ("--force" in args or "-f" in args):
            removed = True
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(evaluator, "_inspect", inspect)
    monkeypatch.setattr(evaluator, "_command", command)
    assert evaluator.stop(attempt)
    assert removed, (
        "A nonrunning created snapshot does not cancel an in-flight daemon start"
    )
    assert (evaluator.state_dir / f"{attempt}.stopped").exists()


def test_unsuccessful_removal_does_not_confirm_stop(tmp_path, monkeypatch):
    evaluator = DockerEvaluator(
        docker_binary="/unused/docker", state_dir=tmp_path / "state"
    )
    monkeypatch.setattr(
        evaluator,
        "_inspect",
        lambda _: {"State": {"Running": False, "Status": "created"}},
    )
    monkeypatch.setattr(
        evaluator,
        "_command",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "removal failed"),
    )
    assert evaluator.stop(str(uuid4())) is False


def test_tombstone_failure_still_attempts_shutdown_without_releasing_retry(
    tmp_path, monkeypatch
):
    evaluator = DockerEvaluator(
        docker_binary="/unused/docker", state_dir=tmp_path / "state"
    )
    stopped = []

    def unavailable_fence(_):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(evaluator, "_fence", unavailable_fence)
    monkeypatch.setattr(
        evaluator, "_stop_locked", lambda attempt: stopped.append(attempt) or True
    )
    attempt = str(uuid4())
    try:
        result = evaluator.stop(attempt)
    except (OSError, IsolationUnavailable):
        result = False
    assert stopped == [attempt], "Fencing failure must not abandon a running container"
    assert result is False, "Undurable fencing cannot authorize a retry"


def test_fresh_supervisor_honors_shared_durable_attempt_fence(tmp_path, monkeypatch):
    state = tmp_path / "shared-state"
    previous = DockerEvaluator(docker_binary="/unused/docker", state_dir=state)
    attempt = str(uuid4())
    monkeypatch.setattr(previous, "_inspect", lambda _: None)
    assert previous.stop(attempt)

    recovered = DockerEvaluator(docker_binary="/unused/docker", state_dir=state)
    monkeypatch.setattr(recovered, "_image_id", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(recovered, "_preflight", lambda: None)

    def unexpected_docker_operation(*args, **kwargs):
        pytest.fail("A fenced attempt must not query or create its sandbox again")

    monkeypatch.setattr(recovered, "_inspect", unexpected_docker_operation)
    inputs = tmp_path / "input"
    inputs.mkdir()
    with pytest.raises(IsolationUnavailable, match="fenced"):
        recovered.evaluate(inputs, tmp_path / "output", str(uuid4()), attempt)


def test_stop_and_launch_share_same_attempt_lock_across_supervisors(
    tmp_path, monkeypatch
):
    state = tmp_path / "shared-state"
    launching = DockerEvaluator(docker_binary="/unused/docker", state_dir=state)
    recovering = DockerEvaluator(docker_binary="/unused/docker", state_dir=state)
    attempt = str(uuid4())
    entered = threading.Event()
    done = threading.Event()
    outcomes = []
    monkeypatch.setattr(recovering, "_inspect", lambda _: None)

    def stop():
        entered.set()
        outcomes.append(recovering.stop(attempt))
        done.set()

    with launching._attempt_lock(attempt):
        thread = threading.Thread(target=stop, daemon=True)
        thread.start()
        assert entered.wait(timeout=1)
        assert not done.wait(timeout=0.05), (
            "Stop must not confirm absence during launch's critical section"
        )
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert outcomes == [True]
    assert (state / f"{attempt}.stopped").exists()


def test_deadline_stops_container_while_heartbeat_callback_is_blocked(
    tmp_path, monkeypatch
):
    evaluator = DockerEvaluator(
        docker_binary="/unused/docker", state_dir=tmp_path / "state", timeout=0.05
    )
    inputs = tmp_path / "input"
    inputs.mkdir()
    callback_entered = threading.Event()
    stopped = threading.Event()
    stopped_while_callback_blocked = []
    callback_returned = threading.Event()
    sandbox_created = False

    class ActiveLogProcess(_FinishedLogProcess):
        def poll(self):
            return 0 if stopped.is_set() else None

    def inspect(_):
        if not sandbox_created or stopped.is_set():
            return None
        return {"State": {"Running": True, "ExitCode": 0}}

    def command(*args, **kwargs):
        nonlocal sandbox_created
        if args[0] == "create":
            sandbox_created = True
        return subprocess.CompletedProcess(args, 0, "a" * 64, "")

    def stop(_):
        stopped_while_callback_blocked.append(
            callback_entered.is_set() and not callback_returned.is_set()
        )
        stopped.set()
        return True

    def blocked_heartbeat():
        callback_entered.set()
        try:
            assert stopped.wait(timeout=2), (
                "Wall-clock enforcement waited for the blocked database callback"
            )
        finally:
            callback_returned.set()

    monkeypatch.setattr(evaluator, "_image_id", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(evaluator, "_preflight", lambda: None)
    monkeypatch.setattr(evaluator, "_create_command", lambda *a: ["create"])
    monkeypatch.setattr(evaluator, "_inspect", inspect)
    monkeypatch.setattr(evaluator, "_command", command)
    monkeypatch.setattr(evaluator, "stop", stop)
    monkeypatch.setattr(
        runner_module.subprocess, "Popen", lambda *a, **kw: ActiveLogProcess(b"")
    )
    result = evaluator.evaluate(
        inputs,
        tmp_path / "output",
        str(uuid4()),
        str(uuid4()),
        heartbeat=blocked_heartbeat,
    )
    assert any(stopped_while_callback_blocked)
    assert result["status"] == "failed"
    assert result["metrics"] is None
    assert "wall-clock" in result["error"]


def test_watchdog_thread_start_failure_still_stops_started_container(
    tmp_path, monkeypatch
):
    evaluator = DockerEvaluator(
        docker_binary="/unused/docker", state_dir=tmp_path / "state"
    )
    inputs = tmp_path / "input"
    inputs.mkdir()
    stopped = []

    class UnstartableThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

        def join(self, timeout=None):
            raise RuntimeError("cannot join thread before it is started")

    monkeypatch.setattr(evaluator, "_image_id", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(evaluator, "_preflight", lambda: None)
    monkeypatch.setattr(evaluator, "_create_command", lambda *a: ["create"])
    monkeypatch.setattr(evaluator, "_inspect", lambda _: None)
    monkeypatch.setattr(
        evaluator,
        "_command",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "a" * 64, ""),
    )
    monkeypatch.setattr(
        evaluator, "stop", lambda attempt: stopped.append(attempt) or True
    )
    monkeypatch.setattr(runner_module.threading, "Thread", UnstartableThread)
    attempt = str(uuid4())
    try:
        result = evaluator.evaluate(inputs, tmp_path / "output", str(uuid4()), attempt)
    except RuntimeError:
        result = None
    assert stopped and all(value == attempt for value in stopped)
    if result is not None:
        assert result["status"] == "failed"
