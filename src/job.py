"""Bounded subprocess execution for the legacy loss layer.

This is process supervision, NOT a security sandbox. The WebUI evaluator runner supplies
filesystem, network and OS isolation before executing untrusted candidate code.
"""
import json
import logging
import math
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
METRIC_NAMES = {"sharpe_ratio", "annualized_return", "max_drawdown", "trade_per_day", "win_rate", "return_per_trade"}
MAX_RESULT_BYTES = 1_048_576
MAX_LOG_BYTES = 2_097_152


def evaluator_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Do not pass coordinator credentials or the whole ambient environment to candidates."""
    allowed = {"PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "AKQUANT_DATA_CACHE", "TRADEGRAD_DATA_WORKERS"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    if overrides:
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"Unsupported evaluator environment keys: {sorted(unknown)}")
        env.update(overrides)
    env.update(PYTHONHASHSEED="0", TRADEGRAD_DATA_WORKERS="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", POLARS_MAX_THREADS="1")
    return env


class ProcessWithLogging:
    def __init__(self, process, log_files, log_threads):
        self.process, self.log_files, self.log_threads = process, log_files, log_threads

    def __getattr__(self, name):
        return getattr(self.process, name)

    def cleanup_logging(self):
        for thread in self.log_threads:
            thread.join(timeout=5)
        for handle in self.log_files:
            handle.close()


def _stream_output(pipe, handle, verbose_prefix=None):
    written = 0
    try:
        # Fixed-size reads also bound memory for a child producing a huge line.
        for block in iter(lambda: pipe.read(8192), ""):
            if written < MAX_LOG_BYTES:
                part = block[:MAX_LOG_BYTES - written]
                handle.write(part)
                handle.flush()
                written += len(part)
    finally:
        pipe.close()


def submit(log_dir: str, cmd: list[str], verbose: bool = False, *, env=None):
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    handles = tuple((directory / name).open("w", encoding="utf-8") for name in ("log.out", "log.err"))
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace", env=evaluator_environment(env),
                                   start_new_session=(os.name == "posix"))
    except Exception:
        for handle in handles:
            handle.close()
        raise
    threads = tuple(threading.Thread(target=_stream_output, args=(pipe, handle), daemon=True)
                    for pipe, handle in zip((process.stdout, process.stderr), handles))
    for thread in threads:
        thread.start()
    return ProcessWithLogging(process, handles, threads)


def _stop_group(process, sig):
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass


def monitor(process, log_dir: str, poll_interval: float = 0.05, timeout: float = 900, verbose: bool = False):
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _stop_group(process, signal.SIGKILL)
            process.wait()
    finally:
        # A completed leader must not leave descendants holding logs or doing work.
        _stop_group(process, signal.SIGKILL)
        process.cleanup_logging()
    return {"returncode": process.returncode, "timed_out": timed_out}


def load_performance(path: Path) -> dict:
    if not path.is_file():
        raise ValueError("missing metrics.json")
    if path.stat().st_size > MAX_RESULT_BYTES:
        raise ValueError("metrics.json exceeds size limit")
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid metrics JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("metrics"), dict):
        raise ValueError("invalid metrics schema")
    numbers = {"combined_score": value.get("combined_score"), **{k: value["metrics"].get(k) for k in METRIC_NAMES}}
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in numbers.values()):
        raise ValueError("metrics require all finite numeric fields")
    if value["metrics"]["trade_per_day"] <= 0:
        raise ValueError("no completed trades")
    return value


def run_job(log_dir: str, cmd: list[str], timeout: float = 900):
    """Return legacy (elapsed, error), with a structured job.json alongside the logs."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    result_path = directory / "metrics.json"
    result_path.unlink(missing_ok=True)  # A prior attempt's result cannot make this attempt succeed.
    started = time.monotonic()
    try:
        process = submit(directory, cmd)
        outcome = monitor(process, directory, timeout=timeout)
    except OSError as exc:
        outcome = {"returncode": None, "timed_out": False, "run_time": time.monotonic() - started,
                   "status": "failed", "error": f"evaluation could not start: {exc}", "stderr": ""}
        (directory / "job.json").write_text(json.dumps(outcome, indent=2))
        return outcome["run_time"], outcome["error"]
    outcome["run_time"] = time.monotonic() - started
    stderr = (directory / "log.err").read_text().strip()
    if outcome["timed_out"]:
        error = "evaluation timed out"
    elif outcome["returncode"] != 0:
        error = f"evaluation exited with code {outcome['returncode']}: {stderr}"
    else:
        try:
            load_performance(result_path)
            error = ""
        except (ValueError, OSError) as exc:
            error = f"invalid evaluation result: {exc}"
    outcome.update(status="failed" if error else "succeeded", error=error, stderr=stderr)
    (directory / "job.json").write_text(json.dumps(outcome, indent=2))
    return outcome["run_time"], error
