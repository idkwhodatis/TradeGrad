"""Offline hygiene checks; these do not claim hosted or browser execution."""

import secrets
from xml.etree import ElementTree as ET

import pytest

from script.ci_test_environment import SECRET_FIELDS, create_environment, main
from script.summarize_ci_junit import summarize


def test_ci_values_are_ephemeral_distinct_and_not_printed(capsys):
    first, second = create_environment(), create_environment()
    assert len({first[name] for name in SECRET_FIELDS}) == len(SECRET_FIELDS)
    assert all(first[name] != second[name] for name in SECRET_FIELDS)
    assert all(len(first[name]) >= 64 for name in SECRET_FIELDS)
    assert first["DATABASE_URL"] == first["TEST_DATABASE_URL"]
    assert "@127.0.0.1:5432/" in first["DATABASE_URL"]
    assert capsys.readouterr().out == ""


def test_ci_helper_refuses_non_ci_invocation(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit):
        main()
    assert capsys.readouterr().out == ""


def test_junit_export_removes_all_free_text_and_arbitrary_attributes(tmp_path):
    value = secrets.token_hex(32)
    source, target = tmp_path / "raw.xml", tmp_path / "summary.xml"
    source.write_text(
        f'<testsuites name="{value}"><testsuite name="{value}" tests="3" failures="1" '
        f'errors="1" skipped="1" time="1.235" hostname="{value}">'
        f'<properties><property name="credential" value="{value}"/></properties>'
        f'<testcase name="{value}" classname="{value}" file="{value}" time="1.1">'
        f'<failure message="{value}">{value}</failure><system-out>{value}</system-out>'
        f'<system-err>{value}</system-err></testcase><testcase name="bad-token-{value}">'
        f'<error message="{value}"/></testcase><testcase name="skip-{value}">'
        f'<skipped message="{value}"/></testcase></testsuite></testsuites>'
    )
    summarize(source, target)
    result = target.read_text()
    assert value not in result
    root = ET.fromstring(result)
    assert root.find("testsuite").get("tests") == "3"
    assert len(root.findall(".//testcase")) == 3
    assert len(root.findall(".//failure")) == 1
    assert len(root.findall(".//error")) == 1
    assert len(root.findall(".//skipped")) == 1
    assert root.find(".//testcase").get("time") == "1.1"


def test_junit_export_rejects_symlink_inputs(tmp_path):
    original, link = tmp_path / "raw.xml", tmp_path / "linked.xml"
    original.write_text("<testsuites/>")
    link.symlink_to(original)
    with pytest.raises(ValueError):
        summarize(link, tmp_path / "summary.xml")


def test_junit_export_drops_non_numeric_or_excessive_numeric_fields(tmp_path):
    source, target = tmp_path / "raw.xml", tmp_path / "summary.xml"
    value = secrets.token_hex(32)
    source.write_text(
        f'<testsuite tests="{value}" failures="NaN" errors="-1" time="Infinity">'
        f'<testcase name="irrelevant" time="{10**100}"/></testsuite>'
    )
    summarize(source, target)
    result = target.read_text()
    assert value not in result
    suite = ET.fromstring(result).find("testsuite")
    assert suite.attrib == {"name": "suite-1"}
    assert suite.find("testcase").attrib == {"name": "case-1"}
