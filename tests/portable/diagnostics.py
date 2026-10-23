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
        "NameError",
        "ValidationError",
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
FIXTURE_ASSERTIONS = frozenset(
    {
        "fixture_inventory_held",
        "fixture_inventory_available",
        "fixture_inventory_semantics",
        "fixture_inventory_reserved",
        "fixture_inventory_conservation",
    }
)
REJECTIONS = {
    "sell_inventory": (
        "engine",
        "runtimes/portable_engine/runtime.py",
        "event sell exceeds next-open unreserved holdings",
    ),
    "next_open": (
        "policy",
        "packages/strategy_sdk/contracts.py",
        "signal must explicitly take effect at declared NextOpen",
    ),
    "intrabar_limit": (
        "engine",
        "runtimes/portable_engine/runtime.py",
        "unsupported native intrabar-only limit fill",
    ),
    "limit_breach": (
        "engine",
        "runtimes/portable_engine/export.py",
        "unsupported non-open or limit-breaching native fill",
    ),
}
MODULES = (
    "runtimes/portable_engine/entrypoint.py",
    "runtimes/portable_engine/runtime.py",
    "runtimes/portable_engine/export.py",
    "packages/strategy_sdk/contracts.py",
    "packages/strategy_sdk/json.py",
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
        classes = re.findall(
            r"(?:^|\n)(?:pydantic_core\._pydantic_core\.)?"
            r"([A-Za-z][A-Za-z0-9]{0,40})(?::|$)",
            raw,
            flags=re.MULTILINE,
        )
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
        for code, (expected_role, module, message) in REJECTIONS.items():
            if (
                role == expected_role
                and diagnostic[role]["exception"] == "ValueError"
                and any(frame["module"] == module for frame in frames)
                and re.search(
                    r"(?:^|\n)ValueError: " + re.escape(message) + r"$",
                    raw,
                    re.MULTILINE,
                )
            ):
                diagnostic[role]["rejection"] = code
        assertions = re.findall(
            r"(?:^|\n)AssertionError: ([a-z_]+)$", raw, flags=re.MULTILINE
        )
        recognized = [value for value in assertions if value in FIXTURE_ASSERTIONS]
        if recognized:
            diagnostic[role]["fixture_assertion"] = recognized[-1]
    return diagnostic


def assert_status(result, expected):
    if result.get("status") != expected:
        # No assertion introspection or raw result dict containing candidate logs.
        pytest.fail(json.dumps(safe_diagnostic(result), sort_keys=True), pytrace=False)


def assert_rejection(result, expected):
    """A failed fixture is not evidence of the intended controller rejection."""
    assert_status(result, "failed")
    role = REJECTIONS[expected][0]
    diagnostic = safe_diagnostic(result)
    if diagnostic[role].get("rejection") != expected:
        pytest.fail(json.dumps(diagnostic, sort_keys=True), pytrace=False)
