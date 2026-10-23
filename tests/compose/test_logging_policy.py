"""Inert Docker-output contracts, not daemon or rollover tests."""

import json

import pytest

from script.ci_compose_logging import SERVICES, verify

SOURCE = {"GITHUB_ACTIONS": "true", "COMPOSE_PROJECT_NAME": "tg-ci-" + "a" * 16}


def fake(changed=None):
    calls = []

    def call(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "ps":
            return "\n".join(
                f"{index:012x} {name}" for index, name in enumerate(sorted(SERVICES))
            )
        return json.dumps(
            changed
            or {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}}
        )

    return call, calls


def test_exact_scoped_service_options_without_private_inspection():
    call, calls = fake()
    report = verify(SOURCE, call, lambda: 5)
    assert report["services"] == 8 and report["runtime_configuration_verified"]
    assert report["rollover_stress_test_executed"] is False
    assert report["immutable_run_logs_deleted"] is False
    assert len(calls) == 9
    assert all(item[1]["deadline"] == 35 for item in calls)
    assert all(".HostConfig.LogConfig" in item[0][2] for item in calls[1:])
    assert "Config.Env" not in str(calls) and "logs" not in str(calls)


@pytest.mark.parametrize(
    "changed",
    [
        {"Type": "none", "Config": {}},
        {"Type": "json-file", "Config": {"max-size": "-1", "max-file": "3"}},
        {
            "Type": "json-file",
            "Config": {"max-size": "10m", "max-file": "3", "env": "SECRET_KEY"},
        },
    ],
)
def test_unsupported_or_unbounded_actual_config_fails(changed):
    call, _ = fake(changed)
    with pytest.raises(ValueError, match="logging differs"):
        verify(SOURCE, call, lambda: 5)


@pytest.mark.parametrize(
    "inventory",
    ["", "abc api", "a" * 12 + " other", "\n".join(["a" * 12 + " api"] * 8)],
)
def test_incomplete_foreign_or_duplicate_service_inventory_fails(inventory):
    def call(*args, **kwargs):
        return inventory

    with pytest.raises(ValueError):
        verify(SOURCE, call, lambda: 5)


def test_unapproved_project_and_invalid_size_never_query_daemon():
    def fail(*args, **kwargs):
        pytest.fail("Should fail before querying daemon")

    for source in (
        {},
        {**SOURCE, "COMPOSE_PROJECT_NAME": "other"},
        {**SOURCE, "TRADEGRAD_SERVICE_LOG_MAX_SIZE": "1g"},
    ):
        with pytest.raises(ValueError):
            verify(source, fail)
