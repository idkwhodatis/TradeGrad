"""Pure source projection: raw browser report data must never escape."""

import json
import re
import secrets
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from script.ci_browser_diagnostics import (
    NOTE_HTTP_CATEGORIES,
    NOTE_HTTP_CODES,
    NOTE_HTTP_FIELDS,
    NOTE_HTTP_PREFIX,
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


def note_diagnostic(**changes):
    return {
        "category": "research_note_create",
        "http_status": 422,
        "error_code": "validation_error",
        "validation_fields": ["content", "evidence", "source_id"],
        **changes,
    }


def diagnostic_report(tmp_path, text):
    report = ET.Element("testsuites")
    case = ET.SubElement(report, "testcase")
    ET.SubElement(case, "failure").text = text
    path = tmp_path / "private.xml"
    path.write_bytes(ET.tostring(report))
    return path


def test_versioned_note_http_diagnostic_exports_only_fixed_vocabulary(tmp_path):
    private = secrets.token_urlsafe(48)
    value = note_diagnostic()
    path = diagnostic_report(
        tmp_path,
        private + "\n  Error: " + NOTE_HTTP_PREFIX + json.dumps(value) + "\n" + private,
    )
    result = failure_sites(path, tmp_path)
    assert result == [
        {
            "case_index": 0,
            "outcome": "failed",
            "source_frames": [],
            "http_diagnostics": [value],
        }
    ]
    assert private not in failure_diagnostic(path, tmp_path)


@pytest.mark.parametrize(
    "changes",
    [
        {"category": "private-category"},
        {"category": ["research_note_create"]},
        {"http_status": "422"},
        {"http_status": True},
        {"http_status": 99},
        {"http_status": 600},
        {"http_status": 422.0},
        {"error_code": "private-server-message"},
        {"error_code": {"validation_error": True}},
        {"validation_fields": "content"},
        {"validation_fields": ["private-source-value"]},
        {"validation_fields": [{"content": "private"}]},
        {"validation_fields": ["title", "content"]},
        {"validation_fields": ["content", "content"]},
        {"validation_fields": sorted(NOTE_HTTP_FIELDS)[:17]},
        {"error_code": "database_unavailable"},
        {"private": "unrecognized-extra-key"},
    ],
)
def test_note_http_diagnostic_rejects_nonallowlisted_or_unbounded_values(
    tmp_path, changes
):
    path = diagnostic_report(
        tmp_path, NOTE_HTTP_PREFIX + json.dumps(note_diagnostic(**changes))
    )
    assert failure_sites(path, tmp_path) == [
        {"case_index": 0, "outcome": "failed", "source_frames": []}
    ]


@pytest.mark.parametrize(
    "text",
    [
        "RESEARCH_NOTE_HTTP_DIAGNOSTIC_V0:" + json.dumps(note_diagnostic()),
        NOTE_HTTP_PREFIX + "{malformed}",
        NOTE_HTTP_PREFIX + json.dumps(note_diagnostic()) + " private trailing text",
        NOTE_HTTP_PREFIX
        + json.dumps(note_diagnostic()).replace(
            '"http_status": 422', '"http_status": 503, "http_status": 422'
        ),
        NOTE_HTTP_PREFIX + json.dumps({"category": "research_note_create"}),
        NOTE_HTTP_PREFIX + json.dumps(note_diagnostic())[:-1] + " " * 2048 + "}",
    ],
)
def test_note_http_diagnostic_rejects_bad_framing_and_duplicate_keys(tmp_path, text):
    path = diagnostic_report(tmp_path, text)
    assert "http_diagnostics" not in failure_sites(path, tmp_path)[0]


def test_note_http_diagnostics_deduplicate_and_bound_records(tmp_path):
    values = [
        note_diagnostic(
            category="controlled_note_create",
            http_status=status,
            error_code="database_unavailable",
            validation_fields=[],
        )
        for status in range(500, 506)
    ]
    path = diagnostic_report(
        tmp_path,
        "\n".join(
            NOTE_HTTP_PREFIX + json.dumps(value)
            for value in [values[0], values[0], *values]
        ),
    )
    result = failure_sites(path, tmp_path)[0]["http_diagnostics"]
    assert len(result) == 4 and result == values[:4]


def test_note_http_diagnostics_bound_malformed_candidate_work(tmp_path):
    path = diagnostic_report(
        tmp_path,
        (NOTE_HTTP_PREFIX + "{malformed}\n") * 32
        + NOTE_HTTP_PREFIX
        + json.dumps(note_diagnostic()),
    )
    assert "http_diagnostics" not in failure_sites(path, tmp_path)[0]


def test_browser_diagnostic_report_bounds_remain_20mb_and_512_cases(tmp_path):
    path = tmp_path / "private.xml"
    path.write_bytes(b" " * 20_000_001)
    with pytest.raises(ValueError):
        failure_sites(path, tmp_path)
    report = ET.Element("testsuites")
    for _ in range(512):
        ET.SubElement(report, "testcase")
    path.write_bytes(ET.tostring(report))
    assert failure_sites(path, tmp_path) == []
    ET.SubElement(report, "testcase")
    path.write_bytes(ET.tostring(report))
    with pytest.raises(ValueError):
        failure_sites(path, tmp_path)


def test_frontend_and_exporter_share_exact_fixed_vocabulary():
    source = (
        Path(__file__).resolve().parents[2]
        / "frontend/tests/e2e/research-note-diagnostics.ts"
    ).read_text()
    for name, expected in (
        ("noteHttpCategories", NOTE_HTTP_CATEGORIES),
        ("noteHttpCodes", NOTE_HTTP_CODES),
        ("noteHttpFields", NOTE_HTTP_FIELDS),
    ):
        match = re.search(
            rf"export const {name} = (\[.*?\]) as const;", source, re.DOTALL
        )
        assert match is not None
        assert set(re.findall(r'"([^"\n]+)"', match[1])) == expected
    assert f'"{NOTE_HTTP_PREFIX}"' in source
