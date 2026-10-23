"""Opt-in read-only observation gate against an already-running disposable stack.

Run AFTER the real import/research workload. No local startup, mocks or alternate
storage. Collection opens no socket. This is not a representative capacity test.
"""

import json
import os
import time
from urllib.parse import urlsplit

import pytest

from script.compose_bounds import alarm_deadline


def test_real_stack_operations_after_completed_fixture_workload():
    if os.getenv("TRADEGRAD_REQUIRE_OPERATIONS_INTEGRATION") != "1":
        pytest.skip("Explicit disposable real-stack operations gate not enabled")
    import httpx

    origin = os.getenv("E2E_API_URL", "")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        pytest.fail(
            "Operations gate requires an explicit loopback E2E_API_URL origin",
            pytrace=False,
        )
    email, password = os.getenv("E2E_EMAIL"), os.getenv("E2E_PASSWORD")
    if not email or not password:
        pytest.fail(
            "Operations gate requires generated disposable fixture credentials",
            pytrace=False,
        )
    deadline = time.monotonic() + 45
    with httpx.Client(
        base_url=origin, timeout=5, trust_env=False, follow_redirects=False
    ) as client:

        def request(method, path, **kwargs):
            try:
                with (
                    alarm_deadline(min(deadline, time.monotonic() + 10)),
                    client.stream(method, "/api/v1" + path, **kwargs) as response,
                ):
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 128_000 or time.monotonic() > deadline:
                            pytest.fail(
                                "Operations response exceeded byte/time bounds",
                                pytrace=False,
                            )
                        chunks.append(chunk)
                    return response.status_code, json.loads(b"".join(chunks))
            except (httpx.HTTPError, ValueError, TimeoutError):
                pytest.fail(
                    "Operations stack request failed; no private response or credential logged",
                    pytrace=False,
                )

        status, auth = request(
            "POST",
            "/login/access-token",
            data={"username": email, "password": password},
        )
        assert status == 200
        token = auth.get("access_token")
        if not isinstance(token, str):
            pytest.fail(
                "Disposable account login did not return a token", pytrace=False
            )
        headers = {"Authorization": "Bearer " + token}
        status, result = request("GET", "/operations?days=31", headers=headers)
        assert status == 200
        assert result["scope"] == "owner" and result["disk"]["state"] == "restricted"
        assert result["queue_wait"]["count"] >= 1
        assert result["attempt_runtime"]["count"] >= 1
        assert result["imports"]["sampled_completed"] >= 1
        assert result["observation_ttl_seconds"] == 60
        assert request("GET", "/operations?scope=deployment", headers=headers)[0] == 403
        assert request("GET", "/operations")[0] == 401
