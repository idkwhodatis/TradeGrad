"""Source-only descriptions, exact anchors and immutable prose revision contracts."""

import ast
import copy

import pytest
from pydantic import ValidationError

from backend.app.workbench_schemas import SpecRevisionCreate
from packages.research_trace.contracts import StrategySpec
from packages.research_trace.specification import (
    describe_source,
    revise_prose,
    source_locations,
    source_sha256,
    spec_sha256,
    validate_source_spec,
)
from packages.research_trace.templates import available_templates, template_source


@pytest.mark.parametrize("template", available_templates())
def test_controlled_catalog_is_exact_source_and_parameters(template):
    source = template_source(template)
    spec = describe_source(source, source_sha256(source))
    assert spec.template_id == template and spec.edges and spec.coverage == "complete"
    assert all(rule.mapping == "controlled" for rule in spec.rules)
    validate_source_spec(spec, source)
    for rule in spec.rules:
        start, end = source_locations(source)[rule.source_ref.symbol]
        assert (rule.source_ref.line_start, rule.source_ref.line_end) == (start, end)
    modified = source + "\n# Mechanism not certified by template identity\n"
    arbitrary = describe_source(modified, source_sha256(modified))
    assert arbitrary.template_id is None and not arbitrary.edges
    assert all(rule.mapping != "controlled" for rule in arbitrary.rules)


@pytest.mark.parametrize("template", available_templates())
def test_prose_revision_preserves_code_graph_and_all_nonprose_fields(template):
    source = template_source(template)
    initial = describe_source(source, source_sha256(source))
    before = initial.model_dump(mode="json")
    rule = initial.rules[0]
    changed = revise_prose(
        initial,
        [
            {
                "rule_id": rule.rule_id,
                "title": "Reviewed title",
                "description": "Human description, not runtime evidence.",
            }
        ],
        source=source,
    )
    assert initial.model_dump(mode="json") == before
    assert changed.revision == 2 and spec_sha256(changed) != spec_sha256(initial)
    assert changed.rules[0].source == "human_explanation"
    assert changed.code_sha256 == initial.code_sha256 and changed.edges == initial.edges
    for old, new in zip(initial.rules, changed.rules):
        assert old.model_dump(
            exclude={"title", "description", "source"}
        ) == new.model_dump(exclude={"title", "description", "source"})


def test_source_never_runs_or_imports(tmp_path):
    marker = tmp_path / "must-not-exist"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('do not execute')\ndef calculate():\n    return 42\n"
    result = describe_source(source, source_sha256(source))
    assert not marker.exists()
    assert result.rules[0].source_ref.symbol == "calculate"
    assert result.rules[0].mapping == "inferred" and result.missing_reasons


@pytest.mark.parametrize("source", ["x = 1\n", "def malformed(:", "# only comment\n"])
def test_unmapped_source_is_not_fabricated_logic(source):
    spec = describe_source(source, source_sha256(source))
    assert spec.template_id is None and spec.rules[0].mapping == "unmapped"
    assert spec.rules[0].source_ref.symbol == "<module>"
    assert spec.coverage == "partial" and spec.missing_reasons


def test_template_metadata_alone_never_certifies_a_graph():
    source = 'RESEARCH_TEMPLATE = {"template_id": "daily_timing.v1"}\ndef arbitrary():\n    return 0\n'
    spec = describe_source(source, source_sha256(source))
    assert spec.template_id is None and not spec.edges
    assert spec.rules[0].parameters == {} and spec.rules[0].stage_id is None


def test_native_source_in_a_different_mode_is_not_a_controlled_native_graph():
    source = template_source("daily_timing")
    spec = describe_source(source, source_sha256(source), allow_controlled=False)
    assert spec.template_id is None and not spec.edges


def test_parameter_changes_are_bound_without_source_normalization():
    source = template_source("cross_sectional")
    first = describe_source(source, source_sha256(source))
    second = describe_source(source, source_sha256(source), parameters={"top_n": 2})
    assert second.code_sha256 == first.code_sha256
    assert spec_sha256(first) != spec_sha256(second)
    assert all(rule.parameters["top_n"] == 2 for rule in second.rules)
    with pytest.raises(ValueError, match="hash"):
        describe_source(source + "\n", source_sha256(source))


@pytest.mark.parametrize(
    "tamper", ["line", "symbol", "file", "hash", "graph", "mapping"]
)
def test_source_anchor_and_controlled_graph_tampering_fails(tamper):
    source = template_source("daily_timing")
    raw = describe_source(source, source_sha256(source)).model_dump(mode="json")
    anchor = raw["rules"][0]["source_ref"]
    if tamper == "line":
        anchor["line_start"] += 1
    if tamper == "symbol":
        anchor["symbol"] = "invented"
    if tamper == "file":
        anchor["file"] = "other.py"
    if tamper == "hash":
        anchor["code_sha256"] = "0" * 64
    if tamper == "graph":
        raw["edges"] = []
    if tamper == "mapping":
        raw["rules"][0]["mapping"] = "human_confirmed"
    with pytest.raises(ValueError):
        validate_source_spec(StrategySpec.model_validate(raw), source)


@pytest.mark.parametrize(
    "edits", [[], [{"rule_id": "invented", "title": "X", "description": "Y"}]]
)
def test_edits_cannot_retarget_rule_ids(edits):
    source = "def function():\n    pass\n"
    spec = describe_source(source, source_sha256(source))
    with pytest.raises(ValueError):
        revise_prose(spec, edits, source=source)


def test_duplicate_edits_are_rejected():
    source = "x=1\n"
    spec = describe_source(source, source_sha256(source))
    edit = {"rule_id": spec.rules[0].rule_id, "title": "X", "description": "Y"}
    with pytest.raises(ValueError):
        revise_prose(spec, [edit, edit], source=source)


def test_duplicate_source_symbols_and_unicode_crlf_keep_exact_anchors():
    source = "# 中文\r\ndef same():\r\n    return 1\r\ndef same():\r\n    return 2\r\n"
    spec = describe_source(source, source_sha256(source))
    assert [rule.source_ref.symbol for rule in spec.rules] == ["same", "same@4"]
    assert [rule.source_ref.line_start for rule in spec.rules] == [2, 4]


def test_source_structure_collection_is_bounded_and_discloses_gap():
    source = "\n".join(f"def f{i}():\n    pass" for i in range(200))
    spec = describe_source(source, source_sha256(source))
    assert len(spec.rules) <= 128 and any(
        "bounded" in gap for gap in spec.missing_reasons
    )


def test_revision_request_cannot_modify_code_graph_or_runtime_fields():
    source = "x=1\n"
    spec = describe_source(source, source_sha256(source))
    body = {
        "code_sha256": spec.code_sha256,
        "base_revision": 1,
        "base_spec_sha256": spec_sha256(spec),
        "rules": [{"rule_id": "source-module", "title": "X", "description": "Y"}],
    }
    for field in ["code", "edges", "parameters", "source_ref", "mapping"]:
        invalid = copy.deepcopy(body)
        invalid["rules"][0][field] = "forbidden"
        with pytest.raises(ValidationError):
            SpecRevisionCreate.model_validate(invalid)


def test_host_inspection_module_has_no_exec_eval_compile_or_strategy_import():
    import inspect

    from packages.research_trace import specification

    tree = ast.parse(inspect.getsource(specification))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"exec", "eval", "compile", "__import__"}
        for node in ast.walk(tree)
    )
