"""Pure source projection: raw browser report data must never escape."""

import json
import secrets
from xml.etree import ElementTree as ET

import pytest

from script.ci_browser_diagnostics import (
    failure_diagnostic,
    failure_sites,
    private_browser_report,
)


def test_only_known_source_basename_and_bounded_line_escape(tmp_path):
    source = tmp_path / "frontend/tests/e2e"
    source.mkdir(parents=True)
    (source / "final-test-workflow.spec.ts").write_text("\n" * 20)
    private = secrets.token_urlsafe(48)
    report = ET.Element("testsuites", name=private)
    case = ET.SubElement(report, "testcase", name=private, classname=private)
    error = ET.SubElement(case, "failure", message=private)
    error.text = (
        private
        + "\n at /private/runner/frontend/tests/e2e/final-test-workflow.spec.ts:12:5\n"
        " at /private/unknown.spec.ts:1:1\n at final-test-workflow.spec.ts:999:1\n"
        " at final-test-workflow.spec.ts:0:1\n at final-test-workflow.spec.ts:12:99999\n"
    )
    path = tmp_path / "private.xml"
    path.write_bytes(ET.tostring(report))
    result = failure_sites(path, tmp_path)
    assert result == [
        {
            "case_index": 0,
            "outcome": "failed",
            "source_frames": [
                {"file": "final-test-workflow.spec.ts", "line": 12, "column": 5}
            ],
        }
    ]
    assert private not in json.dumps(result)
    assert "/private" not in json.dumps(result)
    diagnostic = failure_diagnostic(path, tmp_path)
    assert diagnostic == "BROWSER_SOURCE_DIAGNOSTICS_V1:" + json.dumps(
        result, sort_keys=True
    )
    assert private not in diagnostic and "/private" not in diagnostic


@pytest.mark.parametrize(
    "content",
    [
        b"<testsuites>",
        b"<!DOCTYPE testsuites><testsuites/>",
        b'<!ENTITY hidden "x"><testsuites/>',
    ],
)
def test_malformed_or_declared_reports_fail_closed(tmp_path, content):
    path = tmp_path / "private.xml"
    path.write_bytes(content)
    with pytest.raises((ValueError, ET.ParseError)):
        failure_sites(path, tmp_path)
    assert (
        failure_diagnostic(path, tmp_path)
        == "BROWSER_SOURCE_DIAGNOSTICS_V1:unavailable"
    )


def test_missing_report_and_unknown_frames_do_not_imply_pass(tmp_path):
    with pytest.raises(ValueError):
        failure_sites(tmp_path / "missing.xml", tmp_path)
    assert (
        failure_diagnostic(tmp_path / "missing.xml", tmp_path)
        == "BROWSER_SOURCE_DIAGNOSTICS_V1:unavailable"
    )
    path = tmp_path / "private.xml"
    path.write_text(
        "<testsuites><testcase><failure>unavailable</failure></testcase></testsuites>"
    )
    assert failure_sites(path, tmp_path) == [
        {"case_index": 0, "outcome": "failed", "source_frames": []}
    ]


def test_projection_exception_text_cannot_escape(monkeypatch, tmp_path):
    def failed_projection(*_):
        raise ValueError(secrets.token_urlsafe(48))

    monkeypatch.setattr(
        "script.ci_browser_diagnostics.failure_sites", failed_projection
    )
    assert (
        failure_diagnostic(tmp_path / "private.xml", tmp_path)
        == "BROWSER_SOURCE_DIAGNOSTICS_V1:unavailable"
    )


@pytest.mark.parametrize("failed", [False, True])
def test_owned_private_report_is_removed_on_success_and_failure(tmp_path, failed):
    report = None
    try:
        with private_browser_report(tmp_path) as report:
            assert report.parent.parent == tmp_path
            assert report.parent.stat().st_mode & 0o077 == 0
            report.write_text("private generated browser diagnostic")
            if failed:
                raise RuntimeError("inert acceptance failure")
    except RuntimeError:
        assert failed
    assert report is not None and not report.parent.exists()
