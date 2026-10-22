"""Bounded retry and process-isolated SDK calls, with redacted failure classes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


class ProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def classify_error(exc: Exception) -> ProviderError:
    # Never copy upstream messages: URLs, headers and credentials may be embedded.
    name = type(exc).__name__.lower()
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429 or "ratelimit" in name:
        return ProviderError("rate_limited", retryable=True)
    if status in {500, 502, 503, 504} or any(
        x in name for x in ["timeout", "connection"]
    ):
        return ProviderError("upstream_temporarily_unavailable", retryable=True)
    if status in {401, 403}:
        return ProviderError("upstream_access_denied")
    if isinstance(exc, (KeyError, ValueError, TypeError)):
        return ProviderError("upstream_schema_changed")
    return ProviderError("upstream_request_failed")


@dataclass(frozen=True)
class CallPolicy:
    timeout: float = 20.0
    attempts: int = 2
    min_interval: float = 1.0
    backoff: float = 0.5

    def __post_init__(self):
        if not 0 < self.timeout <= 60 or not 1 <= self.attempts <= 3:
            raise ValueError("Timeout/attempt policy exceeds supported limits")
        if not 0 <= self.min_interval <= 30 or not 0 <= self.backoff <= 10:
            raise ValueError("Invalid pacing/backoff policy")


_BUDGET_RESERVER = ContextVar("marketdata_budget_reserver", default=None)


@contextmanager
def budget_scope(reserver):
    token = _BUDGET_RESERVER.set(reserver)
    try:
        yield
    finally:
        _BUDGET_RESERVER.reset(token)


def reserve_budget(key: str):
    reserver = _BUDGET_RESERVER.get()
    if reserver is not None:
        reserver(key)


_LOCK = threading.Lock()
_NEXT: dict[str, float] = {}


def with_retries(operation, *, budget_key: str, policy: CallPolicy):
    for attempt in range(policy.attempts):
        # One shared per-process true-upstream/endpoint budget, not per symbol.
        with _LOCK:
            delay = max(0, _NEXT.get(budget_key, 0) - time.monotonic())
            time.sleep(delay)
            _NEXT[budget_key] = time.monotonic() + policy.min_interval
        reserve_budget(budget_key)
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - redact SDK failures at the trust boundary
            error = exc if isinstance(exc, ProviderError) else classify_error(exc)
            if not error.retryable or attempt + 1 == policy.attempts:
                raise error from None
            time.sleep(min(policy.backoff * 2**attempt, 10))
    raise AssertionError("unreachable")


SDK_MODULES = {
    "packages.marketdata.providers.sdk_worker",
    "packages.marketdata.providers.openbb_sdk",
}
PUBLIC_NETWORK_ENV = {
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}


def sdk_environment(home: Path) -> dict[str, str]:
    """Retain normal network trust/proxies, never ambient user/SDK credentials."""
    root = Path(__file__).resolve().parents[3]
    env = {key: value for key, value in os.environ.items() if key in PUBLIC_NETWORK_ENV}
    env.update(
        {
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "TMPDIR": str(home),
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        }
    )
    return env


def _kill_owned_group(pid: int):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_sdk_process(
    module: str, payload: dict, *, timeout: float, max_output_bytes: int = 100_000_000
) -> dict:
    """Private HOME, explicit env, hard deadline, bounded files, no log reflection."""
    if (
        module not in SDK_MODULES
        or not 0 < timeout <= 120
        or not 1024 <= max_output_bytes <= 100_000_000
    ):
        raise ValueError("Unsupported SDK module or process bounds")
    encoded = json.dumps(payload, allow_nan=False).encode()
    if len(encoded) > 1_000_000:
        raise ValueError("SDK request is too large")
    with tempfile.TemporaryDirectory(prefix="tradegrad-sdk-") as work:
        home = Path(work)
        output = home / "output.json"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "packages.marketdata.providers.process_entry",
                module,
                str(output),
                str(max_output_bytes),
                str(os.getpid()),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=sdk_environment(home),
            cwd=Path(__file__).resolve().parents[3],
        )
        try:
            process.communicate(encoded, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_owned_group(process.pid)
            process.communicate()
            raise ProviderError("upstream_timeout", retryable=True) from None
        finally:
            # Clean descendants even when the SDK parent returned first.
            _kill_owned_group(process.pid)
        if output.exists() and output.stat().st_size >= max_output_bytes:
            raise ProviderError("upstream_payload_too_large")
        if not output.exists() or process.returncode:
            raise ProviderError("sdk_process_failed")
        try:
            response = json.loads(output.read_text())
        except (ValueError, UnicodeError):
            raise ProviderError("sdk_invalid_response") from None
        if not isinstance(response, dict):
            raise ProviderError("sdk_invalid_response")
        return response


def sdk_call(operation: str, params: dict, *, timeout: float):
    payload = run_sdk_process(
        "packages.marketdata.providers.sdk_worker",
        {"operation": operation, "params": params},
        timeout=timeout,
    )
    if "error" in payload:
        raise ProviderError(payload["error"], retryable=payload.get("retryable", False))
    return payload["result"]


def restore_frame(payload):
    import pandas as pd

    if "arrow_ipc_base64" in payload:
        import base64

        import pyarrow as pa

        frame = (
            pa.ipc.open_stream(base64.b64decode(payload["arrow_ipc_base64"]))
            .read_all()
            .to_pandas()
        )
        frame.attrs.update(payload.get("attributes", {}))
        if payload.get("index_frequency") and isinstance(frame.index, pd.DatetimeIndex):
            frame.index.freq = payload["index_frequency"]
        return frame
    frame = pd.DataFrame(
        payload["data"], columns=payload["columns"], index=payload["index"]
    )
    return frame
