"""Offline build-context allowlist contract, not an actual Docker build."""

import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_every_native_adapter_copy_has_explicit_build_context_allowlist():
    rules = (ROOT / ".dockerignore").read_text().splitlines()
    assert rules[0] == "**"
    assert {
        "!packages/",
        "!packages/tradegrad_adapter/",
        "!packages/tradegrad_adapter/results.py",
    }.issubset(rules)
    sources = {
        source
        for line in (ROOT / "runtimes/evaluator/Dockerfile").read_text().splitlines()
        if line.startswith("COPY ")
        for source in shlex.split(line)[1:-1]
        if source.startswith("packages/tradegrad_adapter/")
    }
    assert {
        "packages/tradegrad_adapter/results.py",
        "packages/tradegrad_adapter/native_contract.py",
        "packages/tradegrad_adapter/indicators.py",
    }.issubset(sources)
    for source in sources:
        assert (ROOT / source).is_file()
        assert f"!{source}" in rules, f"Docker COPY input is excluded: {source}"


def test_native_allowlist_does_not_open_operational_data_or_secrets():
    rules = (ROOT / ".dockerignore").read_text().splitlines()
    assert not set(rules) & {
        "!**",
        "!*",
        "!./**",
        "!backend/",
        "!frontend/",
        "!tests/",
        "!.env",
        "!runs/",
        "!storage/",
        "!artifacts/",
        "!.git/",
        "!.venv/",
    }


def test_research_capture_runtime_closure_is_explicit_and_minimal():
    rules = (ROOT / ".dockerignore").read_text().splitlines()
    assert "!packages/research_trace/" in rules
    copied = set()
    for dockerfile in (
        "runtimes/evaluator/Dockerfile",
        "runtimes/portable_engine/Dockerfile",
    ):
        for line in (ROOT / dockerfile).read_text().splitlines():
            if line.startswith("COPY "):
                copied.update(
                    source
                    for source in shlex.split(line)[1:-1]
                    if source.startswith("packages/research_trace/")
                )
    assert copied == {
        f"packages/research_trace/{name}.py"
        for name in (
            "__init__",
            "contracts",
            "recorder",
            "transport",
            "native",
            "portable",
        )
    }
    for source in copied:
        assert (ROOT / source).is_file()
        assert f"!{source}" in rules
    assert "!packages/research_trace/*.py" not in rules
    assert "!packages/research_trace/storage.py" not in rules
