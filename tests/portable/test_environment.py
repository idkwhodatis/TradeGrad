"""Build-input hashes do not imply actual image or execution certification."""

import pytest

from packages.tradegrad_adapter.portable_environment import (
    INPUTS,
    TREES,
    portable_environment_digest,
    portable_environment_manifest,
)


def root(tmp_path):
    for name in INPUTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"reviewed:{name}\n")
    for name in TREES:
        path = tmp_path / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "example.py").write_text("# trusted source identity\n")
    return tmp_path


def test_every_runtime_sdk_lock_and_build_context_change_changes_identity(tmp_path):
    directory = root(tmp_path)
    manifest = portable_environment_manifest(directory)
    assert manifest["schema_version"] == "tradegrad.portable-build-inputs.v1"
    required = {
        ".dockerignore",
        "runtimes/portable_engine/Dockerfile",
        "runtimes/portable_policy/Dockerfile",
        "runtimes/portable_engine/calendar-requirements.lock",
        "runtimes/evaluator/requirements.lock",
        "runtimes/portable_policy/requirements.lock",
        "workers/research/portable_staging.py",
        "packages/tradegrad_adapter/portable_config.py",
        "packages/tradegrad_adapter/portable_results.py",
    }
    assert required <= manifest["files"].keys()
    initial = portable_environment_digest(directory)
    for name in sorted(manifest["files"]):
        path = directory / name
        before = path.read_bytes()
        path.write_bytes(before + b"changed")
        assert portable_environment_digest(directory) != initial
        path.write_bytes(before)
    (directory / "packages/strategy_sdk/new_contract.py").write_text("# added contract")
    assert portable_environment_digest(directory) != initial


def test_missing_or_symlinked_runtime_input_fails_closed(tmp_path):
    directory = root(tmp_path)
    path = directory / INPUTS[0]
    path.unlink()
    with pytest.raises(ValueError, match="missing"):
        portable_environment_manifest(directory)
    (directory / "outside").write_text("unreviewed")
    path.symlink_to(directory / "outside")
    with pytest.raises(ValueError, match="symlink"):
        portable_environment_manifest(directory)


def test_real_reviewed_portable_receipt_captures_every_lock():
    from packages.tradegrad_adapter.portable_environment import ROOT
    from packages.tradegrad_adapter.strategy_package import (
        capture_portable_environment_evidence,
    )
    from workers.research.portable_optimization import LOCKS

    manifest = portable_environment_manifest()
    receipt = capture_portable_environment_evidence(
        portable_environment_digest(),
        {"engine": "sha256:" + "a" * 64, "policy": "sha256:" + "b" * 64},
        manifest,
        LOCKS,
        ROOT,
    )
    assert set(receipt["dependency_locks"]) == {
        name for name in manifest["files"] if name.endswith("requirements.lock")
    }
