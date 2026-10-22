"""Offline supervisor tests. These do not substitute for Docker isolation tests."""

import json
import os
import secrets
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from runtimes.evaluator.runner import (
    DockerEvaluator,
    IsolationUnavailable,
    _metrics,
    _regular_tree,
)


@pytest.fixture
def runner(tmp_path):
    return DockerEvaluator(docker_binary="/not/a/docker", state_dir=tmp_path / "state")


def test_missing_isolation_fails_closed(runner, tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    assert not runner.available()
    with pytest.raises(IsolationUnavailable):
        runner.evaluate(inputs, tmp_path / "outputs", str(uuid4()), str(uuid4()))


def test_container_command_is_nonroot_offline_and_bounded(
    runner, tmp_path, monkeypatch
):
    monkeypatch.setattr(os, "getuid", lambda: 1001)
    args = runner._create_command(
        "sha256:" + "a" * 64,
        tmp_path / "in",
        tmp_path / "out",
        str(uuid4()),
        str(uuid4()),
    )
    for flag in (
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
    ):
        assert flag in args
    assert args[args.index("--user") + 1].startswith("1001:")
    assert args[args.index("--pids-limit") + 1] == "64"
    assert "type=bind,src=" in " ".join(args)
    assert "dst=/input,readonly" in " ".join(args)
    assert "dst=/output" not in " ".join(args)  # Output lives only in bounded tmpfs.
    assert "/output:rw,noexec,nosuid,nodev,size=67108864" in " ".join(args)
    assert "/var/run/docker.sock" not in " ".join(args)


def test_refuse_root_candidate(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(IsolationUnavailable):
        runner._create_command(
            "sha256:" + "a" * 64, tmp_path, tmp_path, str(uuid4()), str(uuid4())
        )


def test_credential_environment_not_forwarded(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", secrets.token_hex(32))
    monkeypatch.setenv("DATABASE_URL", "postgresql://" + secrets.token_hex(32))
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.invalid")
    runner = DockerEvaluator(state_dir=tmp_path / "state")
    assert set(runner.env) == {"PATH", "LANG"}


def test_remote_daemon_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEGRAD_DOCKER_HOST", "tcp://remote:2375")
    with pytest.raises(ValueError, match="local Docker socket"):
        DockerEvaluator(state_dir=tmp_path / "state")


def test_unknown_stop_never_confirms_termination(runner):
    assert runner.stop(str(uuid4())) is False


def test_stop_fences_future_creation(runner, tmp_path, monkeypatch):
    attempt = str(uuid4())
    monkeypatch.setattr(runner, "_inspect", lambda _: None)
    assert runner.stop(attempt)
    assert (runner.state_dir / f"{attempt}.stopped").read_text()
    monkeypatch.setattr(runner, "_image_id", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(runner, "_preflight", lambda: None)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    with pytest.raises(IsolationUnavailable, match="permanently fenced"):
        runner.evaluate(inputs, tmp_path / "outputs", str(uuid4()), attempt)


def test_inspect_does_not_confuse_daemon_error_with_absence(runner, monkeypatch):
    monkeypatch.setattr(
        runner,
        "_command",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 1, "", "Cannot connect to the Docker daemon"
        ),
    )
    with pytest.raises(IsolationUnavailable):
        runner._inspect(str(uuid4()))


def test_inspect_rejects_container_identity_collision(runner, monkeypatch):
    monkeypatch.setattr(
        runner,
        "_command",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 0, json.dumps([{"Config": {"Labels": {}}}]), ""
        ),
    )
    with pytest.raises(IsolationUnavailable, match="identity"):
        runner._inspect(str(uuid4()))


def test_invalid_uuid_cannot_reach_docker(runner):
    with pytest.raises(ValueError):
        runner.stop("--privileged")


@pytest.mark.parametrize(
    "missing",
    [
        "MemoryLimit",
        "SwapLimit",
        "CpuCfsPeriod",
        "CpuCfsQuota",
        "PidsLimit",
        "CgroupVersion",
        "SecurityOptions",
    ],
)
def test_missing_kernel_enforcement_fails_closed(runner, monkeypatch, missing):
    capabilities = dict(
        MemoryLimit=True,
        SwapLimit=True,
        CpuCfsPeriod=True,
        CpuCfsQuota=True,
        PidsLimit=True,
        CgroupVersion="2",
        CgroupDriver="systemd",
        OSType="linux",
        SecurityOptions=["name=seccomp,profile=builtin"],
    )
    capabilities.pop(missing)
    monkeypatch.setattr(
        runner,
        "_command",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 0, json.dumps(capabilities), ""
        ),
    )
    with pytest.raises(IsolationUnavailable, match="enforce"):
        runner._preflight()


def test_output_rejects_symlink_and_byte_budget(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="Symlinks"):
        _regular_tree(root, maximum_bytes=10)
    (root / "escape").unlink()
    (root / "big").write_bytes(b"x" * 11)
    with pytest.raises(ValueError, match="budget"):
        _regular_tree(root, maximum_bytes=10)


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), "1", None])
def test_metrics_schema_rejects_non_numeric_and_nonfinite(tmp_path, bad):
    payload = json.loads(
        (Path(__file__).parents[1] / "baseline/expected.json").read_text()
    )["cn_time"]
    payload["combined_score"] = bad
    target = tmp_path / "metrics.json"
    target.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="finite"):
        _metrics(target)


def test_metrics_schema_accepts_real_baseline(tmp_path):
    payload = json.loads(
        (Path(__file__).parents[1] / "baseline/expected.json").read_text()
    )["cn_time"]
    target = tmp_path / "metrics.json"
    target.write_text(json.dumps(payload))
    assert _metrics(target) == payload
