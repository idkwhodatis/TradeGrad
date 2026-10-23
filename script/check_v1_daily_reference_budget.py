"""Pure, bounded JSON-only engineering guardrail; never runs or rewrites a benchmark.

A receipt is separate from the raw measurement and is created exclusively. It is
not production acceptance, environment equivalence, or whole-workflow success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALIBRATION = ROOT / "docs/evidence/v1-daily-reference-calibration-39930596032.json"
BUDGET = ROOT / "docs/evidence/v1-daily-reference-engineering-budget-v1.json"
CALIBRATION_SHA256 = "e00fe50e03d9a9715aa4ea3b11d45b1768ecf0bda788e23e90c5a0688476a259"
BUDGET_SHA256 = "60d5256bab7f8f0050ed8329f2c282b81da5b5cfbe78a351678ce4b67f52eeb8"
MAX_BYTES, MAX_NODES, MAX_DEPTH = 65536, 4096, 16
HASH_FIELDS = (
    "chart_artifact_sha256",
    "config_sha256",
    "dataset_snapshot_sha256",
    "environment_sha256",
    "profile_sha256",
    "seed_code_sha256",
    "source_tree_sha256",
    "universe_sha256",
    "uploaded_csv_sha256",
)
VERSION_FIELDS = ("source_tree_sha256", "environment_sha256", "evaluator_image_digest")


class InvalidReport(ValueError):
    """Fixed error category only: never echo input or filesystem paths."""


def require(condition, category="invalid_report"):
    if not condition:
        raise InvalidReport(category)


def pairs_unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "invalid_json")
        result[key] = value
    return result


def parse_json(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "invalid_json")
    try:
        value = json.loads(raw, object_pairs_hook=pairs_unique)
        pending, nodes = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            require(nodes <= MAX_NODES and depth <= MAX_DEPTH, "invalid_json")
            if isinstance(item, dict):
                require(len(item) <= 128, "invalid_json")
                require(all(len(key) <= 256 for key in item), "invalid_json")
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                require(len(item) <= 128, "invalid_json")
                pending.extend((child, depth + 1) for child in item)
            elif isinstance(item, str):
                require(len(item) <= 2048, "invalid_json")
            elif type(item) in (float, int):
                require(math.isfinite(item) and abs(item) <= 10**18, "invalid_json")
        require(type(value) is dict, "invalid_json")
        return value
    except (UnicodeError, ValueError, OverflowError, RecursionError):
        raise InvalidReport("invalid_json") from None


def read_bytes(path):
    # Nonblocking regular-file-only reads exclude pipes, devices and sockets.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(
            stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BYTES, "invalid_file"
        )
        return handle.read(MAX_BYTES + 1)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def get(value, path):
    for key in path.split("."):
        require(type(value) is dict and key in value)
        value = value[key]
    return value


def positive(value):
    require(
        type(value) in (int, float) and math.isfinite(value) and 0 < value <= 600000
    )
    return value


def integer(value, low, high):
    require(type(value) is int and low <= value <= high)


def hex_hash(value, length=64):
    require(
        type(value) is str and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None
    )


def exact(value, expected):
    # Python equality alone confuses true with 1 and false with 0.
    return json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True)


def validate_measurements(report):
    identity = get(report, "identity")
    for field in HASH_FIELDS:
        hex_hash(get(identity, field))
    image = get(identity, "evaluator_image_digest")
    require(type(image) is str and image.startswith("sha256:"))
    hex_hash(image[7:])
    if get(identity, "ci_commit") is not None:
        hex_hash(identity["ci_commit"], 40)
    integer(get(identity, "source_bytes"), 1, 100_000_000)
    integer(get(identity, "source_file_count"), 1, 10000)
    hex_hash(get(report, "single_evaluation.evaluation_payload_sha256"))
    hardware = get(report, "hardware")
    require(
        set(hardware)
        == {
            "scope",
            "logical_cpus",
            "affinity_cpus",
            "physical_memory_bytes",
            "client_cgroup_cpu_quota_cores",
            "client_cgroup_memory_limit_bytes",
            "worker_container_limits_verified",
        }
    )
    for key, upper in (
        ("logical_cpus", 65536),
        ("affinity_cpus", 65536),
        ("physical_memory_bytes", 10**16),
        ("client_cgroup_memory_limit_bytes", 10**16),
    ):
        if hardware[key] is not None:
            integer(hardware[key], 1, upper)
    quota = hardware["client_cgroup_cpu_quota_cores"]
    if quota is not None:
        require(
            type(quota) in (int, float) and math.isfinite(quota) and 0 < quota <= 65536
        )
    integer(get(report, "http_requests"), 1, get(report, "bounds.requests"))
    for section in ("metadata_api", "windowed_chart"):
        row = get(report, section)
        p50, p95, maximum = [
            positive(get(row, key)) for key in ("p50_ms", "p95_ms", "max_ms")
        ]
        require(p50 <= p95 <= maximum)
    total = positive(get(report, "total_wall_ms"))
    for section, count in (("standalone_configured_run", 1), ("concurrent_runs", 2)):
        row = get(report, section)
        wall = positive(get(row, "batch_wall_ms"))
        require(wall <= total)
        for key in ("completion_wall_ms", "persisted_attempt_duration_ms"):
            durations = get(row, key)
            require(type(durations) is list and len(durations) == count)
            require(all(positive(duration) <= wall for duration in durations))
        physical = get(row, "persisted_physical_evaluation_lifecycle_ms")
        require(type(physical) is list and len(physical) == count)
        for index, durations in enumerate(physical):
            require(type(durations) is list and len(durations) == 5)
            require(
                all(
                    positive(duration) <= row["persisted_attempt_duration_ms"][index]
                    for duration in durations
                )
            )
    single = positive(get(report, "single_evaluation.persisted_lifecycle_ms"))
    require(
        single
        == report["standalone_configured_run"][
            "persisted_physical_evaluation_lifecycle_ms"
        ][0][-1]
    )
    positive(get(report, "import.completion_wall_ms"))
    concurrent = report["concurrent_runs"]
    overlap = positive(get(concurrent, "attempt_overlap_ms"))
    require(overlap <= min(concurrent["persisted_attempt_duration_ms"]))
    positive(get(concurrent, "physical_evaluation_overlap_ms"))
    integer(get(concurrent, "physical_evaluation_overlapping_pairs"), 1, 25)


def check(raw, source_commit, github_run_id, *, budget_raw=None, calibration_raw=None):
    """Return only a separate bounded receipt, without modifying any input bytes."""
    receipt = {
        "schema_version": "tradegrad.daily-reference-budget-receipt.v1",
        "scope": "engineering_regression_guardrail_only_not_workflow_acceptance",
        "status": "failed",
        "capacity_claim": False,
        "user_agreed_production_profile": False,
        "production_slo": False,
        "raw_report_sha256": digest(raw),
        "budget_sha256": BUDGET_SHA256,
        "calibration_report_sha256": CALIBRATION_SHA256,
        "checks": [],
        "issues": [],
    }
    try:
        hex_hash(source_commit, 40)
        require(
            type(github_run_id) is str
            and re.fullmatch(r"[1-9][0-9]{0,19}", github_run_id) is not None
        )
        budget_raw = read_bytes(BUDGET) if budget_raw is None else budget_raw
        calibration_raw = (
            read_bytes(CALIBRATION) if calibration_raw is None else calibration_raw
        )
        require(digest(budget_raw) == BUDGET_SHA256, "frozen_budget_hash_mismatch")
        require(
            digest(calibration_raw) == CALIBRATION_SHA256, "calibration_hash_mismatch"
        )
        budget, calibration, report = map(
            parse_json, (budget_raw, calibration_raw, raw)
        )
        receipt["budget_id"] = budget["budget_id"]
        for path, expected in budget["required_report_fields"].items():
            if not exact(get(report, path), expected):
                receipt["issues"].append("contract_mismatch:" + path)
        if receipt["issues"]:
            return receipt
        validate_measurements(report)
        identity = report["identity"]
        receipt["provenance"] = {
            "source_commit_label": source_commit,
            "github_run_id_label": github_run_id,
            "external_labels_are_not_tree_verification": True,
            "report_ci_commit_label": identity["ci_commit"],
            "report_identity": {
                key: identity[key]
                for key in (
                    *HASH_FIELDS,
                    "evaluator_image_digest",
                    "source_bytes",
                    "source_file_count",
                )
            },
            "observed_host": report["hardware"],
            "hardware_equal_to_calibration_observations": exact(
                report["hardware"], calibration["hardware"]
            ),
            "changed_version_fields": [
                key
                for key in VERSION_FIELDS
                if identity[key] != calibration["identity"][key]
            ],
            "source_commit_label_changed_from_calibration": source_commit
            != budget["calibration"]["source_commit"],
            "environment_identical_verified": False,
            "worker_container_reservations_verified": False,
        }
        for metric in budget["metrics"]:
            observed = positive(get(report, metric["path"]))
            passed = observed <= metric["maximum_ms"]
            receipt["checks"].append(
                {
                    "path": metric["path"],
                    "observed_ms": observed,
                    "maximum_ms": metric["maximum_ms"],
                    "passed": passed,
                }
            )
            if not passed:
                receipt["issues"].append("budget_exceeded:" + metric["path"])
        if not receipt["issues"]:
            receipt["status"] = "passed"
    except (InvalidReport, KeyError, TypeError, OSError) as exc:
        receipt["issues"].append(
            str(exc)
            if isinstance(exc, InvalidReport)
            else "invalid_report_or_reference"
        )
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--source-commit",
        required=True,
        help="40-hex external provenance label, not tree proof",
    )
    parser.add_argument(
        "--github-run-id", required=True, help="External run provenance label"
    )
    args = parser.parse_args(argv)
    try:
        raw = read_bytes(args.report)
        receipt = check(raw, args.source_commit, args.github_run_id)
        # Never truncate existing data, including a raw report or prior receipt.
        with args.receipt.open("x", encoding="utf-8") as output:
            output.write(
                json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + "\n"
            )
    except (OSError, InvalidReport):
        print("budget checker failed: input unavailable or receipt not a new file")
        return 2
    print("daily reference engineering budget: " + receipt["status"])
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
