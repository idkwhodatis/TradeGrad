"""Offline build-context allowlist contract, not an actual Docker build."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_result_adapter_has_explicit_build_context_allowlist():
    rules = (ROOT / ".dockerignore").read_text().splitlines()
    assert rules[0] == "**"
    assert {
        "!packages/",
        "!packages/tradegrad_adapter/",
        "!packages/tradegrad_adapter/results.py",
    }.issubset(rules)
    source = "packages/tradegrad_adapter/results.py"
    assert f"COPY {source} " in (ROOT / "runtimes/evaluator/Dockerfile").read_text()


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
