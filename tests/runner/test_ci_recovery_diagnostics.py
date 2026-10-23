"""Pure redaction checks; no processes, containers or database are started."""

import json
import secrets

import pytest

from script.ci_recovery_diagnostics import recovery_diagnostic


def test_recovery_diagnostics_expose_only_allowlisted_state_and_elapsed_time():
    value = recovery_diagnostic(
        ("running", "running", -12.345, 22.456, False, False, True),
        container_state="running",
    )
    assert value == {
        "database": "observed",
        "run_status": "running",
        "attempt_status": "running",
        "lease_remaining_seconds": -12.35,
        "worker_heartbeat_age_seconds": 22.46,
        "sandbox_stopped": False,
        "finished": False,
        "recovery_blocked": True,
        "container": "running",
    }


@pytest.mark.parametrize("field", range(8))
def test_unexpected_data_is_never_serialized(field):
    private = secrets.token_urlsafe(48)
    row = ["running", "running", -1, 2, False, False, False]
    state = "absent"
    if field == 7:
        state = private
    else:
        row[field] = private
    assert private not in json.dumps(recovery_diagnostic(row, container_state=state))


@pytest.mark.parametrize("number", [float("nan"), float("inf"), True, 10**1000])
def test_invalid_elapsed_values_do_not_escape_json(number):
    value = recovery_diagnostic(
        ("running", "running", number, number, False, False, False)
    )
    assert value["lease_remaining_seconds"] is None
    assert value["worker_heartbeat_age_seconds"] is None
    json.dumps(value, allow_nan=False)


def test_unavailable_database_has_no_exception_details():
    assert recovery_diagnostic(None) == {"database": "unavailable"}
