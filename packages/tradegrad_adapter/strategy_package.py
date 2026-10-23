"""Bounded deterministic reference packages, never a portable offline runtime.

No strategy is imported or executed. Only explicitly supplied verified inputs are
packaged; directories, logs, environment variables and upstream trees are not.
The secret scan is a rejection heuristic, not a proof that code is secret-free.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from packages.tradegrad_adapter.results import MAX_WIRE_BYTES, canonical_json_bytes

MAX_ENTRIES = 128
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ENTRY_BYTES = MAX_WIRE_BYTES
MAX_ENVIRONMENT_BYTES = 2 * 1024 * 1024
MAX_CODE_BYTES = 1024 * 1024
NATIVE_ENV_SCHEMA = "tradegrad.execution-environment.v1"
PORTABLE_ENV_SCHEMA = "tradegrad.portable-execution-environment.v1"
SHA = re.compile(r"^[0-9a-f]{64}$")
IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
LOCK_PATHS = (
    "runtimes/evaluator/requirements.lock",
    "runtimes/research/requirements.lock",
)
FIXED_PATHS = frozenset(
    {
        "strategy/strategy.py",
        "strategy/parameters.json",
        "strategy/strategy-manifest.v1.json",
        "strategy/policy-manifest.json",
        "strategy/state-contract.json",
        "REPLAY.txt",
        "selection/frozen-selection.json",
        "selection/source-config.json",
        "selection/final-config.json",
        "data/dataset-manifest.json",
        "data/universe-manifest.json",
        "data/split-version.json",
        "data/semantics.json",
        "environment/execution-environment.json",
        "environment/source-references.json",
        "reference/result-bundle.json",
        "reference/decision-trace.json",
        "evidence/exposure-snapshot.json",
        "README.txt",
        "package-manifest.json",
    }
)
LOCK_EXPORT_PREFIX = "environment/locks/"
ALLOWED_LOCKS = frozenset(
    {
        *LOCK_PATHS,
        "runtimes/portable_engine/requirements.lock",
        "runtimes/portable_engine/calendar-requirements.lock",
        "runtimes/portable_policy/requirements.lock",
        "runtimes/data/requirements.lock",
        "backend/requirements.lock",
    }
)


class PackageError(ValueError):
    pass


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tree(value: Any, depth: int = 0, budget: list | None = None):
    budget = [0] if budget is None else budget
    budget[0] += 1
    if depth > 32 or budget[0] > 250_000:
        raise PackageError("JSON complexity limit exceeded")
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) is list:
        for child in value:
            _tree(child, depth + 1, budget)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _tree(child, depth + 1, budget)
        return
    raise PackageError("Only finite JSON primitives and string keys are supported")


def canonical(value: Any) -> bytes:
    try:
        _tree(value)
        result = canonical_json_bytes(value)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise PackageError("Invalid finite canonical JSON") from exc
    if len(result) > MAX_ENTRY_BYTES:
        raise PackageError("JSON entry exceeds package size limit")
    return result


def parse_canonical(content: bytes) -> Any:
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise PackageError("Duplicate JSON key")
            value[key] = child
        return value

    try:
        value = json.loads(content, object_pairs_hook=pairs)
        if canonical(value) != content:
            raise PackageError("Noncanonical JSON bytes")
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PackageError("Invalid canonical JSON bytes") from exc


def safe_relative_path(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 250
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or name.startswith("/")
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or str(PurePosixPath(name)) != name
    ):
        raise PackageError("Unsafe package or reference path")
    return name


# No values are printed on rejection. This deliberately rejects rather than
# redacting code: a redacted file would no longer be the selected pinned source.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)[\"']?\s*[=:]\s*[\"'][^\"'\n]{8,}[\"']"
    ),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|redis|https?)://[^\s/@:]+:[^\s/@]+@"),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b"
    ),
    re.compile(
        r"(?i)[?&](?:api[_-]?key|access[_-]?token|password|secret)=[^&\s\"\']{8,}"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)


def reject_embedded_secrets(content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise PackageError("Package content must be UTF-8 text") from exc
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        raise PackageError(
            "Potential embedded credential detected; export refused without changing pinned bytes"
        )
    # Archive JSON is generated from fixed records. Reject accidental inclusion of
    # operational payload fields rather than trusting a file-name allowlist alone.
    if text.startswith(("{", "[")):
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return
        forbidden = {
            "password",
            "passwd",
            "secret",
            "api_key",
            "access_token",
            "client_secret",
            "credential_ref",
            "raw_model_response",
            "raw_model_responses",
            "prompt",
            "prompts",
            "logs",
            "environment_variables",
        }

        def inspect(item, depth=0):
            if depth > 32:
                raise PackageError("JSON metadata depth limit exceeded")
            if isinstance(item, dict):
                for key, child in item.items():
                    if key.lower().replace("-", "_") in forbidden and child not in (
                        None,
                        "",
                        [],
                        {},
                    ):
                        raise PackageError(
                            "Excluded operational or credential metadata detected"
                        )
                    inspect(child, depth + 1)
            elif isinstance(item, list):
                for child in item:
                    inspect(child, depth + 1)

        inspect(value)


def _read_reviewed_file(root: Path, name: str, limit: int) -> bytes:
    safe_relative_path(name)
    root = root.resolve()
    path = root
    for part in name.split("/"):
        path /= part
        if path.is_symlink():
            raise PackageError("Reviewed input must not be a symlink")
    if not path.resolve().is_relative_to(root):
        raise PackageError("Reviewed input escapes root")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise PackageError("Reviewed input must be a bounded regular file")
            content = stream.read(limit + 1)
    except OSError as exc:
        raise PackageError("Reviewed input is unavailable") from exc
    if len(content) > limit:
        raise PackageError("Reviewed input exceeds limit")
    return content


def _lock_records(root: Path, manifest: dict, names: Iterable[str]) -> dict:
    records = {}
    for name in sorted(names):
        if name not in ALLOWED_LOCKS:
            raise PackageError("Unapproved dependency lock path")
        content = _read_reviewed_file(root, name, 512 * 1024)
        if manifest["files"].get(name) != sha256(content):
            raise PackageError("Dependency lock differs from execution build inputs")
        reject_embedded_secrets(content)
        records[name] = {"sha256": sha256(content), "content": content.decode("utf-8")}
    return records


def capture_environment_evidence(
    expected_environment_digest: str, image_id: str, root: Path | None = None
) -> dict:
    """Call before evaluation, then persist canonical bytes on the actual attempt.

    For a legacy run this may only reconstruct evidence if the current reviewed
    inputs still hash EXACTLY to the original environment digest.
    """
    from packages.tradegrad_adapter.environment import INPUTS, ROOT

    root = ROOT if root is None else root
    manifest = {
        "schema_version": "tradegrad.build-inputs.v1",
        "files": {
            name: sha256(_read_reviewed_file(root, name, MAX_ENTRY_BYTES))
            for name in INPUTS
        },
    }
    if sha256(canonical(manifest)) != expected_environment_digest:
        raise PackageError(
            "Historical execution environment no longer matches current reviewed inputs"
        )
    value = {
        "schema_version": NATIVE_ENV_SCHEMA,
        "environment_digest": expected_environment_digest,
        "image_id": image_id,
        "build_inputs": manifest,
        "dependency_locks": _lock_records(root, manifest, LOCK_PATHS),
        "provenance": "captured_at_execution",
    }
    validate_environment_evidence(value, expected_environment_digest, image_id)
    return value


def capture_portable_environment_evidence(
    expected_environment_digest: str,
    image_ids: dict,
    build_inputs: dict,
    dependency_lock_paths: Iterable[str],
    root: Path,
) -> dict:
    """Portable sibling images are pinned separately, never collapsed to one ID."""
    for name, expected in build_inputs.get("files", {}).items():
        if sha256(_read_reviewed_file(root, name, MAX_ENTRY_BYTES)) != expected:
            raise PackageError(
                "Portable reviewed source differs from pinned build inputs"
            )
    value = {
        "schema_version": PORTABLE_ENV_SCHEMA,
        "environment_digest": expected_environment_digest,
        "image_ids": image_ids,
        "build_inputs": build_inputs,
        "dependency_locks": _lock_records(root, build_inputs, dependency_lock_paths),
        "provenance": "captured_at_execution",
    }
    validate_environment_evidence(value, expected_environment_digest, image_ids)
    return value


def validate_environment_evidence(
    value: dict, expected_digest: str, expected_images: str | dict
) -> None:
    if not isinstance(value, dict):
        raise PackageError("Missing execution environment evidence")
    portable = value.get("schema_version") == PORTABLE_ENV_SCHEMA
    key = "image_ids" if portable else "image_id"
    if set(value) != {
        "schema_version",
        "environment_digest",
        key,
        "build_inputs",
        "dependency_locks",
        "provenance",
    }:
        raise PackageError("Unexpected execution environment evidence fields")
    if value["schema_version"] not in {NATIVE_ENV_SCHEMA, PORTABLE_ENV_SCHEMA}:
        raise PackageError("Unsupported execution environment schema")
    images = value[key]
    if portable:
        if (
            not isinstance(images, dict)
            or set(images) != {"engine", "policy"}
            or any(not IMAGE.fullmatch(str(image)) for image in images.values())
        ):
            raise PackageError("Portable engine and policy require actual image IDs")
    elif not isinstance(images, str) or not IMAGE.fullmatch(images):
        raise PackageError("Execution requires an actual content-addressed image ID")
    manifest = value["build_inputs"]
    if (
        images != expected_images
        or value["environment_digest"] != expected_digest
        or not SHA.fullmatch(str(expected_digest))
        or not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "files"}
        or not isinstance(manifest["files"], dict)
        or not manifest["files"]
        or len(manifest["files"]) > 512
        or sha256(canonical(manifest)) != expected_digest
        or value["provenance"]
        not in {"captured_at_execution", "reconstructed_exact_digest"}
    ):
        raise PackageError("Execution environment identity mismatch")
    for name, digest in manifest["files"].items():
        safe_relative_path(name)
        if not SHA.fullmatch(str(digest)):
            raise PackageError("Invalid reviewed source checksum")
    locks = value["dependency_locks"]
    if (
        not isinstance(locks, dict)
        or not locks
        or (not portable and set(locks) != set(LOCK_PATHS))
        or (
            portable
            and set(locks)
            != {
                name for name in manifest["files"] if name.endswith("requirements.lock")
            }
        )
    ):
        raise PackageError("Missing exact dependency locks")
    for name, record in locks.items():
        if (
            name not in ALLOWED_LOCKS
            or not isinstance(record, dict)
            or set(record) != {"sha256", "content"}
            or not isinstance(record["content"], str)
        ):
            raise PackageError("Invalid dependency lock record")
        content = record["content"].encode("utf-8")
        if (
            len(content) > 512 * 1024
            or sha256(content) != record["sha256"]
            or manifest["files"].get(name) != record["sha256"]
        ):
            raise PackageError("Dependency lock checksum mismatch")
    if len(canonical(value)) > MAX_ENVIRONMENT_BYTES:
        raise PackageError("Execution evidence exceeds limit")


@dataclass(frozen=True)
class PackageInput:
    strategy_code: bytes
    strategy_mode: str
    strategy_sha256: str
    parameters: dict | None
    selection: dict
    source_config: dict
    final_config: dict
    dataset_manifest: dict
    universe_manifest: dict
    split_version: dict
    environment_evidence: dict
    result_content: bytes
    result_sha256: str
    exposure_snapshot: dict
    exposure_as_of: str
    policy_manifest: dict | None = None


@dataclass(frozen=True)
class BuiltPackage:
    archive: bytes
    archive_sha256: str
    content_sha256: str
    manifest: dict


def _allowed_path(name: str) -> bool:
    return name in FIXED_PATHS or (
        name.startswith(LOCK_EXPORT_PREFIX)
        and name[len(LOCK_EXPORT_PREFIX) :] in ALLOWED_LOCKS
    )


def deterministic_archive(entries: Iterable[tuple[str, bytes]]) -> bytes:
    """Accept byte pairs, not files/ZipInfo/directories, and enforce a fixed allowlist."""
    files = {}
    total = 0
    for name, content in entries:
        safe_relative_path(name)
        if name in files or not _allowed_path(name):
            raise PackageError("Duplicate or non-allowlisted package entry")
        if type(content) is not bytes or len(content) > MAX_ENTRY_BYTES:
            raise PackageError("Package entry is not bounded bytes")
        reject_embedded_secrets(content)
        total += len(content)
        files[name] = content
        if len(files) > MAX_ENTRIES or total > MAX_TOTAL_BYTES:
            raise PackageError("Package count or size limit exceeded")
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_STORED, allowZip64=False
    ) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o444) << 16
            info.compress_type = zipfile.ZIP_STORED
            info.flag_bits = 0
            info.extra = b""
            info.comment = b""
            archive.writestr(info, files[name])
    data = output.getvalue()
    if len(data) > MAX_TOTAL_BYTES:
        raise PackageError("Final archive exceeds size limit")
    return data


def config_projection(config: dict) -> dict:
    """Original config hash remains pinned; model/prompt/credential fields are omitted."""
    keys = {
        "schema_version",
        "execution_mode",
        "strategy_mode",
        "task",
        "profile",
        "dataset",
        "seed_strategy",
        "universe",
        "windows",
        "warmup",
        "market_profile",
        "objective",
        "environment_digest",
        "benchmark",
        "evaluation_mode",
        "strategy_kind",
        "seed_manifest",
        "seed_parameters",
        "initial_cash",
        "initial_state",
        "engine_profile",
        "selected_manifest",
        "selected_parameters",
        "selection_id",
        "split_version_id",
        "split_sha256",
        "source_config_sha256",
        "selected_strategy",
        "holdout",
        "max_runtime_seconds",
    }
    result = {key: config[key] for key in sorted(keys) if key in config}
    if "source_config" in config:
        result["source_config"] = config_projection(config["source_config"])
    return result


def build_strategy_package(value: PackageInput) -> BuiltPackage:
    if (
        type(value.strategy_code) is not bytes
        or not 0 < len(value.strategy_code) <= MAX_CODE_BYTES
        or sha256(value.strategy_code) != value.strategy_sha256
    ):
        raise PackageError("Selected strategy bytes differ from frozen checksum")
    reject_embedded_secrets(value.strategy_code)
    if value.strategy_mode not in {"native_akquant", "portable"}:
        raise PackageError("Unsupported strategy interface")
    native = value.strategy_mode == "native_akquant"
    if (
        len(value.result_content) > MAX_WIRE_BYTES
        or sha256(value.result_content) != value.result_sha256
    ):
        raise PackageError("Reference result checksum or size mismatch")
    result = parse_canonical(value.result_content)
    if not isinstance(result, dict) or not isinstance(
        result.get("decision_trace"), dict if native else list
    ):
        raise PackageError("Reference result has no decision trace")
    selection = value.selection
    for key, actual in (
        ("source_code_sha256", value.strategy_sha256),
        ("source_config_sha256", sha256(canonical(value.source_config))),
        ("final_config_sha256", sha256(canonical(value.final_config))),
    ):
        if selection.get(key) != actual:
            raise PackageError("Frozen selection identity mismatch")
    effective_config = value.final_config.get("source_config", value.final_config)
    if effective_config.get("dataset", {}).get("sha256") != sha256(
        canonical(value.dataset_manifest)
    ):
        raise PackageError("Dataset manifest differs from frozen config")
    for group in ("files", "artifact_files"):
        for name, digest in value.dataset_manifest.get(group, {}).items():
            safe_relative_path(name)
            if not SHA.fullmatch(str(digest)):
                raise PackageError("Dataset reference has invalid checksum")
    universe = value.universe_manifest
    if (
        universe.get("reference_sha256")
        != effective_config.get("universe", {}).get("sha256")
        or universe.get("serialization")
        not in {"original_canonical_json", "serialization_not_original"}
        or universe.get("manifest_sha256")
        != sha256(canonical(universe.get("manifest")))
        or (
            universe["serialization"] == "original_canonical_json"
            and universe["manifest_sha256"] != universe["reference_sha256"]
        )
    ):
        raise PackageError(
            "Universe export reference or serialization identity mismatch"
        )
    environment = value.environment_evidence
    images = environment.get("image_ids", environment.get("image_id"))
    validate_environment_evidence(
        environment, value.final_config.get("environment_digest"), images
    )
    if (value.strategy_mode == "native_akquant") != (
        environment["schema_version"] == NATIVE_ENV_SCHEMA
    ):
        raise PackageError("Strategy interface and execution environment disagree")
    if native and value.parameters is not None:
        raise PackageError(
            "Native strategy parameters are embedded in exact source, not invented externally"
        )
    if not native and not isinstance(value.parameters, dict):
        raise PackageError("Portable policies require explicit canonical parameters")
    if not native:
        try:
            from packages.strategy_sdk import hash_json
            from packages.tradegrad_adapter.portable_final_config import (
                PortableFinalEvaluationConfig,
            )
            from packages.tradegrad_adapter.portable_results import (
                validate_portable_result,
            )
            from workers.research.portable_final_staging import (
                validate_portable_final_binding,
            )

            final = PortableFinalEvaluationConfig.model_validate(value.final_config)
            manifest = validate_portable_final_binding(
                final, value.policy_manifest, value.parameters
            )
            result = validate_portable_result(value.result_content)
            if (
                final.source_config.model_dump(mode="json") != value.source_config
                or final.selected_strategy.sha256 != value.strategy_sha256
                or result["identity"]["run_config_sha256"] != final.sha256
                or result["identity"]["strategy_sha256"] != value.strategy_sha256
                or result["identity"]["parameters_sha256"]
                != hash_json(value.parameters)
                or result["window"]["start"] != str(final.holdout.start)
                or result["window"]["end"] != str(final.holdout.end)
            ):
                raise ValueError("Portable final identity differs")
            # Execution-specific narrowed snapshots legitimately change these two
            # hashes. All policy semantics and state schema remain exactly selected.
            result_manifest = dict(result["manifest"])
            result_manifest["universe"] = {
                **result_manifest["universe"],
                "snapshot_sha256": manifest.universe.snapshot_sha256,
            }
            result_manifest["prices"] = {
                **result_manifest["prices"],
                "actions_snapshot_sha256": manifest.prices.actions_snapshot_sha256,
            }
            if result_manifest != manifest.model_dump(mode="json"):
                raise ValueError("Portable reference policy contract differs")
        except (ValueError, TypeError, KeyError):
            raise PackageError(
                "Portable package needs exact selected contract and verified final result"
            ) from None
    capabilities = {
        "portable_interface": "unsupported_native"
        if native
        else "declared_portable_contract_not_execution_evidence",
        "isolated_execution": "not_independently_verified_by_package",
        "prefix_future_checks": "not_verified_by_package",
        "external_framework_equivalence": "not_established",
        "ledger_trust": "native_strategy_and_engine_share_process_untrusted"
        if native
        else "requires_separate_engine_evidence",
        "offline_runtime": "not_included",
        "universe_serialization": value.universe_manifest["serialization"],
        "replay_scope": "selected_strategy_execution_not_model_reoptimization",
        "reproducibility": "reference_only_requires_matching_snapshot_and_pinned_images",
    }
    parameters = {
        "schema_version": "tradegrad.strategy-parameters.v1",
        "status": "embedded_in_source" if native else "explicit",
        "values": None if native else value.parameters,
    }
    strategy_manifest = {
        "schema_version": "strategy-manifest.v1",
        "mode": value.strategy_mode,
        "source_sha256": value.strategy_sha256,
        "parameters_sha256": sha256(canonical(parameters)),
        "capabilities": capabilities,
    }
    files = {
        "strategy/strategy.py": value.strategy_code,
        "strategy/parameters.json": canonical(parameters),
        "strategy/strategy-manifest.v1.json": canonical(strategy_manifest),
        "selection/frozen-selection.json": canonical(selection),
        "selection/source-config.json": canonical(
            config_projection(value.source_config)
        ),
        "selection/final-config.json": canonical(config_projection(value.final_config)),
        "data/dataset-manifest.json": canonical(value.dataset_manifest),
        "data/universe-manifest.json": canonical(value.universe_manifest),
        "data/split-version.json": canonical(value.split_version),
        "data/semantics.json": canonical(
            {
                "evaluator_symbol_mapping": value.dataset_manifest.get(
                    "evaluator_symbol_mapping"
                ),
                "listings": value.dataset_manifest.get("listings"),
                "price_semantics": value.dataset_manifest.get(
                    "price_semantics", value.dataset_manifest.get("price_basis")
                ),
                "corporate_actions": "Referenced by dataset manifest file hashes; raw action records are not redistributed",
                "calendar": "See each listing's pinned calendar/timezone; unknown when absent",
            }
        ),
        "environment/execution-environment.json": canonical(
            {
                key: item
                for key, item in environment.items()
                if key != "dependency_locks"
            }
        ),
        "environment/source-references.json": canonical(
            {
                "build_inputs": environment["build_inputs"],
                "source_distribution": "References only; upstream licensing remains unresolved",
                "full_upstream_code_included": False,
            }
        ),
        "reference/result-bundle.json": value.result_content,
        "reference/decision-trace.json": canonical(result["decision_trace"]),
        "evidence/exposure-snapshot.json": canonical(
            {
                "as_of": value.exposure_as_of,
                "snapshot": value.exposure_snapshot,
                "interpretation": "Immutable snapshot, not a claim that historical or external data use is fully tracked",
            }
        ),
        "README.txt": b"TradeGrad reference-only strategy package v1\nRequires the matching private dataset snapshot and exact pinned execution image(s).\nThis is NOT a self-contained offline runtime or independent execution attestation.\nNative strategy and engine share a process; native results remain untrusted.\nPortable interface, isolated execution, prefix/future checks and framework equivalence are separate capabilities.\nRaw market data, full upstream code, credentials, logs, prompts and model responses are excluded.\nConfig files are execution-only projections; frozen original hashes are in the selection record.\nThe embedded-secret rejection heuristic is not a proof of being secret-free.\nNo redistribution license is granted for selected source or upstream code.\n",
    }
    files["REPLAY.txt"] = (
        b"Reference replay instructions\n"
        b"1. Verify package-manifest.json inventory hashes and the separately registered archive checksum.\n"
        b"2. Obtain the same authorized private dataset and universe snapshots. Verify original manifest/file hashes, pinned calendar schedule, corporate-action coverage and action evidence. Raw data is not included.\n"
        b"3. Restore the exact reviewed build inputs, dependency locks and content-addressed execution image(s) from environment/execution-environment.json. Do not substitute mutable tags.\n"
        b"4. Reconstruct only the frozen final holdout and its past-only warmup from selection/final-config.json. Original full config hashes are in selection/frozen-selection.json; model/prompt fields are deliberately omitted from the projection.\n"
        b"5. Load the exact selected source as inert input to the supported isolated runner. Portable replay uses disjoint engine/policy mounts, explicit parameters, the saved initial state and state contract; never import policy on the host.\n"
        b"6. Compare result and decision-trace artifacts with reference/. Differences require investigation, not rewriting the selection or source checkpoint. This archive is not an offline runtime, runtime certification, target-framework equivalence or investment evidence.\n"
    )
    if not native:
        files["strategy/policy-manifest.json"] = canonical(value.policy_manifest)
        files["strategy/state-contract.json"] = canonical(
            {
                "state_schema": value.policy_manifest["state_schema"],
                "initial_state": value.source_config["initial_state"],
                "restart_semantics": "Replay begins with the frozen source initial state, never optimizer final state",
            }
        )
    for name, record in environment["dependency_locks"].items():
        files[LOCK_EXPORT_PREFIX + name] = record["content"].encode("utf-8")
    entries = {
        name: {"sha256": sha256(content), "bytes": len(content)}
        for name, content in sorted(files.items())
    }
    content_hash = sha256(canonical(entries))
    manifest = {
        "schema_version": "tradegrad.strategy-package.v1",
        "profile": "reference_only.v1",
        "content_sha256": content_hash,
        "content_hash_scope": "canonical file inventory excluding package-manifest.json; archive SHA-256 stored separately",
        "files": entries,
        "capabilities": capabilities,
        "raw_market_data_included": False,
        "secret_scan": "heuristic_rejection_not_a_secret_free_proof",
    }
    files["package-manifest.json"] = canonical(manifest)
    archive = deterministic_archive(files.items())
    return BuiltPackage(archive, sha256(archive), content_hash, manifest)
