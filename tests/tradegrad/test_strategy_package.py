"""Pure deterministic synthetic package evidence, not image/isolation acceptance."""

import copy
import io
import json
import secrets
import stat
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from packages.tradegrad_adapter import strategy_package as package
from packages.tradegrad_adapter.strategy_package import (
    LOCK_PATHS,
    MAX_CODE_BYTES,
    MAX_ENTRY_BYTES,
    PackageError,
    PackageInput,
    build_strategy_package,
    canonical,
    capture_environment_evidence,
    capture_portable_environment_evidence,
    deterministic_archive,
    parse_canonical,
    reject_embedded_secrets,
    safe_relative_path,
    sha256,
    validate_environment_evidence,
)


@pytest.fixture
def package_input():
    """Invented image IDs and result bytes exercise the contract only."""
    code = b"# synthetic package fixture, never executed\nclass Strategy: pass\n"
    locks = {
        name: {
            "content": "fixture-package==0.0.0\n",
            "sha256": sha256(b"fixture-package==0.0.0\n"),
        }
        for name in LOCK_PATHS
    }
    build = {
        "schema_version": "tradegrad.build-inputs.v1",
        "files": {name: item["sha256"] for name, item in locks.items()},
    }
    environment = {
        "schema_version": package.NATIVE_ENV_SCHEMA,
        "environment_digest": sha256(canonical(build)),
        "image_id": "sha256:" + "a" * 64,
        "build_inputs": build,
        "dependency_locks": locks,
        "provenance": "captured_at_execution",
    }
    dataset = {
        "is_synthetic": True,
        "files": {"data/INDEX.csv": "b" * 64},
        "artifact_files": {"raw.json": "c" * 64},
        "evaluator_symbol_mapping": {"fixture-listing": "INDEX"},
        "price_semantics": {"native_mode": "synthetic"},
        "listings": [{"calendar": "XSHG", "timezone": "Asia/Shanghai"}],
    }
    source = {
        "schema_version": "tradegrad.run.v1",
        "dataset": {"sha256": sha256(canonical(dataset))},
        "environment_digest": environment["environment_digest"],
        "model": {"credential_ref": "omitted-server-reference"},
        "prompt_version": "source_prompts.v1",
        "universe": {"sha256": "e" * 64},
    }
    final = {
        "schema_version": "tradegrad.final-evaluation.v1",
        "source_config": source,
        "source_config_sha256": sha256(canonical(source)),
        "environment_digest": environment["environment_digest"],
        "holdout": {"start": "2025-01-01", "end": "2025-02-01"},
    }
    selection = {
        "source_code_sha256": sha256(code),
        "source_config_sha256": sha256(canonical(source)),
        "final_config_sha256": sha256(canonical(final)),
    }
    result = canonical(
        {
            "schema_version": "synthetic.contract-fixture",
            "decision_trace": {
                "status": "synthetic_not_execution_evidence",
                "decisions": [],
            },
        }
    )
    return PackageInput(
        code,
        "native_akquant",
        sha256(code),
        None,
        selection,
        source,
        final,
        dataset,
        {
            "schema_version": "tradegrad.universe-export.v1",
            "reference_sha256": "e" * 64,
            "serialization": "serialization_not_original",
            "manifest": {"state": "user_fixed", "members": []},
            "manifest_sha256": sha256(
                canonical({"state": "user_fixed", "members": []})
            ),
        },
        {"schema_version": "tradegrad.split.v1"},
        environment,
        result,
        sha256(result),
        {
            "classification": "previously_exposed",
            "independent_unseen_holdout_established": False,
        },
        "2026-10-22T00:00:00+00:00",
    )


def test_exact_repeat_bytes_hashes_and_fixed_zip_metadata(package_input):
    first = build_strategy_package(package_input)
    second = build_strategy_package(copy.deepcopy(package_input))
    assert first == second
    assert first.archive_sha256 == sha256(first.archive)
    assert first.archive_sha256 != first.content_sha256
    with zipfile.ZipFile(io.BytesIO(first.archive)) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert len(archive.infolist()) <= 128
        for entry in archive.infolist():
            assert entry.date_time == (1980, 1, 1, 0, 0, 0)
            assert entry.create_system == 3
            assert entry.compress_type == zipfile.ZIP_STORED
            assert entry.extra == entry.comment == b""
            assert stat.S_ISREG(entry.external_attr >> 16)
            assert stat.S_IMODE(entry.external_attr >> 16) == 0o444
        assert archive.read("strategy/strategy.py") == package_input.strategy_code
        assert (
            archive.read("reference/result-bundle.json") == package_input.result_content
        )
        inventory = first.manifest["files"]
        assert first.content_sha256 == sha256(canonical(inventory))
        assert "package-manifest.json" not in inventory
        for path, identity in inventory.items():
            assert identity == {
                "sha256": sha256(archive.read(path)),
                "bytes": len(archive.read(path)),
            }
        for path in archive.namelist():
            if path.endswith(".json"):
                assert canonical(json.loads(archive.read(path))) == archive.read(path)


def test_reference_only_exclusions_parameters_and_separate_capabilities(package_input):
    result = build_strategy_package(package_input)
    with zipfile.ZipFile(io.BytesIO(result.archive)) as archive:
        assert set(archive.namelist()) <= package.FIXED_PATHS | {
            package.LOCK_EXPORT_PREFIX + name for name in LOCK_PATHS
        }
        assert not any(
            path.endswith(".csv") or path.startswith("src/") or "log" in path
            for path in archive.namelist()
        )
        assert b"omitted-server-reference" not in result.archive
        assert b"source_prompts.v1" not in result.archive
        assert (
            json.loads(archive.read("strategy/parameters.json"))["status"]
            == "embedded_in_source"
        )
        assert (
            json.loads(archive.read("evidence/exposure-snapshot.json"))["as_of"]
            == package_input.exposure_as_of
        )
    status = result.manifest["capabilities"]
    assert (
        status["ledger_trust"] == "native_strategy_and_engine_share_process_untrusted"
    )
    assert status["portable_interface"] == "unsupported_native"
    assert status["isolated_execution"] == "not_independently_verified_by_package"
    assert status["prefix_future_checks"] == "not_verified_by_package"
    assert status["external_framework_equivalence"] == "not_established"
    assert (
        status["replay_scope"] == "selected_strategy_execution_not_model_reoptimization"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/absolute",
        "../escape",
        "a/../b",
        "a\\b",
        "C:/drive",
        "a//b",
        "a/./b",
        "a/",
        "",
        "\x00",
    ],
)
def test_reject_unsafe_paths(path):
    with pytest.raises(PackageError):
        safe_relative_path(path)
    with pytest.raises(PackageError):
        deterministic_archive([(path, b"content")])


def test_reject_duplicate_nonallowlisted_nonbytes_and_limits(monkeypatch):
    with pytest.raises(PackageError, match="Duplicate"):
        deterministic_archive([("README.txt", b"a"), ("README.txt", b"b")])
    for name, content in [
        ("arbitrary-secret.txt", b"a"),
        ("README.txt", Path("README.md")),
        ("README.txt", "text"),
    ]:
        with pytest.raises(PackageError):
            deterministic_archive([(name, content)])
    with pytest.raises(PackageError, match="bounded bytes"):
        deterministic_archive([("README.txt", b"x" * (MAX_ENTRY_BYTES + 1))])
    monkeypatch.setattr(package, "MAX_ENTRIES", 1)
    with pytest.raises(PackageError, match="count"):
        deterministic_archive([("README.txt", b"a"), ("strategy/strategy.py", b"b")])
    monkeypatch.setattr(package, "MAX_TOTAL_BYTES", 2)
    with pytest.raises(PackageError, match="size"):
        deterministic_archive([("README.txt", b"abc")])


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), {1: "bad"}, {"x": object()}, {"x": (1, 2)}]
)
def test_reject_noncanonical_nonfinite_json(value):
    with pytest.raises(PackageError):
        canonical(value)


@pytest.mark.parametrize(
    "content", [b'{"x":1,"x":2}', b'{ "x":1}', b'{"x":NaN}', b"\xff"]
)
def test_reject_duplicate_noncanonical_json_bytes(content):
    with pytest.raises(PackageError):
        parse_canonical(content)


def test_secret_scan_rejects_without_redacting_selected_bytes(package_input):
    disposable = secrets.token_urlsafe(30)
    for key in ("password", "api_key", "client_secret", "access_token"):
        source = f'{key} = "{disposable}"\n'.encode()
        pinned = replace(
            package_input,
            strategy_code=source,
            strategy_sha256=sha256(source),
            selection={**package_input.selection, "source_code_sha256": sha256(source)},
        )
        with pytest.raises(PackageError, match="credential"):
            build_strategy_package(pinned)
        assert pinned.strategy_code == source
        with pytest.raises(PackageError, match="credential"):
            reject_embedded_secrets(canonical({key: disposable}))


@pytest.mark.parametrize(
    "mutation",
    ["code", "result", "config", "dataset", "image", "lock", "path", "params"],
)
def test_fail_closed_on_changed_pinned_inputs(package_input, mutation):
    value = copy.deepcopy(package_input)
    if mutation == "code":
        value = replace(value, strategy_code=value.strategy_code + b"#changed")
    elif mutation == "result":
        value = replace(value, result_content=value.result_content + b" ")
    elif mutation == "config":
        value.final_config["holdout"]["end"] = "2025-03-01"
    elif mutation == "dataset":
        value.dataset_manifest["is_synthetic"] = False
    elif mutation == "image":
        value.environment_evidence["image_id"] = "mutable-tag:latest"
    elif mutation == "lock":
        value.environment_evidence["dependency_locks"][LOCK_PATHS[0]]["content"] += (
            "changed"
        )
    elif mutation == "path":
        value.environment_evidence["build_inputs"]["files"]["../escape"] = "a" * 64
    else:
        value = replace(value, parameters={"invented": 1})
    with pytest.raises(PackageError):
        build_strategy_package(value)


def test_portable_alias_without_exact_policy_manifest_is_rejected(package_input):
    value = copy.deepcopy(package_input)
    environment = value.environment_evidence
    environment["schema_version"] = package.PORTABLE_ENV_SCHEMA
    environment["image_ids"] = {
        "engine": environment.pop("image_id"),
        "policy": "sha256:" + "d" * 64,
    }
    with pytest.raises(PackageError, match="Unsupported strategy interface"):
        build_strategy_package(
            replace(
                value, strategy_mode="portable_policy", parameters={"threshold": 0.25}
            )
        )
    with pytest.raises(PackageError):
        build_strategy_package(
            replace(value, strategy_mode="portable", parameters={"threshold": 0.25})
        )


def test_legacy_environment_reconstruction_requires_exact_current_inputs(
    tmp_path, monkeypatch
):
    from packages.tradegrad_adapter import environment

    monkeypatch.setattr(environment, "INPUTS", LOCK_PATHS)
    for name in LOCK_PATHS:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture-only==0.0.0\n")
    digest = environment.environment_digest(tmp_path)
    evidence = capture_environment_evidence(digest, "sha256:" + "a" * 64, tmp_path)
    assert evidence["environment_digest"] == digest
    (tmp_path / LOCK_PATHS[0]).write_text("different-fixture==0.0.0\n")
    with pytest.raises(PackageError, match="Historical"):
        capture_environment_evidence(digest, "sha256:" + "a" * 64, tmp_path)
    # The historical record still contains the original lock, not today's file.
    assert (
        evidence["dependency_locks"][LOCK_PATHS[0]]["content"]
        == "fixture-only==0.0.0\n"
    )


def test_reviewed_files_reject_symlink_nonregular_and_path_escape(tmp_path):
    real = tmp_path / "regular"
    real.write_bytes(b"fixture")
    (tmp_path / "link").symlink_to(real)
    (tmp_path / "dir").mkdir()
    for name in ("link", "dir", "../escape"):
        with pytest.raises(PackageError):
            package._read_reviewed_file(tmp_path, name, 100)
    with pytest.raises(PackageError):
        package._read_reviewed_file(tmp_path, "regular", 1)


def test_source_over_limit_and_result_over_limit(package_input):
    for value in (
        replace(package_input, strategy_code=b"x" * (MAX_CODE_BYTES + 1)),
        replace(package_input, result_content=b"x" * (MAX_ENTRY_BYTES + 1)),
    ):
        with pytest.raises(PackageError):
            build_strategy_package(value)


def test_portable_capture_requires_both_actual_images_and_exact_reviewed_locks(
    tmp_path,
):
    lock = "runtimes/portable_policy/requirements.lock"
    target = tmp_path / lock
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# no third party policy dependencies\n")
    manifest = {
        "schema_version": "tradegrad.portable-build-inputs.v1",
        "files": {lock: sha256(target.read_bytes())},
    }
    image_ids = {"engine": "sha256:" + "c" * 64, "policy": "sha256:" + "d" * 64}
    evidence = capture_portable_environment_evidence(
        sha256(canonical(manifest)), image_ids, manifest, [lock], tmp_path
    )
    validate_environment_evidence(evidence, sha256(canonical(manifest)), image_ids)
    with pytest.raises(PackageError):
        capture_portable_environment_evidence(
            sha256(canonical(manifest)),
            {"engine": image_ids["engine"]},
            manifest,
            [lock],
            tmp_path,
        )
    target.write_bytes(b"# mutated reviewed dependency input\n")
    with pytest.raises(PackageError, match="differs"):
        capture_portable_environment_evidence(
            sha256(canonical(manifest)), image_ids, manifest, [lock], tmp_path
        )


def test_json_depth_and_lone_surrogate_are_rejected():
    value = None
    for _ in range(35):
        value = [value]
    with pytest.raises(PackageError):
        canonical(value)
    # Canonical JSON escapes Unicode consistently, never serializes executable objects.
    assert canonical({"value": "中文"}) == b'{"value":"\\u4e2d\\u6587"}'


def test_data_manifest_path_references_are_not_export_file_requests(package_input):
    value = copy.deepcopy(package_input)
    value.dataset_manifest["files"] = {"../escape.csv": "a" * 64}
    value.source_config["dataset"]["sha256"] = sha256(canonical(value.dataset_manifest))
    value.selection["source_config_sha256"] = sha256(canonical(value.source_config))
    value.final_config["source_config_sha256"] = value.selection["source_config_sha256"]
    value.selection["final_config_sha256"] = sha256(canonical(value.final_config))
    with pytest.raises(PackageError, match="path"):
        build_strategy_package(value)


@pytest.mark.parametrize(
    "field",
    [
        "credential_ref",
        "raw_model_response",
        "raw_model_responses",
        "prompt",
        "prompts",
        "logs",
        "environment_variables",
    ],
)
def test_allowlisted_json_cannot_smuggle_excluded_operational_payloads(field):
    disposable = secrets.token_urlsafe(24)
    with pytest.raises(PackageError, match="metadata"):
        deterministic_archive(
            [("data/dataset-manifest.json", canonical({field: disposable}))]
        )


def test_declared_native_parameters_are_exported_without_changing_legacy_packages(
    package_input,
):
    from packages.tradegrad_adapter.native_contract import NativeStrategyContract

    value = copy.deepcopy(package_input)
    declaration = NativeStrategyContract.model_validate(
        {
            "lookback_bars": 20,
            "warmup_bars": 20,
            "requires_shorting": False,
            "parameters": [
                {
                    "name": "allocation",
                    "kind": "number",
                    "value": 0.2,
                    "minimum": 0.1,
                    "maximum": 0.9,
                }
            ],
        }
    ).model_dump(mode="json")
    value.source_config["strategy_contract"] = declaration
    value.final_config["source_config"] = copy.deepcopy(value.source_config)
    value.selection["source_config_sha256"] = sha256(canonical(value.source_config))
    value.final_config["source_config_sha256"] = value.selection["source_config_sha256"]
    value.selection["final_config_sha256"] = sha256(canonical(value.final_config))
    built = build_strategy_package(value)
    with zipfile.ZipFile(io.BytesIO(built.archive)) as archive:
        parameters = json.loads(archive.read("strategy/parameters.json"))
        assert parameters["status"] == "explicit" and parameters["values"] == {
            "allocation": 0.2
        }
        assert (
            json.loads(archive.read("strategy/strategy-manifest.v1.json"))[
                "native_declaration"
            ]
            == declaration
        )
        assert (
            json.loads(archive.read("selection/source-config.json"))[
                "strategy_contract"
            ]
            == declaration
        )
