"""Pure source/fake-Popen contracts. NO child, signal, socket, DB or Docker run."""

import ast
import json
import signal
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from script import ci_application_browser as gate
from script import ci_process_owner as ownership
from script.ci_test_environment import create_environment
from script.research_browser_acceptance import REQUIRED_RESEARCH_CASES

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def forbid_real_process_execution(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("Pure test attempted a real process or signal", pytrace=False)

    monkeypatch.setattr(ownership.subprocess, "Popen", denied)
    monkeypatch.setattr(ownership.os, "pidfd_open", denied)
    monkeypatch.setattr(ownership.signal, "pidfd_send_signal", denied)
    monkeypatch.setattr(ownership.os, "waitpid", denied)
    monkeypatch.setattr(ownership, "contain", denied)
    monkeypatch.setattr(ownership, "stop_direct_children", denied)
    monkeypatch.setattr(gate, "build_opener", denied)


def source(root, mode="fixture"):
    return {
        **create_environment(),
        "PATH": "/usr/bin:/bin",
        "HOME": "/approved/home",
        "GITHUB_ACTIONS": "true",
        "TRADEGRAD_REQUIRE_APPLICATION_BROWSER": "1",
        "DATA_PROVIDER_MODE": mode,
        "E2E_LIVE_PROVIDERS": "1" if mode == "live" else "0",
        "GITHUB_WORKSPACE": str(root),
        "STORAGE_ROOT": str(root / "var/storage"),
        "TRADEGRAD_RUNNER_STATE_DIR": str(root / "var/runner"),
        "E2E_EMAIL": "first@example.com",
        "E2E_OTHER_EMAIL": "second@example.com",
        "E2E_ADMIN_EMAIL": "admin@example.invalid",
        "EVALUATOR_IMAGE": "fixture-native",
        "PORTABLE_ENGINE_IMAGE": "fixture-engine",
        "PORTABLE_POLICY_IMAGE": "fixture-policy",
        "MODEL_API_KEY": "must-not-be-inherited",
        "MODEL_PROFILES": "must-not-be-inherited",
        "OPENAI_API_KEY": "must-not-be-inherited",
        "HTTP_PROXY": "must-not-be-inherited",
        "NODE_OPTIONS": "must-not-be-inherited",
        "PYTHONPATH": "must-not-be-inherited",
        "VITE_PRIVATE": "must-not-be-inherited",
        "DOCKER_HOST": "must-not-be-inherited",
    }


@pytest.mark.parametrize("mode", ["fixture", "live"])
def test_source_requires_disposable_exact_database_and_explicit_mode(tmp_path, mode):
    env = source(tmp_path, mode)
    assert gate.validate_source(env, tmp_path) == mode
    for key, bad in (
        ("GITHUB_ACTIONS", "false"),
        ("TRADEGRAD_REQUIRE_APPLICATION_BROWSER", "0"),
        ("DATABASE_URL", "postgresql://wrong/db"),
        ("TEST_DATABASE_URL", "sqlite://"),
        ("E2E_LIVE_PROVIDERS", "0" if mode == "live" else "1"),
        ("DATA_PROVIDER_MODE", "invalid"),
        ("SECRET_KEY", "not-generated"),
        ("STORAGE_ROOT", "/outside"),
    ):
        with pytest.raises(ValueError):
            gate.validate_source({**env, key: bad}, tmp_path)


def test_vite_cannot_rehydrate_private_dotenv_settings(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/.env.local").write_text("VITE_PRIVATE=not-for-browser\n")
    with pytest.raises(ValueError, match="ambient"):
        gate.validate_source(source(tmp_path), tmp_path)


def test_role_env_allowlists_separate_logins_database_and_models(tmp_path):
    env = source(tmp_path)
    browser = gate.node_environment(env, "fixture")
    service = gate.service_environment(env, "fixture")
    http = gate.http_environment(env, "fixture")
    vite = gate.base_environment(env)
    for value in (browser, service, http, vite):
        if "must-not-be-inherited" in value.values():
            pytest.fail("Ambient secret or execution hook leaked", pytrace=False)
    for value in (browser, http, vite):
        assert "DATABASE_URL" not in value and "SECRET_KEY" not in value
    assert not any(key.startswith("E2E_") for key in service)
    assert not any(key.startswith("E2E_") for key in vite)
    if browser.get("E2E_PASSWORD") != env["E2E_PASSWORD"]:
        pytest.fail("Generated browser login not preserved", pytrace=False)
    assert browser["E2E_MODEL_PROFILES"] == "0"
    assert service["MODEL_PROFILES_ENABLED"] == "false"
    assert service["MODEL_PROFILES"] == "[]"
    assert browser["E2E_BASE_URL"] == gate.FRONTEND
    assert gate.node_environment(env, "live")["E2E_LIVE_PROVIDERS"] == "1"


def test_commands_own_all_services_and_vite_without_shell_or_npm():
    commands = gate.commands(ROOT, "/usr/bin/node")
    assert len(commands) == 7
    assert commands[-1][1].endswith("/node_modules/vite/bin/vite.js")
    assert "--strictPort" in commands[-1]
    assert {tuple(command[-2:]) for command in commands[1:6]} == {
        ("workers.research", "dispatcher"),
        ("workers.research", "worker"),
        ("workers.research", "reconciler"),
        ("workers.data", "dispatcher"),
        ("workers.data", "worker"),
    }
    assert not any(
        part in {"sh", "bash", "setsid", "npm", "npx"}
        for command in commands
        for part in command
    )


def owner():
    value = ownership.ProcessOwner(100)
    value.started = True
    return value


def test_uncertain_pidfd_open_retains_child_for_exact_final_cleanup(monkeypatch):
    value = owner()
    calls = []
    process = SimpleNamespace(pid=201, returncode=None)
    monkeypatch.setattr(
        ownership.subprocess, "Popen", lambda *a, **k: calls.append((a, k)) or process
    )
    monkeypatch.setattr(
        ownership.os, "pidfd_open", lambda _: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(OSError):
        value.spawn(["trusted"], cwd=ROOT, env={"CI": "true"})
    assert value.children[201].process is process
    assert value.children[201].fd is None
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["stdout"] == ownership.subprocess.DEVNULL
    assert calls[0][1]["stderr"] == ownership.subprocess.DEVNULL
    assert "shell" not in calls[0][1]


def test_owner_reaps_and_closes_pinned_identity_before_any_possible_reuse(monkeypatch):
    value = owner()
    child = ownership.OwnedProcess(SimpleNamespace(pid=201, returncode=None), False)
    child.fd = 44
    value.children[201] = child
    results = iter([(201, 0), ChildProcessError()])
    closed = []

    def wait(*_):
        result = next(results)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(ownership.os, "waitpid", wait)
    monkeypatch.setattr(ownership.os, "close", closed.append)
    assert value.reap() is True
    assert child.process.returncode == 0 and child.fd is None and closed == [44]
    # These checks would trip the no-real-signal barrier if a stale root were used.
    value.signal_child(child, signal.SIGKILL)


def test_pinned_signal_checks_current_parent_and_never_uses_numeric_pid(monkeypatch):
    value = owner()
    child = ownership.OwnedProcess(SimpleNamespace(pid=201, returncode=None), True)
    child.fd = 44
    calls = []
    monkeypatch.setattr(ownership.os, "getpid", lambda: 100)
    monkeypatch.setattr(
        ownership.Path, "read_text", lambda *_: "201 (worker) S 100 201"
    )
    monkeypatch.setattr(
        ownership.signal, "pidfd_send_signal", lambda *a: calls.append(a)
    )
    value.signal_child(child, signal.SIGTERM)
    assert calls == [(44, signal.SIGTERM)]
    monkeypatch.setattr(ownership.Path, "read_text", lambda *_: "201 (other) S 999 201")
    with pytest.raises(RuntimeError, match="parent changed"):
        value.signal_child(child, signal.SIGKILL)
    assert calls == [(44, signal.SIGTERM)]


def test_running_child_is_not_absence_proof(monkeypatch):
    monkeypatch.setattr(ownership.os, "waitpid", lambda *_: (0, 0))
    assert owner().reap() is False


def fake_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(ownership.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(
        ownership.time,
        "sleep",
        lambda seconds: setattr(clock, "now", clock.now + seconds),
    )
    return clock


def test_detached_descendants_require_repeated_cleanup_and_echild(monkeypatch):
    value = owner()
    fake_clock(monkeypatch)
    kills = []
    monkeypatch.setattr(value, "reap", lambda: len(kills) >= 3)
    monkeypatch.setattr(
        ownership, "stop_direct_children", lambda: kills.append("exact-adopted-child")
    )
    assert value.cleanup() is True
    assert len(kills) == 3


def test_uncertain_cleanup_never_turns_green(monkeypatch):
    value = owner()
    fake_clock(monkeypatch)
    monkeypatch.setattr(value, "reap", lambda: False)
    monkeypatch.setattr(ownership, "stop_direct_children", lambda: None)
    assert value.cleanup() is False


def test_service_death_interrupt_and_absolute_deadline_all_fail(monkeypatch):
    value = owner()
    fake_clock(monkeypatch)
    monkeypatch.setattr(value, "reap", lambda: False)
    value.interrupted = True
    with pytest.raises(RuntimeError, match="interrupted"):
        value.check()
    value.interrupted = False
    value.deadline = 0
    with pytest.raises(RuntimeError, match="expired"):
        value.check()
    value.deadline = 100
    value.children[201] = ownership.OwnedProcess(SimpleNamespace(returncode=1), True)
    with pytest.raises(RuntimeError, match="service exited"):
        value.check()


@pytest.mark.parametrize(
    "xml",
    [
        "<testsuites/>",
        "<testsuite><testcase><skipped/></testcase></testsuite>",
        "<testsuite><testcase/><testcase><failure/></testcase></testsuite>",
        "<testsuite><testcase/><error/></testsuite>",
    ],
)
def test_missing_empty_skipped_and_failed_junit_cannot_pass(tmp_path, xml):
    report = tmp_path / "private.xml"
    with pytest.raises(ValueError):
        gate.export_junit(report, tmp_path / "public.xml")
    report.write_text(xml)
    with pytest.raises(ValueError):
        gate.export_junit(report, tmp_path / "public.xml")


def test_report_sanitizes_private_text_and_live_scope_is_not_fixture_evidence(tmp_path):
    report, public = tmp_path / "private.xml", tmp_path / "public.xml"
    report.write_text(
        '<testsuite name="PRIVATE"><testcase name="PRIVATE"><system-out>PRIVATE</system-out></testcase></testsuite>'
    )
    gate.export_junit(report, public)
    assert "PRIVATE" not in public.read_text()
    with pytest.raises(ValueError, match="three scoped"):
        gate.export_junit(report, public, live=True)
    report.write_text(
        "<testsuite>" + '<testcase name="public @provider-flow"/>' * 3 + "</testsuite>"
    )
    gate.export_junit(report, public, live=True)


class FakeOwner:
    def __init__(self):
        self.spawned = []
        self.ran = []

    def check(self):
        pass

    def spawn(self, arguments, **kwargs):
        self.spawned.append((arguments, kwargs))

    def run(self, arguments, **kwargs):
        self.ran.append((arguments, kwargs))
        env = kwargs["env"]
        if "PLAYWRIGHT_JUNIT_OUTPUT_FILE" in env:
            report = ET.Element("testsuite")
            for _ in range(3):
                ET.SubElement(report, "testcase", name="public @provider-flow")
            if env.get("E2E_LIVE_PROVIDERS") == "0":
                for filename, titles in REQUIRED_RESEARCH_CASES.items():
                    for title in titles:
                        ET.SubElement(
                            report, "testcase", name=title, classname=filename
                        )
            Path(env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"]).write_text(
                ET.tostring(report, encoding="unicode")
            )
        if "script.benchmark_v1" in arguments:
            return json.dumps(
                {
                    "schema_version": "tradegrad.reference-performance.v1",
                    "status": "complete",
                }
            ).encode()
        if "script.benchmark_v1_daily_reference" in arguments:
            return json.dumps(
                {
                    "schema_version": "tradegrad.daily-reference-performance.v1",
                    "status": "succeeded",
                }
            ).encode()
        if "script.benchmark_v1_workloads" in arguments:
            return json.dumps(
                {
                    "schema_version": "tradegrad.fixture-workloads.v1",
                    "status": "succeeded",
                }
            ).encode()
        for argument in arguments:
            if argument.startswith("--junitxml="):
                Path(argument.split("=", 1)[1]).write_text(
                    "<testsuite><testcase/></testsuite>"
                )
        return b""


@pytest.mark.parametrize("mode", ["fixture", "live"])
def test_fixture_preserves_every_gate_live_is_explicit_and_never_falls_back(
    tmp_path, monkeypatch, mode
):
    private = tmp_path / "private"
    private.mkdir()
    value = FakeOwner()
    monkeypatch.setattr(gate, "wait_ready", lambda _: None)
    gate.exercise(
        value, tmp_path, source(tmp_path, mode), private, "/usr/bin/node", mode
    )
    browser = value.ran[0][0]
    assert "tests/e2e" in browser and "--global-timeout=2400000" in browser
    assert "--retries=0" in browser and "--forbid-only" in browser
    assert len(value.spawned) == (8 if mode == "fixture" else 7)
    assert all(options["service"] for _, options in value.spawned)
    assert not any(key.startswith("E2E_") for key in value.spawned[6][1]["env"])
    assert ("--grep" in browser) is (mode == "live")
    if mode == "fixture":
        assert len(value.ran) == 5
        assert "script.benchmark_v1" in value.ran[1][0]
        assert "script.benchmark_v1_workloads" in value.ran[2][0]
        assert "script.benchmark_v1_daily_reference" in value.ran[3][0]
        assert "tests/system/test_operations_pipeline.py" in value.ran[4][0]
        assert (tmp_path / "var/ci-evidence/m6-workload-performance.json").exists()
    else:
        assert browser[-2:] == ["--grep", "@provider-flow"]
        assert len(value.ran) == 1
        assert not (tmp_path / "var/ci-evidence").exists()


def test_no_group_pid_shell_or_external_dependency_escape_in_owner_sources():
    for name in ("ci_process_owner.py", "ci_application_browser.py"):
        tree = ast.parse((ROOT / "script" / name).read_text())
        forbidden = {
            "kill",
            "killpg",
            "system",
            "poll",
            "wait",
            "terminate",
            "communicate",
        }
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in forbidden
            for node in ast.walk(tree)
        )
        assert not any(
            isinstance(node, ast.keyword) and node.arg == "shell"
            for node in ast.walk(tree)
        )


def test_owned_command_absolute_timeout_leaves_identity_for_final_cleanup(monkeypatch):
    value = owner()
    clock = fake_clock(monkeypatch)
    child = ownership.OwnedProcess(SimpleNamespace(pid=201, returncode=None), False)
    monkeypatch.setattr(value, "spawn", lambda *a, **k: child)
    monkeypatch.setattr(value, "reap", lambda: False)
    with pytest.raises(TimeoutError, match="deadline"):
        value.run(["trusted"], cwd=ROOT, env={}, seconds=1)
    assert 1 <= clock.now < 2
    assert child.process.returncode is None


def test_captured_command_rejects_output_overflow_without_printing(monkeypatch, capsys):
    value = owner()
    fake_clock(monkeypatch)
    closed = []
    stream = SimpleNamespace(fileno=lambda: 42, close=lambda: closed.append(True))
    child = ownership.OwnedProcess(
        SimpleNamespace(pid=201, returncode=None, stdout=stream), False
    )
    monkeypatch.setattr(value, "spawn", lambda *a, **k: child)
    monkeypatch.setattr(value, "reap", lambda: False)
    monkeypatch.setattr(ownership.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(ownership.os, "read", lambda *a: b"PRIVATE")

    class Selector:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def register(self, *_):
            pass

        def get_map(self):
            return {42: True}

        def select(self, *_):
            return [(SimpleNamespace(fileobj=stream), None)]

    monkeypatch.setattr(ownership.selectors, "DefaultSelector", Selector)
    with pytest.raises(ValueError, match="output exceeded"):
        value.run(["trusted"], cwd=ROOT, env={}, seconds=10, maximum=3)
    assert closed == [True]
    assert "PRIVATE" not in capsys.readouterr().out


def test_adopted_cleanup_error_even_followed_by_echild_is_failure(monkeypatch):
    value = owner()
    clock = fake_clock(monkeypatch)
    calls = []

    def kill_children():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("Uncertain identity")

    monkeypatch.setattr(value, "reap", lambda: clock.now >= 5 and len(calls) > 1)
    monkeypatch.setattr(ownership, "stop_direct_children", kill_children)
    assert value.cleanup() is False
    assert len(calls) == 2


def test_unknown_child_or_nondefault_reaper_policy_prevents_launch(monkeypatch):
    value = owner()
    value.started = False
    monkeypatch.setattr(ownership.signal, "getsignal", lambda _: signal.SIG_IGN)
    with pytest.raises(RuntimeError, match="Exclusive"):
        value.contain()
    assert value.started is False
    monkeypatch.setattr(ownership.signal, "getsignal", lambda _: signal.SIG_DFL)
    monkeypatch.setattr(ownership, "direct_children", lambda: [999])
    with pytest.raises(RuntimeError, match="Exclusive"):
        value.contain()
    assert value.started is False


def test_container_cleanup_selects_full_id_plus_exact_storage_not_label_alone(tmp_path):
    own, other = "a" * 64, "b" * 64
    removals = []
    private = tmp_path / "private"
    private.mkdir()

    class ContainerOwner:
        def run(self, arguments, **kwargs):
            assert kwargs["cleanup"] is True
            assert kwargs["maximum"] == 262144 and kwargs["seconds"] <= 10
            assert "DATABASE_URL" not in kwargs["env"]
            if "ps" in arguments:
                if arguments[-1].endswith("portable-evaluator-v1"):
                    return b""
                return ((other if removals else own + " " + other) + "\n").encode()
            if "inspect" in arguments:
                identity = arguments[-1]
                path = (
                    tmp_path / "var/storage/runs/owned"
                    if identity == own
                    else Path("/another/job/storage")
                )
                return json.dumps(
                    [
                        {
                            "Id": identity,
                            "Config": {
                                "Labels": {"io.tradegrad.managed": "evaluator-v1"}
                            },
                            "Mounts": [{"Type": "bind", "Source": str(path)}],
                        }
                    ]
                ).encode()
            assert arguments[-3:-1] == ["rm", "--force"]
            removals.append(arguments[-1])
            return b""

    gate.cleanup_containers(ContainerOwner(), tmp_path, source(tmp_path), private)
    assert removals == [own]


@pytest.mark.parametrize(
    "failure",
    ["uncertain_stop", "container_cleanup", "interrupted", "exercise", "final_stop"],
)
def test_success_requires_work_and_two_process_absence_proofs_and_container_absence(
    tmp_path, monkeypatch, failure
):
    events = []

    class LifecycleOwner:
        interrupted = failure == "interrupted"

        def __init__(self, _):
            self.cleanups = 0

        def contain(self):
            events.append("contain")

        def check(self):
            pass

        def cleanup(self):
            self.cleanups += 1
            events.append("stop")
            return not (
                (failure == "uncertain_stop" and self.cleanups == 1)
                or (failure == "final_stop" and self.cleanups == 2)
            )

    def exercise(*_):
        events.append("exercise")
        if failure == "exercise":
            raise KeyboardInterrupt()

    def containers(*_):
        events.append("containers")
        if failure == "container_cleanup":
            raise RuntimeError("Uncertain container absence")

    monkeypatch.setattr(gate, "ProcessOwner", LifecycleOwner)
    monkeypatch.setattr(gate, "exercise", exercise)
    monkeypatch.setattr(gate, "cleanup_containers", containers)
    monkeypatch.setattr(gate.shutil, "which", lambda *a, **k: "/usr/bin/node")
    assert gate.run(source(tmp_path), tmp_path) == 1
    assert events[:3] == ["contain", "exercise", "stop"]
    assert events[-1] == "stop"
    assert ("containers" in events) is (failure != "uncertain_stop")


def test_main_emits_only_fixed_diagnostic_on_private_exception(monkeypatch, capsys):
    def fail(*_):
        raise RuntimeError("PRIVATE_DATABASE_AND_BROWSER_CREDENTIALS")

    monkeypatch.setattr(gate, "run", fail)
    monkeypatch.setattr(gate.os, "umask", lambda _: None)
    assert gate.main() == 1
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    assert "unconfirmed" in captured.err


@pytest.mark.parametrize("stage,commands", [("m5", 1), ("m6", 3), ("completion", 5)])
def test_explicit_stage_preserves_only_that_stage_acceptance(
    tmp_path, monkeypatch, stage, commands
):
    private = tmp_path / "private"
    private.mkdir()
    env = source(tmp_path)
    env["TRADEGRAD_APPLICATION_STAGE"] = stage
    if stage != "completion":
        env.pop("E2E_ADMIN_EMAIL")
    assert gate.validate_source(env, tmp_path) == "fixture"
    assert gate.node_environment(env, "fixture")["TRADEGRAD_REQUIRE_SETTINGS_E2E"] == (
        "1" if stage == "completion" else "0"
    )
    value = FakeOwner()
    monkeypatch.setattr(gate, "wait_ready", lambda _: None)
    gate.exercise(value, tmp_path, env, private, "/usr/bin/node", "fixture")
    assert len(value.ran) == commands
    assert "tests/e2e" in value.ran[0][0] and "--grep" not in value.ran[0][0]
    if stage != "m5":
        assert "script.benchmark_v1" in value.ran[1][0]
        assert "script.benchmark_v1_workloads" in value.ran[2][0]


def test_unknown_stage_never_silently_skips_gates(tmp_path):
    env = source(tmp_path)
    env["TRADEGRAD_APPLICATION_STAGE"] = "typo"
    with pytest.raises(ValueError, match="stage"):
        gate.validate_source(env, tmp_path)


def test_native_final_diagnostic_is_explicit_fixture_only_and_fixed_target(
    tmp_path, monkeypatch
):
    env = source(tmp_path)
    env.update(
        TRADEGRAD_APPLICATION_STAGE="m5", TRADEGRAD_BROWSER_DIAGNOSTIC="native-final"
    )
    assert gate.validate_source(env, tmp_path) == "fixture"
    for update in (
        {"DATA_PROVIDER_MODE": "live", "E2E_LIVE_PROVIDERS": "1"},
        {"TRADEGRAD_APPLICATION_STAGE": "completion"},
        {"TRADEGRAD_BROWSER_DIAGNOSTIC": "arbitrary"},
    ):
        with pytest.raises(ValueError):
            gate.validate_source({**env, **update}, tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    value = FakeOwner()
    monkeypatch.setattr(gate, "wait_ready", lambda *_: None)
    gate.exercise(value, tmp_path, env, private, "node", "fixture")
    browser = next(
        arguments
        for arguments, kwargs in value.ran
        if "PLAYWRIGHT_JUNIT_OUTPUT_FILE" in kwargs["env"]
    )
    assert browser[3] == "tests/e2e/final-test-workflow.spec.ts"
    assert "--forbid-only" in browser and "--retries=0" in browser
    assert not any("script.benchmark_v1" in arguments for arguments, _ in value.ran)


def test_diagnostic_workflow_cannot_be_mistaken_for_full_gate():
    workflow = (ROOT / ".github/workflows/v1-validation.yml").read_text()
    baseline = workflow.split("  baseline:\n", 1)[1].split("  application:\n", 1)[0]
    diagnostic = workflow.split("  native_final_diagnostic:\n", 1)[1]
    assert "if: ${{ !(" in baseline and "[native-final-diagnostic]" in baseline
    assert "Native final Chromium diagnostic ONLY, not full acceptance" in diagnostic
    assert "TRADEGRAD_BROWSER_DIAGNOSTIC: native-final" in diagnostic
    assert "DATA_PROVIDER_MODE: fixture" in diagnostic
    assert "native-final-DIAGNOSTIC-${{ github.sha }}" in diagnostic
    assert "persist-credentials: false" in diagnostic
    assert "pytest tests/system" not in diagnostic
    assert "Build local evaluator image (never published)" in diagnostic
    assert "TRADEGRAD_APPLICATION_STAGE: m5" in diagnostic
