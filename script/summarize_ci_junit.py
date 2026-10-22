"""Export outcome-only JUnit evidence; never upload raw diagnostic payloads.

Raw reports can contain generated credentials in assertion arguments, captured
output or serialized browser calls. Keep them on the disposable runner. This
allowlist exporter drops all free text, arbitrary attributes and attachments.
"""

import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

NUMBER = re.compile(r"[0-9]{1,10}(?:\.[0-9]{1,9})?\Z")


def summarize(source: Path, destination: Path) -> None:
    if source.is_symlink() or source.stat().st_size > 20_000_000:
        raise ValueError("Unsafe or oversized JUnit source")
    root = ET.fromstring(source.read_bytes())
    output = ET.Element("testsuites")
    for index, suite in enumerate(root.iter("testsuite")):
        target = ET.SubElement(output, "testsuite", name=f"suite-{index + 1}")
        for field in ("tests", "failures", "errors", "skipped", "time"):
            value = suite.get(field, "")
            if NUMBER.fullmatch(value):
                target.set(field, value)
        for case_index, case in enumerate(suite.findall("testcase")):
            item = ET.SubElement(target, "testcase", name=f"case-{case_index + 1}")
            duration = case.get("time", "")
            if NUMBER.fullmatch(duration):
                item.set("time", duration)
            for outcome in ("failure", "error", "skipped"):
                if case.find(outcome) is not None:
                    ET.SubElement(item, outcome, message="See masked CI job logs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(output).write(destination, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        source = Path(argument)
        if source.is_file():
            # Names come only from the workflow's explicit allowlist, not XML.
            summarize(source, Path("var/ci-evidence") / source.name)
