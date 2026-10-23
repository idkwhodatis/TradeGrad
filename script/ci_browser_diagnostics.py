"""Project only repository-allowlisted browser source locations from private XML."""

import re
from pathlib import Path
from xml.etree import ElementTree as ET


def failure_sites(source: Path, root: Path) -> list[dict]:
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_size > 20_000_000
    ):
        raise ValueError("Browser report is unavailable")
    content = source.read_bytes()
    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise ValueError("Unsupported report declarations")
    report = ET.fromstring(content)
    cases = list(report.iter("testcase"))
    if len(cases) > 512:
        raise ValueError("Browser case bound exceeded")
    allowed = {
        path.name: len(path.read_text().splitlines())
        for path in (root / "frontend/tests/e2e").glob("*.spec.ts")
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 1_000_000
    }
    result = []
    for index, case in enumerate(cases):
        failures = [child for child in case if child.tag in {"failure", "error"}]
        if not failures:
            continue
        frames = set()
        for failure in failures:
            # Only a known public source basename and bounded numeric line/column
            # escape. Never copy a title, message, argument, URL, body or raw stack.
            for match in re.finditer(
                r"(?:^|[\s(/])([A-Za-z0-9_-]+\.spec\.ts):(\d{1,6}):(\d{1,5})(?=\D|$)",
                failure.text or "",
            ):
                name, line, column = match[1], int(match[2]), int(match[3])
                if (
                    name in allowed
                    and 1 <= line <= allowed[name]
                    and 1 <= column <= 10000
                ):
                    frames.add((name, line, column))
        result.append(
            {
                "case_index": index,
                "outcome": "failed",
                "source_frames": [
                    {"file": name, "line": line, "column": column}
                    for name, line, column in sorted(frames)[:12]
                ],
            }
        )
    return result
