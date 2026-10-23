"""Offline JSON-only guardrail tests. No services, sockets, engines or model calls."""

import copy
import hashlib
import json
from decimal import ROUND_CEILING, Decimal

import pytest

from script import check_v1_daily_reference_budget as gate

COMMIT = "8115ef88b14499108e02249d7b8f5d2d616311f4"
RUN = "39930596032"


@pytest.fixture
def report():
    return json.loads(gate.CALIBRATION.read_bytes())


def set_path(report, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        report = report[part]
    report[parts[-1]] = value


def check(report, **kwargs):
    return gate.check(json.dumps(report).encode(), COMMIT, RUN, **kwargs)


def test_frozen_calibration_and_chosen_budget_are_distinct_immutable_records():
    raw, budget_raw = gate.CALIBRATION.read_bytes(), gate.BUDGET.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == gate.CALIBRATION_SHA256
    assert hashlib.sha256(budget_raw).hexdigest() == gate.BUDGET_SHA256
    budget = json.loads(budget_raw)
    calibration = json.loads(raw)
    assert calibration["performance_threshold"] is None
    assert calibration["capacity_claim"] is False
    assert calibration["user_agreed_production_profile"] is False
    assert budget["production_slo"] is False
    assert budget["calibration"]["workflow_conclusion"] == "failure"
    assert budget["calibration"]["independent_performance_report_status"] == "succeeded"
    for metric in budget["metrics"]:
        observed = gate.get(calibration, metric["path"])
        assert metric["calibration_observed_ms"] == observed
        quantum = metric["round_up_quantum_ms"]
        assert (
            metric["maximum_ms"]
            == int(
                (Decimal(str(observed)) * 5 / quantum).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            * quantum
        )
    receipt = gate.check(raw, COMMIT, RUN)
    assert receipt["status"] == "passed"
    assert len(receipt["checks"]) == 9
    assert receipt["raw_report_sha256"] == gate.CALIBRATION_SHA256
    assert receipt["schema_version"] != calibration["schema_version"]
    assert gate.CALIBRATION.read_bytes() == raw
    assert gate.BUDGET.read_bytes() == budget_raw


@pytest.mark.parametrize(
    "path,value",
    [
        ("status", "failed"),
        ("performance_threshold", 300),
        ("capacity_claim", True),
        ("user_agreed_production_profile", True),
        ("live_data", True),
        ("model_calls", 1),
        ("model_calls", False),
        ("profile_id", "other"),
        ("workload.listings", 2),
        ("workload.daily_bars", 1457),
        ("workload.uploaded_csv_bytes", 80120),
        ("identity.uploaded_csv_sha256", "a" * 64),
        ("identity.profile_sha256", "a" * 64),
        ("identity.seed_code_sha256", "a" * 64),
        ("bounds.poll_milliseconds", 100),
        ("metadata_api.count", 19),
        ("windowed_chart.count", 19),
        ("metadata_api.warmup_excluded", 0),
        ("windowed_chart.window_days", 90),
        ("single_evaluation.samples", 2),
        ("single_evaluation.model_calls", 1),
        ("concurrent_runs.physical_engine_evaluations", 9),
        ("concurrent_runs.native_docker_successes", 9),
        ("concurrent_runs.sandbox_stopped", False),
        ("concurrent_runs.same_exact_config_dataset_strategy", False),
        ("concurrent_runs.verified_model_calls", 1),
        ("standalone_configured_run.sandbox_stopped", False),
        ("standalone_configured_run.native_docker_successes", 4),
        ("cleanup.cleanup_uncertain", True),
        ("cleanup.unknown_mutation_outcome", True),
        ("cleanup.tracked_nonterminal", 1),
        ("cleanup.terminal_confirmed", 1),
        ("windowed_chart.stable_persisted_artifact_hash_across_reads", False),
        ("windowed_chart.same_exact_new_dataset_and_standalone_2024_evaluation", False),
    ],
)
def test_wrong_profile_failed_incomplete_or_unmeasured_report_fails(
    report, path, value
):
    set_path(report, path, value)
    receipt = check(report)
    assert receipt["status"] == "failed"
    assert "contract_mismatch:" + path.split(".")[0] in " ".join(receipt["issues"])
    assert receipt["checks"] == []


@pytest.mark.parametrize(
    "path,value",
    [
        ("concurrent_runs.attempt_overlap_ms", 0),
        ("concurrent_runs.physical_evaluation_overlap_ms", 0),
        ("concurrent_runs.physical_evaluation_overlapping_pairs", 0),
        ("concurrent_runs.physical_evaluation_overlapping_pairs", True),
        ("single_evaluation.persisted_lifecycle_ms", 1),
        ("standalone_configured_run.persisted_physical_evaluation_lifecycle_ms", [[1]]),
        ("concurrent_runs.completion_wall_ms", [2]),
        ("metadata_api.p50_ms", 0),
        ("metadata_api.p95_ms", -1),
        ("metadata_api.max_ms", False),
        ("metadata_api.p95_ms", float("nan")),
        ("metadata_api.p95_ms", float("inf")),
        ("metadata_api.max_ms", 1),
        ("identity.source_tree_sha256", "untrusted source label"),
        ("identity.evaluator_image_digest", "not immutable"),
        ("hardware.logical_cpus", True),
        ("http_requests", 1025),
    ],
)
def test_invalid_measurements_do_not_pass(report, path, value):
    set_path(report, path, value)
    assert check(report)["status"] == "failed"


def test_inclusive_budget_threshold_and_failure_preserve_raw(report):
    report["import"]["completion_wall_ms"] = 8000
    assert check(report)["status"] == "passed"
    report["import"]["completion_wall_ms"] = 8000.001
    before = copy.deepcopy(report)
    receipt = check(report)
    assert receipt["status"] == "failed"
    assert receipt["issues"] == ["budget_exceeded:import.completion_wall_ms"]
    assert report == before
    assert report["performance_threshold"] is None


def test_new_source_and_environment_recorded_not_rejected_or_called_identical(report):
    for field in ("source_tree_sha256", "environment_sha256"):
        report["identity"][field] = "a" * 64
    report["identity"]["evaluator_image_digest"] = "sha256:" + "b" * 64
    report["identity"]["ci_commit"] = "c" * 40
    report["hardware"]["logical_cpus"] = 8
    report["identity"]["config_sha256"] = "d" * 64
    receipt = gate.check(json.dumps(report).encode(), "c" * 40, "42")
    assert receipt["status"] == "passed"
    provenance = receipt["provenance"]
    assert set(provenance["changed_version_fields"]) == set(gate.VERSION_FIELDS)
    assert provenance["source_commit_label_changed_from_calibration"] is True
    assert provenance["environment_identical_verified"] is False
    assert provenance["worker_container_reservations_verified"] is False
    assert provenance["hardware_equal_to_calibration_observations"] is False
    assert provenance["report_identity"]["config_sha256"] == "d" * 64
    assert provenance["source_commit_label"] == "c" * 40


@pytest.mark.parametrize(
    "raw",
    [
        b'{"private":"SECRET", "status":1, "status":2}',
        b"[]",
        b"{}",
        b"{",
        b'"SECRET"',
        b"\xff",
        b'{"x":' + b"[" * 100 + b"]" * 100 + b"}",
        b'{"x":"' + b"x" * 2049 + b'"}',
        b" " * (gate.MAX_BYTES + 1),
        b'{"x":1e9999}',
        b'{"x":' + b"9" * 5000 + b"}",
    ],
)
def test_bounded_json_and_error_redaction(raw):
    receipt = gate.check(raw, COMMIT, RUN)
    assert receipt["status"] == "failed"
    assert "SECRET" not in json.dumps(receipt)


def test_no_unvalidated_extras_are_copied_to_receipt(report):
    report["private"] = "SECRET"
    report["identity"]["private"] = "SECRET"
    receipt = check(report)
    assert receipt["status"] == "passed"
    assert "SECRET" not in json.dumps(receipt)


def test_reference_mutation_fails_closed(report):
    assert check(report, budget_raw=gate.BUDGET.read_bytes() + b" ")["issues"] == [
        "frozen_budget_hash_mismatch"
    ]
    assert check(report, calibration_raw=gate.CALIBRATION.read_bytes() + b" ")[
        "issues"
    ] == ["calibration_hash_mismatch"]


def test_receipt_cannot_be_used_as_raw_measurement(report):
    assert check(check(report))["status"] == "failed"


def test_cli_creates_new_receipt_never_truncates_existing_inputs(tmp_path):
    raw = gate.CALIBRATION.read_bytes()
    source = tmp_path / "raw.json"
    source.write_bytes(raw)
    receipt = tmp_path / "receipt.json"
    args = [
        "--report",
        str(source),
        "--receipt",
        str(receipt),
        "--source-commit",
        COMMIT,
        "--github-run-id",
        RUN,
    ]
    assert gate.main(args) == 0
    written = receipt.read_bytes()
    assert json.loads(written)["status"] == "passed"
    assert source.read_bytes() == raw
    assert gate.main(args) == 2
    assert receipt.read_bytes() == written
    args[3] = str(source)
    assert gate.main(args) == 2
    assert source.read_bytes() == raw


def test_cli_failure_is_nonzero_and_separate_receipt(tmp_path, report):
    report["cleanup"]["cleanup_uncertain"] = True
    source = tmp_path / "raw.json"
    raw = json.dumps(report).encode()
    source.write_bytes(raw)
    receipt = tmp_path / "receipt.json"
    assert (
        gate.main(
            [
                "--report",
                str(source),
                "--receipt",
                str(receipt),
                "--source-commit",
                COMMIT,
                "--github-run-id",
                RUN,
            ]
        )
        == 1
    )
    assert json.loads(receipt.read_bytes())["status"] == "failed"
    assert source.read_bytes() == raw
