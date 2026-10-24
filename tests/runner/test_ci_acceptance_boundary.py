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


def test_research_notes_require_actual_postgres_without_filtered_acceptance():
    import re
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/v1-validation.yml"
    ).read_text()
    application = workflow.split("  application:\n", 1)[1].split("\n  compose:", 1)[0]
    blocks = re.split(r"(?=^      - name: )", application, flags=re.MULTILINE)
    postgres = next(
        block
        for block in blocks
        if "run: .venv-data/bin/python -m pytest tests/integration " in block
    )
    assert 'TRADEGRAD_NOTES_POSTGRES_REQUIRED: "1"' in postgres
    assert "--ignore" not in postgres and " -k " not in postgres
    assert "continue-on-error" not in postgres and "|| true" not in postgres
    assert "--junitxml=m1-m2-postgres-junit.xml" in postgres


def test_portable_upload_contracts_use_required_data_runtime_and_keep_evidence():
    import re
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/v1-validation.yml"
    ).read_text()
    application = workflow.split("  application:\n", 1)[1].split("\n  compose:", 1)[0]
    blocks = re.split(r"(?=^      - name: )", application, flags=re.MULTILINE)
    source = "tests/portable/test_upload_admission.py"
    research = next(
        block
        for block in blocks
        if "run: .venv-research/bin/python -m pytest tests/portable " in block
    )
    assert f"--ignore={source}" in research
    upload = next(
        block
        for block in blocks
        if f"run: .venv-data/bin/python -m pytest {source} " in block
    )
    assert "--ignore" not in upload and " -k " not in upload
    assert "continue-on-error" not in upload and "        if:" not in upload
    report_name = "v1-portable-upload-admission-junit.xml"
    assert f"--junitxml={report_name}" in upload
    export = next(block for block in blocks if "script/summarize_ci_junit.py" in block)
    assert report_name in export


def test_daily_engineering_budget_is_required_after_fixture_performance():
    import re
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/v1-validation.yml"
    ).read_text()
    application = workflow.split("  application:\n", 1)[1].split("\n  compose:", 1)[0]
    blocks = re.split(r"(?=^      - name: )", application, flags=re.MULTILINE)
    browser = next(
        block
        for block in blocks
        if "run: python -m script.ci_application_browser" in block
    )
    budget = next(
        block
        for block in blocks
        if "python -m script.check_v1_daily_reference_budget" in block
    )
    assert "id: application_browser" in browser
    assert blocks.index(budget) > blocks.index(browser)
    assert "env.DATA_PROVIDER_MODE == 'fixture'" in budget
    assert "steps.application_browser.outcome == 'success'" in budget
    assert "continue-on-error" not in budget and "|| true" not in budget
    assert "--report var/ci-evidence/v1-daily-reference-performance.json" in budget
    assert "--receipt var/ci-evidence/v1-daily-reference-budget-receipt.json" in budget
    assert '--source-commit "${{ github.sha }}"' in budget
    assert '--github-run-id "${{ github.run_id }}"' in budget
    evidence = next(
        block
        for block in blocks
        if "name: m1-m6-${{ env.DATA_PROVIDER_MODE }}" in block
    )
    assert "var/ci-evidence/v1-daily-reference-budget-receipt.json" in evidence


def test_live_sdk_probes_cannot_suppress_independent_browser_evidence():
    import re
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/v1-validation.yml"
    ).read_text()
    application = workflow.split("  application:\n", 1)[1].split("\n  compose:", 1)[0]
    blocks = re.split(r"(?=^      - name: )", application, flags=re.MULTILINE)
    runtime = next(block for block in blocks if "id: data_runtime" in block)
    assert ".venv-data/bin/python -m pip install --require-hashes" in runtime
    browser = next(block for block in blocks if "id: application_browser" in block)
    live = next(
        block
        for block in blocks
        if "name: Explicit scoped live-provider smoke" in block
    )
    assert blocks.index(runtime) < blocks.index(browser) < blocks.index(live)
    assert "!cancelled()" in live and "env.DATA_PROVIDER_MODE == 'live'" in live
    assert "steps.data_runtime.outcome == 'success'" in live
    assert "steps.application_browser.outcome" not in live
    assert "continue-on-error" not in live and "|| true" not in live
    assert 'TRADEGRAD_MARKETDATA_LIVE: "1"' in live
    assert 'TRADEGRAD_OPENBB_LIVE: "1"' in live
    assert (
        "tests/marketdata/test_live_providers.py tests/marketdata/test_openbb.py"
        in live
    )
    assert "--junitxml=m2-live-provider-junit.xml" in live


def test_workflow_preserves_failed_steps_and_chains_verified_cleanup():
    import re
    from pathlib import Path

    # Inspect this workflow's fixed step blocks without adding a YAML dependency
    # to the separately hash-locked API runtime. GitHub parses the actual YAML.
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/v1-validation.yml"
    ).read_text()
    application = workflow.split("  application:\n", 1)[1].split("\n  compose:", 1)[0]
    blocks = re.split(r"(?=^      - name: )", application, flags=re.MULTILINE)
    previous = "application_ready"
    groups = 0
    for block in blocks:
        match = re.search(r"^        id: (\w+)$", block, flags=re.MULTILINE)
        identifier = match[1] if match else ""
        if identifier.startswith("acceptance_") and not identifier.endswith(
            "_boundary"
        ):
            groups += 1
            assert "continue-on-error" not in block
            assert re.findall(r"^        if: (.+)$", block, flags=re.MULTILINE) == [
                "${{ !cancelled() && steps." + previous + ".outcome == 'success' }}"
            ]
            assert "set +e\n" in block
            assert "status=$?\n" in block
            assert 'echo "exit_code=$status" >> "$GITHUB_OUTPUT"\n' in block
            assert block.rstrip().endswith('exit "$status"')
            previous = identifier + "_boundary"
        elif identifier.endswith("_boundary"):
            source = identifier.removesuffix("_boundary")
            assert re.findall(r"^        if: (.+)$", block, flags=re.MULTILINE) == [
                "${{ !cancelled() && steps." + source + ".outcome != 'skipped' }}"
            ]
            assert "script.ci_acceptance_boundary" in block
            assert "steps." + source + ".outputs.exit_code" in block
    assert groups >= 5
    browser = next(
        block
        for block in blocks
        if "run: python -m script.ci_application_browser" in block
    )
    assert re.findall(r"^        if: (.+)$", browser, flags=re.MULTILINE) == [
        "${{ !cancelled() && steps." + previous + ".outcome == 'success' }}"
    ]


def test_independent_runtime_acceptance_is_mandatory_and_keeps_safe_evidence():
    import re
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/v1-validation.yml"
    ).read_text()
    blocks = re.split(r"(?=^      - name: )", workflow, flags=re.MULTILINE)
    group = next(block for block in blocks if "id: acceptance_05i\n" in block)
    assert 'TRADEGRAD_REQUIRE_INDEPENDENT_INTEGRATION: "1"' in group
    assert "tests/system/test_independent_pipeline.py -q" in group
    report = "v1-independent-application-junit.xml"
    assert f"--junitxml={report}" in group
    assert "continue-on-error" not in group and "|| true" not in group
    boundary = next(
        block for block in blocks if "id: acceptance_05i_boundary\n" in block
    )
    for name in (
        "test_real_original_rng_and_audit_survive_independent_replay",
        "test_real_three_arm_no_trade_failure_retry_and_unchanged_optimizer",
        "test_real_exact_maintained_template_diagnostic_verifies_all_semantics",
    ):
        assert name in boundary
    exports = [block for block in blocks if "script/summarize_ci_junit.py" in block]
    assert any(report in block for block in exports)
