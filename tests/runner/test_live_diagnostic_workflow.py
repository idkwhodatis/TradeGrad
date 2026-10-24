"""Pure source checks: bounded live SDK diagnosis is never full acceptance.

Do not import provider SDKs, launch services, or contact an upstream. These
checks need only pytest and the standard library in the locked API runtime.
"""

import ast
import itertools
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/v1-validation.yml").read_text()
JOBS = dict(
    re.findall(
        r"^  (\w+):\n(.*?)(?=^  \w+:\n|\Z)",
        WORKFLOW.split("\njobs:\n", 1)[1],
        flags=re.MULTILINE | re.DOTALL,
    )
)
DIAGNOSTIC = JOBS["live_sdk_diagnostic"]
REPORT = "var/ci-evidence/v1-live-source-diagnostic.json"


def _field(source, name, indent=4):
    matches = re.findall(
        rf"^{' ' * indent}{re.escape(name)}: (.+)$", source, flags=re.MULTILINE
    )
    assert len(matches) == 1, (name, matches)
    return matches[0]


def _condition(job, context):
    """Evaluate only this workflow's static boolean selector expression.

    This intentionally rejects unknown nodes rather than executing workflow
    text. No YAML dependency is added to the separately locked API runtime.
    """
    expression = _field(JOBS[job], "if")
    assert expression.startswith("${{ ") and expression.endswith(" }}")
    expression = expression[4:-3].replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression).strip()

    def value(node):
        if isinstance(node, ast.Constant):
            assert isinstance(node.value, str)
            return node.value
        if isinstance(node, ast.Attribute):
            return context[ast.unparse(node)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not value(node.operand)
        if isinstance(node, ast.BoolOp):
            values = [value(item) for item in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            assert isinstance(node.op, ast.Or)
            return any(values)
        if isinstance(node, ast.Compare):
            assert len(node.ops) == len(node.comparators) == 1
            left, right = value(node.left), value(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left.casefold() == right.casefold()
            assert isinstance(node.ops[0], ast.NotEq)
            return left.casefold() != right.casefold()
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name) and node.func.id == "contains"
            assert len(node.args) == 2 and not node.keywords
            return value(node.args[1]).casefold() in value(node.args[0]).casefold()
        raise AssertionError(f"Unsupported selector node: {type(node).__name__}")

    return value(ast.parse(expression, mode="eval").body)


MESSAGES = [
    "ordinary change",
    "[live-data]",
    "[native-final-diagnostic]",
    "[live-sdk-diagnostic]",
    "[live-sdk-diagnostic] [live-data]",
    "[live-sdk-diagnostic] [native-final-diagnostic] [live-data]",
    "[LIVE-SDK-DIAGNOSTIC]",
    "live-sdk-diagnostic without its opt-in brackets",
]
CONTEXTS = list(
    itertools.product(
        ["workflow_dispatch"],
        ["full", "native-final-diagnostic", "live-sdk-diagnostic"],
        ["fixture", "live"],
        MESSAGES,
    )
) + [
    (event, "", "", message)
    for event in ("push", "pull_request")
    for message in MESSAGES
]


@pytest.mark.parametrize("event,scope,mode,message", CONTEXTS)
def test_selectors_never_launch_other_jobs_for_live_sdk_diagnosis(
    event, scope, mode, message
):
    context = {
        "github.event_name": event,
        "inputs.validation_scope": scope,
        "inputs.provider_mode": mode,
        "github.event.head_commit.message": message,
    }
    selected = {job for job in JOBS if _condition(job, context)}
    if (event == "workflow_dispatch" and scope == "live-sdk-diagnostic") or (
        event == "push" and "[live-sdk-diagnostic]" in message.casefold()
    ):
        # Including mixed markers: diagnosis cannot trigger native/browser,
        # Compose, application, or baseline acceptance as a side effect.
        expected = {"live_sdk_diagnostic"}
    elif (event == "workflow_dispatch" and scope == "native-final-diagnostic") or (
        event == "push" and "[native-final-diagnostic]" in message.casefold()
    ):
        expected = {"native_final_diagnostic"}
    else:
        expected = {"baseline", "application"}
        if mode != "live" and not (
            event == "push" and "[live-data]" in message.casefold()
        ):
            expected.update({"compose", "research_capacity"})
    assert selected == expected


def test_dispatch_defaults_and_full_acceptance_dependencies_remain_unchanged():
    scope = WORKFLOW.split("      validation_scope:\n", 1)[1].split(
        "      provider_mode:\n", 1
    )[0]
    assert _field(scope, "default", 8) == "full"
    assert _field(scope, "type", 8) == "choice"
    assert _field(scope, "options", 8) == (
        "[full, native-final-diagnostic, live-sdk-diagnostic]"
    )
    assert set(JOBS) == {
        "baseline",
        "application",
        "compose",
        "research_capacity",
        "native_final_diagnostic",
        "live_sdk_diagnostic",
    }
    assert _field(JOBS["application"], "needs") == "baseline"
    assert _field(JOBS["compose"], "needs") == "baseline"
    assert _field(JOBS["research_capacity"], "needs") == "baseline"
    assert "needs:" not in DIAGNOSTIC
    assert "not full acceptance" in _field(DIAGNOSTIC, "name")
    assert _field(DIAGNOSTIC, "timeout-minutes") == "15"
    assert _field(DIAGNOSTIC, "runs-on") == "ubuntu-22.04"


@pytest.mark.parametrize(
    "branch,expected",
    [
        ("master", True),
        ("feat/webui-v1", True),
        ("feat/strategy-research-v1", True),
        ("master-copy", False),
        ("feat/strategy-research-v1-handoff", False),
        ("unrelated", False),
    ],
)
def test_push_branch_allowlist_includes_master_without_broadening_other_refs(
    branch, expected
):
    push = WORKFLOW.split("  push:\n", 1)[1].split("  pull_request:\n", 1)[0]
    branches = ast.literal_eval(_field(push, "branches"))
    assert branches == ["master", "feat/webui-v1", "feat/strategy-research-v1"]
    assert (branch in branches) is expected
    assert push.strip() == f"branches: {_field(push, 'branches')}"


@pytest.mark.parametrize(
    "branch", ["master", "feat/webui-v1", "feat/strategy-research-v1"]
)
def test_ordinary_push_selects_every_full_fixture_acceptance_job(branch):
    context = {
        "github.event_name": "push",
        "github.ref": f"refs/heads/{branch}",
        "github.ref_name": branch,
        "inputs.validation_scope": "",
        "inputs.provider_mode": "",
        "github.event.head_commit.message": "ci: validate direct master changes",
    }
    assert {job for job in JOBS if _condition(job, context)} == {
        "baseline",
        "application",
        "compose",
        "research_capacity",
    }
    mode = WORKFLOW.split("      provider_mode:\n", 1)[1].split("  push:\n", 1)[0]
    assert _field(mode, "default", 8) == "fixture"
    assert _field(mode, "options", 8) == "[fixture, live]"


def test_diagnostic_inherits_only_existing_read_permission_without_credentials():
    permissions = re.search(
        r"^permissions:\n(.*?)(?=^\S)", WORKFLOW, flags=re.MULTILINE | re.DOTALL
    )
    assert permissions is not None
    assert permissions[1].strip() == "contents: read"
    assert not re.search(r"^[ \t]+permissions:", WORKFLOW, flags=re.MULTILINE)
    assert not re.search(r"^env:", WORKFLOW, flags=re.MULTILINE)
    assert "persist-credentials: false" in DIAGNOSTIC
    assert all(
        forbidden not in DIAGNOSTIC.casefold()
        for forbidden in (
            "secrets.",
            "github.token",
            "password",
            "api_key",
            "postgres",
            "database_url",
            "docker",
            "playwright",
            "chromium",
            "setup-node",
            "npm ",
            "services:",
            "container:",
            "environment:",
            "continue-on-error",
            "|| true",
            "set +e",
            "exit 0",
            "tee ",
        )
    )
    # Disabling checkout credential persistence is mandatory, not a grant.
    assert "credential" not in DIAGNOSTIC.replace("persist-credentials: false", "")
    env_keys = re.findall(r"^\s{6,10}([A-Z][A-Z_]+):", DIAGNOSTIC, re.MULTILINE)
    assert set(env_keys) == {
        "PYTHONDONTWRITEBYTECODE",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "TRADEGRAD_MARKETDATA_LIVE",
        "TRADEGRAD_OPENBB_LIVE",
    }


def test_diagnostic_uses_only_pinned_actions_and_hash_locked_data_runtime():
    actions = re.findall(r"^\s+(?:- )?uses: ([^\s]+)", DIAGNOSTIC, re.MULTILINE)
    assert actions == [
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    ]
    assert 'python-version: "3.12"' in DIAGNOSTIC
    steps = re.split(r"(?=^      - )", DIAGNOSTIC, flags=re.MULTILINE)[1:]
    assert len(steps) == 5
    install = next(step for step in steps if "python -m venv" in step)
    assert "        if:" not in install
    commands = [
        line.strip()
        for line in install.split("        run: |\n", 1)[1].splitlines()
        if line.strip()
    ]
    assert commands == [
        "python -m venv .venv-data",
        (
            ".venv-data/bin/python -m pip install --require-hashes "
            "-r runtimes/data/requirements.lock"
        ),
        ".venv-data/bin/openbb-build",
        ".venv-data/bin/python -m script.validate_runtime_split --runtime data",
    ]
    probe = next(step for step in steps if "script.probe_v1_live_sources" in step)
    assert "        if:" not in probe
    assert _field(probe, "run", 8) == (
        ".venv-data/bin/python -m script.probe_v1_live_sources --report " + REPORT
    )
    assert 'TRADEGRAD_MARKETDATA_LIVE: "1"' in probe
    assert 'TRADEGRAD_OPENBB_LIVE: "1"' in probe
    # Exactly these two command-bearing steps. Any additional full-suite,
    # service startup, secret generation, fallback, or error masking fails.
    assert len(re.findall(r"^        run:", DIAGNOSTIC, re.MULTILINE)) == 2


def test_only_bounded_json_is_preserved_even_after_probe_failure():
    steps = re.split(r"(?=^      - )", DIAGNOSTIC, flags=re.MULTILINE)[1:]
    upload = steps[-1]
    assert "actions/upload-artifact@" in upload
    assert _field(upload, "if", 8) == "always()"
    assert _field(upload, "name", 10) == "live-sdk-DIAGNOSTIC-${{ github.sha }}"
    assert _field(upload, "path", 10) == REPORT
    assert _field(upload, "if-no-files-found", 10) == "error"
    assert _field(upload, "retention-days", 10) == "14"
    assert "include-hidden-files: true" not in upload
    assert not any(
        value in DIAGNOSTIC for value in ("*.xml", "*.json", "junit", "trace.zip")
    )
    for job in (
        "baseline",
        "application",
        "compose",
        "research_capacity",
        "native_final_diagnostic",
    ):
        assert REPORT not in JOBS[job]
        assert "live-sdk-DIAGNOSTIC-" not in JOBS[job]
