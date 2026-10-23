"""Actual locked SDK with in-memory HTTP transport only; never opens sockets."""

import json
import secrets

import httpx
import pytest

from packages.tradegrad_adapter.model_sdk_process import execute


def payload(**updates):
    return {
        "secret": secrets.token_urlsafe(32),
        "base_url": "https://models.example.com/v1",
        "model": "model-a",
        "messages": [{"role": "user", "content": "synthetic prompt"}],
        "temperature": 0,
        "max_output_tokens": 16,
        "timeout_seconds": 2,
        "max_request_bytes": 1024,
        "max_response_bytes": 1024,
        "synthetic": True,
        **updates,
    }


def response(**updates):
    return {
        "id": "synthetic-response",
        "object": "chat.completion",
        "created": 0,
        "model": "model-a",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "synthetic reply"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        **updates,
    }


def test_actual_sdk_request_identity_auth_and_bounded_nonstreaming_contract():
    values = payload()
    calls = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == "https://models.example.com/v1/chat/completions"
        if request.headers["authorization"] != "Bearer " + values["secret"]:
            pytest.fail("SDK authentication header differs", pytrace=False)
        assert request.headers["accept-encoding"] == "identity"
        body = json.loads(request.content)
        assert body == {
            "model": "model-a",
            "messages": values["messages"],
            "temperature": 0,
            "max_tokens": 16,
            "stream": False,
        }
        return httpx.Response(200, json=response())

    result = execute(values, transport=httpx.MockTransport(handle))
    if values["secret"] in json.dumps(result):
        pytest.fail("SDK result leaked credential material", pytrace=False)
    assert len(calls) == 1
    assert result["ok"] is True
    assert result["response"]["text"] == "synthetic reply"
    assert result["response"]["usage_source"] == "synthetic_fixture"


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_explicit_http_error_not_retried_and_provider_body_never_leaks(status):
    values, calls = payload(), []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status, json={"error": {"message": values["secret"], "type": "synthetic"}}
        )

    result = execute(values, transport=httpx.MockTransport(handle))
    if values["secret"] in json.dumps(result):
        pytest.fail("SDK result leaked credential material", pytrace=False)
    assert len(calls) == 1
    assert result == {
        "ok": False,
        "error_class": f"model_http_{status}",
        "outcome_unknown": False,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "redirect",
        "malformed",
        "oversize",
        "encoded",
        "bad_usage",
        "bad_id",
        "empty_choices",
        "tool",
    ],
)
def test_untrusted_response_is_bounded_and_fail_closed(kind):
    calls = []

    def handle(request):
        calls.append(request)
        if kind == "redirect":
            return httpx.Response(
                302, headers={"location": "https://different.example.com/v1"}
            )
        if kind == "malformed":
            return httpx.Response(200, content=b"not json")
        if kind == "oversize":
            return httpx.Response(200, content=b"x" * 2048)
        if kind == "encoded":
            return httpx.Response(
                200,
                headers={"content-encoding": "br"},
                stream=httpx.ByteStream(b"untrusted"),
            )
        data = response()
        if kind == "bad_usage":
            data["usage"]["prompt_tokens"] = -1
        if kind == "bad_id":
            data["id"] = "x" * 300
        if kind == "empty_choices":
            data["choices"] = []
        if kind == "tool":
            data["choices"][0]["finish_reason"] = "tool_calls"
        return httpx.Response(200, json=data)

    result = execute(payload(), transport=httpx.MockTransport(handle))
    assert len(calls) == 1
    assert result["ok"] is False
    assert result["outcome_unknown"] is False


def test_wire_request_body_bound_prevents_transport():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    result = execute(
        payload(messages=[{"role": "user", "content": "x" * 2000}]),
        transport=httpx.MockTransport(handle),
    )
    assert calls == []
    assert result == {
        "ok": False,
        "error_class": "request_too_large",
        "outcome_unknown": False,
    }


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError]
)
def test_network_errors_remain_uncertain_without_sdk_retry(error):
    calls = []

    def handle(request):
        calls.append(request)
        raise error("synthetic error; never retain exception contents")

    result = execute(payload(), transport=httpx.MockTransport(handle))
    assert len(calls) == 1
    assert result == {
        "ok": False,
        "error_class": "model_transport_unknown",
        "outcome_unknown": True,
    }
