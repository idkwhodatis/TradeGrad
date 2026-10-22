"""Bounded, replayable model calls; this module has no OpenAI import at import time.

A durable started record without a completed response is *uncertain*, not a
license to charge for another request. Completed records replay locally. This is
not global exactly-once across the provider, filesystem and database.
"""

from __future__ import annotations

import copy
import os
import re
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


class EnvironmentOpenAIModel:
    """Opt-in live provider; credentials stay in environment, never serialized.

    SDK retries are disabled because ambiguous requests are not safe to replay.
    The caller must explicitly configure live_enabled after obtaining authority
    for paid calls. Construction alone does not make a request.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        secret_env: str,
        live_enabled: bool = False,
        provider: str = "openai-compatible",
    ):
        if not live_enabled:
            raise ValueError("live model requests are disabled")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", secret_env):
            raise ValueError("invalid model credential environment reference")
        secret = os.environ.get(secret_env)
        if not secret:
            raise ValueError("model credential environment reference is not set")
        from openai import OpenAI

        self.client = OpenAI(api_key=secret, base_url=base_url, max_retries=0)
        self.model, self.provider = model, provider
        self.redactions = (secret,)

    def complete(self, request: ModelRequest) -> ModelResponse:
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.user_prompt})
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=request.max_output_tokens,
            timeout=request.timeout_seconds,
            temperature=request.parameters.get("temperature", 0),
        )
        usage = response.usage
        return ModelResponse(
            response.choices[0].message.content or "",
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
            getattr(response, "id", None),
        )


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
    ):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.input_digest = input_digest
        self.provider = provider
        self.budgets = dict(budgets)
        self.prompt_version = prompt_version
        self.parameters = dict(parameters or {})
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

    def _query(self, user_prompt: str, system_prompt: str | None = None) -> str:
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
            uncertain = isinstance(error, (TimeoutError, ConnectionError)) or type(
                error
            ).__name__ in {
                "APITimeoutError",
                "APIConnectionError",
                "ReadTimeout",
                "ConnectTimeout",
                "ConnectError",
                "RemoteProtocolError",
            }
            row.update(
                status="started" if uncertain else "failed",
                error_class=type(error).__name__,
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
            provider_request_id=response.provider_request_id,
            price=response.price,
            response_bytes=len(text.encode()),
        )
        if response.input_tokens is not None and response.output_tokens is not None:
            if min(response.input_tokens, response.output_tokens) < 0:
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
        return text
