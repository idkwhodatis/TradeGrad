"""Bound bodies before multipart parsing, including chunked requests without length."""

import json
from uuid import uuid4


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in {
            "POST",
            "PUT",
            "PATCH",
        }:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            # The server already owns this chunk. Do not copy an oversized one
            # into our bounded accumulator before rejecting the request.
            if len(body) + len(chunk) > self.max_bytes:
                correlation = str(uuid4())
                content = json.dumps(
                    {
                        "code": "request_size",
                        "message": "Request exceeds the configured upload byte limit",
                        "details": None,
                        "correlation_id": correlation,
                    }
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"x-correlation-id", correlation.encode()),
                            (b"cache-control", b"no-store"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": content})
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        pending = True

        async def bounded_receive():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
