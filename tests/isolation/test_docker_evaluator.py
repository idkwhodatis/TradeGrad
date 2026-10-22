"""Real Docker + AKQuant security gate. CI requires this suite without skips."""

import json
import os
import secrets
import shutil
import textwrap
import time
from pathlib import Path
from uuid import uuid4

import pytest

from runtimes.evaluator.runner import DockerEvaluator

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner(tmp_path):
    evaluator = DockerEvaluator(state_dir=tmp_path / "runner-state", timeout=45)
    if not evaluator.available():
        if os.getenv("TRADEGRAD_REQUIRE_DOCKER") == "1":
            pytest.fail("M1 requires the actual Docker evaluator image and daemon")
        pytest.skip(
            "Actual Docker gate has not run: prebuilt evaluator/daemon unavailable"
        )
    return evaluator


def inputs(tmp_path, task="cn_time", preamble=""):
    target = tmp_path / "inputs"
    target.mkdir()
    market = task.split("_")[0].upper()
    shutil.copytree(ROOT / "tests/baseline/fixtures" / market, target / "data")
    shutil.copytree(
        ROOT / "tests/baseline/fixtures" / f"{market}_list", target / "universe"
    )
    (target / "strategy.py").write_text(
        textwrap.dedent(preamble)
        + "\n"
        + (ROOT / "seed_strategies" / f"{task}.py").read_text()
    )
    (target / "request.json").write_text(
        json.dumps(
            {
                "task": task,
                "market": market,
                "profile": "corrected_explicit_config",
                "start_date": "2024-01-02",
                "end_date": "2024-04-27",
            }
        )
    )
    return target


@pytest.mark.parametrize("task", ["cn_time", "cn_cross", "us_time", "us_cross"])
def test_real_seed_in_actual_isolation(runner, tmp_path, task):
    attempt = str(uuid4())
    result = runner.evaluate(
        inputs(tmp_path, task), tmp_path / "outputs", str(uuid4()), attempt
    )
    assert result["status"] == "succeeded", result
    expected = json.loads((ROOT / "tests/baseline/expected.json").read_text())[task]
    assert result["metrics"]["metrics"] == expected["metrics"]
    assert result["metrics"]["combined_score"] == expected["combined_score"]
    assert result["image_digest"].startswith("sha256:")
    assert runner._inspect(attempt) is None


def test_real_files_network_environment_and_kernel_limits(
    runner, tmp_path, monkeypatch
):
    private = tmp_path / "host-private.txt"
    private.write_text("host-only-test-sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", secrets.token_hex(32))
    monkeypatch.setenv("DATABASE_URL", "postgresql://" + secrets.token_hex(32))
    probe = f"""
import os, pathlib, resource, socket
assert os.getuid() != 0
assert 'OPENAI_API_KEY' not in os.environ and 'DATABASE_URL' not in os.environ
assert not pathlib.Path({str(private)!r}).exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
assert int(pathlib.Path('/sys/fs/cgroup/pids.max').read_text()) == 64
assert int(pathlib.Path('/sys/fs/cgroup/memory.max').read_text()) == 1024 * 1024 * 1024
quota, period = pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().split()
assert int(quota) / int(period) == 1
assert resource.getrlimit(resource.RLIMIT_NOFILE) == (128, 128)
assert resource.getrlimit(resource.RLIMIT_FSIZE) == (67108864, 67108864)
for path in ('/input/strategy.py', '/opt/tradegrad/src/evaluate.py', '/forbidden-root-write'):
    try:
        pathlib.Path(path).write_text('must fail')
    except OSError:
        pass
    else:
        raise AssertionError('Container allowed a forbidden filesystem write')
for address in ('127.0.0.1', '1.1.1.1'):
    with socket.socket() as client:
        client.settimeout(0.25)
        try:
            client.connect((address, 443))
        except OSError:
            pass
        else:
            raise AssertionError('Container gained network access')
print('ACTUAL_ISOLATION_PROBE_OK', flush=True)
"""
    result = runner.evaluate(
        inputs(tmp_path, preamble=probe),
        tmp_path / "outputs",
        str(uuid4()),
        str(uuid4()),
    )
    assert result["status"] == "succeeded", result
    assert "ACTUAL_ISOLATION_PROBE_OK" in result["logs"]["stdout"]
    assert private.read_text() == "host-only-test-sentinel"
    assert secrets.token_hex(32) not in json.dumps(result)


def test_actual_tmpfs_output_quota(runner, tmp_path):
    probe = """
import errno, pathlib
files = []
blocked = False
try:
    for i in range(100):
        path = pathlib.Path('/output') / ('quota-' + str(i))
        files.append(path)
        path.write_bytes(b'x' * 1024 * 1024)
except OSError as exc:
    assert exc.errno == errno.ENOSPC
    blocked = True
finally:
    for path in files:
        path.unlink(missing_ok=True)
assert blocked, 'No output tmpfs limit was enforced'
print('TMPFS_QUOTA_OK', flush=True)
"""
    result = runner.evaluate(
        inputs(tmp_path, preamble=probe),
        tmp_path / "outputs",
        str(uuid4()),
        str(uuid4()),
    )
    assert result["status"] == "succeeded", result
    assert "TMPFS_QUOTA_OK" in result["logs"]["stdout"]


def test_actual_process_and_inode_limits(runner, tmp_path):
    probe = """
import errno, os, pathlib, signal, time
children = []
limited = False
try:
    for i in range(100):
        child = os.fork()
        if child == 0:
            time.sleep(60)
            os._exit(0)
        children.append(child)
except OSError as exc:
    assert exc.errno == errno.EAGAIN
    limited = True
finally:
    for child in children:
        os.kill(child, signal.SIGKILL)
    for child in children:
        os.waitpid(child, 0)
assert limited, 'PID controller was not enforced'
files = []
limited = False
try:
    for i in range(2000):
        path = pathlib.Path('/output') / ('inode-' + str(i))
        path.touch()
        files.append(path)
except OSError as exc:
    assert exc.errno == errno.ENOSPC
    limited = True
finally:
    for path in files:
        path.unlink()
assert limited, 'Output inode controller was not enforced'
print('PROCESS_AND_INODE_LIMITS_OK', flush=True)
"""
    result = runner.evaluate(
        inputs(tmp_path, preamble=probe),
        tmp_path / "outputs",
        str(uuid4()),
        str(uuid4()),
    )
    assert result["status"] == "succeeded", result
    assert "PROCESS_AND_INODE_LIMITS_OK" in result["logs"]["stdout"]


@pytest.mark.parametrize("cancel", [False, True])
def test_actual_timeout_cancel_kills_entire_container(runner, tmp_path, cancel):
    runner.timeout = 30 if cancel else 5
    probe = """
import os, signal, time
if os.fork() == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
while True:
    time.sleep(1)
"""
    started = time.monotonic()
    attempt = str(uuid4())
    result = runner.evaluate(
        inputs(tmp_path, preamble=probe),
        tmp_path / "outputs",
        str(uuid4()),
        attempt,
        cancelled=lambda: cancel and time.monotonic() - started > 5,
    )
    assert result["status"] == ("cancelled" if cancel else "failed"), result
    assert time.monotonic() - started < 20
    assert runner._inspect(attempt) is None
    assert runner.stop(attempt)


def test_zero_exit_without_valid_metrics_is_failed(runner, tmp_path):
    result = runner.evaluate(
        inputs(tmp_path, preamble="import os\nos._exit(0)"),
        tmp_path / "outputs",
        str(uuid4()),
        str(uuid4()),
    )
    assert result["status"] == "failed", result
    assert "protocol" in result["error"]


def test_actual_memory_limit(runner, tmp_path):
    runner.memory_mb = 512
    result = runner.evaluate(
        inputs(
            tmp_path,
            preamble="chunks=[]\nwhile True:\n    chunks.append(bytearray(32*1024*1024))",
        ),
        tmp_path / "outputs",
        str(uuid4()),
        str(uuid4()),
    )
    assert result["status"] == "failed", result
    assert "memory" in result["error"] or result["exit_code"] == 137
