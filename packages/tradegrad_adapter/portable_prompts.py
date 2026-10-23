"""Versioned portable prompt projection and source-only protected templates.

No SDK template is imported or executed. TradeTGD itself is unchanged; only its
model boundary receives the portable data/objective instructions before hashing.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROMPT_VERSION = "portable_business_rules.v1"
START = "### EDIT START"
END = "### EDIT END"
_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "strategy_sdk" / "templates"


def template_source(kind: str) -> str:
    if kind not in {"portfolio", "event"}:
        raise ValueError("Unsupported portable template kind")
    filename = "portfolio_momentum.py" if kind == "portfolio" else "event_trend.py"
    source = (_TEMPLATE_ROOT / filename).read_text(encoding="utf-8")
    if kind == "portfolio":
        start, end = (
            "            scores = []",
            '            decision = TargetPortfolio(\n                action="rebalance"',
        )
    else:
        start, end = (
            "            rising = bars[-1].close > bars[-2].close",
            "            if rising and",
        )
    if source.count(start) != 1 or source.count(end) != 1:
        raise ValueError("Reviewed template anchors changed")
    return source.replace(start, "            # " + START + "\n" + start).replace(
        end, "            # " + END + "\n" + end
    )


def validate_user_policy_source(source: str, kind: str) -> None:
    """Syntax/interface claims only. Never import or execute source here."""
    if (
        kind not in {"portfolio", "event"}
        or not isinstance(source, str)
        or not 0 < len(source.encode("utf-8")) <= 131072
    ):
        raise ValueError("Portable source must be bounded UTF-8 Python")
    tree = ast.parse(source.encode("utf-8"), filename="policy.py")
    if not any(
        isinstance(node, ast.ClassDef) and node.name == "Policy" for node in tree.body
    ):
        raise ValueError(
            "Portable source must declare a Policy class; runtime validates its interface in isolation"
        )


def validate_policy_source(
    source: str, kind: str, *, baseline: str | None = None
) -> None:
    """Reject protected changes before any sandbox is launched, without exec."""
    if not isinstance(source, str) or not 0 < len(source.encode()) <= 1_048_576:
        raise ValueError("Portable source exceeds its byte bounds")
    baseline = template_source(kind) if baseline is None else baseline
    if source.count(START) != 1 or source.count(END) != 1:
        raise ValueError(
            "Portable source must preserve exactly one protected edit region"
        )
    before, body = source.split(START)
    middle, after = body.split(END)
    expected_before, expected_body = baseline.split(START)
    _, expected_after = expected_body.split(END)
    if before != expected_before or after != expected_after:
        raise ValueError(
            "Portable source changed protected SDK, state, data or execution scaffolding"
        )
    if not middle.strip():
        raise ValueError("Portable business region cannot be empty")
    ast.parse(source, filename="policy.py")


def transform_prompt(text: str | None) -> str | None:
    if text is None:
        return None
    import re

    # Project known instructions only; fenced candidate source remains byte-exact.
    native = "`self.get_history_map` (multi-symbol) or `self.get_history` (single-symbol) — use whichever the current strategy code already uses. Available fields include `open`, `high`, `low`, `close`, `volume`."
    pieces = re.split(r"(```[^\n]*\n.*?\n```)", text, flags=re.DOTALL)
    for index in range(0, len(pieces), 2):
        pieces[index] = (
            pieces[index]
            .replace(
                native,
                "the immutable `observation.history` snapshot, ordered oldest-first and bounded by the declared lookback; never access files, networks or future bars.",
            )
            .replace(
                "`combined_score` (0-100, above 50 is good)",
                "`combined_score` (total_return_pct minus max_drawdown_pct; not the paper's native reward)",
            )
            .replace(
                "`combined_score` is ranged from 0 to 100, with higher values indicating better performance. And above 50 is considered good.",
                "`combined_score` equals total_return_pct minus max_drawdown_pct. Higher is better; this is a distinct portable objective, not a bounded native score.",
            )
        )
    text = "".join(pieces)
    return (
        text
        + "\n\nPortable contract portable_business_rules.v1: modify business rules only between the existing EDIT markers. Preserve all bytes outside them. SDK imports/types, state schema, warmup, account/data access, order capabilities and timing remain template-managed. This is a new research configuration, not paper-protocol equivalence."
    )


class PortablePromptBoundary:
    """Projection precedes RecordedModel, so persisted request hashes are actual."""

    def __init__(self, recorded):
        self.recorded = recorded

    def query(self, user_prompt, system_prompt=None, **kwargs):
        return self.recorded.query(
            transform_prompt(user_prompt), transform_prompt(system_prompt), **kwargs
        )


def parse_portable_code(response: str, language: str = "python") -> str:
    """Extract exact source bytes, without the upstream native-class restriction."""
    import re

    if (
        language != "python"
        or not isinstance(response, str)
        or len(response.encode()) > 1_100_000
    ):
        raise ValueError("Portable model response exceeds text bounds")
    match = re.search(r"```python\n(.*?)\n```", response, flags=re.DOTALL)
    if match is None:
        raise ValueError("Portable rewrite requires one Python fenced block")
    source = match.group(1)
    tree = ast.parse(source, filename="policy.py")
    if not any(
        isinstance(node, ast.ClassDef) and node.name == "Policy" for node in tree.body
    ):
        raise ValueError("Portable rewrite must retain its Policy class")
    if source.count(START) != 1 or source.count(END) != 1:
        raise ValueError("Portable rewrite must retain its one protected edit region")
    return source


def portable_step_method(source_step):
    """Bind unchanged upstream step bytecode to a private protocol parser.

    The upstream retry decorator closes over its implementation rather than
    exposing __wrapped__. Copy that trusted closure, changing only parse_code
    resolution. Neither source module globals nor the native method is mutated.
    This is not compilation or execution of a policy/model response.
    """
    from types import CellType, FunctionType

    if (
        source_step.__module__ != "src.util"
        or "func" not in source_step.__code__.co_freevars
    ):
        raise ValueError("Reviewed upstream retry wrapper changed")
    closure = []
    for name, cell in zip(
        source_step.__code__.co_freevars, source_step.__closure__, strict=True
    ):
        if name == "func":
            original = cell.cell_contents
            if (
                not isinstance(original, FunctionType)
                or original.__module__ != "src.optimizer"
                or original.__name__ != "step"
            ):
                raise ValueError("Reviewed TradeTGD step changed")
            copied = FunctionType(
                original.__code__,
                {**original.__globals__, "parse_code": parse_portable_code},
                original.__name__,
                original.__defaults__,
                original.__closure__,
            )
            closure.append(CellType(copied))
        else:
            closure.append(cell)
    return FunctionType(
        source_step.__code__,
        source_step.__globals__,
        source_step.__name__,
        source_step.__defaults__,
        tuple(closure),
    )
