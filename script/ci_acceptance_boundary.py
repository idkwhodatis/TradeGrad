"""Permit independent CI groups only after completed, error-free fixture teardown.

Assertion failures remain failing GitHub steps. This barrier is not a passing
test result: it only allows another privately isolated harness to run. Missing,
incomplete, all-skipped, setup/teardown-error or inconsistent reports fail closed.
"""

import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def verify_boundary(
    source: Path, outcome: str, exit_code: str, required_cases: set[str]
) -> None:
    if (
        (outcome, exit_code) not in {("success", "0"), ("failure", "1")}
        or not required_cases
        or source.is_symlink()
        or not source.is_file()
        or source.stat().st_size > 20_000_000
    ):
        raise ValueError("Acceptance completion evidence is unavailable")
    root = ET.fromstring(source.read_bytes())
    suites = root.findall("testsuite") if root.tag == "testsuites" else []
    if len(suites) != 1 or len(list(root)) != 1:
        raise ValueError("Expected one complete pytest suite")
    suite = suites[0]
    counts = {}
    for key in ("tests", "failures", "errors", "skipped"):
        value = suite.get(key, "")
        if not value.isascii() or not value.isdecimal() or len(value) > 6:
            raise ValueError("Invalid acceptance outcome count")
        counts[key] = int(value)
    cases = suite.findall("testcase")
    if counts["tests"] != len(cases) or not cases:
        raise ValueError("Incomplete acceptance test cases")
    completed = set()
    actual = {key: 0 for key in ("failures", "errors", "skipped")}
    for case in cases:
        outcomes = [
            child.tag for child in case if child.tag in {"failure", "error", "skipped"}
        ]
        if "skipped" not in outcomes and "error" not in outcomes:
            completed.add(case.get("name", "").split("[", 1)[0])
        if len(outcomes) > 1:
            raise ValueError("Ambiguous acceptance test outcome")
        for singular, plural in (
            ("failure", "failures"),
            ("error", "errors"),
            ("skipped", "skipped"),
        ):
            actual[plural] += outcomes.count(singular)
    if not required_cases.issubset(completed):
        raise ValueError("Required acceptance cases did not complete")
    if any(counts[key] != value for key, value in actual.items()):
        raise ValueError("Acceptance counts disagree with case outcomes")
    if counts["errors"] or counts["skipped"] == counts["tests"]:
        raise ValueError("Setup or teardown was not verified")
    if (outcome == "success") != (counts["failures"] == 0):
        raise ValueError("Process outcome disagrees with completed test report")


def main(arguments):
    try:
        if len(arguments) != 4:
            raise ValueError(
                "Expected report, step outcome, exit status and required cases"
            )
        verify_boundary(
            Path(arguments[0]),
            arguments[1],
            arguments[2],
            set(filter(None, arguments[3].split(","))),
        )
    except Exception:  # noqa: BLE001 - reports may contain private diagnostic payloads
        print(
            "Independent acceptance blocked: fixture teardown is unconfirmed",
            file=sys.stderr,
        )
        return 1
    print(
        "Fixture teardown verified; any prior assertion failure remains a failed gate"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
