"""Fail-closed I2-I7 real-browser coverage, independent of other passing cases.

Only fixed repository case identities are compared. Private JUnit free text is
never included in exceptions, logs or uploaded evidence. Collection/skips are not
acceptance, and a duplicate/retried case cannot impersonate a missing scenario.
"""

from pathlib import Path
from xml.etree import ElementTree as ET

MAX_REPORT_BYTES = 20_000_000
MAX_CASES = 512
REQUIRED_RESEARCH_CASES = {
    "strategy-understanding.spec.ts": [
        "daily_timing: real app creation binds graph, rules and exact source; keyboard cannot edit the graph",
        "cross_sectional: real app creation binds graph, rules and exact source; keyboard cannot edit the graph",
        "human description revisions preserve code and old evidence through reload, history and login",
        "a description save with a lost response reloads and retries the same committed revision",
        "manual derived lineage stays distinct from algorithm references and arbitrary code has no controlled graph",
        "missing or mismatched source and revision pins never silently load newer evidence or another owner's strategy",
        "real isolated candidate leaves keep their own immutable pins, survive login and retain private bookmarks",
    ],
    "decision-replay.spec.ts": [
        "actual daily decisions retain non-trades, saved operands, pre-state and execution identity through history and owner checks",
        "actual cross-sectional candidate filters preserve rank losers and the full observed portfolio event",
        "native and portable capture authoring preserve immutable inputs, explicit scope and omitted default through reload",
    ],
    "mechanism-comparison.spec.ts": [
        "comment-only native versions preserve scoped unchanged observations, explicit event identity, immutable history and durable owner-scoped cache",
        "code-parent comparison exposes real allocation and fee changes, both path-dependent pre-states and exact behavioral drilldown without causal certification",
        "actual execution-only source retains one-sided native events and unknown pre-states without date or instrument pairing",
    ],
    "independent-verification.spec.ts": [
        "actual original-start diagnostic reports complete semantic parity and exact event/hash/history admission",
        "actual three-arm manual control retains failed outcomes, immutable conditions, retries and exact baseline comparison",
    ],
    "research-notes.spec.ts": [
        "rule notes persist exact revisions, search, optimistic conflicts, escaped Markdown and owner admission",
        "actual recorded decision note restores original code/spec/leaf/event/instrument/stage after spec edit and relogin",
    ],
    "continuous-research.spec.ts": [
        "continuous frozen four-symbol research restores rule, decision, parent comparison and independent control from one evidence note"
    ],
}


def require_research_cases(cases):
    if not cases or len(cases) > MAX_CASES:
        raise ValueError("Required research browser case inventory is invalid")
    for file, names in REQUIRED_RESEARCH_CASES.items():
        for name in names:
            matches = [
                case
                for case in cases
                if case.get("classname") == file and case.get("name") == name
            ]
            if len(matches) != 1 or any(
                matches[0].find(tag) is not None
                for tag in ("skipped", "failure", "error")
            ):
                raise ValueError(
                    "Required research browser scenario is missing, duplicated or not passed"
                )


def verify_research_browser_report(path: Path):
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > MAX_REPORT_BYTES
    ):
        raise ValueError("Required research browser report is unavailable")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise ValueError("Required research browser report has unsupported framing")
    root = ET.fromstring(raw)
    cases = list(root.iter("testcase"))
    if any(
        case.find(tag) is not None for case in cases for tag in ("failure", "error")
    ):
        raise ValueError("Research browser acceptance contains a failed case")
    require_research_cases(cases)
