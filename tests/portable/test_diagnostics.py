"""Pure privacy regression; no Docker discovery, process or policy execution."""

import json
import secrets

import pytest

from tests.portable.diagnostics import assert_status, safe_diagnostic


@pytest.mark.parametrize("role", ["engine", "policy"])
def test_only_fixed_errors_exception_names_and_reviewed_frames_survive(role):
    material = secrets.token_urlsafe(32)
    result = {
        "status": "failed",
        "error": material,
        "logs": {
            f"{role}_stderr": (
                f'File "/private/{material}", line 88, in {material}\n'
                'File "/opt/tradegrad/runtimes/portable_engine/runtime.py", line 123, in execute\n'
                f"ValueError: {material}\n"
            )
        },
        "bundle": {"private": material},
    }
    safe = safe_diagnostic(result)
    if material in json.dumps(safe):
        pytest.fail("Private diagnostic material escaped allowlist", pytrace=False)
    assert safe[role] == {
        "exception": "ValueError",
        "frames": [{"module": "runtimes/portable_engine/runtime.py", "line": 123}],
    }
    assert safe["error"] == "unclassified"
    with pytest.raises(pytest.fail.Exception) as error:
        assert_status(result, "succeeded")
    if material in str(error.value):
        pytest.fail("Private result escaped test failure", pytrace=False)


def test_fixed_channel_failure_is_visible_without_raw_stderr():
    result = {
        "status": "failed",
        "error": "Portable channel ended before trusted result",
    }
    assert safe_diagnostic(result)["error"] == result["error"]
    assert_status(result, "failed")


@pytest.mark.parametrize("value", [None, {}, [], 1])
def test_malformed_log_values_are_not_stringified(value):
    result = {"status": "failed", "logs": {"engine_stderr": value}}
    assert safe_diagnostic(result)["engine"] == {
        "exception": "unclassified",
        "frames": [],
    }


@pytest.mark.parametrize("value", [None, {}, [], 1])
def test_malformed_error_status_are_not_stringified(value):
    safe = safe_diagnostic({"status": value, "error": value})
    assert safe["status"] == "unknown"
    assert safe["error"] == "unclassified"
