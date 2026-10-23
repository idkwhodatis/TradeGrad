"""Inert AST/fake-clock checks, not process, engine or database acceptance."""

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/system/test_worker_recovery.py"
TREE = ast.parse(SOURCE.read_text())
HARNESS = next(
    node
    for node in TREE.body
    if isinstance(node, ast.ClassDef) and node.name == "ProcessHarness"
)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def method(name, **globals_):
    """Compile only the chosen pure observation method, never import the harness."""
    node = next(
        node
        for node in HARNESS.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    namespace = {
        "json": json,
        "pytest": pytest,
        "datetime": datetime,
        "require": require,
        **globals_,
    }
    exec(  # noqa: S102 - only the named, repository-owned observation AST
        compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"),
        namespace,
    )
    return namespace[name]


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.parametrize("final_observation", [False, True])
def test_last_sleep_does_not_hide_success_from_requested_final_observation(
    final_observation,
):
    clock = FakeTime()
    calls = []
    alive = []
    harness = SimpleNamespace(
        remaining=lambda maximum: maximum, alive=lambda: alive.append(True)
    )

    def predicate():
        calls.append(clock.now)
        if len(calls) == 1:
            clock.now = 17.95
            return False
        return "complete immutable predicate result"

    until = method("until", time=clock)
    if final_observation:
        assert until(
            harness, predicate, "timeout", timeout=18, final_observation=True
        ) == ("complete immutable predicate result")
    else:
        with pytest.raises(pytest.fail.Exception, match="timeout"):
            until(harness, predicate, "timeout", timeout=18)
    assert len(calls) == (2 if final_observation else 1)
    assert len(alive) == len(calls)
    assert clock.sleeps == [0.15]


def test_final_observation_is_once_only_and_never_promotes_diagnostics_to_success():
    clock = FakeTime()
    calls = []
    diagnostics = []
    harness = SimpleNamespace(remaining=lambda maximum: maximum, alive=lambda: None)

    def predicate():
        calls.append(clock.now)
        clock.now = 23
        return False

    def diagnostic():
        diagnostics.append(True)
        return {"sandbox_stopped": True, "container": "absent"}

    with pytest.raises(pytest.fail.Exception, match="timeout"):
        method("until", time=clock)(
            harness,
            predicate,
            "timeout",
            timeout=22,
            final_observation=True,
            diagnostics=diagnostic,
        )
    assert len(calls) == 2
    assert diagnostics == [True]
    assert clock.sleeps == [0.15]


def test_final_observation_retains_live_process_and_overall_deadline_checks():
    clock = FakeTime()
    calls = []

    def alive():
        calls.append("alive")
        if len(calls) > 1:
            raise AssertionError("owned process stopped")

    def predicate():
        clock.now = 23
        return False

    harness = SimpleNamespace(remaining=lambda maximum: maximum, alive=alive)
    with pytest.raises(AssertionError, match="owned process stopped"):
        method("until", time=clock)(
            harness, predicate, "timeout", timeout=22, final_observation=True
        )

    def expired(maximum):
        raise AssertionError("overall harness deadline")

    harness.remaining = expired
    with pytest.raises(AssertionError, match="overall harness deadline"):
        method("until", time=clock)(
            harness, predicate, "timeout", timeout=22, final_observation=True
        )


def test_observation_budget_tracks_production_timing_without_changing_stop_deadline():
    constants = {}
    for node in TREE.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
            if name.startswith(("RECOVERY_", "RECONCILER_")):
                constants[name] = eval(
                    compile(ast.Expression(node.value), str(SOURCE), "eval"),
                    {},
                    constants,
                )
    assert constants == {
        "RECOVERY_LEASE_SECONDS": 10,
        "RECONCILER_POLL_SECONDS": 2,
        "RECOVERY_GRACE_SECONDS": 3,
        "RECOVERY_OBSERVATION_OVERHEAD_SECONDS": 4,
        "RECOVERY_OBSERVATION_SECONDS": 22,
    }
    runner = ast.parse((ROOT / "runtimes/evaluator/runner.py").read_text())
    cls = next(
        node
        for node in runner.body
        if isinstance(node, ast.ClassDef) and node.name == "DockerEvaluator"
    )
    init = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    defaults = dict(
        zip(
            (arg.arg for arg in init.args.kwonlyargs),
            init.args.kw_defaults,
            strict=True,
        )
    )
    assert (
        ast.literal_eval(defaults["grace_seconds"])
        == constants["RECOVERY_GRACE_SECONDS"]
    )
    reconciler = ast.parse((ROOT / "workers/research/__main__.py").read_text())
    sleeps = [
        node
        for node in ast.walk(reconciler)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "time.sleep"
    ]
    assert len(sleeps) == 1
    assert ast.literal_eval(sleeps[0].args[0]) == constants["RECONCILER_POLL_SECONDS"]
    init = next(
        node
        for node in HARNESS.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assert "self.deadline = time.monotonic() + 120" in ast.unparse(init)
    gate = next(
        node
        for node in TREE.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_two_concurrent")
    )
    calls = [
        node
        for node in ast.walk(gate)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "harness.until"
    ]
    final = [
        node
        for node in calls
        if any(key.arg == "final_observation" for key in node.keywords)
    ]
    assert len(final) == 1
    assert {key.arg: ast.unparse(key.value) for key in final[0].keywords} == {
        "timeout": "RECOVERY_OBSERVATION_SECONDS",
        "diagnostics": "lambda: harness.recovery_diagnostics(old)",
        "final_observation": "True",
    }


@pytest.mark.parametrize("elapsed", [19.0, 19.999, 20.0, 20.001, 22.0])
def test_full_predicate_still_rejects_stop_at_or_after_natural_completion(elapsed):
    start = datetime(2026, 10, 23, tzinfo=UTC)
    old = [{"attempt_id": "owned-attempt", "started_at": start.isoformat()}]
    detail = {
        "status": "interrupted",
        "generation": 1,
        "attempts": [
            {
                "id": "owned-attempt",
                "sandbox_stopped": True,
                "status": "interrupted",
                "finished_at": (start + timedelta(seconds=elapsed)).isoformat(),
            }
        ],
    }
    harness = SimpleNamespace(
        runs=[{"id": "owned-run"}],
        detail=lambda _: detail,
        runner=SimpleNamespace(_inspect=lambda _: None),
    )
    if elapsed < 20:
        assert method("reconciled")(harness, old) is True
    else:
        with pytest.raises(AssertionError, match="natural slow-seed completion"):
            method("reconciled")(harness, old)


@pytest.mark.parametrize(
    "unsafe", ["container_present", "unknown_stop", "wrong_attempt", "unfinished"]
)
def test_final_observation_cannot_accept_incomplete_containment(unsafe):
    start = datetime(2026, 10, 23, tzinfo=UTC)
    attempt = {
        "id": "owned-attempt",
        "sandbox_stopped": True,
        "status": "interrupted",
        "finished_at": (start + timedelta(seconds=19)).isoformat(),
    }
    container = None
    if unsafe == "container_present":
        container = {"State": {"Running": False}}
    elif unsafe == "unknown_stop":
        attempt["sandbox_stopped"] = False
    elif unsafe == "wrong_attempt":
        attempt["id"] = "other-attempt"
    else:
        attempt["finished_at"] = None
    detail = {"status": "interrupted", "generation": 1, "attempts": [attempt]}
    harness = SimpleNamespace(
        runs=[{"id": "owned-run"}],
        detail=lambda _: detail,
        runner=SimpleNamespace(_inspect=lambda _: container),
    )
    with pytest.raises(AssertionError):
        method("reconciled")(
            harness, [{"attempt_id": "owned-attempt", "started_at": start.isoformat()}]
        )


def test_final_observation_does_not_extend_expired_global_budget():
    clock = FakeTime()
    remaining_calls = []
    predicate_calls = []

    def remaining(maximum):
        remaining_calls.append(maximum)
        if len(remaining_calls) > 1:
            raise AssertionError("overall harness deadline")
        return maximum

    def predicate():
        predicate_calls.append(True)
        clock.now = 121
        return False

    harness = SimpleNamespace(remaining=remaining, alive=lambda: None)
    with pytest.raises(AssertionError, match="overall harness deadline"):
        method("until", time=clock)(
            harness, predicate, "timeout", timeout=22, final_observation=True
        )
    assert len(predicate_calls) == 1
    assert len(remaining_calls) == 2
