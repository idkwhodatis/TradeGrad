"""Pure launcher checks: no child, socket, signal, database or browser execution."""

import importlib.util
import os
import secrets
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "operator_browser_launcher", ROOT / "tests/system/operator_browser_launcher.py"
)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def test_node_environment_is_explicit_and_never_inherits_private_settings():
    private = secrets.token_urlsafe(32)
    password = secrets.token_urlsafe(32)
    source = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/browser-home",
        "CI": "true",
        "E2E_PASSWORD": password,
        "E2E_API_URL": "http://127.0.0.1:5001",
        "DATABASE_URL": private,
        "MODEL_PROFILES": private,
        "MODEL_API_KEY": private,
        "NODE_OPTIONS": private,
        "HTTP_PROXY": private,
        "GITHUB_ACTIONS": "true",
        "TRADEGRAD_REQUIRE_MODEL_INTEGRATION": "1",
        "E2E_MODEL_BROWSER_MODE": "forged selector",
        "PLAYWRIGHT_JUNIT_OUTPUT_FILE": "/tmp/owned-report/junit.xml",
        "PLAYWRIGHT_HTML_OUTPUT_DIR": private,
    }
    result = launcher.browser_environment(source)
    if private in result.values() or result.get("E2E_PASSWORD") != password:
        pytest.fail(
            "Browser environment violated generated-secret separation", pytrace=False
        )
    assert set(result) == {
        "PATH",
        "HOME",
        "CI",
        "E2E_PASSWORD",
        "E2E_API_URL",
        "PLAYWRIGHT_JUNIT_OUTPUT_FILE",
    }
    assert result["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] == "/tmp/owned-report/junit.xml"


@pytest.mark.parametrize(
    "mode,filename,tag",
    [
        ("native_akquant", "configured-research.spec.ts", "@operator-model-profile"),
        ("portable", "portable-research.spec.ts", "@portable-operator-profile"),
    ],
)
def test_command_is_one_fixed_browser_case_without_shell_or_install(
    mode, filename, tag
):
    command = launcher.browser_command("/usr/bin/node", Path("/approved"), mode)
    assert command[:4] == [
        "/usr/bin/node",
        "/approved/frontend/node_modules/playwright/cli.js",
        "test",
        f"tests/e2e/{filename}",
    ]
    assert command[4:6] == ["--grep", tag]
    assert "--retries=0" in command
    assert "--global-timeout=300000" in command
    assert (
        "--reporter=/approved/tests/system/operator_profile_reporter.cjs,junit"
        in command
    )


@pytest.mark.parametrize("mode", ["", "native", "all", "../path", ".*"])
def test_unsupported_mode_fails_before_containment_or_launch(monkeypatch, mode):
    monkeypatch.setattr(
        launcher, "contain", lambda *_: pytest.fail("Unexpected containment")
    )
    monkeypatch.setattr(
        launcher.subprocess, "Popen", lambda *_: pytest.fail("Unexpected child")
    )
    with pytest.raises(ValueError, match="Unsupported hosted browser mode"):
        launcher.supervise("node", ROOT, os.getpid(), mode)


def test_parent_death_race_prevents_any_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(
        launcher.ctypes,
        "CDLL",
        lambda *a, **k: SimpleNamespace(prctl=lambda *a: calls.append(a) or 0),
    )
    monkeypatch.setattr(launcher.os, "getppid", lambda: 202)
    with pytest.raises(RuntimeError, match="parent changed"):
        launcher.contain(201)
    assert calls == [(36, 1, 0, 0, 0), (1, signal.SIGTERM, 0, 0, 0)]


def test_reaping_reports_echild_as_stop_proof(monkeypatch):
    leader = SimpleNamespace(pid=201, returncode=None)
    outcomes = iter([(201, 0), ChildProcessError()])

    def waitpid(*_):
        value = next(outcomes)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(launcher.os, "waitpid", waitpid)
    assert launcher.reap_children(leader) is True
    assert leader.returncode == 0


def test_running_child_is_not_stop_proof(monkeypatch):
    monkeypatch.setattr(launcher.os, "waitpid", lambda *_: (0, 0))
    assert launcher.reap_children(None) is False


def test_cleanup_pins_and_rechecks_exact_child_before_signal(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "direct_children", lambda: [201])
    monkeypatch.setattr(launcher.os, "getpid", lambda: 100)
    monkeypatch.setattr(
        launcher.os, "pidfd_open", lambda pid: calls.append(("open", pid)) or 44
    )
    monkeypatch.setattr(launcher.Path, "read_text", lambda *_: "201 (node) S 100 201")
    monkeypatch.setattr(
        launcher.signal, "pidfd_send_signal", lambda *a: calls.append(("signal", *a))
    )
    monkeypatch.setattr(launcher.os, "close", lambda fd: calls.append(("close", fd)))
    launcher.stop_direct_children()
    assert calls == [("open", 201), ("signal", 44, signal.SIGKILL), ("close", 44)]


def test_cleanup_refuses_reparented_nonowned_pid(monkeypatch):
    monkeypatch.setattr(launcher, "direct_children", lambda: [201])
    monkeypatch.setattr(launcher.os, "getpid", lambda: 100)
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda _: 44)
    monkeypatch.setattr(launcher.Path, "read_text", lambda *_: "201 (node) S 999 201")
    monkeypatch.setattr(launcher.os, "close", lambda _: None)
    monkeypatch.setattr(
        launcher.signal,
        "pidfd_send_signal",
        lambda *a: pytest.fail("Nonowned child was signalled"),
    )
    with pytest.raises(RuntimeError, match="ownership changed"):
        launcher.stop_direct_children()


def fake_supervisor(monkeypatch, *, launch_error=None, cleanup_confirmed=True):
    calls = []
    leader = SimpleNamespace(pid=201, returncode=None)
    monkeypatch.setattr(launcher.signal, "signal", lambda *a: None)
    monkeypatch.setattr(launcher, "contain", lambda _: None)

    def launch(*a, **k):
        if launch_error:
            raise launch_error
        return leader

    monkeypatch.setattr(launcher.subprocess, "Popen", launch)
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda _: 44)
    monkeypatch.setattr(launcher.os, "close", lambda _: None)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    ticks = iter(range(1000))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(ticks))

    def reap(_):
        leader.returncode = 0
        return cleanup_confirmed

    monkeypatch.setattr(launcher, "reap_children", reap)
    monkeypatch.setattr(
        launcher, "stop_direct_children", lambda: calls.append("cleanup")
    )
    return calls


def test_success_requires_final_owned_child_cleanup(monkeypatch):
    calls = fake_supervisor(monkeypatch)
    assert launcher.supervise("node", ROOT, os.getpid()) == 0
    assert calls == ["cleanup"]


@pytest.mark.parametrize("mode", ["native_akquant", "portable"])
def test_reporter_selector_is_overwritten_by_validated_mode(monkeypatch, mode):
    fake_supervisor(monkeypatch)
    launch = launcher.subprocess.Popen
    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return launch(*args, **kwargs)

    monkeypatch.setenv("E2E_MODEL_BROWSER_MODE", "forged")
    monkeypatch.setattr(launcher.subprocess, "Popen", capture)
    assert launcher.supervise("node", ROOT, os.getpid(), mode) == 0
    assert captured["env"]["E2E_MODEL_BROWSER_MODE"] == mode
    assert set(captured["env"]) <= {*launcher.ENV_KEYS, "E2E_MODEL_BROWSER_MODE"}


def test_uncertain_launch_still_cleans_children_and_fails(monkeypatch):
    calls = fake_supervisor(monkeypatch, launch_error=KeyboardInterrupt())
    assert launcher.supervise("node", ROOT, os.getpid()) == 1
    assert calls == ["cleanup"]


def test_unconfirmed_cleanup_never_reports_success(monkeypatch):
    calls = fake_supervisor(monkeypatch, cleanup_confirmed=False)
    assert launcher.supervise("node", ROOT, os.getpid()) == 1
    assert len(calls) > 1


def test_browser_timeout_requests_grace_before_exact_cleanup(monkeypatch):
    calls = []
    leader = SimpleNamespace(pid=201, returncode=None)
    monkeypatch.setattr(launcher.signal, "signal", lambda *a: None)
    monkeypatch.setattr(launcher, "contain", lambda _: None)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: leader)
    monkeypatch.setattr(launcher.os, "pidfd_open", lambda _: 44)
    monkeypatch.setattr(launcher.os, "close", lambda _: None)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    ticks = iter(range(0, 10000, 5))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        launcher.signal, "pidfd_send_signal", lambda *a: calls.append(("grace", *a))
    )
    monkeypatch.setattr(launcher, "reap_children", lambda _: "cleanup" in calls)
    monkeypatch.setattr(
        launcher, "stop_direct_children", lambda: calls.append("cleanup")
    )
    assert launcher.supervise("node", ROOT, os.getpid()) == 1
    assert calls == [("grace", 44, signal.SIGINT), "cleanup"]


def test_launcher_main_requires_all_hosted_gates_before_supervision(monkeypatch):
    for key in (
        "GITHUB_ACTIONS",
        "TRADEGRAD_REQUIRE_MODEL_INTEGRATION",
        "TRADEGRAD_REQUIRE_MODEL_BROWSER",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        launcher, "supervise", lambda *a: pytest.fail("Unopted browser launch")
    )
    assert launcher.main() == 1
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("TRADEGRAD_REQUIRE_MODEL_INTEGRATION", "1")
    assert launcher.main() == 1


@pytest.mark.parametrize("explicit", [False, True])
def test_native_legacy_and_explicit_cli_keep_original_target(monkeypatch, explicit):
    for key, value in {
        "GITHUB_ACTIONS": "true",
        "TRADEGRAD_REQUIRE_MODEL_INTEGRATION": "1",
        "TRADEGRAD_REQUIRE_MODEL_BROWSER": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        launcher.sys,
        "argv",
        ["launcher", "node", str(ROOT), "123"]
        + (["native_akquant"] if explicit else []),
    )
    calls = []
    monkeypatch.setattr(launcher, "supervise", lambda *args: calls.append(args) or 0)
    assert launcher.main() == 0
    assert calls == [("node", ROOT, 123, "native_akquant")]


def test_portable_cli_requires_additional_opt_in_and_rejects_unknown_mode(monkeypatch):
    for key, value in {
        "GITHUB_ACTIONS": "true",
        "TRADEGRAD_REQUIRE_MODEL_INTEGRATION": "1",
        "TRADEGRAD_REQUIRE_MODEL_BROWSER": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TRADEGRAD_REQUIRE_PORTABLE_MODEL_INTEGRATION", raising=False)
    args = ["launcher", "node", str(ROOT), "123", "portable"]
    monkeypatch.setattr(launcher.sys, "argv", args)
    calls = []
    monkeypatch.setattr(
        launcher, "supervise", lambda *values: calls.append(values) or 0
    )
    assert launcher.main() == 1 and not calls
    monkeypatch.setenv("TRADEGRAD_REQUIRE_PORTABLE_MODEL_INTEGRATION", "1")
    assert launcher.main() == 0 and calls == [("node", ROOT, 123, "portable")]
    calls.clear()
    args[-1] = ".*"
    assert launcher.main() == 1 and not calls


def test_interrupt_during_final_cleanup_cannot_report_browser_success(monkeypatch):
    calls = fake_supervisor(monkeypatch)
    handlers = {}
    monkeypatch.setattr(
        launcher.signal,
        "signal",
        lambda kind, callback: handlers.update({kind: callback}),
    )

    def stop():
        calls.append("cleanup")
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(launcher, "stop_direct_children", stop)
    assert launcher.supervise("node", ROOT, os.getpid()) == 1
    assert calls == ["cleanup"]
