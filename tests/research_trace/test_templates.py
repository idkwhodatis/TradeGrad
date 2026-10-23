"""AST-only host tests plus opt-in real native-engine subprocess acceptance.

The host never imports candidate Python. Opt-in executions are restricted to the
two maintained controlled sources; the child checks the exact source SHA before
AKQuant loads it. This is native engine evidence, not Docker/HTTP/browser proof.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from packages.research_trace.contracts import CaptureConfig, Predicate
from packages.research_trace.recorder import Recorder, recording
from packages.research_trace.samples import (
    CASH_ONLY_EXPECTED,
    CONTROLLED_SOURCE_SHA256,
    CROSS_FIRST_READY,
    CROSS_SECTION_TAPE,
    DATES,
    MISSING_STATE_EXAMPLES,
    RETENTION_CASH_EXPECTED,
    SAME_TIMESTAMP_EVENTS,
    TIMING_EXPECTED,
    TIMING_TAPE,
)
from packages.research_trace.templates import (
    available_templates,
    native_contract,
    template_source,
    template_spec,
)

ROOT = Path(__file__).resolve().parents[2]
LOCAL_ENGINE = ROOT.parent / "tradegrad-webui-v1/.venv/bin/python"


@pytest.mark.parametrize("template_id", available_templates())
def test_specs_are_ast_only_hash_bound_and_have_exact_source_anchors(
    template_id, monkeypatch
):
    original_import = builtins.__import__

    def no_candidate_import(name, *args, **kwargs):
        assert name not in (
            "akquant",
            "seed_strategies.research_time",
            "seed_strategies.research_cross",
        )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_candidate_import)
    source = template_source(template_id)
    code_hash = hashlib.sha256(source.encode()).hexdigest()
    spec = template_spec(template_id, expected_code_sha256=code_hash)
    assert spec.code_sha256 == code_hash
    assert spec.template_id == template_id
    assert spec.coverage == "complete" and spec.edges
    tree = ast.parse(source)
    methods = {
        f"{node.name}.{item.name}": item
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for item in node.body
        if isinstance(item, ast.FunctionDef)
    }
    for rule in spec.rules:
        assert rule.mapping == "controlled"
        assert rule.source_ref.code_sha256 == code_hash
        assert rule.source_ref.file == "strategy.py"
        node = methods[rule.source_ref.symbol]
        assert (rule.source_ref.line_start, rule.source_ref.line_end) == (
            node.lineno,
            node.end_lineno,
        )


def test_edited_source_never_silently_reuses_old_spec_or_controlled_graph():
    original = template_source("cross_sectional")
    modified = original.replace('"top_n": 1,', '"top_n": 2,', 1)
    with pytest.raises(ValueError, match="immutable code hash"):
        template_spec(
            "cross_sectional",
            source=modified,
            expected_code_sha256=hashlib.sha256(original.encode()).hexdigest(),
        )
    spec = template_spec("cross_sectional", source=modified)
    assert spec.template_id is None and spec.edges == []
    assert spec.coverage == "partial"
    assert all(
        rule.mapping == "inferred" and rule.parameters["top_n"] == 2
        for rule in spec.rules
    )


def test_source_inspection_never_executes_top_level_statements(tmp_path):
    trap = tmp_path / "must-not-exist"
    source = f"open({str(trap)!r}, 'w').write('executed')\n" + template_source(
        "daily_timing"
    )
    spec = template_spec("daily_timing", source=source)
    assert spec.coverage == "partial" and not trap.exists()
    hostile = source.replace('"parameters": {', '"parameters": dict(', 1)
    with pytest.raises((SyntaxError, ValueError)):
        template_spec("daily_timing", source=hostile)
    assert not trap.exists()


@pytest.mark.parametrize("template_id", available_templates())
def test_explicit_native_parameter_contract(template_id):
    contract = native_contract(
        template_id, parameters={"liquidity_enabled": False, "max_weight": 0.25}
    )
    assert contract.requires_shorting is False
    assert contract.lookback_bars == contract.warmup_bars == 3
    assert contract.parameter_values()["liquidity_enabled"] is False
    assert contract.parameter_values()["max_weight"] == 0.25
    with pytest.raises(ValueError):
        native_contract(template_id, parameters={"max_weight": 1.1})
    with pytest.raises(ValueError):
        native_contract(template_id, parameters={"liquidity_enabled": 1})
    with pytest.raises(ValueError):
        native_contract(template_id, parameters={"unknown_parameter": 2})


@pytest.mark.parametrize("template_id", available_templates())
def test_frozen_sample_source_pin_is_exact(template_id):
    assert (
        hashlib.sha256(template_source(template_id).encode()).hexdigest()
        == CONTROLLED_SOURCE_SHA256[template_id]
    )


def test_manual_samples_are_frozen_and_not_an_alternative_simulator():
    assert CROSS_SECTION_TAPE.dates == TIMING_TAPE.dates == DATES
    with pytest.raises((AttributeError, TypeError)):
        CROSS_SECTION_TAPE.initial_cash = 3
    expected = {row.instrument_id: row for row in CROSS_FIRST_READY}
    assert expected["C"].score > expected["B"].score
    assert expected["C"].selected is False and expected["B"].target_quantity == 333
    assert expected["A"].rank == 2 and expected["A"].reason == "rank_not_selected"
    assert RETENTION_CASH_EXPECTED["D_reason"] == "holding_retained"
    assert CASH_ONLY_EXPECTED["B_target_quantity"] == 8


def test_missing_states_are_distinct_valid_contract_examples():
    rows = [
        Predicate(
            condition_id="example",
            operator="gt",
            operands=list(row.operands) if row.operands else None,
            threshold=row.threshold,
            state=row.state,
            missing_reason=row.missing_reason,
            source="static_inference",
        )
        for row in MISSING_STATE_EXAMPLES
    ]
    assert {row.state for row in rows} == {
        "false",
        "not_evaluated",
        "unknown",
        "not_sampled",
    }
    assert rows[0].operands == [10.5] and rows[0].missing_reason is None
    assert all(row.missing_reason for row in rows[1:])


def test_two_portfolio_decisions_at_same_timestamp_do_not_collapse():
    recorder = Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="research"))
    with recording(recorder):
        for timestamp, event_id, phase, sequence in SAME_TIMESTAMP_EVENTS:
            recorder.begin_decision(
                data_event_id=event_id,
                event_seq=sequence,
                scope="portfolio",
                callback_phase=phase,
                decision_time=timestamp,
                information_cutoff=timestamp,
                market_timezone="America/New_York",
            )
            recorder.emit("ranking", {"candidates": []})
            recorder.end_decision()
    assert len(recorder.records) == 2
    assert len({row.decision_id for row in recorder.records}) == 2
    assert recorder.records[0].pairing_key() != recorder.records[1].pairing_key()
    assert all(row.scope == "portfolio" for row in recorder.records)


ENGINE_SCRIPT = r"""
import hashlib, json, sys
from pathlib import Path
import pandas as pd
import akquant as aq
from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.recorder import Recorder, recording
from packages.research_trace.samples import CROSS_SECTION_TAPE, TIMING_TAPE
from src.evaluate import Date2Symbols
name, source_path, expected_sha, mode, mutation, parameters = sys.argv[1:]
path = Path(source_path)
assert not path.is_symlink()
assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha
assert path.name in {'research_cross.py', 'research_time.py'}
tape = CROSS_SECTION_TAPE if name == 'cross_sectional' else TIMING_TAPE
data = {}
for item in tape.instruments:
    closes = list(item.closes)
    if mutation == 'future':
        closes[4:] = [value * 10 for value in closes[4:]]
    if mutation == 'tie' and item.instrument_id == 'A':
        closes = list(next(value.closes for value in tape.instruments if value.instrument_id == 'B'))
    data[item.instrument_id] = pd.DataFrame({'date': pd.to_datetime(tape.dates).tz_localize(tape.market_timezone) + pd.Timedelta(hours=16),
        'open': closes, 'high': [x + .1 for x in closes], 'low': [x - .1 for x in closes], 'close': closes,
        'volume': item.volumes, 'symbol': item.instrument_id})
context = {'all_symbols': sorted(data), 'date2symbols': Date2Symbols(dict(tape.universe))} if name == 'cross_sectional' else {'target_symbol': 'target_symbol'}
context['tradegrad_parameters'] = json.loads(parameters)
recorder = Recorder(code_sha256=expected_sha, config=CaptureConfig(mode=mode, instrument_ids=['B'] if mutation == 'scope' else []))
with recording(recorder):
    result = aq.run_backtest(data=data, strategy_source=str(path), context=context,
        initial_cash=tape.initial_cash, commission_rate=0, stamp_tax_rate=0, slippage=0,
        history_depth=252, lot_size=1, timezone=tape.market_timezone, show_progress=False)
def table(frame):
    # Pandas positional index labels are not semantic engine records. Preserve
    # every row value/order while discarding random IDs and stale row labels.
    return json.loads(frame.drop(columns=[c for c in frame.columns if c in ('id','order_id','trade_id','execution_id')]).reset_index(drop=True).to_json(orient='split'))
value = {'records': [row.model_dump(mode='json') for row in recorder.records], 'summary': recorder.summary(),
    'orders': table(result.orders_df), 'fills': table(result.executions_df), 'positions': table(result.positions_df),
    'trades': table(result.trades_df), 'equity': json.loads(result.equity_curve.to_json()),
    'metrics': json.loads(result.metrics_df.to_json())}
print('CONTROLLED_NATIVE_RESULT:' + json.dumps(value, allow_nan=False, sort_keys=True))
"""


def _native(template_id, *, mode="research", mutation="none", parameters=None):
    requested = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    required = os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1"
    local_opt_in = os.environ.get("TRADEGRAD_LOCAL_ENGINE_BRIDGE") == "1"
    if not (requested or required or local_opt_in):
        pytest.skip("Actual controlled-template engine subprocess checks are opt-in")
    if requested:
        engine = Path(requested)
    elif importlib.util.find_spec("akquant") is not None:
        engine = Path(sys.executable)
    elif local_opt_in:
        engine = LOCAL_ENGINE
    else:
        pytest.fail(
            "Required native tests need AKQuant or TRADEGRAD_NATIVE_TEST_PYTHON"
        )
    assert engine.is_file(), "The explicitly selected pinned native engine is absent"
    filename = (
        "research_cross.py" if template_id == "cross_sectional" else "research_time.py"
    )
    source = ROOT / "seed_strategies" / filename
    expected = CONTROLLED_SOURCE_SHA256[template_id]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, (
        "Frozen source changed; review hand-worked expectations before updating the fixture pin"
    )
    completed = subprocess.run(
        [
            str(engine),
            "-B",
            "-c",
            ENGINE_SCRIPT,
            template_id,
            str(source),
            expected,
            mode,
            mutation,
            json.dumps(parameters or {}),
        ],
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-8192:].decode(errors="replace")
    lines = [
        line
        for line in completed.stdout.decode().splitlines()
        if line.startswith("CONTROLLED_NATIVE_RESULT:")
    ]
    assert len(lines) == 1
    return json.loads(lines[0].split(":", 1)[1])


def _day(result, day):
    return next(
        row for row in result["records"] if row["decision_time"].startswith(day)
    )


def _stage(decision, stage):
    return next(row for row in decision["stages"] if row["stage_id"] == stage)


@pytest.mark.parametrize("template_id", available_templates())
def test_actual_native_trace_enabled_disabled_preserves_orders_fills_positions_metrics(
    template_id,
):
    on, off = _native(template_id), _native(template_id, mode="off")
    assert on["summary"]["coverage"] == "complete", on["summary"]
    assert len(on["records"]) == 8 and off["records"] == []
    for section in ("orders", "fills", "positions", "trades", "equity", "metrics"):
        assert on[section] == off[section], section
    assert on["orders"]["data"] and on["fills"]["data"] and on["trades"]["data"]
    order_column = on["orders"]["columns"].index("created_at")
    first_order_time = min(row[order_column] for row in on["orders"]["data"])
    assert (
        first_order_time >= 1704402000000
    )  # No order during the first two warmup sessions.


def test_actual_cross_matches_hand_calculation_and_next_day_fill():
    result = _native("cross_sectional")
    day = _day(result, "2024-01-04")
    candidates = {
        row["instrument_id"]: row for row in _stage(day, "ranking")["candidates"]
    }
    features = {
        (row["instrument_id"], row["feature_id"]): row
        for row in _stage(day, "features")["features"]
    }
    targets = {
        row["instrument_id"]: row
        for row in _stage(day, "allocation")["constrained_plan"]
    }
    for expected in CROSS_FIRST_READY:
        row = candidates[expected.instrument_id]
        assert row["score"] == pytest.approx(expected.score)
        assert row["rank"] == expected.rank and row["selected"] is expected.selected
        assert row["reason"] == expected.reason
        assert features[expected.instrument_id, "average_dollar_volume"][
            "value"
        ] == pytest.approx(expected.average_dollar_volume)
        assert (
            targets[expected.instrument_id]["target_quantity"]
            == expected.target_quantity
        )
    links = _stage(day, "execution")["order_links"]
    assert (
        len(links) == 1
        and links[0]["instrument_id"] == "B"
        and len(links[0]["child_order_ids"]) == 1
    )
    assert links[0]["status"] == "intended" and links[0]["fill_ids"] == []
    assert (
        _stage(_day(result, "2024-01-05"), "ranking")["candidates"][1]["reason"]
        == "holding_retained"
    )
    assert any(
        row["reason"] == "outside_universe"
        for row in _stage(_day(result, "2024-01-09"), "ranking")["candidates"]
    )
    columns = result["orders"]["columns"]
    first = result["orders"]["data"][0]
    assert first[columns.index("updated_at")] > first[columns.index("created_at")]


def test_actual_timing_missed_signal_warmup_and_risk_operands():
    result = _native("daily_timing")
    warmup = _day(result, "2024-01-02")
    assert (
        _stage(warmup, "features")["features"][0]["validity"] == "warmup_insufficient"
    )
    assert _stage(warmup, "signal")["predicates"][0]["state"] == "not_evaluated"
    for date, expected in TIMING_EXPECTED.items():
        day = _day(result, date)
        features = {
            row["feature_id"]: row for row in _stage(day, "features")["features"]
        }
        assert features["fast_mean"]["value"] == expected["fast_mean"]
        assert features["slow_mean"]["value"] == expected["slow_mean"]
        assert (
            _stage(day, "signal")["predicates"][0]["state"] == expected["entry_state"]
        )
        assert _stage(day, "signal")["candidates"][0]["reason"] == expected["reason"]
        assert (
            _stage(day, "allocation")["constrained_plan"][0]["target_quantity"]
            == expected["target_quantity"]
        )
    assert _stage(_day(result, "2024-01-04"), "execution")["order_links"] == []
    risk = next(
        row
        for row in _stage(_day(result, "2024-01-08"), "allocation")["predicates"]
        if row["condition_id"] == "within_max_weight"
    )
    assert (
        risk["state"] == "false"
        and risk["operands"] == [0.8]
        and risk["threshold"] == 0.4
    )


def test_actual_filter_toggle_changes_selection_and_ties_are_deterministic():
    unfiltered = _native("cross_sectional", parameters={"liquidity_enabled": False})
    day = _day(unfiltered, "2024-01-04")
    assert [
        row["instrument_id"]
        for row in _stage(day, "ranking")["candidates"]
        if row["selected"]
    ] == ["C"]
    assert {row["state"] for row in _stage(day, "liquidity")["predicates"]} == {
        "not_evaluated"
    }
    tied = _native("cross_sectional", mutation="tie")
    candidates = {
        row["instrument_id"]: row
        for row in _stage(_day(tied, "2024-01-04"), "ranking")["candidates"]
    }
    assert candidates["A"]["rank"] == 1 and candidates["B"]["rank"] == 2


@pytest.mark.parametrize("template_id", available_templates())
def test_actual_future_data_mutation_cannot_change_earlier_decisions(template_id):
    base, changed = _native(template_id), _native(template_id, mutation="future")

    def prefix(result):
        rows = []
        for row in result["records"][:4]:
            row.pop("decision_id")
            for stage in row["stages"]:
                for link in stage["order_links"]:
                    link["child_order_ids"] = ["generated-order-id"] * len(
                        link["child_order_ids"]
                    )
            rows.append(row)
        return rows

    assert prefix(base) == prefix(changed)


def test_actual_capture_scope_does_not_shrink_calculation_universe():
    base, scoped = (
        _native("cross_sectional"),
        _native("cross_sectional", mutation="scope"),
    )
    for section in ("orders", "fills", "positions", "trades", "equity", "metrics"):
        assert base[section] == scoped[section]
    assert scoped["summary"]["coverage"] == "partial"
    rows = _stage(_day(scoped, "2024-01-04"), "ranking")["candidates"]
    assert len(rows) == 1 and rows[0]["instrument_id"] == "B" and rows[0]["rank"] == 1


def test_actual_cash_reserve_limit_is_not_a_ranking_rejection():
    result = _native("cross_sectional", parameters={"cash_reserve": 0.99})
    day = _day(result, "2024-01-04")
    candidate = next(
        row
        for row in _stage(day, "ranking")["candidates"]
        if row["instrument_id"] == "B"
    )
    assert candidate["selected"] is True and candidate["rank"] == 1
    allocation = _stage(day, "allocation")
    target = next(
        row for row in allocation["constrained_plan"] if row["instrument_id"] == "B"
    )
    assert target["target_quantity"] == CASH_ONLY_EXPECTED["B_target_quantity"]
    assert tuple(target["constraints"]) == CASH_ONLY_EXPECTED["B_constraints"]
    condition = next(
        row
        for row in allocation["predicates"]
        if row["condition_id"] == "within_cash_budget"
    )
    assert (
        condition["state"] == "false"
        and condition["operands"] == [4000.0]
        and condition["threshold"] == 100.0
    )
