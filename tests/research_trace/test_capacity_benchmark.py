"""Benchmark-contract tests; mock timing values here are never measured evidence."""

from copy import deepcopy
from dataclasses import replace

import pytest

from packages.research_trace.comparison_contracts import DEFAULT_LIMITS, ComparisonError
from packages.research_trace.storage import TABLES, TraceReader
from script import research_trace_benchmark as benchmark
from script.research_trace_benchmark_fixture import frozen_namespace, trace_shaped_input


def contract_report():
    """Deliberately fake data solely for fail-closed validator unit tests."""
    samples = benchmark.timings(list(range(1, 31)))
    section = {
        "rows": 750_000,
        "changed": 0,
        "equal": 750_000,
        "left_only": 0,
        "right_only": 0,
        "ambiguous": 0,
        "left_availability": "available",
        "right_availability": "available",
    }
    source = benchmark.source_evidence()
    report = {
        "schema_version": benchmark.REPORT_VERSION,
        "fixture_version": benchmark.FIXTURE_VERSION,
        "status": "passed",
        "profile": {
            "listings": 1000,
            "sessions": 750,
            "variants": 3,
            "features_per_listing": 1,
            "predicates_per_listing": 1,
        },
        "source_start": deepcopy(source),
        "source_end": deepcopy(source),
        "environment": {
            "packages": {"duckdb": "1.4.5"},
            "reference": benchmark.REFERENCE,
        },
        "collection": [],
        "audits": [],
        "queries": [],
        "comparisons": [],
    }
    for variant in range(3):
        report["collection"].append(
            {
                "variant": variant,
                "manifest_sha256": "a" * 64,
                "status": "passed",
                "coverage": "complete",
                "missing_reasons": [],
                "decisions_seen": 750,
                "decisions_saved": 750,
                "detail_rows_seen": 2_250_000,
                "detail_rows_saved": 2_250_000,
                "table_rows": {
                    table: 750_000
                    if table in {"candidates", "features", "predicates"}
                    else 750
                    for table in TABLES
                },
                "elapsed_seconds": 1.0,
                "peak_rss_bytes": 1,
                "compressed_artifact_bytes": 1,
            }
        )
        tables = {
            table: {
                "rows": 750_000
                if table in {"candidates", "features", "predicates"}
                else 750,
                "sessions": 750,
                "decisions": 750,
                **(
                    {
                        "exact_frozen_instrument_set": True,
                        "min_rows_per_event": 1000,
                        "max_rows_per_event": 1000,
                        "min_instruments_per_event": 1000,
                        "max_instruments_per_event": 1000,
                    }
                    if table in {"candidates", "features", "predicates"}
                    else {}
                ),
            }
            for table in TABLES
        }
        report["audits"].append(
            {
                "variant": variant,
                "status": "passed",
                "manifest_sha256": "a" * 64,
                "physical_tables": tables,
                "all_artifact_hashes_verified": True,
            }
        )
        report["queries"].append(
            {
                "variant": variant,
                "status": "passed",
                "queries": {
                    f"{shape}.{mode}": deepcopy(samples)
                    for shape in ("candidate_page50", "instrument_stage_detail")
                    for mode in ("first_open", "hot")
                },
            }
        )
    for variant in (1, 2):
        sections = {
            name: deepcopy(section) for name in ("candidates", "features", "predicates")
        }
        sections["candidates"]["changed"] = 0 if variant == 1 else 1500
        sections["candidates"]["equal"] = 750_000 - sections["candidates"]["changed"]
        sections.update(
            {
                "events": {"rows": 750, "equal": 750},
                "targets": {
                    "rows": 1500 if variant == 1 else 3000,
                    "observed_equality": "equal" if variant == 1 else "different",
                },
                "orders": {"rows": 0, "left_availability": "not_collected"},
                "turnover": {"rows": 0, "observed_equality": "unknown"},
            }
        )
        report["comparisons"].append(
            {
                "variant": variant,
                "status": "passed",
                "code_changed": True,
                "limits": DEFAULT_LIMITS.model_dump(mode="json"),
                "sections": sections,
                "cached_comparison": deepcopy(samples),
                "observed_behavior": "insufficient_coverage"
                if variant == 1
                else "changed",
                "physical_output_rows": 2_252_250 if variant == 1 else 2_253_750,
                "output_rows": 2_252_250 if variant == 1 else 2_253_750,
                "uncompressed_payload_bytes": 1,
                "expanded_payload_bytes": 1,
                "comparison_deadline_seconds": 300,
                "first_comparison_ms": 1.0,
                "elapsed_seconds": 1.0,
                "peak_rss_bytes": 1,
                "compressed_artifact_bytes": 1,
            }
        )
    for item in report["comparisons"]:
        sections = {}
        for name, state in item["sections"].items():
            expected = 750 * (
                1000
                if name in {"candidates", "features", "predicates"}
                else 2
                if name == "targets"
                else 1
                if name == "events"
                else 0
            )
            sections[name] = {
                "rows": state["rows"],
                "expanded_canonical_sha256": "c" * 64,
                "stored_payload_bytes": 1 if name == "events" else 0,
                "expanded_payload_bytes": 1 if name == "events" else 0,
                "event_pairs": 750 if expected else 0,
                "side_observation_counts": [expected, expected],
                "source_observations_sha256": ["d" * 64, "e" * 64],
                "restored_observations_sha256": ["d" * 64, "e" * 64],
            }
        item["full_equivalence_audit"] = {
            "codec": benchmark.PAYLOAD_CODEC,
            "payload_accounting_version": benchmark.PAYLOAD_ACCOUNTING_VERSION,
            "all_rows_roundtripped": True,
            "all_source_observations_equivalent": True,
            "sections": sections,
            "source_manifest_sha256": ["a" * 64, "a" * 64],
            "elapsed_seconds": 1.0,
            "deadline_seconds": benchmark.AUDIT_DEADLINE_SECONDS,
            "closure_confirmed": True,
            "source_scan_bytes": 1,
            "source_byte_budget": DEFAULT_LIMITS.max_input_bytes,
            "fixture_record_byte_budget": benchmark.AUDIT_RECORD_BYTES,
            "physical_output_rows": item["physical_output_rows"],
            "uncompressed_payload_bytes": 1,
            "expanded_payload_bytes": 1,
        }
    for group in ("collection", "audits", "queries", "comparisons"):
        for item in report[group]:
            item.setdefault("elapsed_seconds", 1.0)
            item.setdefault("peak_rss_bytes", 1)
            item.update(
                {
                    "source_start": deepcopy(source),
                    "source_end": deepcopy(source),
                    "environment": deepcopy(report["environment"]),
                }
            )
    return report


def test_percentiles_use_explicit_nearest_rank_p95_and_all_samples():
    measured = benchmark.timings(list(range(1, 31)))
    assert measured["p50_ms"] == 15.5
    assert measured["p95_ms"] == 29
    assert measured["max_ms"] == measured["count"] == 30
    with pytest.raises(ValueError, match="invalid_samples"):
        benchmark.timings(list(range(1, 30)))
    with pytest.raises(ValueError, match="invalid_samples"):
        benchmark.timings([float("nan")] * 30)


def test_validator_accepts_complete_contract_only():
    assert benchmark.validate_report(contract_report())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(status="failed"),
        lambda r: r["profile"].update(sessions=749),
        lambda r: r["profile"].update(listings=999),
        lambda r: r["profile"].update(features_per_listing=0),
        lambda r: r["source_end"].update(relevant_files_sha256={}),
        lambda r: r["environment"]["packages"].update(duckdb="1.4.4"),
        lambda r: r["collection"].pop(),
        lambda r: r["collection"][0].update(elapsed_seconds=float("inf")),
        lambda r: r["audits"][0]["physical_tables"]["features"].update(
            min_instruments_per_event=999
        ),
        lambda r: r["audits"][0].update(all_artifact_hashes_verified=False),
        lambda r: r["audits"][0].update(manifest_sha256="b" * 64),
        lambda r: r["queries"][0]["source_start"].update(parent_head="b" * 40),
        lambda r: r["collection"][0].update(detail_rows_saved=2_249_999),
        lambda r: r["collection"][0]["table_rows"].update(predicates=749_999),
        lambda r: r["collection"][0].update(coverage="partial"),
        lambda r: r["queries"][0]["queries"].pop("candidate_page50.hot"),
        lambda r: r["queries"][0]["queries"]["candidate_page50.hot"].update(count=29),
        lambda r: r["queries"][0]["queries"].update(
            {"candidate_page50.hot": benchmark.timings([2001.0] * 30)}
        ),
        lambda r: r["comparisons"][0]["sections"]["candidates"].update(rows=749_999),
        lambda r: r["comparisons"][1]["sections"]["candidates"].update(changed=0),
        lambda r: r["comparisons"][0]["sections"]["turnover"].update(
            observed_equality="equal"
        ),
        lambda r: r["comparisons"][0]["limits"].update(max_output_bytes=4 * 1024**3),
        lambda r: r["comparisons"][0].update(comparison_deadline_seconds=600),
        lambda r: r["comparisons"][0].update(first_comparison_ms=300_001),
        lambda r: r["comparisons"][0]["full_equivalence_audit"].update(
            all_rows_roundtripped=False
        ),
        lambda r: r["comparisons"][0]["full_equivalence_audit"]["sections"][
            "features"
        ].update(side_observation_counts=[749999, 750000]),
        lambda r: r["comparisons"][0]["full_equivalence_audit"]["sections"][
            "predicates"
        ].update(restored_observations_sha256=["f" * 64, "e" * 64]),
    ],
)
def test_validator_rejects_shortcuts_and_missing_measurements(mutation):
    report = contract_report()
    mutation(report)
    with pytest.raises(ValueError):
        benchmark.validate_report(report)


def test_budget_is_derived_from_baseline_and_fail_closed(tmp_path):
    report = contract_report()
    baseline = tmp_path / "unit-contract-not-measurement.json"
    benchmark.write_json(baseline, report)
    budget = benchmark.freeze_budget(report, baseline)
    benchmark.validate_budget(report, budget, report)
    altered = deepcopy(budget)
    altered["maximum_metrics"]["collection.0.elapsed_seconds"] *= 10
    with pytest.raises(ValueError, match="altered_budget"):
        benchmark.validate_budget(report, altered, report)
    slower = deepcopy(report)
    slower["collection"][0]["elapsed_seconds"] = 2.01
    with pytest.raises(ValueError, match="regression"):
        benchmark.validate_budget(slower, budget, report)


def test_regression_multiplier_never_relaxes_production_comparison_deadline(tmp_path):
    # Deliberately fake timings test the validator only, never performance.
    report = contract_report()
    report["comparisons"][0]["first_comparison_ms"] = 200_000
    baseline = tmp_path / "unit-deadline-contract-not-measurement.json"
    benchmark.write_json(baseline, report)
    budget = benchmark.freeze_budget(report, baseline)
    slower = deepcopy(report)
    slower["comparisons"][0]["first_comparison_ms"] = 301_000
    assert (
        slower["comparisons"][0]["first_comparison_ms"]
        < budget["maximum_metrics"]["comparisons.1.first_comparison_ms"]
    )
    with pytest.raises(ValueError, match="comparison_deadline_exceeded"):
        benchmark.validate_report(slower)
    with pytest.raises(ValueError, match="comparison_deadline_exceeded"):
        benchmark.validate_budget(slower, budget, report)


def test_report_reader_rejects_duplicate_and_nonfinite_json(tmp_path):
    path = tmp_path / "invalid.json"
    for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
        path.write_text(raw)
        with pytest.raises(ValueError):
            benchmark.read_json(path)


def test_real_synthetic_feature_and_predicate_detail_not_empty(tmp_path):
    value = trace_shaped_input(tmp_path / "trace", frozen_namespace(), sessions=3)
    reader = TraceReader(value.trace_manifest, value.trace_paths)
    event = reader.query("events", limit=1)["items"][0]
    benchmark.query_shape(reader, "candidate_page50", event)
    benchmark.query_shape(reader, "instrument_stage_detail", event)
    assert (
        value.trace_manifest.detail_rows_seen
        == value.trace_manifest.detail_rows_saved
        == 9000
    )
    assert value.economic_bundle == {}


@pytest.mark.parametrize(
    "change",
    [
        lambda source, env: source["relevant_files_sha256"].update(
            {"packages/research_trace/storage.py": "b" * 64}
        ),
        lambda source, env: source["relevant_files_sha256"].update(
            {"backend/requirements.lock": "b" * 64}
        ),
        lambda source, env: source.update(parent_head="b" * 40),
        lambda source, env: env["packages"].update(duckdb="1.4.4"),
        lambda source, env: env.update(python="3.13.0"),
        lambda source, env: env.update(logical_cpus_affinity=99),
        lambda source, env: env.update(cgroup={"cpu.max": "100000 100000"}),
        lambda source, env: env.update(host_node_sha256="b" * 64),
    ],
)
def test_resume_rejects_changed_code_dependencies_and_environment(change):
    source = benchmark.source_evidence()
    environment = {
        "packages": {"duckdb": "1.4.5"},
        "python": "3.12.0",
        "logical_cpus_affinity": 2,
        "cgroup": {"cpu.max": None},
        "host_node_sha256": "a" * 64,
    }
    owner = {
        "fixture_version": benchmark.FIXTURE_VERSION,
        "resume_identity": deepcopy(benchmark.resume_identity(source, environment)),
    }
    benchmark.admit_resume(owner, source, environment)
    change(source, environment)
    with pytest.raises(ValueError, match="resume_source_or_environment_changed"):
        benchmark.admit_resume(owner, source, environment)


def test_resume_preserves_runtime_identity_but_allows_artifact_disk_usage():
    source = benchmark.source_evidence()
    environment = {"disk_free_bytes_at_start": 1000, "packages": {"duckdb": "1.4.5"}}
    owner = {
        "fixture_version": benchmark.FIXTURE_VERSION,
        "resume_identity": deepcopy(benchmark.resume_identity(source, environment)),
    }
    environment["disk_free_bytes_at_start"] = 999
    benchmark.admit_resume(owner, source, environment)


def test_driver_rejects_stale_clean_resume_before_any_worker(tmp_path, monkeypatch):
    from argparse import Namespace

    current = benchmark.source_evidence()
    previous = deepcopy(current)
    previous["relevant_files_sha256"]["packages/research_trace/storage.py"] = "b" * 64
    current["dirty"] = False
    environment = {"packages": {"duckdb": "1.4.5"}, "python": "3.12.0"}
    owner = {
        "fixture_version": benchmark.FIXTURE_VERSION,
        "resume_identity": benchmark.resume_identity(previous, environment),
    }
    benchmark.write_json(tmp_path / "benchmark-owner.json", owner)
    monkeypatch.setattr(benchmark, "source_evidence", lambda: current)
    monkeypatch.setattr(benchmark, "environment_evidence", lambda work: environment)

    def forbidden(*args, **kwargs):
        raise AssertionError("A stale cache must be rejected before launching workers")

    monkeypatch.setattr(benchmark.subprocess, "run", forbidden)
    args = Namespace(
        work_dir=tmp_path,
        resume=True,
        require_clean=True,
        report=tmp_path / "must-not-exist.json",
    )
    with pytest.raises(ValueError, match="resume_source_or_environment_changed"):
        benchmark.run(args)
    assert not args.report.exists()


def test_cached_phase_keeps_and_checks_its_own_original_provenance():
    source = benchmark.source_evidence()
    source["dirty"] = False
    environment = {"packages": {"duckdb": "1.4.5"}}
    phase = {
        "status": "passed",
        "source_start": deepcopy(source),
        "source_end": deepcopy(source),
        "environment": deepcopy(environment),
    }
    benchmark.admit_cached_phase(phase, source, environment, require_clean=True)
    phase["source_start"]["dirty"] = True
    with pytest.raises(ValueError, match="dirty_cached_phase"):
        benchmark.admit_cached_phase(phase, source, environment, require_clean=True)
    phase["source_start"]["dirty"] = False
    phase["environment"]["packages"]["pydantic"] = "different"
    with pytest.raises(ValueError, match="cached_phase_provenance_changed"):
        benchmark.admit_cached_phase(phase, source, environment)


def test_full_roundtrip_matches_all_original_observations(tmp_path, monkeypatch):
    from uuid import uuid4

    from packages.research_trace.comparison import compare_evaluations
    from packages.research_trace.comparison_contracts import ComparisonPublication

    namespace = frozen_namespace()
    left = trace_shaped_input(tmp_path / "left", namespace, sessions=1)
    right = trace_shaped_input(tmp_path / "right", namespace, sessions=1, variant=2)
    manifest, artifacts = compare_evaluations(
        left,
        right,
        tmp_path / "comparison",
        publication=ComparisonPublication(
            comparison_id=uuid4(), run_id=uuid4(), execution_attempt_id=uuid4()
        ),
    )
    paths = {artifact.name: artifact.path for artifact in artifacts}
    proof = benchmark.full_comparison_audit(manifest, paths, left, right)
    assert proof["all_rows_roundtripped"]
    assert proof["all_source_observations_equivalent"]
    for section in ("candidates", "features", "predicates"):
        checked = proof["sections"][section]
        assert checked["rows"] == 1000
        assert checked["side_observation_counts"] == [1000, 1000]
        assert (
            checked["source_observations_sha256"]
            == checked["restored_observations_sha256"]
        )
    assert proof["expanded_payload_bytes"] > proof["uncompressed_payload_bytes"]
    original = benchmark.source_rows

    def changed_source(value, table, control):
        for row in original(value, table, control):
            if table == "features" and row["ordinal"] == 0:
                body = benchmark.json.loads(row["payload"])
                body["value"] = -1
                row = {**row, "payload": benchmark.canonical_bytes(body).decode()}
            yield row

    monkeypatch.setattr(benchmark, "source_rows", changed_source)
    with pytest.raises(ValueError, match="comparison_full_observation_bytes"):
        benchmark.full_comparison_audit(manifest, paths, left, right)
    monkeypatch.setattr(benchmark, "source_rows", original)
    original_headers = benchmark.source_event_headers

    def changed_numeric_type(value, control):
        headers, stages = original_headers(value, control)
        for header in headers.values():
            # Python == accepts this, but it is not the original JSON value.
            header["pre_state"]["cash"] = int(header["pre_state"]["cash"])
        return headers, stages

    monkeypatch.setattr(benchmark, "source_event_headers", changed_numeric_type)
    with pytest.raises(ValueError, match="comparison_full_event_header"):
        benchmark.full_comparison_audit(manifest, paths, left, right)
    monkeypatch.setattr(benchmark, "source_event_headers", original_headers)
    original_decode = benchmark.decode_record

    def lossy_decoder(*args):
        record, size = original_decode(*args)
        return record.model_copy(update={"ambiguous": not record.ambiguous}), size

    monkeypatch.setattr(benchmark, "decode_record", lossy_decoder)
    with pytest.raises(ValueError, match="comparison_roundtrip"):
        benchmark.full_comparison_audit(manifest, paths, left, right)
    monkeypatch.setattr(benchmark, "decode_record", original_decode)
    for stop in ("cancel", "deadline"):
        control = benchmark.CapacityAuditControl()
        observed_live_connections = []

        def interrupted_source(
            value,
            table,
            owned,
            stop=stop,
            observed_live_connections=observed_live_connections,
        ):
            from contextlib import closing

            with closing(original(value, table, owned)) as stream:
                for row in stream:
                    if table == "candidates":
                        observed_live_connections.append(owned.active_connections)
                        if stop == "cancel":
                            owned.cancel()
                        else:
                            owned.deadline = 0
                    yield row

        monkeypatch.setattr(benchmark, "source_rows", interrupted_source)
        with pytest.raises(ComparisonError) as error:
            benchmark.full_comparison_audit(
                manifest, paths, left, right, control=control
            )
        assert error.value.code == (
            "comparison_cancelled" if stop == "cancel" else "comparison_timeout"
        )
        assert observed_live_connections and min(observed_live_connections) >= 2
        assert control.closure_confirmed and control.active_connections == 0
    event_ref = next(
        ref
        for ref in left.trace_manifest.artifacts
        if benchmark._table_ref(left.trace_manifest, ref, "events")
    )
    oversized = tmp_path / "oversized" / event_ref.name
    oversized.parent.mkdir()
    with benchmark._connection() as connection:
        connection.execute(
            "COPY (SELECT * REPLACE ($value AS stage_ids) FROM read_parquet($input)) TO $output (FORMAT PARQUET)",
            {
                "value": "x" * (benchmark.AUDIT_RECORD_BYTES + 1),
                "input": str(left.trace_paths[event_ref.name]),
                "output": str(oversized),
            },
        )
    replacement = event_ref.model_copy(
        update={
            "sha256": benchmark.sha(oversized),
            "size_bytes": oversized.stat().st_size,
        }
    )
    poisoned = replace(
        left,
        trace_manifest=left.trace_manifest.model_copy(
            update={
                "artifacts": [
                    replacement if ref.name == event_ref.name else ref
                    for ref in left.trace_manifest.artifacts
                ]
            }
        ),
        trace_paths={**left.trace_paths, event_ref.name: oversized},
    )
    control = benchmark.CapacityAuditControl()
    with pytest.raises(ValueError, match="audit_source_physical_bounds"):
        list(original(poisoned, "events", control))
    assert control.closure_confirmed and control.active_connections == 0
