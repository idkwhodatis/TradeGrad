"""M6 ASGI body limits with in-memory messages; no socket/proxy/capacity proof."""

import asyncio
import json
from uuid import UUID

import pytest

from backend.app.api.body_limit import BodyLimitMiddleware


def exchange(method, messages, *, headers=(), maximum=10):
    iterator = iter(messages)
    sent, seen = [], []
    reads = []

    async def receive():
        value = next(iterator)
        reads.append(value)
        return value

    async def send(value):
        sent.append(value)

    async def app(scope, receive, send):
        seen.append(await receive())

    asyncio.run(
        BodyLimitMiddleware(app, maximum)(
            {"type": "http", "method": method, "headers": list(headers)}, receive, send
        )
    )
    return sent, seen, reads


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
@pytest.mark.parametrize(
    "chunks", [[b"a" * 10], [b"a" * 4, b"b" * 6], [b"", b"c" * 10, b""]]
)
def test_exact_body_limit_is_accepted_without_changing_bytes(method, chunks):
    messages = [
        {"type": "http.request", "body": content, "more_body": index < len(chunks) - 1}
        for index, content in enumerate(chunks)
    ]
    sent, seen, reads = exchange(method, messages)
    assert sent == [] and reads == messages
    assert seen == [
        {"type": "http.request", "body": b"".join(chunks), "more_body": False}
    ]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
@pytest.mark.parametrize(
    "headers", [[], [(b"content-length", b"1")], [(b"content-length", b"999999999")]]
)
def test_over_limit_rejects_before_parser_and_does_not_drain_remainder(method, headers):
    messages = [
        {"type": "http.request", "body": b"a" * 10, "more_body": True},
        {"type": "http.request", "body": b"b", "more_body": True},
        {"type": "http.request", "body": b"never consumed", "more_body": False},
    ]
    sent, seen, reads = exchange(method, messages, headers=headers)
    assert seen == [] and reads == messages[:2]
    assert len(sent) == 2 and sent[0]["status"] == 413
    response = json.loads(sent[1]["body"])
    assert response["code"] == "request_size"
    assert str(UUID(response["correlation_id"])) == response["correlation_id"]
    assert (
        dict(sent[0]["headers"])[b"x-correlation-id"]
        == response["correlation_id"].encode()
    )
    assert dict(sent[0]["headers"])[b"cache-control"] == b"no-store"


def test_incomplete_disconnected_body_never_reaches_application():
    messages = [
        {"type": "http.request", "body": b"partial", "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent, seen, reads = exchange("POST", messages)
    assert sent == [] and seen == [] and reads == messages


@pytest.mark.parametrize("prefix", [b"", b"a" * 9, b"a" * 10])
def test_oversized_chunk_is_rejected_before_accumulator_copy(monkeypatch, prefix):
    from backend.app.api import body_limit

    copies = []

    class BoundedAccumulator(bytearray):
        def extend(self, chunk):
            assert len(self) + len(chunk) <= 10, (
                "Over-budget chunk copied before rejection"
            )
            copies.append(chunk)
            super().extend(chunk)

    monkeypatch.setattr(body_limit, "bytearray", BoundedAccumulator, raising=False)
    messages = [
        {"type": "http.request", "body": prefix, "more_body": True},
        {"type": "http.request", "body": b"b" * 11, "more_body": False},
    ]
    sent, seen, reads = exchange("POST", messages)
    assert sent[0]["status"] == 413 and seen == [] and reads == messages
    assert copies == [prefix]
