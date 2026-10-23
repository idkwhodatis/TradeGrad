"""Pure XML barrier tests; no application processes or external services."""

from xml.etree import ElementTree as ET

import pytest

from script.ci_acceptance_boundary import main, verify_boundary


def report(path, outcomes):
    root = ET.Element("testsuites")
    suite = ET.SubElement(
        root,
        "testsuite",
        tests=str(len(outcomes)),
        failures=str(outcomes.count("failure")),
        errors=str(outcomes.count("error")),
        skipped=str(outcomes.count("skipped")),
    )
    for index, outcome in enumerate(outcomes):
        case = ET.SubElement(
            suite,
            "testcase",
            name="test_required" if index == 0 else f"test_other_{index}",
        )
        if outcome:
            ET.SubElement(case, outcome)
    path.write_bytes(ET.tostring(root))
    return root


@pytest.mark.parametrize(
    "outcomes,outcome",
    [
        ([None], "success"),
        (["failure"], "failure"),
        ([None, "failure", "skipped"], "failure"),
    ],
)
def test_assertion_failures_allow_independent_harness_only_after_teardown(
    tmp_path, outcomes, outcome
):
    path = tmp_path / "result.xml"
    report(path, outcomes)
    verify_boundary(
        path, outcome, "0" if outcome == "success" else "1", {"test_required"}
    )


@pytest.mark.parametrize(
    "outcomes,outcome",
    [
        (["error"], "failure"),
        (["skipped"], "success"),
        ([], "success"),
        ([None], "failure"),
        (["failure"], "success"),
        ([None], "skipped"),
        ([None], "cancelled"),
    ],
)
def test_unconfirmed_or_inconsistent_completion_stops_following_group(
    tmp_path, outcomes, outcome
):
    path = tmp_path / "result.xml"
    report(path, outcomes)
    with pytest.raises(ValueError):
        verify_boundary(
            path, outcome, "0" if outcome == "success" else "1", {"test_required"}
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "truncated",
        "symlink",
        "test_count",
        "failure_count",
        "duplicate_outcome",
        "duplicate_suite",
        "bad_count",
        "nested_suite",
    ],
)
def test_invalid_reports_fail_closed(tmp_path, mutation):
    path = tmp_path / "result.xml"
    root = report(path, [None])
    if mutation == "missing":
        path.unlink()
    elif mutation == "truncated":
        path.write_text("<testsuites>")
    elif mutation == "symlink":
        target = tmp_path / "target.xml"
        path.rename(target)
        path.symlink_to(target)
    else:
        suite = root[0]
        if mutation == "test_count":
            suite.set("tests", "2")
        elif mutation == "failure_count":
            suite.set("failures", "1")
        elif mutation == "duplicate_outcome":
            ET.SubElement(suite[0], "failure")
            ET.SubElement(suite[0], "error")
        elif mutation == "duplicate_suite":
            ET.SubElement(root, "testsuite")
        elif mutation == "bad_count":
            suite.set("tests", "-1")
        else:
            ET.SubElement(suite, "testsuite")
            suite.remove(suite[0])
        path.write_bytes(ET.tostring(root))
    assert main([str(path), "success", "0", "test_required"]) == 1


@pytest.mark.parametrize("exit_code", ["", "2", "3", "4", "5", "130", "137"])
def test_interrupted_or_internal_pytest_exit_never_unlocks(tmp_path, exit_code):
    path = tmp_path / "result.xml"
    report(path, ["failure"])
    with pytest.raises(ValueError):
        verify_boundary(path, "failure", exit_code, {"test_required"})


def test_missing_mandatory_case_never_unlocks(tmp_path):
    path = tmp_path / "result.xml"
    report(path, [None])
    with pytest.raises(ValueError):
        verify_boundary(path, "success", "0", {"test_different"})


def test_workflow_preserves_failed_steps_and_chains_verified_cleanup():
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2] / ".github/workflows/v1-validation.yml"
        ).read_text()
    )
    steps = workflow["jobs"]["application"]["steps"]
    previous = "application_ready"
    groups = 0
    for step in steps:
        identifier = step.get("id", "")
        if identifier.startswith("acceptance_") and not identifier.endswith(
            "_boundary"
        ):
            groups += 1
            assert "continue-on-error" not in step
            assert (
                step["if"]
                == "${{ !cancelled() && steps." + previous + ".outcome == 'success' }}"
            )
            script = step["run"]
            assert "set +e\n" in script
            assert "status=$?\n" in script
            assert 'echo "exit_code=$status" >> "$GITHUB_OUTPUT"\n' in script
            assert script.rstrip().endswith('exit "$status"')
            previous = identifier + "_boundary"
        elif identifier.endswith("_boundary"):
            source = identifier.removesuffix("_boundary")
            assert (
                step["if"]
                == "${{ !cancelled() && steps." + source + ".outcome != 'skipped' }}"
            )
            assert "script.ci_acceptance_boundary" in step["run"]
            assert "steps." + source + ".outputs.exit_code" in step["run"]
    assert groups >= 5
    browser = next(
        step for step in steps if "ci_application_browser" in step.get("run", "")
    )
    assert (
        browser["if"]
        == "${{ !cancelled() && steps." + previous + ".outcome == 'success' }}"
    )
