"""Pure XML admission only, not browser or application execution evidence."""

import re
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from script.ci_application_browser import export_junit
from script.research_browser_acceptance import (
    REQUIRED_RESEARCH_CASES,
    require_research_cases,
    verify_research_browser_report,
)


def complete_report():
    root = ET.Element("testsuite")
    for filename, names in REQUIRED_RESEARCH_CASES.items():
        for name in names:
            ET.SubElement(root, "testcase", classname=filename, name=name)
    return root


def test_mandatory_inventory_matches_exact_real_browser_source_cases():
    root = Path(__file__).resolve().parents[2] / "frontend/tests/e2e"
    for filename, names in REQUIRED_RESEARCH_CASES.items():
        source = (root / filename).read_text()
        for name in names:
            rendered = source
            for template in ("daily_timing", "cross_sectional"):
                if name.startswith(template + ":"):
                    assert '["daily_timing", "cross_sectional"] as const' in source
                    rendered = source.replace("${templateId}", template)
            assert (
                len(
                    re.findall(
                        r"\btest\(\s*([\"\x27`])" + re.escape(name) + r"\1\s*,",
                        rendered,
                    )
                )
                == 1
            )


@pytest.mark.parametrize(
    "fault", ["skipped", "failure", "error", "missing", "duplicate", "name", "file"]
)
def test_each_required_case_must_pass_once_despite_other_passing_cases(tmp_path, fault):
    report = complete_report()
    first = report[0]
    if fault in {"skipped", "failure", "error"}:
        ET.SubElement(first, fault)
    elif fault == "missing":
        report.remove(first)
    elif fault == "duplicate":
        report.append(ET.fromstring(ET.tostring(first)))
    elif fault == "name":
        first.set("name", "unrelated passing test")
    else:
        first.set("classname", "other.spec.ts")
    path = tmp_path / "private.xml"
    path.write_bytes(ET.tostring(report))
    with pytest.raises(ValueError):
        verify_research_browser_report(path)
    with pytest.raises(ValueError):
        export_junit(path, tmp_path / "public.xml", require_research=True)


def test_complete_exact_inventory_passes_and_public_projection_has_no_titles(tmp_path):
    report = complete_report()
    require_research_cases(list(report))
    private, public = tmp_path / "private.xml", tmp_path / "public.xml"
    private.write_bytes(ET.tostring(report))
    verify_research_browser_report(private)
    export_junit(private, public, require_research=True)
    exported = public.read_text()
    assert "continuous frozen" not in exported and "spec.ts" not in exported


@pytest.mark.parametrize("fault", ["missing", "symlink", "doctype", "too_many"])
def test_unavailable_or_unsupported_report_fails_closed(tmp_path, fault):
    path = tmp_path / "private.xml"
    if fault == "symlink":
        target = tmp_path / "target.xml"
        target.write_bytes(ET.tostring(complete_report()))
        path.symlink_to(target)
    elif fault == "doctype":
        path.write_text("<!DOCTYPE testsuite><testsuite/>")
    elif fault == "too_many":
        root = complete_report()
        for _ in range(513):
            ET.SubElement(root, "testcase")
        path.write_bytes(ET.tostring(root))
    with pytest.raises(ValueError):
        verify_research_browser_report(path)


@pytest.mark.parametrize("fault", ["missing", "skipped", "failure", "duplicate"])
def test_genuine_one_sided_native_case_is_mandatory(tmp_path, fault):
    report = complete_report()
    assert len(report) == 18
    name = "actual execution-only source retains one-sided native events and unknown pre-states without date or instrument pairing"
    target = next(case for case in report if case.get("name") == name)
    assert target.get("classname") == "mechanism-comparison.spec.ts"
    if fault == "missing":
        report.remove(target)
    elif fault == "duplicate":
        report.append(ET.fromstring(ET.tostring(target)))
    else:
        ET.SubElement(target, fault)
    path = tmp_path / "private.xml"
    path.write_bytes(ET.tostring(report))
    with pytest.raises(ValueError):
        verify_research_browser_report(path)
    with pytest.raises(ValueError):
        export_junit(path, tmp_path / "public.xml", require_research=True)
