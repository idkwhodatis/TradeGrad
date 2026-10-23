"""One bounded SDK call in a trusted, disposable research process.

This entrypoint imports no strategy, optimizer, evaluator or application code.
Its stdin is a private pipe, not a persisted config. stdout is a small protocol;
provider exceptions, headers, URLs, keys and raw error bodies are never printed.
The parent enforces the hard wall deadline and confirms process exit.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import sys

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

MAX_INPUT = 2 * 1048576


class RejectedResponse(Exception):
    pass


class BoundedTransport(httpx.BaseTransport):
    """Bound raw HTTP body before either httpx decompression or SDK JSON parsing."""

    def __init__(self, inner, *, request_bytes, response_bytes):
        self.inner = inner
        self.request_bytes = request_bytes
        self.response_bytes = response_bytes

    def handle_request(self, request):
        if len(request.content) > self.request_bytes:
            raise RejectedResponse("request_too_large")
        response = self.inner.handle_request(request)
        try:
            encoding = response.headers.get("content-encoding", "identity").lower()
            if encoding not in ("", "identity"):
                raise RejectedResponse("encoded_response_not_supported")
            length = response.headers.get("content-length")
            if length is not None and (
                not length.isdecimal() or int(length) > self.response_bytes
            ):
                raise RejectedResponse("response_too_large")
            chunks, size = [], 0
            raw_chunks = (
                (response.content,)
                if response.is_stream_consumed
                else response.iter_raw()
            )
            for chunk in raw_chunks:
                size += len(chunk)
                if size > self.response_bytes:
                    raise RejectedResponse("response_too_large")
                chunks.append(chunk)
            if 300 <= response.status_code < 400:
                raise RejectedResponse("redirect_not_allowed")
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                request=request,
            )
        finally:
            response.close()

    def close(self):
        self.inner.close()


def sdk_call(payload, *, transport=None):
    """Injectable HTTP transport is for pure, socket-free tests only."""
    bounded = BoundedTransport(
        transport
        if transport is not None
        else httpx.HTTPTransport(retries=0, trust_env=False),
        request_bytes=payload["max_request_bytes"],
        response_bytes=payload["max_response_bytes"],
    )
    with (
        httpx.Client(
            transport=bounded,
            trust_env=False,
            follow_redirects=False,
            timeout=payload["timeout_seconds"],
        ) as http,
        OpenAI(
            api_key=payload["secret"],
            base_url=payload["base_url"],
            max_retries=0,
            http_client=http,
            default_headers={"Accept-Encoding": "identity"},
        ) as client,
    ):
        result = client.chat.completions.create(
            model=payload["model"],
            messages=payload["messages"],
            temperature=payload["temperature"],
            max_tokens=payload["max_output_tokens"],
            timeout=payload["timeout_seconds"],
            stream=False,
        )
    if len(result.choices) != 1:
        raise RejectedResponse("invalid_response_schema")
    choice = result.choices[0]
    if (
        not isinstance(choice.message.content, str)
        or choice.message.tool_calls
        or choice.message.function_call
        or choice.finish_reason not in ("stop", "length")
        or len(choice.message.content.encode()) > payload["max_response_bytes"]
    ):
        raise RejectedResponse("invalid_response_schema")
    usage = result.usage
    if usage is not None and any(
        type(value) is not int or value < 0 or value > 100000000
        for value in (usage.prompt_tokens, usage.completion_tokens)
    ):
        raise RejectedResponse("invalid_response_usage")
    identifier = result.id
    if not isinstance(identifier, str) or len(identifier.encode()) > 256:
        raise RejectedResponse("invalid_response_identifier")
    return {
        "text": choice.message.content,
        "input_tokens": usage.prompt_tokens if usage else None,
        "output_tokens": usage.completion_tokens if usage else None,
        "provider_request_id": identifier,
        "usage_source": "synthetic_fixture" if payload["synthetic"] else "provider",
    }


def execute(payload, *, transport=None):
    try:
        return {"ok": True, "response": sdk_call(payload, transport=transport)}
    except RejectedResponse as error:
        # str(error) is selected only from our fixed constants, not provider text.
        return {"ok": False, "error_class": str(error), "outcome_unknown": False}
    except (APITimeoutError, APIConnectionError, httpx.TransportError) as error:
        if isinstance(error.__cause__, RejectedResponse):
            return {
                "ok": False,
                "error_class": str(error.__cause__),
                "outcome_unknown": False,
            }
        return {
            "ok": False,
            "error_class": "model_transport_unknown",
            "outcome_unknown": True,
        }
    except APIStatusError as error:
        return {
            "ok": False,
            "error_class": f"model_http_{error.status_code}",
            "outcome_unknown": False,
        }
    except Exception:  # noqa: BLE001 - never print the SDK's raw provider details
        return {
            "ok": False,
            "error_class": "invalid_model_response",
            "outcome_unknown": False,
        }


def contain_process(payload):
    # Fail closed outside Linux. This trusted child cannot outlive a killed
    # worker or extend the wall deadline with a slow-drip response. The parent
    # PID check closes the race between launch and PR_SET_PDEATHSIG installation.
    if sys.platform != "linux":
        raise RuntimeError("model_process_containment_unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    if (
        libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0
        or os.getppid() != payload["parent_pid"]
    ):
        raise RuntimeError("model_process_containment_unavailable")
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, min(float(payload["timeout_seconds"]), 120.0))


def main():
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        result = {
            "ok": False,
            "error_class": "model_request_too_large",
            "outcome_unknown": False,
        }
    else:
        try:
            payload = json.loads(data)
            contain_process(payload)
            result = execute(payload)
        except Exception:  # noqa: BLE001 - no request/credential diagnostics
            result = {
                "ok": False,
                "error_class": "model_process_protocol_error",
                "outcome_unknown": True,
            }
    sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
