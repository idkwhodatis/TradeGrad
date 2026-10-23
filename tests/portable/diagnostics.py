"""Allowlisted hosted-test diagnostics, never raw policy/engine output or inputs."""

import json
import re

import pytest

MESSAGES = frozenset(
    {
        "Portable sibling isolation configuration mismatch",
        "Portable sibling input mount mismatch",
        "Unexpected portable sibling mount",
        "Portable channel ended before trusted result",
        "Portable channels ended without exactly one complete trusted result",
        "Portable attach CLI failed",
        "Portable attach CLI identity no longer reserved",
        "Portable evaluation or decision deadline exceeded",
        "Portable lease/cancellation callback blocked",
        "Portable evaluation cancelled or lease lost",
        "Portable channel refused input",
        "Portable log budget exceeded",
        "Portable frame length exceeds budget",
        "Unsolicited or trailing portable output",
        "Trusted portable result input or final-state identity mismatch",
        "Portable state continuity or decision budget violated",
        "Portable sibling termination unconfirmed; retry is blocked",
        *(
            f"Portable {role} create failed or uncertain"
            for role in ("engine", "policy")
        ),
        *(f"Portable {role} clean exit unconfirmed" for role in ("engine", "policy")),
    }
)
EXCEPTIONS = frozenset(
    {
        "ValueError",
        "TypeError",
        "AttributeError",
        "KeyError",
        "IndexError",
        "ImportError",
        "ModuleNotFoundError",
        "PermissionError",
        "FileNotFoundError",
        "RuntimeError",
        "OSError",
        "EOFError",
        "BrokenPipeError",
        "AssertionError",
        "PanicException",
    }
)
MODULES = (
    "runtimes/portable_engine/entrypoint.py",
    "runtimes/portable_engine/runtime.py",
    "runtimes/portable_policy/entrypoint.py",
    "runtimes/portable_policy/protocol.py",
    "packages/tradegrad_adapter/portable_results.py",
)


def safe_diagnostic(result):
    status = result.get("status")
    error = result.get("error")
    diagnostic = {
        "status": status
        if type(status) is str
        and status in {"succeeded", "failed", "cancelled", "interrupted"}
        else "unknown",
        "error": error if type(error) is str and error in MESSAGES else "unclassified",
    }
    logs = result.get("logs")
    for role in ("engine", "policy"):
        raw = logs.get(f"{role}_stderr", "") if type(logs) is dict else ""
        raw = raw[-32768:] if type(raw) is str else ""
        classes = re.findall(r"(?:^|\n)([A-Za-z][A-Za-z0-9]{0,40}):", raw)
        frames = []
        for module in MODULES:
            pattern = (
                re.escape(f'File "/opt/tradegrad/{module}", line ')
                + r"([0-9]{1,5})(?:,|\n)"
            )
            frames.extend(
                {"module": module, "line": int(line)}
                for line in re.findall(pattern, raw)
            )
        diagnostic[role] = {
            "exception": next(
                (name for name in reversed(classes) if name in EXCEPTIONS),
                "unclassified",
            ),
            "frames": frames[-3:],
        }
    return diagnostic


def assert_status(result, expected):
    if result.get("status") != expected:
        # No assertion introspection or raw result dict containing candidate logs.
        pytest.fail(json.dumps(safe_diagnostic(result), sort_keys=True), pytrace=False)
