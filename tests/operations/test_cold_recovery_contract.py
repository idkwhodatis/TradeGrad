"""Inert preparation checks, NOT real PG/process/delete/recovery acceptance."""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "system/test_cold_retention_recovery.py"


def _tree():
    return ast.parse(SOURCE.read_text())


def _calls(tree, name):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == name
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        )
    ]


def test_hosted_gate_requires_explicit_opt_in_and_existing_gated_fixture():
    tree = _tree()
    test = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_real_")
    )
    assert [arg.arg for arg in test.args.args] == ["cold_research_harness"]
    assert len(test.decorator_list) == 1
    decorator = ast.unparse(test.decorator_list[0])
    assert "pytest.mark.skipif" in decorator
    assert "TRADEGRAD_REQUIRE_COLD_RECOVERY" in decorator
    assert "!= '1'" in decorator
    assert _calls(test, "alarm_deadline")
    assert "research_harness as research_harness" in SOURCE.read_text()
    fixture = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "cold_research_harness"
    )
    text = ast.unparse(fixture)
    assert "TRADEGRAD_REQUIRE_RESEARCH_INTEGRATION" in text
    assert "script.ci_owned_group" in text
    assert text.index("stop_processes") < text.index("getfixturevalue")


def test_fixture_progress_sql_is_read_only_and_process_signals_are_identity_safe():
    tree = _tree()
    statements = _calls(tree, "execute")
    assert len(statements) == 4
    for call in statements:
        assert isinstance(call.args[0], ast.Constant)
        assert call.args[0].value.startswith("SELECT ")
        assert ";" not in call.args[0].value
    for name in (
        "kill",
        "killpg",
        "Popen",
        "poll",
        "wait",
        "utime",
        "unlink",
        "rmtree",
    ):
        assert not _calls(tree, name), name
    signals = _calls(tree, "signal_owned_group")
    assert [ast.unparse(call.args[1]) for call in signals] == [
        "signal.SIGSTOP",
        "signal.SIGCONT",
    ]
    assert len(_calls(tree, "stop_processes")) == 2
    assert len(_calls(tree, "spawn")) == 1  # only the existing owned harness spawns


def test_real_apply_is_after_stop_and_dry_run_with_exact_plan_and_private_receipt():
    tree = _tree()
    flow = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_exercise_cold_resume"
    )
    calls = _calls(flow, "cold_retention")
    assert len(calls) == 2
    stop = _calls(flow, "stop_processes")[0]
    restart = _calls(flow, "_restart_services")[0]
    assert stop.lineno < calls[0].lineno < calls[1].lineno < restart.lineno
    keywords = {kw.arg: kw.value for kw in calls[1].keywords if kw.arg}
    assert ast.literal_eval(keywords["apply"]) is True
    assert ast.unparse(keywords["expected_plan_sha256"]) == "plan['plan_sha256']"
    assert ast.unparse(keywords["acknowledgement"]) == "ACKNOWLEDGEMENT"
    assert ast.unparse(keywords["receipt_path"]) == "receipt"
    text = ast.unparse(flow)
    for required in (
        "timedelta(days=31)",
        "before_references == after_references",
        "before_files == after_files",
        "before_files.items()",
        "CheckpointStore.load",
        "old['calls']",
        "old['lineage']",
        "old['checkpoint_id']",
        "old['artifacts']",
        "old['evaluations']",
        "len(fresh) == 1",
        "'validation'",
        "['paused', 'succeeded']",
    ):
        assert required in text, required
