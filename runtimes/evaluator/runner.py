"""Trusted, fail-closed Docker supervisor for native research evaluations.

Candidate code never runs in this process. Docker access belongs to this trusted
supervisor only; the candidate gets neither the socket nor coordinator secrets.
Native engine metrics are observations, not a tamper-proof trusted ledger.
"""

from __future__ import annotations

import base64
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

RESULT_PREFIX = "TRADEGRAD_RESULT_V1:"


class IsolationUnavailable(RuntimeError):
    """No safe execution or termination confirmation is available."""


def _identity(value: str) -> str:
    return str(UUID(str(value)))


def _regular_tree(root: Path, *, maximum_bytes: int, maximum_files: int = 1024) -> int:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Expected a non-symlink directory")
    total = count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            if item.is_symlink():
                raise ValueError("Symlinks are not accepted in evaluation files")
        for name in files:
            item = Path(directory) / name
            if not item.is_file():
                raise ValueError("Only regular evaluation files are accepted")
            count += 1
            total += item.stat().st_size
            if total > maximum_bytes or count > maximum_files:
                raise ValueError("Evaluation file budget exceeded")
    return total


def _metrics(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        raise ValueError("Missing or oversized metrics.json")

    return _validated_metrics_bytes(path.read_bytes())


def _validated_metrics_bytes(data: bytes) -> dict:
    if len(data) > 1_048_576:
        raise ValueError("Oversized metrics JSON")

    def invalid_constant(_):
        raise ValueError("Evaluation metrics must be finite numbers")

    value = json.loads(data, parse_constant=invalid_constant)
    fields = (
        "sharpe_ratio",
        "annualized_return",
        "max_drawdown",
        "trade_per_day",
        "win_rate",
        "return_per_trade",
    )
    if not isinstance(value, dict) or not isinstance(value.get("metrics"), dict):
        raise ValueError("Invalid evaluation result schema")  # noqa: TRY004 - wire schema validation
    numbers = [value.get("combined_score")] + [
        value["metrics"].get(key) for key in fields
    ]
    try:
        valid = all(
            not isinstance(number, bool)
            and isinstance(number, (int, float))
            and math.isfinite(number)
            for number in numbers
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Evaluation metrics must be finite numbers")
    if value.get("error") or value["metrics"]["trade_per_day"] <= 0:
        raise ValueError("No valid evaluated strategy")
    allowed = {
        "combined_score",
        "metrics",
        "market",
        "profile",
        "start_time",
        "end_time",
    }
    if not set(value).issubset(allowed) or set(value["metrics"]) != set(fields):
        raise ValueError("Unexpected evaluation result fields")
    for key in ("market", "profile", "start_time", "end_time"):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > 100):
            raise ValueError("Invalid evaluation metadata")
    return value


def decode_result_protocol(
    stdout: bytes,
    output_dir: Path,
    *,
    expected_config_sha256: str | None = None,
    expected_identity: dict | None = None,
) -> tuple[dict, dict | None]:
    """Validate a bounded data-only wire record; never deserialize engine objects."""
    from packages.tradegrad_adapter.results import (
        MAX_WIRE_BYTES,
        RESULT_PREFIX_V2,
        canonical_json_bytes,
        validate_envelope,
    )

    records = []
    for line in stdout.decode("utf-8", errors="strict").splitlines():
        for prefix in (RESULT_PREFIX, RESULT_PREFIX_V2):
            if line.startswith(prefix):
                records.append((prefix, line[len(prefix) :]))
    if len(records) != 1 or len(records[0][1]) > 4 * ((MAX_WIRE_BYTES + 2) // 3):
        raise ValueError("Missing, duplicated or oversized result protocol record")
    prefix, encoded = records[0]
    data = base64.b64decode(encoded, validate=True)
    if len(data) > MAX_WIRE_BYTES:
        raise ValueError("Result protocol exceeded its byte budget")
    bundle = None
    if prefix == RESULT_PREFIX_V2:
        envelope = validate_envelope(data)
        bundle = envelope["bundle"]
        if (
            expected_config_sha256 is not None
            and bundle["identity"].get("run_config_sha256") != expected_config_sha256
        ):
            raise ValueError("ResultBundle belongs to a different immutable config")
        if expected_identity is not None and bundle["identity"] != expected_identity:
            raise ValueError("ResultBundle staged-input identity mismatch")
        metrics_bytes = canonical_json_bytes(envelope["metrics"])
    else:
        if expected_config_sha256 is not None:
            raise ValueError("Configured research requires a complete V2 ResultBundle")
        metrics_bytes = data
    metrics = _validated_metrics_bytes(metrics_bytes)
    files = {"metrics.json": metrics_bytes}
    if bundle is not None:
        files["result-bundle.json"] = canonical_json_bytes(bundle)
    if not output_dir.is_dir() or any(
        path.is_symlink() for path in (output_dir, *output_dir.parents)
    ):
        raise ValueError("Result output directory must not traverse symlinks")
    if any(
        (output_dir / name).exists() or (output_dir / name).is_symlink()
        for name in files
    ):
        raise ValueError("Result output artifacts cannot overwrite existing paths")
    created = []
    try:
        for name, content in files.items():
            path = output_dir / name
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            created.append(path)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError as error:
        for path in created:
            path.unlink(missing_ok=True)
        raise ValueError(
            "Result artifacts could not be exclusively published"
        ) from error
    return metrics, bundle


def _without_protocol(value: bytes) -> str:
    """Structured output is stored once as an artifact, never repeated in logs."""
    return "\n".join(
        line
        for line in value.decode("utf-8", errors="replace").splitlines()
        if not line.startswith((RESULT_PREFIX, "TRADEGRAD_RESULT_V2:"))
    )


class DockerEvaluator:
    def __init__(
        self,
        image: str | None = None,
        *,
        timeout: float = 120,
        memory_mb: int = 1024,
        cpus: float = 1,
        pids: int = 64,
        output_bytes: int = 64 * 1024 * 1024,
        log_bytes: int = 2 * 1024 * 1024,
        grace_seconds: int = 3,
        docker_binary: str | None = None,
        state_dir: Path | None = None,
    ):
        self.image = image or os.environ.get(
            "EVALUATOR_IMAGE", "tradegrad-evaluator:v1"
        )
        self.binary = docker_binary or shutil.which("docker")
        self.timeout, self.memory_mb, self.cpus, self.pids = (
            timeout,
            memory_mb,
            cpus,
            pids,
        )
        self.output_bytes, self.log_bytes, self.grace_seconds = (
            output_bytes,
            log_bytes,
            grace_seconds,
        )
        self.wire_log_allowance = 0
        self.state_dir = (
            state_dir
            or Path(os.environ.get("TRADEGRAD_RUNNER_STATE_DIR", "var/runner"))
        ).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (
            timeout <= 0
            or memory_mb < 128
            or cpus <= 0
            or pids < 8
            or output_bytes <= 0
            or log_bytes <= 0
        ):
            raise ValueError("Invalid evaluator resource limits")
        # Do not inherit credentials, Docker registry config, proxies, or user CLI plugins.
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
        host = os.environ.get("TRADEGRAD_DOCKER_HOST", "unix:///var/run/docker.sock")
        if not host.startswith("unix:///"):
            raise ValueError("Only an explicitly local Docker socket is supported")
        # Docker otherwise falls back to passwd's home and injects ~/.docker proxy
        # configuration into create, even when HOME and DOCKER_CONFIG are absent.
        config = self.state_dir / "docker-config"
        config.mkdir(exist_ok=True, mode=0o700)
        try:
            with (config / "config.json").open("x") as handle:
                handle.write("{}")
        except FileExistsError:
            if (config / "config.json").read_text() != "{}":
                raise IsolationUnavailable("Runner Docker configuration must be empty")
        self.prefix = [self.binary, "--config", str(config), "--host", host]

    def _command(self, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
        if not self.binary:
            raise IsolationUnavailable(
                "Docker is unavailable; candidate execution is disabled"
            )
        try:
            return subprocess.run(
                [*self.prefix, *args],
                capture_output=True,
                text=True,
                env=self.env,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IsolationUnavailable(
                "Docker did not answer; sandbox state is unknown"
            ) from exc

    def _image_id(self) -> str:
        result = self._command("image", "inspect", self.image, "--format", "{{.Id}}")
        image_id = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise IsolationUnavailable(
                "Prebuilt evaluator image is unavailable; build it before starting workers"
            )
        return image_id

    def bind_environment(self, expected_digest: str) -> str:
        """Resolve a mutable tag once; configured research uses that exact image ID."""
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise ValueError("Invalid immutable environment digest")
        self._preflight()
        image_id = self._image_id()
        label = self._command(
            "image",
            "inspect",
            image_id,
            "--format",
            '{{index .Config.Labels "io.tradegrad.environment_digest"}}',
        )
        if label.returncode or label.stdout.strip() != expected_digest:
            raise IsolationUnavailable(
                "Evaluator image does not match frozen build inputs"
            )
        self.image = image_id
        from packages.tradegrad_adapter.results import MAX_WIRE_BYTES

        self.wire_log_allowance = 4 * ((MAX_WIRE_BYTES + 2) // 3) + 128
        return image_id

    def _preflight(self):
        result = self._command("info", "--format", "{{json .}}")
        try:
            info = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise IsolationUnavailable(
                "Cannot verify Docker isolation capabilities"
            ) from exc
        if not isinstance(info, dict):
            raise IsolationUnavailable("Docker isolation information must be an object")
        required = (
            "MemoryLimit",
            "SwapLimit",
            "CpuCfsPeriod",
            "CpuCfsQuota",
            "PidsLimit",
        )
        if (
            not isinstance(info, dict)
            or result.returncode
            or info.get("OSType") != "linux"
            or info.get("CgroupVersion") != "2"
            or info.get("CgroupDriver") not in {"systemd", "cgroupfs"}
            or not all(info.get(key) is True for key in required)
            or not isinstance(info.get("SecurityOptions"), list)
            or not any(
                "name=seccomp" in value
                for value in info["SecurityOptions"]
                if isinstance(value, str)
            )
        ):
            raise IsolationUnavailable(
                "Docker must enforce Linux cgroup-v2 CPU, memory, swap, PID and seccomp limits"
            )

    def available(self) -> bool:
        try:
            self._preflight()
            self._image_id()
            return True
        except IsolationUnavailable:
            return False

    @staticmethod
    def _name(attempt_id: str) -> str:
        return "tradegrad-" + _identity(attempt_id)

    @contextmanager
    def _attempt_lock(self, attempt_id: str):
        with (self.state_dir / f"{_identity(attempt_id)}.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _fence(self, attempt_id: str):
        with (self.state_dir / f"{_identity(attempt_id)}.stopped").open("w") as handle:
            handle.write(
                "Stopped attempts are never restarted. Create a new attempt UUID.\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(self.state_dir, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _inspect(self, attempt_id: str) -> dict | None:
        result = self._command("inspect", self._name(attempt_id))
        if result.returncode:
            if (
                "No such object:" in result.stderr
                or "No such container:" in result.stderr
            ):
                return None
            raise IsolationUnavailable("Cannot confirm evaluator container state")
        try:
            rows = json.loads(result.stdout)
            if (
                not isinstance(rows, list)
                or len(rows) != 1
                or not isinstance(rows[0], dict)
            ):
                raise ValueError("Docker inspect must return one container object")
            value = rows[0]
            config, state = value.get("Config"), value.get("State")
            if not isinstance(config, dict):
                raise TypeError("Docker inspect returned malformed config")
            labels = config.get("Labels")
            if not isinstance(labels, dict):
                raise TypeError("Docker inspect returned malformed labels")
            if labels.get("io.tradegrad.managed") != "evaluator-v1" or labels.get(
                "io.tradegrad.attempt_id"
            ) != _identity(attempt_id):
                raise IsolationUnavailable(
                    "Container identity does not match this execution attempt"
                )
            if not isinstance(state, dict) or type(state.get("Running")) is not bool:
                raise ValueError("Docker inspect returned malformed state")
            if "ExitCode" in state and type(state["ExitCode"]) is not int:
                raise ValueError("Docker inspect returned malformed exit code")
            return value
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise IsolationUnavailable(
                "Docker returned an invalid container state"
            ) from exc

    def stop(self, attempt_id: str) -> bool:
        """True only after Docker confirms absence. Unknown is not cancelled."""
        try:
            with self._attempt_lock(attempt_id):
                return self._fence_and_stop_locked(attempt_id)
        except OSError:
            # A full/broken state volume must not prevent best-effort containment.
            # Without a durable fence we cannot release a retry, even after stop.
            self._stop_locked(attempt_id)
            return False

    def _fence_and_stop_locked(self, attempt_id: str) -> bool:
        fenced = True
        try:
            self._fence(attempt_id)
        except OSError:
            fenced = False
        stopped = self._stop_locked(attempt_id)
        return fenced and stopped

    def _stop_locked(self, attempt_id: str) -> bool:
        try:
            state = self._inspect(attempt_id)
            if state is None:
                return True
            if state["State"]["Running"]:
                self._command(
                    "stop",
                    "--time",
                    str(self.grace_seconds),
                    self._name(attempt_id),
                    timeout=self.grace_seconds + 10,
                )
                state = self._inspect(attempt_id)
                if state is not None and state["State"]["Running"]:
                    self._command("kill", self._name(attempt_id))
                    state = self._inspect(attempt_id)
            # A timed-out start CLI may leave a daemon-side start request in flight.
            # Removal fences that request; a merely 'created' snapshot does not.
            if state is not None:
                self._command("rm", "--force", self._name(attempt_id))
            return self._inspect(attempt_id) is None
        except (IsolationUnavailable, KeyError):
            return False

    def _create_command(
        self,
        image_id: str,
        input_dir: Path,
        output_dir: Path,
        run_id: str,
        attempt_id: str,
    ) -> list[str]:
        uid = os.getuid()
        if uid == 0:
            raise IsolationUnavailable("The trusted runner must run as a non-root user")
        return [
            "create",
            "--pull=never",
            "--name",
            self._name(attempt_id),
            "--label",
            "io.tradegrad.managed=evaluator-v1",
            "--label",
            f"io.tradegrad.run_id={_identity(run_id)}",
            "--label",
            f"io.tradegrad.attempt_id={_identity(attempt_id)}",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{uid}:{os.getgid()}",
            "--cpus",
            str(self.cpus),
            "--memory",
            f"{self.memory_mb}m",
            "--memory-swap",
            f"{self.memory_mb}m",
            "--pids-limit",
            str(self.pids),
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "core=0:0",
            "--ulimit",
            f"fsize={self.output_bytes}:{self.output_bytes}",
            "--log-driver=json-file",
            f"--log-opt=max-size={self.log_bytes + self.wire_log_allowance}",
            "--log-opt=max-file=1",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864,nr_inodes=4096,mode=1777",
            "--mount",
            f"type=bind,src={input_dir},dst=/input,readonly",
            "--tmpfs",
            f"/output:rw,noexec,nosuid,nodev,size={self.output_bytes},nr_inodes=1024,mode=0700,uid={uid},gid={os.getgid()}",
            "--env",
            "PYTHONHASHSEED=0",
            "--env",
            "OPENBLAS_NUM_THREADS=1",
            "--env",
            "OMP_NUM_THREADS=1",
            "--env",
            "POLARS_MAX_THREADS=1",
            "--env",
            "HOME=/tmp",
            "--env",
            "XDG_CACHE_HOME=/tmp/cache",
            "--env",
            "TRADEGRAD_DATA_WORKERS=1",
            "--env",
            "AKQUANT_DATA_CACHE=/tmp/cache",
            image_id,
        ]

    def evaluate(
        self,
        input_dir: Path,
        output_dir: Path,
        run_id: str,
        attempt_id: str,
        cancelled: Callable[[], bool] = lambda: False,
        heartbeat: Callable[[], None] = lambda: None,
        expected_config_sha256: str | None = None,
    ) -> dict:
        started = time.monotonic()
        deadline = started + self.timeout

        def launch_timeout():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsolationUnavailable(
                    "Evaluation runtime budget expired before sandbox launch"
                )
            return min(20.0, remaining)

        run_id, attempt_id = _identity(run_id), _identity(attempt_id)
        if input_dir.is_symlink() or output_dir.is_symlink():
            raise ValueError("Evaluation paths cannot be symlinks")
        input_dir, output_dir = input_dir.resolve(), output_dir.resolve()
        if any(char in str(input_dir) for char in (",", "\n", "\r", "\x00")):
            raise ValueError("Input path contains unsupported mount syntax")
        if (
            input_dir == output_dir
            or input_dir in output_dir.parents
            or output_dir in input_dir.parents
        ):
            raise ValueError("Inputs and outputs must be separate directories")
        _regular_tree(input_dir, maximum_bytes=512 * 1024 * 1024)
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(output_dir.iterdir()):
            raise ValueError("Refusing to reuse a nonempty evaluation output namespace")
        expected_identity = None
        capture_allowance = 0
        if expected_config_sha256 is not None:
            from packages.tradegrad_adapter.results import (
                MAX_WIRE_BYTES,
                artifact_inventory,
                canonical_json_bytes,
                sha256_bytes,
            )

            request_path = input_dir / "request.json"
            if request_path.stat().st_size > 65536:
                raise ValueError("Oversized configured evaluator request")
            request = json.loads(request_path.read_bytes())
            config = request["evaluation_config"]
            if config["run_config_sha256"] != expected_config_sha256:
                raise ValueError("Configured evaluator input hash mismatch")
            inputs = artifact_inventory(input_dir)
            expected_identity = {
                "strategy_sha256": sha256_bytes(
                    (input_dir / "strategy.py").read_bytes()
                ),
                "run_config_sha256": expected_config_sha256,
                "evaluation_config_sha256": sha256_bytes(canonical_json_bytes(config)),
                "input_manifest_sha256": sha256_bytes(canonical_json_bytes(inputs)),
                "inputs": inputs,
                "task": request["task"],
                "market": request["market"],
                "profile": request["profile"],
            }
            capture_allowance = 4 * ((MAX_WIRE_BYTES + 2) // 3) + 128
            self.wire_log_allowance = capture_allowance
        image_id = self._image_id()
        self._preflight()
        # Callbacks can acquire DB row locks. Never call one while holding flock,
        # because reconciliation holds its row lock while invoking stop().
        was_cancelled = cancelled()
        with self._attempt_lock(attempt_id):
            if (self.state_dir / f"{attempt_id}.stopped").exists():
                raise IsolationUnavailable(
                    "Attempt is permanently fenced; use a new attempt UUID"
                )
            if self._inspect(attempt_id) is not None:
                raise IsolationUnavailable(
                    "An execution environment already exists for this attempt"
                )
            if was_cancelled:
                self._fence(attempt_id)
                return {
                    "status": "cancelled",
                    "exit_code": None,
                    "error": None,
                    "container_id": None,
                    "metrics": None,
                    "logs": {},
                }
            try:
                result = self._command(
                    *self._create_command(
                        image_id, input_dir, output_dir, run_id, attempt_id
                    ),
                    timeout=launch_timeout(),
                )
                if result.returncode or not re.fullmatch(
                    r"[0-9a-f]{64}", result.stdout.strip()
                ):
                    raise IsolationUnavailable(
                        "Failed to create isolated evaluator; no unsafe fallback was used"
                    )
                container_id = result.stdout.strip()
                # Synchronous start under the same lock prevents a delayed start after stop().
                started_result = self._command(
                    "start", container_id, timeout=launch_timeout()
                )
                if started_result.returncode:
                    raise IsolationUnavailable("Failed to start isolated evaluator")
            except IsolationUnavailable:
                if not self._fence_and_stop_locked(attempt_id):
                    return {
                        "status": "interrupted",
                        "error": "Container start outcome is unknown",
                        "exit_code": None,
                        "container_id": self._name(attempt_id),
                        "metrics": None,
                        "logs": {},
                    }
                raise
        logs = {"stdout": bytearray(), "stderr": bytearray()}
        exceeded = threading.Event()
        process = None
        threads: list[threading.Thread] = []
        error = None
        status = "failed"
        metrics = None
        bundle = None
        exit_code = None
        watchdog_done = threading.Event()
        watchdog_reason = []

        def enforce_deadline():
            # DB callbacks may block. Resource enforcement must never depend on
            # their responsiveness or on the coordinator's heartbeat thread.
            while not watchdog_done.wait(0.05):
                if exceeded.is_set() or time.monotonic() - started >= self.timeout:
                    watchdog_reason.append(
                        "Evaluation exceeded its log budget"
                        if exceeded.is_set()
                        else "Evaluation exceeded its wall-clock budget"
                    )
                    self.stop(attempt_id)
                    return

        watchdog = None
        watchdog_started = False

        def capture(pipe, name):
            try:
                while chunk := pipe.read(8192):
                    remaining = (
                        self.log_bytes
                        + (capture_allowance if name == "stdout" else 0)
                        - len(logs[name])
                    )
                    logs[name].extend(chunk[: max(0, remaining)])
                    if len(chunk) > remaining:
                        exceeded.set()
            finally:
                pipe.close()

        try:
            watchdog = threading.Thread(target=enforce_deadline, daemon=True)
            watchdog.start()
            watchdog_started = True
            process = subprocess.Popen(
                [*self.prefix, "logs", "--follow", container_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
            )
            for pipe, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                thread = threading.Thread(
                    target=capture, args=(pipe, name), daemon=True
                )
                thread.start()
                threads.append(thread)
            last_heartbeat = 0.0
            while process.poll() is None:
                now = time.monotonic()
                if now - last_heartbeat >= 1:
                    heartbeat()
                    last_heartbeat = now
                if cancelled():
                    status, error = "cancelled", "Cancellation requested"
                    break
                if now - started > self.timeout:
                    error = "Evaluation exceeded its wall-clock budget"
                    break
                if exceeded.is_set():
                    error = "Evaluation exceeded its log budget"
                    break
                _regular_tree(output_dir, maximum_bytes=self.output_bytes)
                time.sleep(0.1)
            if error:
                if not self.stop(attempt_id):
                    status = "interrupted"
                    error += "; container termination is unconfirmed"
            else:
                for thread in threads:
                    thread.join(timeout=5)
                if (
                    any(thread.is_alive() for thread in threads)
                    or process.returncode != 0
                ):
                    raise IsolationUnavailable(
                        "Evaluator log capture did not finish successfully"
                    )
                state = self._inspect(attempt_id)
                if state is None or state["State"]["Running"]:
                    raise IsolationUnavailable("Container exit is not confirmed")
                exit_code = state["State"]["ExitCode"]
                _regular_tree(output_dir, maximum_bytes=self.output_bytes)
                if exceeded.is_set():
                    error = "Evaluation exceeded its log budget"
                elif state["State"].get("OOMKilled"):
                    error = "Evaluation exceeded its memory budget"
                elif exit_code != 0:
                    error = f"Evaluation exited with code {exit_code}"
                else:
                    if (
                        len(_without_protocol(bytes(logs["stdout"])).encode())
                        > self.log_bytes
                    ):
                        raise ValueError("Evaluation exceeded its ordinary log budget")
                    metrics, bundle = decode_result_protocol(
                        bytes(logs["stdout"]),
                        output_dir,
                        expected_config_sha256=expected_config_sha256,
                        expected_identity=expected_identity,
                    )
                    status = "succeeded"
        except (
            OSError,
            ValueError,
            KeyError,
            IsolationUnavailable,
            RuntimeError,
        ) as exc:
            error = str(exc)
            status = "failed" if self.stop(attempt_id) else "interrupted"
        finally:
            watchdog_done.set()
            if watchdog_started:
                watchdog.join(timeout=self.grace_seconds + 35)
            stopped = self.stop(attempt_id)
            if watchdog_reason:
                status, error = "failed", watchdog_reason[0]
            if not stopped:
                status, error = (
                    "interrupted",
                    "Evaluator termination could not be confirmed; retry is blocked",
                )
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()  # This kills only the attach CLI; the state above remains authoritative.
                    process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)
        return {
            "status": status,
            "exit_code": exit_code,
            "error": error,
            "container_id": container_id,
            "image_digest": image_id,
            "metrics": metrics if status == "succeeded" else None,
            "bundle": bundle if status == "succeeded" else None,
            "logs": {
                key: _without_protocol(bytes(value)) for key, value in logs.items()
            },
            "duration_seconds": time.monotonic() - started,
            "evaluation_mode": "native_akquant",
            "result_trust": "native_strategy_and_engine_share_process",
        }
