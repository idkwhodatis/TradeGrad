import json
import sys
from pathlib import Path

import pytest

from src.job import evaluator_environment, run_job

VALID = {"combined_score": 1.2, "metrics": {name: 1.0 for name in ("sharpe_ratio", "annualized_return", "max_drawdown", "trade_per_day", "win_rate", "return_per_trade")}}


def command(directory, tail="", payload=VALID):
    prefix = "import json, pathlib, sys, os, time; "
    if payload is not None:
        prefix += f"pathlib.Path({str(directory / 'metrics.json')!r}).write_text({json.dumps(payload)!r}); "
    return [sys.executable, "-c", prefix + tail]


def test_warning_is_not_failure(tmp_path):
    _, error = run_job(tmp_path, command(tmp_path, "print('ordinary warning', file=sys.stderr)"))
    assert not error
    result=json.loads((tmp_path / "job.json").read_text())
    assert result["returncode"] == 0
    assert result["stderr"] == "ordinary warning"
    assert result["status"] == "succeeded"


@pytest.mark.parametrize("payload,tail,error_text", [(VALID,"sys.exit(7)","exited with code 7"), (None,"pass","missing metrics"), ({},"pass","schema"), ({"combined_score":float('nan'),"metrics":VALID["metrics"]},"pass","finite"), ({"combined_score":True,"metrics":VALID["metrics"]},"pass","finite")])
def test_exit_and_result_schema_determine_failure(tmp_path,payload,tail,error_text):
    _, error = run_job(tmp_path,command(tmp_path,tail,payload))
    assert error_text in error
    assert json.loads((tmp_path / "job.json").read_text())["status"] == "failed"


def test_stale_result_cannot_hide_missing_output(tmp_path):
    (tmp_path / "metrics.json").write_text(json.dumps(VALID))
    _, error=run_job(tmp_path,command(tmp_path,"pass",None))
    assert "missing metrics" in error


def test_timeout_is_failure_even_with_valid_result(tmp_path):
    _,error=run_job(tmp_path,command(tmp_path,"time.sleep(20)"),timeout=0.1)
    assert "timed out" in error
    assert json.loads((tmp_path / "job.json").read_text())["returncode"] is not None


def test_timeout_reaps_descendant_processes(tmp_path):
    pid_file=tmp_path / "child.pid"
    code=f"import subprocess; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(20)"
    _,error=run_job(tmp_path,command(tmp_path,code),timeout=0.25)
    assert "timed out" in error
    pid=int(pid_file.read_text())
    # A zombie has exited and cannot run; its init parent controls reaping timing in CI.
    stat=Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


def test_credentials_are_not_inherited(tmp_path,monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY","fake-secret-for-regression")
    monkeypatch.setenv("DATABASE_URL","postgres://secret")
    monkeypatch.setenv("TRADEGRAD_DATA_WORKERS","200")
    environment=evaluator_environment()
    assert "OPENROUTER_API_KEY" not in environment
    assert "DATABASE_URL" not in environment
    assert environment["TRADEGRAD_DATA_WORKERS"] == "1"
    _,error=run_job(tmp_path,command(tmp_path,"assert 'OPENROUTER_API_KEY' not in os.environ; assert 'DATABASE_URL' not in os.environ"))
    assert not error
    with pytest.raises(ValueError):
        evaluator_environment({"OPENROUTER_API_KEY":"no"})


def test_timeout_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        run_job(tmp_path,command(tmp_path),timeout=0)
    assert not (tmp_path / "metrics.json").exists()


def test_launch_failure_has_structured_failed_result(tmp_path):
    _,error=run_job(tmp_path,[str(tmp_path / "no-such-python")])
    assert "could not start" in error
    assert json.loads((tmp_path / "job.json").read_text())["status"] == "failed"
