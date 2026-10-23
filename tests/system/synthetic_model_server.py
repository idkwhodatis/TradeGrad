"""Bounded, loopback-only OpenAI wire fixture for explicitly opted-in hosted CI.

Import and construction do not create sockets or start threads. This is NOT a
model, paid-provider acceptance, or useful trading evidence. Its job-local key
and request bodies are never logged or included in its public audit records.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def hosted_model_gate_enabled():
    return (
        os.getenv("GITHUB_ACTIONS") == "true"
        and os.getenv("TRADEGRAD_REQUIRE_MODEL_INTEGRATION") == "1"
    )


@dataclass(frozen=True)
class Reply:
    kind: str = "success"
    text: str = "Synthetic transport fixture, not market or model evidence."
    delay_seconds: float = 0


def completion_body(text):
    return {
        "id": "synthetic-completion",
        "object": "chat.completion",
        "created": 0,
        "model": "synthetic-tradegrad-v1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
    }


class SyntheticModelServer:
    """No external destination, unbounded request, or diagnostic-body fallback."""

    max_requests = 16
    max_request_bytes = 1024 * 1024

    def __init__(self, replies=()):
        self.secret = "synthetic-job-" + secrets.token_urlsafe(32)
        self.secret_env = "TRADEGRAD_SYNTHETIC_" + secrets.token_hex(8).upper()
        self.replies = tuple(replies)
        self._audit = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server = None
        self._thread = None
        self.deadline = None

    @property
    def audit(self):
        with self._lock:
            return [dict(row) for row in self._audit]

    @property
    def request_count(self):
        with self._lock:
            return len(self._audit)

    @property
    def base_url(self):
        if self._server is None:
            raise RuntimeError("Synthetic server has not been started")
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def set_replies(self, replies):
        with self._lock:
            if self._audit:
                raise RuntimeError("Response plans cannot change after a request")
            self.replies = tuple(replies)

    def start(self):
        if not hosted_model_gate_enabled():
            raise RuntimeError("Synthetic HTTP requires both explicit hosted CI gates")
        if self._server is not None:
            raise RuntimeError("Synthetic server already started")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def _write(self, status, content, *, headers=None):
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Connection", "close")
                    for key, value in (headers or {}).items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(content)
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
                self.close_connection = True

            def do_POST(self):
                if (
                    self.path != "/v1/chat/completions"
                    or owner._stop.is_set()
                    or time.monotonic() >= owner.deadline
                ):
                    self._write(404, b"{}")
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    size = 0
                if not 0 < size <= owner.max_request_bytes:
                    self._write(413, b"{}")
                    return
                authorized = hmac.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + owner.secret
                )
                if not authorized:
                    self._write(401, b"{}")
                    return
                try:
                    raw = self.rfile.read(size)
                    body = json.loads(raw)
                except (OSError, ValueError):
                    self._write(400, b"{}")
                    return
                if not isinstance(body, dict):
                    self._write(400, b"{}")
                    return
                with owner._lock:
                    index = len(owner._audit)
                    if index >= owner.max_requests:
                        self._write(429, b"{}")
                        return
                    owner._audit.append(
                        {
                            "path": self.path,
                            "authorized": authorized,
                            "request_sha256": hashlib.sha256(raw).hexdigest(),
                            "request_bytes": len(raw),
                            "model": body.get("model"),
                            "max_tokens": body.get("max_tokens"),
                            "temperature": body.get("temperature"),
                            "message_count": len(body.get("messages", [])),
                        }
                    )
                    reply = (
                        owner.replies[index]
                        if index < len(owner.replies)
                        else Reply("unexpected_request")
                    )
                if owner._stop.wait(min(max(reply.delay_seconds, 0), 15)):
                    return
                if reply.kind in {"rate_limit", "server_error", "unexpected_request"}:
                    status = 429 if reply.kind == "rate_limit" else 500
                    self._write(
                        status,
                        json.dumps({"error": {"message": reply.text}}).encode(),
                    )
                    return
                if reply.kind == "malformed_json":
                    self._write(200, b"{not-json")
                    return
                if reply.kind == "redirect":
                    # Even a redirect to our own fixture must not be followed.
                    self._write(
                        307, b"{}", headers={"Location": owner.base_url + "/blocked"}
                    )
                    return
                body = completion_body(reply.text)
                if reply.kind == "missing_choices":
                    body["choices"] = []
                elif reply.kind == "invalid_usage":
                    body["usage"]["prompt_tokens"] = -1
                elif reply.kind == "oversized":
                    body["choices"][0]["message"]["content"] = "x" * (2 * 1024 * 1024)
                elif reply.kind == "unknown_usage":
                    body.pop("usage")
                self._write(200, json.dumps(body).encode())

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def handle_error(self, _request, _client_address):
                # Socket resets are expected timeout/cancellation test outcomes.
                pass

        self.deadline = time.monotonic() + 300
        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="tradegrad-synthetic-model",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("Owned synthetic server did not stop")

    def __enter__(self):
        return self.start()

    def __exit__(self, *_error):
        self.close()
