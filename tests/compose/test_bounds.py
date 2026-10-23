"""Pure bounds/cleanup regression tests. Every signal/process/clock is mocked."""

import io
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from script import ci_compose_cleanup as cleanup
from script import compose_bounds as bounds
from script.compose_acceptance import Client, NoRedirect


def test_json_body_is_bounded_before_decode():
    assert bounds.read_capped(io.BytesIO(b"four"), 4) == b"four"
    with pytest.raises(ValueError, match="byte budget"):
        bounds.read_capped(io.BytesIO(b"extra"), 4)


def test_http_request_has_absolute_deadline_and_body_cap(monkeypatch):
    monkeypatch.setenv("E2E_API_URL", "http://127.0.0.1:8080")
    client = Client()
    calls = []
    monkeypatch.setattr(
        "script.compose_acceptance.alarm_deadline",
        lambda deadline: calls.append(deadline) or nullcontext(),
    )
    monkeypatch.setattr(
        client.opener, "open", lambda *a, **k: io.BytesIO(b'{"ready":true}')
    )
    assert client.request("/health/ready") == {"ready": True}
    assert calls and calls[0] <= client.deadline
    monkeypatch.setattr("script.compose_acceptance.JSON_BYTES", 4)
    with pytest.raises(ValueError, match="byte budget"):
        client.request("/health/ready")


def test_redirect_is_rejected_instead_of_forwarding_authorization():
    request = Request("http://127.0.0.1:8080/api/v1/status")
    with pytest.raises(HTTPError, match="Redirect refused"):
        NoRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://example.com"
        )


def test_alarm_interrupts_trickle_and_restores_handler(monkeypatch):
    calls = []
    handlers = {}
    monkeypatch.setattr(bounds.sys, "platform", "linux")
    monkeypatch.setattr(bounds.time, "monotonic", lambda: 10)
    monkeypatch.setattr(bounds.signal, "getitimer", lambda _: (0.0, 0.0))
    monkeypatch.setattr(bounds.signal, "getsignal", lambda _: "previous")
    monkeypatch.setattr(
        bounds.signal, "signal", lambda kind, handler: handlers.update({kind: handler})
    )
    monkeypatch.setattr(bounds.signal, "setitimer", lambda *args: calls.append(args))
    with pytest.raises(bounds.DeadlineExpired), bounds.alarm_deadline(12):
        handlers[bounds.signal.SIGALRM](None, None)
    assert calls == [(bounds.signal.ITIMER_REAL, 2), (bounds.signal.ITIMER_REAL, 0)]
    assert handlers[bounds.signal.SIGALRM] == "previous"


def test_expired_or_existing_alarm_fails_before_network(monkeypatch):
    monkeypatch.setattr(bounds.time, "monotonic", lambda: 10)
    signal = Mock()
    monkeypatch.setattr(bounds.signal, "signal", signal)
    with pytest.raises(bounds.DeadlineExpired), bounds.alarm_deadline(9):
        pytest.fail("Expired deadline admitted work")
    monkeypatch.setattr(bounds.signal, "getitimer", lambda _: (1.0, 0.0))
    with pytest.raises(RuntimeError, match="existing timer"), bounds.alarm_deadline(12):
        pytest.fail("Existing deadline was overwritten")
    signal.assert_not_called()


def fake_command_runtime(monkeypatch, chunks):
    events = []
    streams = [
        SimpleNamespace(fileno=lambda: 10, close=lambda: events.append("close-out")),
        SimpleNamespace(fileno=lambda: 11, close=lambda: events.append("close-err")),
    ]
    process = SimpleNamespace(
        pid=1234, returncode=None, stdout=streams[0], stderr=streams[1]
    )

    def wait(timeout):
        events.append(("wait", timeout))
        process.returncode = 0
        return 0

    process.wait = wait
    monkeypatch.setattr(bounds.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(bounds.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(bounds.os, "killpg", lambda *a: events.append(("kill", *a)))
    monkeypatch.setattr(bounds.time, "monotonic", lambda: 0)
    monkeypatch.setattr(bounds.os, "read", lambda fd, _: chunks[fd].pop(0))

    class Selector:
        def __init__(self):
            self.mapping = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def register(self, stream, _events, kind):
            self.mapping[stream.fileno()] = SimpleNamespace(fileobj=stream, data=kind)

        def get_map(self):
            return self.mapping

        def select(self, _timeout):
            return [(key, 1) for key in list(self.mapping.values())]

        def unregister(self, stream):
            self.mapping.pop(stream.fileno())

    monkeypatch.setattr(bounds.selectors, "DefaultSelector", Selector)
    return events


def test_trusted_command_returns_only_bounded_stdout(monkeypatch):
    events = fake_command_runtime(
        monkeypatch, {10: [b"public", b""], 11: [b"private", b""]}
    )
    assert bounds.run_bounded(["reviewed-command"], deadline=10) == "public"
    assert not any(isinstance(item, tuple) and item[0] == "kill" for item in events)


def test_output_flood_kills_reserved_group_before_reaping(monkeypatch):
    events = fake_command_runtime(monkeypatch, {10: [b"x" * 9], 11: [b""]})
    with pytest.raises(ValueError, match="output exceeded"):
        bounds.run_bounded(["reviewed-command"], deadline=10, max_stdout=8)
    assert events[:2] == [("kill", 1234, bounds.signal.SIGKILL), ("wait", 5)]


def test_absolute_command_deadline_cannot_be_reset_by_output(monkeypatch):
    events = fake_command_runtime(monkeypatch, {10: [b"x"], 11: [b""]})
    ticks = iter([0, 2])
    monkeypatch.setattr(bounds.time, "monotonic", lambda: next(ticks))
    with pytest.raises(bounds.DeadlineExpired):
        bounds.run_bounded(["reviewed-command"], deadline=1)
    assert events[:2] == [("kill", 1234, bounds.signal.SIGKILL), ("wait", 5)]


def cleanup_environment(monkeypatch, tmp_path):
    suffix = "a" * 16
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "tg-ci-" + suffix)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(
        "TRADEGRAD_STATE_ROOT", str(tmp_path / "var" / ("compose-" + suffix))
    )


def test_cleanup_requires_post_removal_absence(monkeypatch, tmp_path):
    cleanup_environment(monkeypatch, tmp_path)
    commands = []
    monkeypatch.setattr(cleanup, "command", lambda *a, **k: commands.append(a) or "")
    outcomes = iter([["a" * 64], ["a" * 64]])
    monkeypatch.setattr(cleanup, "owned_containers", lambda *a: next(outcomes))
    with pytest.raises(RuntimeError, match="after removal"):
        cleanup.main()
    assert not any("down" in command for command in commands)


def test_cleanup_requires_post_down_and_project_absence(monkeypatch, tmp_path):
    cleanup_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(cleanup, "owned_containers", lambda *a: [])
    deadlines = []

    def command(*arguments, deadline, **kwargs):
        deadlines.append(deadline)
        return "a" * 12 if arguments[0] == "ps" else ""

    monkeypatch.setattr(cleanup, "command", command)
    with pytest.raises(RuntimeError, match="project container absence"):
        cleanup.main()
    assert len(set(deadlines)) == 1


def test_cleanup_rejects_excess_list_or_uncertain_inspection(monkeypatch):
    with pytest.raises(ValueError):
        cleanup.container_ids(
            "\n".join(f"{i:064x}" for i in range(cleanup.MAX_CONTAINERS + 1))
        )

    def command(*arguments, **kwargs):
        return "a" * 12 if arguments[0] == "ps" else json.dumps([])

    monkeypatch.setattr(cleanup, "command", command)
    with pytest.raises(ValueError, match="object identity"):
        cleanup.owned_containers(Path("/approved/storage"), 10)


def test_browser_outer_wrapper_has_private_environment_and_fixed_deadline(monkeypatch):
    from script import compose_acceptance as acceptance

    seen = {}
    process = SimpleNamespace(
        pid=201, wait=lambda timeout: seen.update(timeout=timeout) or 0
    )

    def launch(arguments, **kwargs):
        seen.update(arguments=arguments, **kwargs)
        return process

    monkeypatch.setattr(acceptance.subprocess, "Popen", launch)
    monkeypatch.setattr(acceptance.os, "pidfd_open", lambda _: 44)
    monkeypatch.setattr(acceptance.os, "close", lambda _: None)
    acceptance.run_browser(
        "node",
        Path("/approved"),
        {
            "E2E_BASE_URL": "http://127.0.0.1:5173",
            "E2E_API_URL": "http://127.0.0.1:8000",
            "PATH": "/usr/bin",
            "DATABASE_URL": "private-test-field",
            "E2E_PASSWORD": "generated-test-field",
        },
    )
    assert "-I" in seen["arguments"]
    assert "DATABASE_URL" not in seen["env"]
    assert seen["timeout"] == acceptance.BROWSER_SECONDS + 55
    assert "generated-test-field" not in seen["arguments"]


def test_browser_outer_timeout_still_fails_after_requested_cleanup(monkeypatch):
    from script import compose_acceptance as acceptance

    calls = []
    outcomes = iter([acceptance.subprocess.TimeoutExpired("owned", 1), 0])

    def wait(timeout):
        calls.append(("wait", timeout))
        value = next(outcomes)
        if isinstance(value, BaseException):
            raise value
        return value

    process = SimpleNamespace(pid=201, wait=wait)
    monkeypatch.setattr(acceptance.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(acceptance.os, "pidfd_open", lambda _: 44)
    monkeypatch.setattr(acceptance.os, "close", lambda _: None)
    monkeypatch.setattr(
        acceptance.signal, "pidfd_send_signal", lambda *a: calls.append(("signal", *a))
    )
    with pytest.raises(RuntimeError, match="outer bound"):
        acceptance.run_browser(
            "node",
            Path("/approved"),
            {
                "E2E_BASE_URL": "http://127.0.0.1:5173",
                "E2E_API_URL": "http://127.0.0.1:8000",
            },
        )
    assert calls == [
        ("wait", acceptance.BROWSER_SECONDS + 55),
        ("signal", 44, acceptance.signal.SIGTERM),
        ("wait", 30),
    ]


def test_browser_uncertain_outer_cleanup_is_never_success(monkeypatch):
    from script import compose_acceptance as acceptance

    def wait(timeout):
        raise acceptance.subprocess.TimeoutExpired("owned", timeout)

    process = SimpleNamespace(pid=201, wait=wait)
    monkeypatch.setattr(acceptance.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(acceptance.os, "pidfd_open", lambda _: 44)
    monkeypatch.setattr(acceptance.os, "close", lambda _: None)
    monkeypatch.setattr(acceptance.signal, "pidfd_send_signal", lambda *a: None)
    with pytest.raises(RuntimeError, match="cleanup remains unconfirmed"):
        acceptance.run_browser(
            "node",
            Path("/approved"),
            {
                "E2E_BASE_URL": "http://127.0.0.1:5173",
                "E2E_API_URL": "http://127.0.0.1:8000",
            },
        )


def test_browser_requires_explicit_loopback_origins_before_launch(monkeypatch):
    from script import compose_acceptance as acceptance

    launch = Mock()
    monkeypatch.setattr(acceptance.subprocess, "Popen", launch)
    for source in (
        {},
        {"E2E_BASE_URL": "https://example.com", "E2E_API_URL": "http://127.0.0.1:8000"},
        {"E2E_BASE_URL": "http://127.0.0.1:5173", "E2E_API_URL": "http://127.0.0.1"},
    ):
        with pytest.raises(ValueError, match="explicit loopback"):
            acceptance.run_browser("node", Path("/approved"), source)
    launch.assert_not_called()


def test_shared_browser_entrypoint_requires_explicit_hosted_opt_in(monkeypatch):
    from script import browser_acceptance

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("TRADEGRAD_REQUIRE_BROWSER", raising=False)
    launch = Mock()
    monkeypatch.setattr(browser_acceptance, "run_browser", launch)
    with pytest.raises(SystemExit):
        browser_acceptance.main()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(SystemExit):
        browser_acceptance.main()
    launch.assert_not_called()


def test_cleanup_also_rechecks_evaluators_after_compose_down(monkeypatch, tmp_path):
    cleanup_environment(monkeypatch, tmp_path)
    commands = []
    monkeypatch.setattr(cleanup, "command", lambda *a, **k: commands.append(a) or "")
    outcomes = iter([[], [], ["a" * 64]])
    monkeypatch.setattr(cleanup, "owned_containers", lambda *a: next(outcomes))
    with pytest.raises(RuntimeError, match="after teardown"):
        cleanup.main()
    assert any("down" in command for command in commands)


def test_model_and_settings_browser_fields_remain_separate():
    from script.compose_browser_launcher import browser_environment

    result = browser_environment(
        {
            "E2E_BASE_URL": "http://127.0.0.1:5173",
            "E2E_API_URL": "http://127.0.0.1:8000",
            "E2E_ADMIN_EMAIL": "fixture@example.com",
            "E2E_ADMIN_PASSWORD": "in-memory-generated-field",
            "TRADEGRAD_REQUIRE_SETTINGS_E2E": "1",
            "E2E_MODEL_PROFILES": "1",
            "MODEL_PROFILES": "private",
        }
    )
    assert result["TRADEGRAD_REQUIRE_SETTINGS_E2E"] == "1"
    assert "E2E_ADMIN_PASSWORD" in result
    assert result["E2E_MODEL_PROFILES"] == "0"
    assert "MODEL_PROFILES" not in result
