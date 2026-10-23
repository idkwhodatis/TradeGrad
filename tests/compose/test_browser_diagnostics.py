"""Pure entrypoint diagnostics; no process, socket or browser is launched."""

import json
import secrets
from xml.etree import ElementTree as ET

import pytest

from script import browser_acceptance


@pytest.mark.parametrize("report_available", [True, False])
def test_browser_failure_exports_only_source_locations_and_stays_failed(
    monkeypatch, tmp_path, capsys, report_available
):
    private = secrets.token_urlsafe(32)
    root = tmp_path
    source = root / "frontend/tests/e2e"
    source.mkdir(parents=True)
    (source / "configured-research.spec.ts").write_text("\n" * 20)
    if report_available:
        report = ET.Element("testsuites", name=private)
        case = ET.SubElement(report, "testcase", name=private)
        failure = ET.SubElement(case, "failure", message=private)
        failure.text = private + "\n at configured-research.spec.ts:12:5\n"
        path = root / "frontend/test-results/junit.xml"
        path.parent.mkdir(parents=True)
        path.write_bytes(ET.tostring(report))
    monkeypatch.setattr(
        browser_acceptance, "__file__", str(root / "script/browser_acceptance.py")
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("TRADEGRAD_REQUIRE_BROWSER", "1")
    monkeypatch.setattr(browser_acceptance.shutil, "which", lambda _: "/reviewed/node")

    def fail(*args):
        raise RuntimeError(private)

    monkeypatch.setattr(browser_acceptance, "run_browser", fail)
    with pytest.raises(SystemExit) as error:
        browser_acceptance.main()
    assert (
        str(error.value)
        == "Hosted browser acceptance failed or containment is unconfirmed"
    )
    output = capsys.readouterr()
    assert private not in output.out + output.err + str(error.value)
    assert not output.err
    prefix = "BROWSER_SOURCE_DIAGNOSTICS_V1:"
    assert output.out.startswith(prefix)
    payload = output.out.removeprefix(prefix).strip()
    if report_available:
        assert json.loads(payload) == [
            {
                "case_index": 0,
                "outcome": "failed",
                "source_frames": [
                    {"file": "configured-research.spec.ts", "line": 12, "column": 5}
                ],
            }
        ]
    else:
        assert payload == "unavailable"
