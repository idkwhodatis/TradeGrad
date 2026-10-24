"""Bounded descriptive source inspection; never execute or import user source.

Controlled graph eligibility is exact-byte membership in the maintained catalog.
Human edits can change prose only; IDs, anchors, parameters and graph stay fixed.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping, Sequence

from .contracts import Rule, SourceRef, StrategySpec, digest
from .templates import available_templates, template_source, template_spec

MAX_SOURCE_BYTES = 1024 * 1024
MAX_RULES = 128


def source_sha256(source: str) -> str:
    if (
        not isinstance(source, str)
        or not source
        or len(source.encode("utf-8")) > MAX_SOURCE_BYTES
    ):
        raise ValueError("Source must be nonempty UTF-8 text of at most 1 MiB")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def source_locations(source: str) -> dict[str, tuple[int, int]]:
    """Syntax-only symbols. A symbol occurrence is disambiguated by its line."""
    source_sha256(source)
    locations = {"<module>": (1, max(1, len(source.splitlines())))}
    try:
        tree = ast.parse(source.encode("utf-8"), filename="strategy.py")
    except (SyntaxError, ValueError, RecursionError):
        return locations

    def visit(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbol = prefix + node.name
                # Duplicate definitions remain distinct and retain their real position.
                if symbol in locations:
                    symbol += f"@{node.lineno}"
                locations[symbol] = (node.lineno, node.end_lineno or node.lineno)
                if len(locations) <= MAX_RULES:
                    visit(node.body, symbol + ".")
            if len(locations) > MAX_RULES:
                return

    visit(tree.body)
    return locations


def describe_source(
    source: str,
    expected_code_sha256: str,
    *,
    parameters: dict | None = None,
    allow_controlled: bool = True,
) -> StrategySpec:
    code_hash = source_sha256(source)
    if code_hash != expected_code_sha256:
        raise ValueError("Source differs from its immutable code hash")
    for template_id in available_templates():
        if allow_controlled and source == template_source(template_id):
            return template_spec(
                template_id,
                source=source,
                expected_code_sha256=code_hash,
                parameters=parameters,
            )
    # Do not trust arbitrary RESEARCH_TEMPLATE declarations as template eligibility.
    locations = source_locations(source)
    symbols = [
        (symbol, location)
        for symbol, location in locations.items()
        if symbol != "<module>" and len(symbol) <= 200
    ]
    rules = []
    for index, (symbol, (start, end)) in enumerate(symbols[:MAX_RULES]):
        rules.append(
            Rule(
                rule_id=f"source-{index + 1}",
                category="input",
                title=symbol[:200],
                description="Syntax-only source structure. This symbol's trading rules, inputs, outputs and runtime behavior have not been reconstructed or verified.",
                parameters={},
                units={},
                inputs=[],
                outputs=[],
                source_ref=SourceRef(
                    code_sha256=code_hash,
                    file="strategy.py",
                    symbol=symbol[:200],
                    line_start=start,
                    line_end=end,
                ),
                source="static_inference",
                stage_id=None,
                mapping="inferred",
            )
        )
    if not rules:
        rules.append(
            Rule(
                rule_id="source-module",
                category="input",
                title="Unmapped source",
                description="No supported rule mapping is available. The source can be inspected, but its internal trading logic and runtime behavior are not inferred.",
                parameters={},
                units={},
                inputs=[],
                outputs=[],
                source_ref=SourceRef(
                    code_sha256=code_hash,
                    file="strategy.py",
                    symbol="<module>",
                    line_start=1,
                    line_end=locations["<module>"][1],
                ),
                source="static_inference",
                stage_id=None,
                mapping="unmapped",
            )
        )
    gaps = [
        "Arbitrary code has syntax-only inferred or unmapped cards, no controlled graph, and no claim of observed execution. Internal trace coverage depends on actual runtime instrumentation."
    ]
    if len(symbols) >= MAX_RULES:
        gaps.append(
            "Source structure is bounded to 128 symbols; additional structures may be omitted."
        )
    spec = StrategySpec(
        code_sha256=code_hash,
        template_id=None,
        rules=rules,
        edges=[],
        coverage="partial",
        missing_reasons=gaps,
        revision=1,
    )
    validate_source_spec(spec, source)
    return spec


def validate_source_spec(spec: StrategySpec, source: str) -> None:
    if spec.code_sha256 != source_sha256(source):
        raise ValueError("Specification refers to different source bytes")
    locations = source_locations(source)
    for rule in spec.rules:
        anchor = rule.source_ref
        if anchor.file != "strategy.py" or locations.get(anchor.symbol) != (
            anchor.line_start,
            anchor.line_end,
        ):
            raise ValueError(
                "Rule source anchor does not identify the exact source symbol"
            )
    if spec.template_id is not None:
        if source != template_source(spec.template_id):
            raise ValueError("Only exact maintained templates have controlled graphs")
        # Parameters are independently bound by the resource; graph and source
        # mapping must still have precisely the maintained structural contract.
        expected = template_spec(spec.template_id, source=source)
        if spec.edges != expected.edges or len(spec.rules) != len(expected.rules):
            raise ValueError(
                "Controlled graph structure differs from maintained template"
            )
        for actual, original in zip(spec.rules, expected.rules):
            if (actual.rule_id, actual.stage_id, actual.source_ref, actual.mapping) != (
                original.rule_id,
                original.stage_id,
                original.source_ref,
                original.mapping,
            ):
                raise ValueError("Controlled mapping differs from maintained template")
    elif spec.edges or any(rule.mapping == "controlled" for rule in spec.rules):
        raise ValueError("Arbitrary source cannot claim controlled mappings")


def revise_prose(
    base: StrategySpec, edits: Sequence[Mapping[str, str]], *, source: str
) -> StrategySpec:
    """Append-only revision contents; callers own persistence/concurrency checks."""
    validate_source_spec(base, source)
    by_id = {rule.rule_id: rule for rule in base.rules}
    edited = set()
    values = base.model_dump(mode="json")
    for edit in edits:
        identifier = edit["rule_id"]
        if identifier not in by_id or identifier in edited:
            raise ValueError("Edits must identify distinct existing rules")
        edited.add(identifier)
        for rule in values["rules"]:
            if rule["rule_id"] == identifier:
                rule.update(
                    title=edit["title"],
                    description=edit["description"],
                    source="human_explanation",
                )
    if not edited:
        raise ValueError("At least one rule must be revised")
    values["revision"] += 1
    result = StrategySpec.model_validate(values)
    if all(result.rules[i] == base.rules[i] for i in range(len(base.rules))):
        raise ValueError("Revision must change a rule's prose")
    validate_source_spec(result, source)
    return result


def spec_sha256(spec: StrategySpec) -> str:
    return digest(spec)
