"""Actual native admission of the complete frozen browser comparison fixture.

This is not HTTP, PostgreSQL or container acceptance. The pytest-free native
child runs the maintained source through the real evaluator entrypoint, with
the same CSV, split windows, warmup, market profile and variant as Playwright.
No short manual tape, synthetic result or independent-observation reward bypass
can certify the optimization fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from packages.research_trace.samples import CONTROLLED_SOURCE_SHA256

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "frontend/tests/fixtures"
COMPARISON = json.loads((FIXTURES / "mechanism-comparison.json").read_text())
CSV_METADATA = json.loads((FIXTURES / "research-cn.json").read_text())

SCRIPT = r"""
import base64, contextlib, csv, hashlib, io, json, sys, tempfile
from pathlib import Path
import pandas as pd
from packages.research_trace.contracts import DecisionRecord
from packages.research_trace.templates import native_contract
from packages.research_trace.transport import (
    CaptureReceiver, TRACE_PREFIX, iter_capture_records,
)
from packages.tradegrad_adapter.config import DateWindow, MarketConfig, canonical_bytes
from packages.tradegrad_adapter.results import validate_envelope
from packages.tradegrad_adapter.windows import stage_csv_window
from runtimes.evaluator.entrypoint import main

source_path, source_sha, fixture_path, fixture_sha, contract_json, split, variant = sys.argv[1:]
source, fixture = Path(source_path), Path(fixture_path)
assert source.name == 'research_time.py' and not source.is_symlink()
assert fixture.name == 'research-cn.csv' and not fixture.is_symlink()
assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha
assert hashlib.sha256(fixture.read_bytes()).hexdigest() == fixture_sha
contract = json.loads(contract_json)
assert variant in {'baseline', 'changed', 'inadmissible_old_variant', 'comment_only', 'execution_only', 'mixed'}
executed_source = source.read_text()
if variant == 'comment_only':
    executed_source += '\n# I4 comparison: documentation only, no executable change.\n'
elif variant in {'execution_only', 'mixed'}:
    mutation = contract['execution_only']
    assert executed_source.count(mutation['recorder_hook']) == 1
    replacement = mutation['replacement'] if variant == 'execution_only' else (
        '    recorder = current_recorder()\n'
        '    return recorder if recorder is not None and recorder.decisions_seen == 0 else None'
    )
    executed_source = executed_source.replace(mutation['recorder_hook'], replacement)
window = contract['windows'][split]
parameters = {} if variant in {'baseline', 'comment_only', 'execution_only', 'mixed'} else {
    'max_weight': 0.15 if variant == 'inadmissible_old_variant'
    else contract['allocation_and_fees']['max_weight'],
}
engine = MarketConfig.for_market('CN', 'corrected_explicit_config').engine_kwargs()
if variant in {'changed', 'inadmissible_old_variant'}:
    engine['commission_rate'] = contract['allocation_and_fees']['commission_rate']

# Reproduce write_bundle's evaluator CSV for these explicitly mapped upload
# fields. Upload normalization casts every numeric Bar field to float; dates
# remain session labels. The complete 76-row source is checked before staging.
fields = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
frame = pd.read_csv(fixture, usecols=fields)
normalized = io.StringIO(newline='')
writer = csv.DictWriter(normalized, fieldnames=fields)
writer.writeheader()
for row in frame.to_dict('records'):
    writer.writerow({key: row[key] if key == 'date' else float(row[key]) for key in fields})
staged = stage_csv_window(
    normalized.getvalue().encode(), listing_id='frozen-browser-fixture',
    window=DateWindow.model_validate(window),
    warmup_sessions=contract['warmup_sessions'], history_policy='require',
)
config = {
    'engine_overrides': engine,
    'window': {**window, 'role': split},
    'warmup': {'sessions': contract['warmup_sessions'], 'history_policy': 'require'},
    'benchmark': None,
    # A local test binding, not a claim of a persisted API run identity.
    'run_config_sha256': hashlib.sha256(canonical_bytes(contract)).hexdigest(),
    'research_capture': {'mode': 'research'},
    'strategy_contract': native_contract('daily_timing', parameters=parameters).model_dump(mode='json'),
}
assert 'result_policy' not in config
request = {
    'task': 'cn_time', 'market': 'CN', 'profile': 'corrected_explicit_config',
    'start_date': window['start'], 'end_date': window['end'], 'evaluation_config': config,
}
report = {
    'source_sha256': source_sha, 'fixture_sha256': fixture_sha,
    'fixture_rows': len(frame), 'staged_sha256': hashlib.sha256(staged.content).hexdigest(),
    'warmup_sessions': staged.report.staged_warmup_sessions,
    'evaluation_rows': staged.report.evaluation_rows,
}
with tempfile.TemporaryDirectory(prefix='native-browser-comparison-') as directory:
    root = Path(directory).resolve(); inputs = root / 'input'; output = root / 'output'
    (inputs / 'data').mkdir(parents=True)
    (inputs / 'strategy.py').write_text(executed_source)
    (inputs / 'data/INDEX.csv').write_bytes(staged.content)
    (inputs / 'request.json').write_bytes(canonical_bytes(request))
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            main(inputs, output)
    except ValueError as error:
        if variant != 'inadmissible_old_variant':
            raise
        assert str(error) == 'No trades executed, invalid strategy.'
        assert 'TRADEGRAD_RESULT_' not in stdout.getvalue()
        report.update(status='rejected', error=str(error))
    else:
        lines = stdout.getvalue().splitlines()
        results = [line for line in lines if line.startswith('TRADEGRAD_RESULT_')]
        assert len(results) == 1 and results[0].startswith('TRADEGRAD_RESULT_V2:')
        envelope = validate_envelope(base64.b64decode(results[0].split(':', 1)[1], validate=True))
        bundle = envelope['bundle']
        receiver = CaptureReceiver(root, config['research_capture'], source='strategy_emitted')
        for line in lines:
            if line.startswith(TRACE_PREFIX):
                receiver.accept(json.loads(line[len(TRACE_PREFIX):]))
        captured = receiver.result()
        records = [DecisionRecord.model_validate(record) for record in iter_capture_records(
            captured['research_capture_path'], captured['research_capture_sha256'],
            captured['research_capture_size_bytes'], config['research_capture'],
        )]
        decisions = {
            record.data_event_id: {
                'pre_state': record.pre_state.model_dump(mode='json', include={'cash', 'equity', 'positions'}),
                'targets': [stage.constrained_plan for stage in record.stages if stage.constrained_plan is not None],
            }
            for record in records if record.pre_state is not None
        }
        report.update(
            status='succeeded', metrics=envelope['metrics'],
            engine=bundle['engine']['requested_kwargs'],
            closed_trades=len(bundle['closed_trades']['rows']),
            fills=len(bundle['fills']['rows']), decisions=decisions,
            capture_mode=captured['research_capture']['capture']['mode'],
            source=executed_source, code_sha256=hashlib.sha256(executed_source.encode()).hexdigest(),
            records=[record.model_dump(mode='json') for record in records],
            capture=captured['research_capture'], bundle=bundle, config=config,
        )
print('BROWSER_COMPARISON_NATIVE:' + json.dumps(report, allow_nan=False, sort_keys=True))
"""


def execute(split, variant):
    python = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    if not python:
        if os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1":
            pytest.fail("Frozen browser admission requires the native interpreter")
        pytest.skip("Actual frozen browser fixture admission is opt-in")
    source = ROOT / "seed_strategies/research_time.py"
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == CONTROLLED_SOURCE_SHA256["daily_timing"]
    )
    process = subprocess.run(
        [
            python,
            "-B",
            "-c",
            SCRIPT,
            str(source),
            CONTROLLED_SOURCE_SHA256["daily_timing"],
            str(FIXTURES / "research-cn.csv"),
            CSV_METADATA["sha256"],
            json.dumps(COMPARISON),
            split,
            variant,
        ],
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TRADEGRAD_DATA_WORKERS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert process.returncode == 0, process.stderr[-8192:] + process.stdout[-2000:]
    rows = [
        line.partition(":")[2]
        for line in process.stdout.splitlines()
        if line.startswith("BROWSER_COMPARISON_NATIVE:")
    ]
    assert len(rows) == 1
    return json.loads(rows[0])


@pytest.mark.parametrize("split,evaluation_rows", [("train", 25), ("validation", 24)])
def test_full_browser_comparison_pair_passes_unchanged_native_reward_gate(
    split, evaluation_rows
):
    baseline, changed = (execute(split, side) for side in ("baseline", "changed"))
    for result in (baseline, changed):
        assert result["status"] == "succeeded"
        assert result["fixture_rows"] == CSV_METADATA["rows"] == 76
        assert result["warmup_sessions"] == COMPARISON["warmup_sessions"] == 20
        assert result["evaluation_rows"] == evaluation_rows
        assert result["closed_trades"] > 0 and result["fills"] > 0
        assert result["metrics"]["combined_score"] is not None
        assert result["metrics"]["metrics"]["trade_per_day"] > 0
        assert result["capture_mode"] == "research"
        assert result["engine"]["lot_size"] == 100
        assert result["engine"]["t_plus_one"] is True
    for key in ("source_sha256", "fixture_sha256", "staged_sha256"):
        assert baseline[key] == changed[key]
    assert baseline["engine"]["commission_rate"] == 0.0005
    assert (
        changed["engine"]["commission_rate"]
        == COMPARISON["allocation_and_fees"]["commission_rate"]
        == 0.001
    )
    assert baseline["decisions"].keys() == changed["decisions"].keys()
    assert any(
        baseline["decisions"][key]["pre_state"]
        != changed["decisions"][key]["pre_state"]
        for key in baseline["decisions"]
    )
    assert any(
        baseline["decisions"][key]["targets"] != changed["decisions"][key]["targets"]
        for key in baseline["decisions"]
    )


def test_previous_15_percent_variant_remains_ineligible_for_optimization():
    result = execute("train", "inadmissible_old_variant")
    assert result["status"] == "rejected"
    assert result["error"] == "No trades executed, invalid strategy."


def stored_browser_input(path, observed, namespace):
    """Persist only actual evaluator records; UUIDs are local query bindings."""
    from uuid import uuid4

    from packages.research_trace.comparison_contracts import ComparisonInput
    from packages.research_trace.contracts import (
        DateRange,
        DecisionRecord,
        EvaluationBinding,
        digest,
    )
    from packages.research_trace.storage import write_trace_artifacts
    from packages.research_trace.templates import template_spec

    config, bundle = observed["config"], observed["bundle"]
    engine = bundle["engine"]
    kwargs = engine["requested_kwargs"]
    source_id = uuid4()
    binding = EvaluationBinding(
        run_id=uuid4(),
        execution_attempt_id=uuid4(),
        evaluation_id=uuid4(),
        candidate_id=None,
        strategy_version_id=source_id,
        code_sha256=observed["code_sha256"],
        dataset_version_id=namespace.dataset_version_id,
        dataset_sha256=namespace.dataset_sha256,
        universe_version_id=None,
        universe_sha256=None,
        execution_config_sha256=digest(config),
        engine_dependencies_sha256=digest(engine["dependencies"]),
        result_manifest_sha256=digest(bundle),
    )
    manifest, artifacts = write_trace_artifacts(
        [DecisionRecord.model_validate(row) for row in observed["records"]],
        binding,
        observed["capture"],
        path,
    )
    ref = next(row for row in artifacts if row.name.endswith(".json"))
    spec = template_spec("daily_timing", source=observed["source"])
    return ComparisonInput(
        binding=binding,
        source_kind="strategy_version",
        source_id=source_id,
        source=observed["source"],
        window=DateRange.model_validate(COMPARISON["windows"]["train"]),
        spec=spec,
        spec_sha256=digest(spec),
        trace_manifest=manifest,
        trace_manifest_sha256=ref.sha256,
        trace_paths={row.name: row.path for row in artifacts},
        common_config={
            "data": {"snapshot_sha256": namespace.dataset_sha256},
            "universe": {
                "effective_membership_sha256": namespace.frozen_universe_sha256
            },
            "window": config["window"],
            "warmup": config["warmup"],
            "initial_state": {
                "cash": kwargs["initial_cash"],
                "positions": "fresh_engine",
            },
            "state_policy": "fresh_full_window_reconstruction",
            "fees": {key: kwargs[key] for key in ("commission_rate", "stamp_tax_rate")},
            "fill": engine["resolved_execution_policy"],
            "market": {
                "currency": "CNY",
                "lot_size": kwargs["lot_size"],
                "t_plus_one": kwargs["t_plus_one"],
            },
            "RNG": {
                "status": "not_recorded",
                "reason": "No historical strategy RNG declaration",
            },
            "engine": {
                "dependencies": engine["dependencies"],
                "resolved_config": engine["resolved_config"],
            },
        },
        economic_bundle=bundle,
        namespace=namespace,
    )


def test_actual_browser_sources_pair_explicit_origins_but_not_execution_only_ids(
    tmp_path,
):
    from uuid import uuid4

    from packages.research_trace.comparison import compare_evaluations
    from packages.research_trace.comparison_contracts import (
        ComparisonPublication,
        EventNamespace,
    )
    from packages.research_trace.comparison_storage import (
        ComparisonControl,
        ComparisonReader,
    )
    from packages.research_trace.contracts import digest

    observed = {
        name: execute("train", name)
        for name in ("baseline", "comment_only", "execution_only", "mixed")
    }
    baseline = observed["baseline"]
    for value in observed.values():
        assert value["status"] == "succeeded"
        assert value["metrics"] == baseline["metrics"]
        assert value["closed_trades"] == baseline["closed_trades"] > 0
        assert value["fills"] == baseline["fills"] > 0
        assert value["engine"] == baseline["engine"]
    namespace = EventNamespace(
        dataset_version_id=uuid4(),
        dataset_sha256=CSV_METADATA["sha256"],
        universe_version_id=None,
        universe_sha256=None,
        frozen_universe_sha256=digest({"instrument_ids": ["target_symbol"]}),
        instrument_namespace="native_symbol",
    )
    inputs = {
        name: stored_browser_input(tmp_path / name, value, namespace)
        for name, value in observed.items()
    }
    compared = {}
    for label, left_name, right_name in (
        ("comment_only", "baseline", "comment_only"),
        ("execution_only:right", "baseline", "execution_only"),
        ("execution_only:left", "execution_only", "baseline"),
        ("mixed:right", "baseline", "mixed"),
        ("mixed:left", "mixed", "baseline"),
    ):
        control = ComparisonControl()
        try:
            manifest, artifacts = compare_evaluations(
                inputs[left_name],
                inputs[right_name],
                tmp_path / (label.replace(":", "-") + "-comparison"),
                publication=ComparisonPublication(
                    comparison_id=uuid4(), run_id=uuid4(), execution_attempt_id=uuid4()
                ),
                control=control,
            )
        finally:
            control.close()
        reader = ComparisonReader(manifest, {row.name: row.path for row in artifacts})
        events = reader.page("events", limit=100).items
        assert reader.page("events", limit=100).total == len(events)
        compared[label] = (manifest, events, reader)

    manifest, paired, _ = compared["comment_only"]
    assert len(paired) == len(baseline["records"]) == 28
    assert all(
        row.left
        and row.right
        and row.match_basis == "source_event"
        and row.status == "equal"
        for row in paired
    )
    fields = ("data_event_id", "callback_phase", "scope", "event_seq", "instrument_id")
    for row in paired:
        assert all(row.left[key] == row.right[key] for key in fields)
        assert row.left["decision_id"] != row.right["decision_id"]
    completions = [
        row
        for row in paired
        if row.left["callback_phase"] == "native.execution_result:on_bar"
    ]
    assert len(completions) == 3
    for row in completions:
        assert row.left["pre_state"] is row.right["pre_state"] is None
        assert any(
            other.left["callback_phase"] == "on_bar"
            and all(
                other.left[key] == row.left[key]
                for key in fields
                if key != "callback_phase"
            )
            for other in paired
        )
    assert {"candidates", "features", "predicates", "targets"} <= set(
        manifest.observed_equal_sections
    )

    for variant in ("execution_only", "mixed"):
        for generic_side, source_side in (("left", "right"), ("right", "left")):
            manifest, events, reader = compared[f"{variant}:{generic_side}"]
            generic = [
                row
                for row in events
                if getattr(row, generic_side)
                and getattr(row, generic_side)["data_event_id"].startswith(
                    "native_order:"
                )
            ]
            assert len(generic) == 3
            matched = [row for row in events if row.left and row.right]
            assert len(matched) == (1 if variant == "mixed" else 0)
            assert all(
                row.match_basis == "source_event" and row.status == "equal"
                for row in matched
            )
            assert len(events) == len(baseline["records"]) + len(
                observed[variant]["records"]
            ) - len(matched)
            for row in generic:
                header = getattr(row, generic_side)
                assert (
                    row.status == generic_side + "_only"
                    and getattr(row, source_side) is None
                )
                assert getattr(row, source_side + "_state") == "unpaired"
                assert getattr(row, generic_side + "_state") == "observed"
                assert row.match_basis == "none" and row.missing_reason
                assert header["callback_phase"] == "native.execution_result"
                assert (
                    header["pre_state"] is None and header["pre_state_missing_reason"]
                )
                assert any(
                    getattr(other, source_side)
                    and all(
                        getattr(other, source_side)[key] == header[key]
                        for key in ("session_date", "instrument_id")
                    )
                    for other in events
                )
            unmatched_sources = [
                row
                for row in events
                if getattr(row, source_side) and getattr(row, generic_side) is None
            ]
            assert unmatched_sources
            assert all(
                getattr(row, generic_side + "_state") == "incomplete_coverage"
                for row in unmatched_sources
            )
            for section in ("candidates", "features", "predicates", "targets"):
                assert manifest.sections[section].observed_equality == "unknown"
                absent = [
                    row
                    for offset in range(0, manifest.sections[section].rows, 100)
                    for row in reader.page(section, offset=offset, limit=100).items
                    if getattr(row, generic_side) is None
                ]
                assert absent and all(
                    getattr(row, generic_side + "_state") == "incomplete_coverage"
                    for row in absent
                )
            assert manifest.observed_behavior == "insufficient_coverage"
            assert not manifest.diagnostic_parity_certified


def test_execution_only_browser_source_passes_validation_reward_gate():
    baseline, execution_only = (
        execute("validation", name) for name in ("baseline", "execution_only")
    )
    assert execution_only["status"] == "succeeded"
    assert execution_only["metrics"] == baseline["metrics"]
    assert execution_only["closed_trades"] == baseline["closed_trades"] > 0
    assert execution_only["fills"] == baseline["fills"] > 0
    assert execution_only["engine"] == baseline["engine"]
    assert execution_only["records"]
    assert all(
        row["data_event_id"].startswith("native_order:")
        and row["callback_phase"] == "native.execution_result"
        and row["pre_state"] is None
        and row["pre_state_missing_reason"]
        for row in execution_only["records"]
    )
