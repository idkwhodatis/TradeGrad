"""Bounded, replayable model calls; this module has no OpenAI import at import time.

A durable started record without a completed response is *uncertain*, not a
license to charge for another request. Completed records replay locally. This is
not global exactly-once across the provider, filesystem and database.
"""

from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .checkpoint import atomic_json, digest, json_digest


class OptimizerStop(BaseException):
    """Bypass upstream broad Exception retry decorators for hard budget/control stops."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ModelCallAbort(OptimizerStop):
    """Internal non-retryable boundary failure; never publish partial optimizer state."""


class ModelCallBoundaryFailure(RuntimeError):
    """Public worker failure raised only after leaving source retry decorators."""


@dataclass(frozen=True)
class ModelRequest:
    call_id: str
    logical_index: int
    user_prompt: str
    system_prompt: str | None
    max_output_tokens: int
    timeout_seconds: float
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_request_id: str | None = None
    # Only actual provider-supplied price metadata may populate this field.
    price: Mapping[str, Any] | None = None
    usage_source: str = "provider"


class ModelProvider(Protocol):
    model: str
    provider: str

    def complete(self, request: ModelRequest) -> ModelResponse: ...


class FrozenModel:
    """Explicit test fixture. Its outputs are not live-model acceptance evidence."""

    model = "deterministic-fixture-v1"
    provider = "fixture"

    def __init__(
        self,
        responses: Sequence[str | ModelResponse] | Mapping[str, str | ModelResponse],
    ):
        self.responses = responses
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        value = (
            self.responses[request.call_id]
            if isinstance(self.responses, Mapping)
            else self.responses[request.logical_index]
        )
        if isinstance(value, ModelResponse):
            return value
        return ModelResponse(str(value), 0, 0, usage_source="synthetic_fixture")


class ModelProviderFailure(RuntimeError):
    """Only fixed, secret-free classifications may cross the SDK boundary."""

    def __init__(self, error_class, *, outcome_unknown=False):
        self.error_class = error_class
        self.outcome_unknown = outcome_unknown
        super().__init__(error_class)


class EnvironmentOpenAIModel:
    """Operator-admitted SDK calls; key resolution occurs in the trusted worker.

    A fresh process owns one request. No per-read timeout is misrepresented as a
    whole-call timeout: the parent kills/reaps the process at the wall deadline.
    Cancellation can wait up to that deadline plus the bounded reap interval.
    An unconfirmed process exit keeps worker finalization/recovery blocked.
    """

    def __init__(self, *, profile, live_enabled=False, boundary=None):
        if not live_enabled:
            raise ValueError("live model requests are disabled")
        secret = os.environ.get(profile.secret_env)
        if (
            not secret
            or len(secret.encode()) > 8192
            or any(ord(c) < 32 for c in secret)
        ):
            raise ModelProviderFailure("model_credential_unavailable")
        self.profile = profile
        self.model, self.provider = profile.model, profile.provider
        self.redactions = (secret, profile.base_url, profile.secret_env)
        self._secret = secret
        self._process = None
        self.contained = True
        self.boundary = boundary or (lambda *_: None)
        self._active_call = None

    def _confirm_receipt(self):
        if self._active_call is not None:
            try:
                self.boundary("stopped", self._active_call)
            except Exception:  # noqa: BLE001 - suppress private persistence diagnostics
                raise ModelProviderFailure(
                    "model_stop_receipt_unconfirmed", outcome_unknown=True
                ) from None
            self._active_call = None

    def close(self):
        try:
            return self._close()
        except ModelProviderFailure:
            # Local exit alone does not replace a durable confirmation receipt.
            return False

    def _close(self):
        process = self._process
        if process is None:
            if self.contained:
                self._confirm_receipt()
            return self.contained
        if process.poll() is not None:
            self.contained = True
            self._process = None
            self._confirm_receipt()
            return True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        try:
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            return False
        self.contained = process.poll() is not None
        if self.contained:
            self._process = None
            self._confirm_receipt()
        return self.contained

    def complete(self, request: ModelRequest) -> ModelResponse:
        if self._process is not None or not self.contained:
            raise ModelProviderFailure(
                "model_containment_unconfirmed", outcome_unknown=True
            )
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.user_prompt})
        timeout = min(request.timeout_seconds, self.profile.request_timeout_seconds)
        if timeout <= 0:
            raise ModelProviderFailure("model_request_deadline")
        payload = {
            "secret": self._secret,
            "base_url": self.profile.base_url,
            "parent_pid": os.getpid(),
            "model": self.model,
            "messages": messages,
            "temperature": self.profile.temperature,
            "max_output_tokens": min(
                request.max_output_tokens, self.profile.max_output_tokens
            ),
            "timeout_seconds": timeout,
            "max_request_bytes": self.profile.max_request_bytes,
            "max_response_bytes": self.profile.max_response_bytes,
            "synthetic": self.profile.synthetic,
        }
        wire = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode()
        if len(wire) > 2 * 1048576:
            raise ModelProviderFailure("model_request_too_large")
        # No inherited credential, proxy, SDK/global auth or Python path settings.
        environment = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # A committed admission serializes launch against stale-lease recovery.
        # Failure/uncertainty here prevents launch; it cannot license another call.
        self._active_call = request.call_id
        try:
            self.boundary("admitted", request.call_id)
        except Exception:  # noqa: BLE001 - suppress private persistence diagnostics
            raise ModelProviderFailure(
                "model_admission_receipt_unconfirmed", outcome_unknown=True
            ) from None
        self.contained = False
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("model_sdk_process.py")),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
            )
        except OSError:
            # Popen reports failed exec only after reaping its failed child.
            self.contained = True
            self._confirm_receipt()
            raise ModelProviderFailure("model_process_unavailable") from None
        self._process, self.contained = process, False
        try:
            output, _ = process.communicate(wire, timeout=timeout)
        except subprocess.TimeoutExpired:
            confirmed = self.close()
            raise ModelProviderFailure(
                "model_request_timeout"
                if confirmed
                else "model_containment_unconfirmed",
                outcome_unknown=True,
            ) from None
        except BaseException:
            self.close()
            raise
        self.contained = process.poll() is not None
        self._process = None if self.contained else process
        if self.contained:
            self._confirm_receipt()
        if not self.contained or process.returncode != 0 or len(output) > 8 * 1048576:
            raise ModelProviderFailure(
                "model_process_outcome_unknown", outcome_unknown=True
            )
        try:
            result = json.loads(output)
            if result.get("ok") is not True:
                error_class = result["error_class"]
                if not isinstance(error_class, str) or not re.fullmatch(
                    r"[a-z0-9_]{1,80}", error_class
                ):
                    raise ValueError("invalid classification")
                raise ModelProviderFailure(
                    error_class, outcome_unknown=result["outcome_unknown"] is True
                )
            response = ModelResponse(**result["response"])
            if (
                not isinstance(response.text, str)
                or len(response.text.encode()) > self.profile.max_response_bytes
                or response.price is not None
            ):
                raise ValueError("invalid bounded response")
            return response
        except ModelProviderFailure:
            raise
        except Exception:  # noqa: BLE001 - child protocol never leaks raw values
            raise ModelProviderFailure(
                "model_process_protocol_error", outcome_unknown=True
            ) from None


def sanitize(text: str, secrets: Sequence[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)([\"']?(?:api[_-]?key|authorization|access[_-]?token|client[_-]?secret)[\"']?\s*[:=]\s*)([\"']?)[^\s,;\n\"']+\2",
        r"\1[REDACTED]",
        text,
    )
    return text


class RecordedModel:
    def __init__(
        self,
        root: Path | str,
        *,
        input_digest: str,
        provider: ModelProvider,
        budgets: Mapping[str, Any],
        prompt_version: str = "tradegrad-source.v1",
        parameters: Mapping[str, Any] | None = None,
        elapsed: Callable[[], float] | None = None,
        fault: Callable[[str], None] | None = None,
        event: Callable[[str, Mapping[str, Any]], None] | None = None,
        replay_roots: Sequence[Path | str] = (),
        initial_records: Sequence[Mapping[str, Any]] = (),
        control: Callable[[], str | None] | None = None,
    ):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.input_digest = input_digest
        self.provider = provider
        self.budgets = dict(budgets)
        self.prompt_version = prompt_version
        self.parameters = dict(parameters or {})
        self.control = control or (lambda: None)
        self.elapsed = elapsed or (lambda: 0.0)
        self.fault = fault or (lambda _: None)
        self.event = event or (lambda *_: None)
        self.secrets = tuple(getattr(provider, "redactions", ()))
        self.replay_roots = [Path(path).absolute() for path in replay_roots]
        self._records: dict[str, dict[str, Any]] = {
            str(row["call_id"]): dict(row) for row in initial_records
        }
        self.context = "unassigned"
        self.ordinal = 0
        # Recovery reads only explicitly passed roots bound to the same input digest.
        for directory in [*self.replay_roots, self.root]:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                row = self._read(path)
                existing = self._records.get(row["call_id"])
                if (
                    existing
                    and existing.get("status") == "completed"
                    and row.get("status") != "completed"
                ):
                    continue
                self._records[row["call_id"]] = row

    def _read(self, path: Path) -> dict[str, Any]:
        import json

        if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("invalid model replay record")
        envelope = json.loads(path.read_bytes())
        row = envelope["record"]
        if (
            envelope["sha256"] != json_digest(row)
            or row["input_digest"] != self.input_digest
        ):
            raise ValueError("model replay record hash or immutable-input mismatch")
        if path.name != digest(row["call_id"].encode()) + ".json":
            raise ValueError("model replay record identity mismatch")
        return row

    def _save(self, row: dict[str, Any]) -> None:
        # Do not let caller mutation convert the durable intent into an apparent
        # completed response. The in-memory view advances only after persistence.
        snapshot = copy.deepcopy(row)
        try:
            atomic_json(
                self.root / (digest(snapshot["call_id"].encode()) + ".json"),
                {"record": snapshot, "sha256": json_digest(snapshot)},
            )
        except Exception as error:
            raise ModelCallAbort("model_journal_persistence_failed") from error
        self._records[snapshot["call_id"]] = snapshot

    def _emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        try:
            self.event(kind, payload)
        except Exception as error:
            raise ModelCallAbort("model_event_publication_failed") from error

    @property
    def records(self) -> list[dict[str, Any]]:
        return sorted(
            (dict(row) for row in self._records.values()),
            key=lambda row: row["logical_index"],
        )

    @property
    def tokens_used(self) -> int:
        return sum(int(row["charged_tokens"]) for row in self._records.values())

    def set_context(self, context: str) -> None:
        self.context, self.ordinal = context, 0

    def mark_adopted(self, call_ids: Sequence[str], adopted: bool = True) -> None:
        for call_id in call_ids:
            row = copy.deepcopy(self._records[call_id])
            row["adopted"] = adopted
            self._save(row)

    def query(
        self, user_prompt: str, system_prompt: str | None = None, **_ignored: Any
    ) -> str:
        try:
            return self._query(user_prompt, system_prompt)
        except Exception as error:
            # The source optimizer broadly retries Exception. Retrying a metadata,
            # filesystem or callback failure could issue the same paid prompt with
            # a fresh call ID. Cross its retry boundary without another request.
            raise ModelCallAbort("model_call_boundary_failed") from error

    def _check_control(self):
        requested = self.control()
        if requested in {"cancel", "pause"}:
            raise OptimizerStop("cancelled" if requested == "cancel" else "paused")

    def _query(self, user_prompt: str, system_prompt: str | None = None) -> str:
        self._check_control()
        call_id = f"{self.context}:{self.ordinal:04d}"
        self.ordinal += 1
        user_prompt = sanitize(user_prompt, self.secrets)
        system_prompt = sanitize(system_prompt, self.secrets) if system_prompt else None
        request_hash = json_digest(
            {
                "user_prompt": user_prompt,
                "system_prompt": system_prompt,
                "model": self.provider.model,
                "provider": self.provider.provider,
                "prompt_version": self.prompt_version,
                "parameters": self.parameters,
            }
        )
        previous = self._records.get(call_id)
        if previous:
            if previous["request_sha256"] != request_hash:
                raise OptimizerStop("model_replay_request_mismatch")
            if previous["status"] == "completed":
                if self.tokens_used > int(self.budgets["max_tokens"]):
                    raise OptimizerStop("max_tokens")
                return previous["response"]
            if previous["status"] == "started":
                raise OptimizerStop("model_call_outcome_unknown")
            raise OptimizerStop("model_call_failed")
        if len(self._records) >= int(self.budgets["max_model_calls"]):
            raise OptimizerStop("max_model_calls")
        remaining_time = float(self.budgets["max_runtime_seconds"]) - self.elapsed()
        if remaining_time <= 0:
            raise OptimizerStop("max_runtime_seconds")
        if (
            len(user_prompt.encode()) + len((system_prompt or "").encode())
            > 1024 * 1024
        ):
            raise OptimizerStop("model_request_too_large")
        # UTF-8 byte count is a conservative token upper bound for supported text
        # tokenizers. Reserve explicit message overhead too; unknown usage is charged
        # at this upper bound, never silently recorded as zero.
        prompt_reservation = (
            len(user_prompt.encode()) + len((system_prompt or "").encode()) + 256
        )
        remaining_tokens = int(self.budgets["max_tokens"]) - self.tokens_used
        max_output = min(
            int(self.parameters.get("max_output_tokens", 4096)),
            remaining_tokens - prompt_reservation,
        )
        if max_output <= 0:
            raise OptimizerStop("max_tokens")
        row = {
            "call_id": call_id,
            "logical_index": len(self._records),
            "input_digest": self.input_digest,
            "model": self.provider.model,
            "provider": self.provider.provider,
            "prompt_version": self.prompt_version,
            "parameters": {**self.parameters, "max_output_tokens": max_output},
            "request_sha256": request_hash,
            "user_prompt": user_prompt,
            "system_prompt": system_prompt,
            "status": "started",
            "response": None,
            "input_tokens": None,
            "output_tokens": None,
            "usage_source": "conservative_reservation",
            "charged_tokens": prompt_reservation + max_output,
            "latency_seconds": None,
            "error_class": None,
            "adopted": False,
            "elapsed_seconds": self.elapsed(),
            "request_bytes": len(user_prompt.encode())
            + len((system_prompt or "").encode()),
            "response_bytes": None,
            "price": None,
            "provider_request_id": None,
        }
        self._emit(
            "budget_updated",
            {
                "elapsed_seconds": self.elapsed(),
                "remaining_runtime_seconds": remaining_time,
                "model_calls": len(self._records),
                "tokens": self.tokens_used,
            },
        )
        self.fault("before_model_request")
        self._save(row)
        started = time.monotonic()
        request = ModelRequest(
            call_id,
            row["logical_index"],
            user_prompt,
            system_prompt,
            max_output,
            remaining_time,
            self.parameters,
        )
        try:
            response = self.provider.complete(request)
        except Exception as error:
            uncertain = (
                getattr(error, "outcome_unknown", False)
                or isinstance(error, (TimeoutError, ConnectionError))
                or type(error).__name__
                in {
                    "APITimeoutError",
                    "APIConnectionError",
                    "ReadTimeout",
                    "ConnectTimeout",
                    "ConnectError",
                    "RemoteProtocolError",
                }
            )
            row.update(
                status="started" if uncertain else "failed",
                error_class=getattr(error, "error_class", type(error).__name__),
                error=sanitize(str(error), self.secrets)[:4096],
                latency_seconds=time.monotonic() - started,
                elapsed_seconds=self.elapsed(),
            )
            self._save(row)
            self._emit("model_call_completed", row)
            raise OptimizerStop(
                "model_call_outcome_unknown" if uncertain else "model_call_failed"
            ) from error
        # Fault here intentionally models an unknown paid-response outcome.
        self.fault("before_model_response")
        text = sanitize(response.text, self.secrets)
        if len(text.encode()) > 1024 * 1024:
            row.update(
                status="failed",
                error_class="ResponseTooLarge",
                latency_seconds=time.monotonic() - started,
            )
            self._save(row)
            raise OptimizerStop("model_response_too_large")
        row.update(
            status="completed",
            response=text,
            latency_seconds=time.monotonic() - started,
            elapsed_seconds=self.elapsed(),
            provider_request_id=sanitize(
                str(response.provider_request_id), self.secrets
            )[:256]
            if response.provider_request_id is not None
            else None,
            price=None
            if response.price is None
            else json.loads(
                sanitize(json.dumps(response.price, allow_nan=False), self.secrets)
            ),
            response_bytes=len(text.encode()),
        )
        if response.input_tokens is not None and response.output_tokens is not None:
            if any(
                type(value) is not int or value < 0 or value > 100000000
                for value in (response.input_tokens, response.output_tokens)
            ):
                raise OptimizerStop("invalid_model_usage")
            row.update(
                input_tokens=int(response.input_tokens),
                output_tokens=int(response.output_tokens),
                charged_tokens=int(response.input_tokens + response.output_tokens),
                usage_source="synthetic_fixture"
                if self.provider.provider == "fixture"
                else response.usage_source,
            )
        self._save(row)
        self.fault("after_model_response")
        self._emit("model_call_completed", row)
        self._emit(
            "budget_updated",
            {"model_calls": len(self._records), "tokens": self.tokens_used},
        )
        if self.tokens_used > int(self.budgets["max_tokens"]):
            raise OptimizerStop("max_tokens")
        if self.elapsed() >= float(self.budgets["max_runtime_seconds"]):
            raise OptimizerStop("max_runtime_seconds")
        self._check_control()
        return text
