"""Two-container portable supervisor. Candidate Python is never imported here.

The caller must durably register the physical evaluation group before run().
Only the reviewed engine owns accounting. The policy owns decisions and bounded
state. Neither container mounts the other's files or a shared writable volume.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from packages.strategy_sdk.contracts import (
    parse_manifest,
    parse_observation,
    parse_reply,
)
from packages.strategy_sdk.json import canonical_json, hash_json, parse_json
from runtimes.evaluator.runner import (
    DockerEvaluator,
    IsolationUnavailable,
    _identity,
    _regular_tree,
)
from runtimes.portable_policy.protocol import (
    MAX_DECISIONS,
    MAX_OBSERVATION_BYTES,
    MAX_REPLY_BYTES,
    MAX_RESULT_BYTES,
    decision_envelope,
    decode,
    frame,
    validate_request,
    validate_response,
    validate_result_frame,
)

ROLES = ("engine", "policy")
MANAGED = "portable-evaluator-v1"


def safe_policy_manifest(manifest):
    """Remove future-dependent dataset/config fingerprints from policy visibility.

    A code/parameter identity and declared semantic assumptions are safe; hashes
    of a full future dataset or its universe are not an as-of observation.
    """
    value = (
        manifest.model_dump(mode="json")
        if hasattr(manifest, "model_dump")
        else json.loads(canonical_json(manifest))
    )
    value["universe"]["snapshot_sha256"] = "0" * 64
    value["prices"]["actions_snapshot_sha256"] = "0" * 64
    value["engine_profile"]["profile_sha256"] = "0" * 64
    value["engine_profile"]["environment_sha256"] = "0" * 64
    value["reference_result_sha256"] = None
    return value


def _safe_root(root: Path, limit: int):
    root = Path(root)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Portable input cannot traverse a symlink")
    root = root.resolve()
    if any(c in str(root) for c in (",", "\r", "\n", "\x00")):
        raise ValueError("Invalid portable mount path")
    _regular_tree(root, maximum_bytes=limit, maximum_files=4)
    return root


def validate_inputs(engine_root, policy_root, expected_config_sha256=None):
    engine_root = _safe_root(engine_root, 36 * 1024 * 1024)
    policy_root = _safe_root(policy_root, 1024 * 1024)
    if (
        engine_root == policy_root
        or engine_root in policy_root.parents
        or policy_root in engine_root.parents
    ):
        raise ValueError("Engine and policy mounts must be disjoint")
    if {p.name for p in engine_root.iterdir()} != {"request.json", "market.json"}:
        raise ValueError(
            "Engine mount accepts only request.json and market.json; never code"
        )
    if {p.name for p in policy_root.iterdir()} != {
        "policy.py",
        "parameters.json",
        "manifest.json",
    }:
        raise ValueError(
            "Policy mount accepts only code, parameters and a safe manifest"
        )
    if any(
        not p.is_file() for root in (engine_root, policy_root) for p in root.iterdir()
    ):
        raise ValueError("Portable roots must contain regular files only")
    if (engine_root / "request.json").stat().st_size > MAX_OBSERVATION_BYTES:
        raise ValueError("Engine request exceeds its byte budget")
    request = parse_json((engine_root / "request.json").read_bytes())
    if (
        not isinstance(request, dict)
        or set(request)
        != {
            "schema_version",
            "manifest",
            "window",
            "initial_cash",
            "initial_state",
            "input_identity",
        }
        or request["schema_version"] != "portable-engine-request.v1"
    ):
        raise ValueError("Invalid portable engine request")
    manifest = parse_manifest(canonical_json(request["manifest"]))
    safe_manifest = parse_manifest((policy_root / "manifest.json").read_bytes())
    if safe_manifest.model_dump(mode="json") != safe_policy_manifest(manifest):
        raise ValueError(
            "Policy manifest contains mismatched or future-dependent fields"
        )
    source_hash = hashlib.sha256((policy_root / "policy.py").read_bytes()).hexdigest()
    params = parse_json((policy_root / "parameters.json").read_bytes())
    if (
        not isinstance(params, dict)
        or source_hash != manifest.code_sha256
        or hash_json(params) != manifest.parameters_sha256
    ):
        raise ValueError("Portable code or parameters mismatch")
    identity = request["input_identity"]
    if (
        not isinstance(identity, dict)
        or set(identity) != {"run_config_sha256", "market_data_sha256"}
        or any(
            not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
            for v in identity.values()
        )
    ):
        raise ValueError("Invalid portable input identity")
    if (
        expected_config_sha256 is not None
        and identity["run_config_sha256"] != expected_config_sha256
    ):
        raise ValueError("Portable immutable config mismatch")
    if (
        hashlib.sha256((engine_root / "market.json").read_bytes()).hexdigest()
        != identity["market_data_sha256"]
    ):
        raise ValueError("Portable market input mismatch")
    return engine_root, policy_root, request, manifest


class PortableDockerEvaluator(DockerEvaluator):
    def __init__(
        self, engine_image=None, policy_image=None, *, decision_timeout=10.0, **kwargs
    ):
        super().__init__(
            image=engine_image
            or os.getenv("PORTABLE_ENGINE_IMAGE", "tradegrad-portable-engine:v1"),
            **kwargs,
        )
        self.images = {
            "engine": self.image,
            "policy": policy_image
            or os.getenv("PORTABLE_POLICY_IMAGE", "tradegrad-portable-policy:v1"),
        }
        if decision_timeout <= 0 or decision_timeout > self.timeout:
            raise ValueError("Invalid portable decision deadline")
        self.decision_timeout = decision_timeout
        self._attachments = {}
        self._attachment_lock = threading.Lock()

    @staticmethod
    def container_name(evaluation_id, role):
        if role not in ROLES:
            raise ValueError("Invalid portable container role")
        return f"tradegrad-portable-{_identity(evaluation_id)}-{role}"

    def bind_environment(self, expected_digest):
        if not isinstance(expected_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_digest
        ):
            raise ValueError("Invalid immutable portable environment digest")
        self._preflight()
        pinned = {}
        for role in ROLES:
            result = self._command(
                "image", "inspect", self.images[role], "--format", "{{.Id}}"
            )
            image_id = result.stdout.strip()
            if result.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise IsolationUnavailable(
                    f"Prebuilt portable {role} image is unavailable; no pull or host fallback"
                )
            labels = self._command(
                "image", "inspect", image_id, "--format", "{{json .Config.Labels}}"
            )
            try:
                value = json.loads(labels.stdout)
            except (ValueError, TypeError) as exc:
                raise IsolationUnavailable(
                    "Cannot verify portable image labels"
                ) from exc
            if (
                labels.returncode
                or not isinstance(value, dict)
                or value.get("io.tradegrad.environment_digest") != expected_digest
                or value.get("io.tradegrad.portable_role") != role
            ):
                raise IsolationUnavailable(
                    f"Portable {role} image does not match frozen build inputs and role"
                )
            pinned[role] = image_id
        self.images = pinned
        return dict(pinned)

    def available(self):
        try:
            self._preflight()
            for role in ROLES:
                result = self._command(
                    "image", "inspect", self.images[role], "--format", "{{.Id}}"
                )
                if result.returncode or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", result.stdout.strip()
                ):
                    return False
            return True
        except IsolationUnavailable:
            return False

    def _inspect_role(self, evaluation_id, role, *, attempt_id=None):
        result = self._command("inspect", self.container_name(evaluation_id, role))
        if result.returncode:
            if (
                "No such object:" in result.stderr
                or "No such container:" in result.stderr
            ):
                return None
            raise IsolationUnavailable("Cannot confirm portable sibling state")
        try:
            rows = json.loads(result.stdout)
            if (
                not isinstance(rows, list)
                or len(rows) != 1
                or not isinstance(rows[0], dict)
            ):
                raise ValueError("Expected one container")
            value = rows[0]
            labels = value["Config"]["Labels"]
            if (
                labels.get("io.tradegrad.managed") != MANAGED
                or labels.get("io.tradegrad.evaluation_id") != _identity(evaluation_id)
                or labels.get("io.tradegrad.role") != role
                or (
                    attempt_id is not None
                    and labels.get("io.tradegrad.attempt_id") != _identity(attempt_id)
                )
            ):
                raise ValueError("Portable container identity mismatch")
            _identity(labels["io.tradegrad.attempt_id"])
            _identity(labels["io.tradegrad.run_id"])
            if type(value["State"]["Running"]) is not bool or (
                "ExitCode" in value["State"]
                and type(value["State"]["ExitCode"]) is not int
            ):
                raise ValueError("Invalid Docker state")
            return value
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise IsolationUnavailable(
                "Portable sibling identity or state unverified"
            ) from exc

    def _create_role_command(self, role, root, run_id, attempt_id, evaluation_id):
        uid = os.getuid()
        if uid == 0:
            raise IsolationUnavailable("Trusted runner must be non-root")
        return [
            "create",
            "--pull=never",
            "--interactive",
            "--name",
            self.container_name(evaluation_id, role),
            "--label",
            f"io.tradegrad.managed={MANAGED}",
            "--label",
            f"io.tradegrad.run_id={_identity(run_id)}",
            "--label",
            f"io.tradegrad.attempt_id={_identity(attempt_id)}",
            "--label",
            f"io.tradegrad.evaluation_id={_identity(evaluation_id)}",
            "--label",
            f"io.tradegrad.role={role}",
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
            "--log-driver=none",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864,nr_inodes=4096,mode=1777",
            "--mount",
            f"type=bind,src={root},dst=/input,readonly",
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
            "AKQUANT_DATA_CACHE=/tmp/cache",
            "--env",
            "TRADEGRAD_DATA_WORKERS=1",
            "--env",
            f"TRADEGRAD_EVALUATION_ID={_identity(evaluation_id)}",
            self.images[role],
        ]

    def _verify_role_configuration(self, evaluation_id, role, root, attempt_id):
        value = self._inspect_role(evaluation_id, role, attempt_id=attempt_id)
        if value is None:
            raise IsolationUnavailable("Created portable sibling disappeared")
        host, config = value.get("HostConfig", {}), value.get("Config", {})
        mounts = value.get("Mounts")
        if (
            value.get("Image") != self.images[role]
            or config.get("User") != f"{os.getuid()}:{os.getgid()}"
            or host.get("ReadonlyRootfs") is not True
            or host.get("NetworkMode") != "none"
            or host.get("Privileged") is not False
            or host.get("CapDrop") != ["ALL"]
            or "no-new-privileges" not in host.get("SecurityOpt", [])
            or host.get("PidMode") not in ("", None)
            or host.get("Devices") not in ([], None)
            or host.get("DeviceRequests") not in ([], None)
            or host.get("Memory") != self.memory_mb * 1024 * 1024
            or host.get("MemorySwap") != self.memory_mb * 1024 * 1024
            or host.get("PidsLimit") != self.pids
            or host.get("NanoCpus") != int(self.cpus * 1_000_000_000)
            or not isinstance(mounts, list)
        ):
            raise IsolationUnavailable(
                "Portable sibling isolation configuration mismatch"
            )
        inputs = [item for item in mounts if item.get("Destination") == "/input"]
        if (
            len(inputs) != 1
            or inputs[0].get("Type") != "bind"
            or inputs[0].get("Source") != str(root)
            or inputs[0].get("RW") is not False
        ):
            raise IsolationUnavailable("Portable sibling input mount mismatch")
        if any(
            item.get("Destination") != "/input"
            and not (item.get("Destination") == "/tmp" and item.get("Type") == "tmpfs")
            for item in mounts
        ):
            raise IsolationUnavailable("Unexpected portable sibling mount")

    def _attachment_exitcode(self, process):
        """Observe an owned child without releasing its PID/group identity."""
        with self._attachment_lock:
            if process.returncode is not None:
                return process.returncode
            try:
                status = os.waitid(
                    os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
            except ChildProcessError:
                raise IsolationUnavailable(
                    "Portable attach CLI identity no longer reserved"
                ) from None
            if status is None:
                return None
            return (
                status.si_status
                if status.si_code == os.CLD_EXITED
                else -status.si_status
            )

    def _kill_attachments(self, evaluation_id):
        # Serialize observation, signalling and reap across controller/watchdog
        # stop calls. A reaped PID is NEVER a safe signal target.
        stopped = True
        with self._attachment_lock:
            for process in self._attachments.get(evaluation_id, ()):
                deadline = time.monotonic() + 3
                if process.returncode is None:
                    try:
                        # WNOWAIT reserves even an exited leader while its
                        # descendants may still hold the protocol pipes.
                        os.waitid(
                            os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                        )
                    except (ChildProcessError, OSError):
                        stopped = False
                        continue
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        stopped = False
                    try:
                        process.wait(timeout=max(0, deadline - time.monotonic()))
                    except (OSError, subprocess.SubprocessError):
                        stopped = False
                        continue
                # After reap only an absence probe is safe. An uncertain group
                # blocks retry; never signal a possibly reused group identifier.
                while True:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    except OSError:
                        stopped = False
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        stopped = False
                        break
                    time.sleep(min(remaining, 0.05))
        return stopped

    def _stop_role(self, evaluation_id, role):
        try:
            value = self._inspect_role(evaluation_id, role)
            if value is None:
                return True
            # rm --force kills the entire container cgroup and fences delayed
            # daemon start operations, including partially-created siblings.
            self._command(
                "rm",
                "--force",
                self.container_name(evaluation_id, role),
                timeout=self.grace_seconds + 10,
            )
            return self._inspect_role(evaluation_id, role) is None
        except Exception:  # noqa: BLE001 - containment must attempt the other sibling after any failure
            return False

    def stop(self, evaluation_id):
        evaluation_id = _identity(evaluation_id)
        results = []
        fenced = False
        try:
            with self._attempt_lock(evaluation_id):
                try:
                    self._fence(evaluation_id)
                    fenced = True
                except OSError:
                    pass
                for role in ROLES:
                    results.append(self._stop_role(evaluation_id, role))
        except OSError:
            # Never let failed durable state prevent best-effort both-role stop.
            results = [self._stop_role(evaluation_id, role) for role in ROLES]
        cli_stopped = self._kill_attachments(evaluation_id)
        return fenced and all(results) and len(results) == 2 and cli_stopped

    def children(self, attempt_id):
        """Enumerate registered local groups AND daemon-discovered siblings."""
        attempt_id = _identity(attempt_id)
        identities = set()
        for path in self.state_dir.glob("*.portable-group.json"):
            try:
                value = json.loads(path.read_bytes())
                if value["attempt_id"] == attempt_id:
                    identities.add(_identity(value["evaluation_id"]))
            except (ValueError, KeyError, OSError) as exc:
                raise IsolationUnavailable(
                    "Invalid local portable group registry"
                ) from exc
        result = self._command(
            "ps",
            "--all",
            "--filter",
            f"label=io.tradegrad.managed={MANAGED}",
            "--filter",
            f"label=io.tradegrad.attempt_id={attempt_id}",
            "--format",
            '{{.Label "io.tradegrad.evaluation_id"}}',
        )
        if result.returncode:
            raise IsolationUnavailable("Cannot enumerate portable siblings")
        for line in result.stdout.splitlines():
            identities.add(_identity(line.strip()))
        for identity in identities:
            for role in ROLES:
                self._inspect_role(identity, role, attempt_id=attempt_id)
        return sorted(identities)

    def stop_attempt(self, attempt_id):
        try:
            ids = self.children(attempt_id)
        except (IsolationUnavailable, ValueError):
            # Even failed enumeration must stop all known local groups.
            ids = []
            for path in self.state_dir.glob("*.portable-group.json"):
                try:
                    value = json.loads(path.read_bytes())
                    if value["attempt_id"] == _identity(attempt_id):
                        ids.append(_identity(value["evaluation_id"]))
                except (ValueError, KeyError, OSError):
                    continue
            for identity in ids:
                self.stop(identity)
            return False
        results = [self.stop(identity) for identity in ids]
        return all(results)

    def _launch(self, evaluation_id, role):
        process = subprocess.Popen(
            [
                *self.prefix,
                "start",
                "--attach",
                "--interactive",
                self.container_name(evaluation_id, role),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            start_new_session=True,
            bufsize=0,
        )
        with self._attachment_lock:
            self._attachments.setdefault(evaluation_id, []).append(process)
        return process

    def run(
        self,
        evaluation_id,
        engine_input_dir,
        policy_input_dir,
        output_dir,
        *,
        run_id,
        attempt_id,
        cancelled: Callable[[], bool] = lambda: False,
        heartbeat: Callable[[], None] = lambda: None,
        expected_config_sha256=None,
    ):
        """Evaluate a previously registered physical group, then confirm both absent.

        Calls are bounded by an independent watchdog, including blocked DB
        callbacks and container stdin/stdout. Unknown teardown means interrupted.
        No successful output is persisted until BOTH siblings are removed.
        """
        started = time.monotonic()
        deadline = started + self.timeout
        evaluation_id, run_id, attempt_id = map(
            _identity, (evaluation_id, run_id, attempt_id)
        )
        engine_root, policy_root, request, manifest = validate_inputs(
            engine_input_dir, policy_input_dir, expected_config_sha256
        )
        output_dir = Path(output_dir)
        if any(p.is_symlink() for p in (output_dir, *output_dir.parents)):
            raise ValueError("Portable output cannot traverse symlinks")
        output_dir = output_dir.resolve()
        for root in (engine_root, policy_root):
            if (
                output_dir == root
                or output_dir in root.parents
                or root in output_dir.parents
            ):
                raise ValueError(
                    "Portable output must be disjoint from both input roots"
                )
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(output_dir.iterdir()):
            raise ValueError("Portable output namespace must be empty")
        self.bind_environment(manifest.engine_profile.environment_sha256)
        processes = {}
        created_ids = {}
        status, error, bundle = "failed", None, None
        logs = {f"{role}_stderr": bytearray() for role in ROLES}
        done, abort = threading.Event(), threading.Event()
        callback_error = []
        callback_started = [None]

        def control():
            try:
                while not done.is_set():
                    callback_started[0] = time.monotonic()
                    if cancelled():
                        callback_error.append("Cancellation requested")
                        abort.set()
                        return
                    heartbeat()
                    callback_started[0] = None
                    done.wait(0.25)
            except Exception as exc:  # noqa: BLE001 - arbitrary external lease callback must fail closed
                callback_error.append(
                    f"Evaluation lease/callback failed: {type(exc).__name__}"
                )
                abort.set()

        def watchdog():
            while not done.wait(0.05):
                if time.monotonic() >= deadline or abort.is_set():
                    self.stop(evaluation_id)
                    return

        watch = threading.Thread(target=watchdog, daemon=True)
        controller = threading.Thread(target=control, daemon=True)
        try:
            with self._attempt_lock(evaluation_id):
                if (self.state_dir / f"{evaluation_id}.stopped").exists():
                    raise IsolationUnavailable(
                        "Physical evaluation is fenced; use a new UUID"
                    )
                for role in ROLES:
                    if self._inspect_role(evaluation_id, role) is not None:
                        raise IsolationUnavailable(
                            "Portable physical group already exists"
                        )
                registry = self.state_dir / f"{evaluation_id}.portable-group.json"
                with registry.open("x") as stream:
                    json.dump(
                        {
                            "evaluation_id": evaluation_id,
                            "run_id": run_id,
                            "attempt_id": attempt_id,
                        },
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                watch.start()
                controller.start()
                for role, root in (("policy", policy_root), ("engine", engine_root)):
                    if abort.is_set() or time.monotonic() >= deadline:
                        raise IsolationUnavailable(
                            "Portable launch cancelled or expired"
                        )
                    remaining = max(0.01, min(10.0, deadline - time.monotonic()))
                    created = self._command(
                        *self._create_role_command(
                            role, root, run_id, attempt_id, evaluation_id
                        ),
                        timeout=remaining,
                    )
                    if created.returncode or not re.fullmatch(
                        r"[0-9a-f]{64}", created.stdout.strip()
                    ):
                        raise IsolationUnavailable(
                            f"Portable {role} create failed or uncertain"
                        )
                    created_ids[role] = created.stdout.strip()
                    self._verify_role_configuration(
                        evaluation_id, role, root, attempt_id
                    )
                for role in ("policy", "engine"):
                    processes[role] = self._launch(evaluation_id, role)
            bundle = self._relay(
                processes,
                evaluation_id,
                manifest,
                request,
                logs,
                deadline,
                abort,
                callback_started,
            )
            for role in ROLES:
                state = self._inspect_role(evaluation_id, role, attempt_id=attempt_id)
                if (
                    state is None
                    or state["State"]["Running"]
                    or state["State"].get("ExitCode") != 0
                    or state["State"].get("OOMKilled")
                ):
                    raise IsolationUnavailable(
                        f"Portable {role} clean exit unconfirmed"
                    )
            status = "succeeded"
        except (OSError, ValueError, RuntimeError, EOFError) as exc:
            error = str(exc)
            status = (
                "cancelled"
                if callback_error == ["Cancellation requested"]
                else "failed"
            )
        finally:
            done.set()
            stopped = self.stop(evaluation_id)
            if watch.ident is not None:
                watch.join(timeout=2 * (self.grace_seconds + 25))
                if watch.is_alive():
                    stopped = False
            if not stopped:
                status, error = (
                    "interrupted",
                    "Portable sibling termination unconfirmed; retry is blocked",
                )
            for process in processes.values():
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            with self._attachment_lock:
                self._attachments.pop(evaluation_id, None)
        if status == "succeeded":
            from packages.tradegrad_adapter.portable_results import canonical_json_bytes

            data = canonical_json_bytes(bundle)
            fd = os.open(
                output_dir / "portable-result.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        return {
            "status": status,
            "error": error,
            "bundle": bundle if status == "succeeded" else None,
            "metrics": bundle["summary"] if status == "succeeded" else None,
            "evaluation_id": evaluation_id,
            "container_ids": dict(created_ids),
            "container_names": {
                role: self.container_name(evaluation_id, role) for role in ROLES
            },
            "image_digests": dict(self.images),
            "duration_seconds": time.monotonic() - started,
            "logs": {
                key: bytes(value).decode("utf-8", errors="replace")
                for key, value in logs.items()
            },
            "evaluation_mode": "portable",
            "result_trust": "isolated_policy_trusted_akquant_engine"
            if status == "succeeded"
            else None,
            "ledger_origin": "trusted_engine" if status == "succeeded" else None,
            "runtime_certified": False,
        }

    def _relay(
        self,
        processes,
        evaluation_id,
        manifest,
        request,
        logs,
        deadline,
        abort,
        callback_started,
    ):
        from packages.tradegrad_adapter.portable_results import validate_portable_result

        selector = selectors.DefaultSelector()
        buffers = {role: bytearray() for role in ROLES}
        writes = {role: bytearray() for role in ROLES}
        eof = set()
        outstanding = None
        sequence = 0
        expected_state = request["initial_state"]
        phase_deadline = min(deadline, time.monotonic() + self.decision_timeout)
        bundle = None
        try:
            for role, process in processes.items():
                for name in ("stdin", "stdout", "stderr"):
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    if name != "stdin":
                        selector.register(stream, selectors.EVENT_READ, (role, name))

            def enqueue(role, value, maximum):
                if writes[role]:
                    raise ValueError("Unconsumed portable write")
                writes[role].extend(frame(value, maximum))
                selector.register(
                    processes[role].stdin, selectors.EVENT_WRITE, (role, "stdin")
                )

            while True:
                now = time.monotonic()
                if abort.is_set():
                    raise IsolationUnavailable(
                        "Portable evaluation cancelled or lease lost"
                    )
                if now >= deadline or now >= phase_deadline:
                    raise IsolationUnavailable(
                        "Portable evaluation or decision deadline exceeded"
                    )
                if (
                    callback_started[0] is not None
                    and now - callback_started[0] > self.decision_timeout
                ):
                    raise IsolationUnavailable(
                        "Portable lease/cancellation callback blocked"
                    )
                exits = [self._attachment_exitcode(p) for p in processes.values()]
                if all(code is not None for code in exits) and len(eof) == 4:
                    if (
                        bundle is None
                        or any(buffers.values())
                        or any(writes.values())
                        or outstanding is not None
                    ):
                        raise ValueError(
                            "Portable channels ended without exactly one complete trusted result"
                        )
                    if any(code != 0 for code in exits):
                        raise IsolationUnavailable("Portable attach CLI failed")
                    return bundle
                for key, _ in selector.select(
                    timeout=min(0.05, max(0.0, phase_deadline - now))
                ):
                    role, name = key.data
                    stream = key.fileobj
                    if name == "stdin":
                        try:
                            written = os.write(stream.fileno(), writes[role])
                        except BlockingIOError:
                            continue
                        if written <= 0:
                            raise EOFError("Portable channel refused input")
                        del writes[role][:written]
                        if not writes[role]:
                            selector.unregister(stream)
                        continue
                    try:
                        data = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(stream)
                        eof.add((role, name))
                        if name == "stdout" and (buffers[role] or bundle is None):
                            raise EOFError(
                                "Portable channel ended before trusted result"
                            )
                        continue
                    if name == "stderr":
                        target = logs[f"{role}_stderr"]
                        if len(target) + len(data) > self.log_bytes:
                            raise ValueError("Portable log budget exceeded")
                        target.extend(data)
                        continue
                    if bundle is not None or (role == "policy" and outstanding is None):
                        raise ValueError("Unsolicited or trailing portable output")
                    buffers[role].extend(data)
                    maximum = MAX_REPLY_BYTES if role == "policy" else MAX_RESULT_BYTES
                    while len(buffers[role]) >= 4:
                        size = struct.unpack("!I", buffers[role][:4])[0]
                        if not 1 <= size <= maximum:
                            raise ValueError("Portable frame length exceeds budget")
                        if len(buffers[role]) < size + 4:
                            break
                        value = decode(bytes(buffers[role][4 : 4 + size]), maximum)
                        del buffers[role][: 4 + size]
                        if bundle is not None:
                            raise ValueError("Trailing trusted result output")
                        if role == "engine":
                            if outstanding is not None:
                                raise ValueError(
                                    "Engine sent a second outstanding observation"
                                )
                            if value.get("kind") == "result":
                                bundle = validate_portable_result(
                                    validate_result_frame(
                                        value, evaluation_id, sequence
                                    )
                                )
                                expected_identity = {
                                    "strategy_sha256": manifest.code_sha256,
                                    "parameters_sha256": manifest.parameters_sha256,
                                    "manifest_sha256": hash_json(manifest),
                                    **request["input_identity"],
                                }
                                if (
                                    bundle["identity"] != expected_identity
                                    or bundle["final_state"] != expected_state
                                    or bundle["manifest"]
                                    != manifest.model_dump(mode="json")
                                    or bundle["window"]
                                    != {**request["window"], "interval": "half_open"}
                                    or bundle["summary"]["initial_cash"]
                                    != request["initial_cash"]
                                    or len(bundle["decision_trace"]) != sequence
                                ):
                                    raise ValueError(
                                        "Trusted portable result input or final-state identity mismatch"
                                    )
                                if writes["policy"]:
                                    raise ValueError(
                                        "Result arrived before policy consumed observation"
                                    )
                                processes["policy"].stdin.close()
                                phase_deadline = min(
                                    deadline, time.monotonic() + self.decision_timeout
                                )
                            else:
                                validate_request(value, evaluation_id, sequence)
                                if (
                                    sequence >= MAX_DECISIONS
                                    or value["observation"]["state"] != expected_state
                                ):
                                    raise ValueError(
                                        "Portable state continuity or decision budget violated"
                                    )
                                observation = parse_observation(
                                    canonical_json(value["observation"]), manifest
                                )
                                outstanding = (value, observation)
                                enqueue("policy", value, MAX_OBSERVATION_BYTES)
                                phase_deadline = min(
                                    deadline, time.monotonic() + self.decision_timeout
                                )
                        else:
                            if outstanding is None:
                                raise ValueError("Duplicate policy response")
                            pending, observation = outstanding
                            validate_response(value, pending)
                            reply = parse_reply(
                                canonical_json(value["reply"]), manifest, observation
                            )
                            expected_state = reply.next_state.model_dump(mode="json")
                            enqueue(
                                "engine",
                                decision_envelope(pending, reply),
                                MAX_REPLY_BYTES,
                            )
                            outstanding = None
                            sequence += 1
                            phase_deadline = min(
                                deadline, time.monotonic() + self.decision_timeout
                            )
        finally:
            selector.close()
