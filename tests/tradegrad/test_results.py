"""Actual pinned-engine export tests. No generated candidate or network is executed."""

import copy
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from packages.tradegrad_adapter.results import (
    MAX_WIRE_BYTES,
    artifact_inventory,
    canonical_json_bytes,
    export_native_result,
    make_envelope,
    validate_engine_overrides,
    validate_envelope,
    validate_result_bundle,
)

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests/baseline/fixtures"


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    from src.evaluate_time import run_backtest

    output = tmp_path_factory.mktemp("native-result-bundle")
    seed = ROOT / "seed_strategies/us_time.py"
    observed = {}

    def snapshot(result, performance, kwargs):
        observed["bundle"] = export_native_result(
            result,
            performance,
            identity={
                "strategy_sha256": hashlib.sha256(seed.read_bytes()).hexdigest(),
                "input_manifest_sha256": "a" * 64,
            },
            window={
                "start": "2024-02-02",
                "end": "2024-04-27",
                "interval": "half_open",
                "warmup": {"history_policy": "require", "sessions": 20},
            },
            engine_kwargs=kwargs,
            currency="USD",
            reward_parameters={
                "ar_weight": 0.5,
                "ar_reference": 20,
                "mdd_reference": 15,
                "freq_floor": 0.0187,
                "freq_sharpness": 1.0,
            },
        )

    result = run_backtest(
        str(seed),
        str(output),
        str(FIXTURE / "US/INDEX.csv"),
        "target_symbol",
        "2024-02-02",
        "2024-04-26",
        "US",
        result_callback=snapshot,
    )
    assert result is not None
    del result
    return observed["bundle"], (output / "metrics.json").read_bytes()


def test_actual_result_reconstructs_without_engine_object(exported):
    bundle, legacy = exported
    reconstructed = validate_envelope(make_envelope(json.loads(legacy), bundle))[
        "bundle"
    ]
    assert canonical_json_bytes(reconstructed) == canonical_json_bytes(bundle)
    for section in (
        "equity",
        "drawdown",
        "positions",
        "orders",
        "fills",
        "closed_trades",
    ):
        table = reconstructed[section]
        assert table["rows"], section
        assert len(table["columns"]) == len(table["rows"][0])
    assert reconstructed["engine"]["resolved_execution_policy"] == {
        "price_basis": "open",
        "bar_offset": 1,
        "temporal": "same_cycle",
        "source": "legacy",
    }
    assert reconstructed["capabilities"]["trusted_ledger"] is False
    assert reconstructed["benchmark"]["series"] is None
    assert reconstructed["decision_trace"]["scores"] is None
    assert reconstructed["fees"]["commission_stamp_transfer_breakdown"] is None


def test_legacy_metrics_bytes_and_reward_are_unchanged(exported):
    bundle, original = exported
    legacy = bundle["legacy_performance"]["value"]
    assert original == json.dumps(legacy, indent=4, allow_nan=False).encode()
    precise = {item["name"]: item for item in bundle["metrics"]}
    for name, value in legacy["metrics"].items():
        assert round(precise[name]["value"], 2) == value
    assert precise["annualized_return"]["unit"] == "percent"
    assert precise["win_rate"]["unit"] == "percent"
    assert precise["combined_score"]["value"] == legacy["combined_score"]


def test_actual_warmup_has_no_prestart_orders_or_fills(exported):
    bundle, _ = exported
    start = datetime.fromisoformat("2024-02-02T00:00:00-05:00")
    for name, field in (
        ("orders", "created_at"),
        ("fills", "timestamp"),
        ("equity", "timestamp"),
    ):
        table = bundle[name]
        index = [column["name"] for column in table["columns"]].index(field)
        assert all(datetime.fromisoformat(row[index]) >= start for row in table["rows"])
    assert len(bundle["decision_trace"]["decisions"]) == len(bundle["orders"]["rows"])


def test_drawdown_and_fees_reconstruct_from_persisted_rows(exported):
    bundle, _ = exported
    peak = 0
    for equity, drawdown in zip(bundle["equity"]["rows"], bundle["drawdown"]["rows"]):
        peak = max(peak, equity[1])
        assert equity[0] == drawdown[0]
        assert drawdown[1] == pytest.approx(1 - equity[1] / peak)
    columns = [c["name"] for c in bundle["fills"]["columns"]]
    total = sum(row[columns.index("commission")] for row in bundle["fills"]["rows"])
    assert total == pytest.approx(bundle["fees"]["total_from_fills"])


@pytest.mark.parametrize(
    "section", ["orders", "equity", "window", "metrics", "fees", "benchmark"]
)
def test_section_tampering_is_rejected(exported, section):
    bundle = copy.deepcopy(exported[0])
    bundle[section] = {"tampered": True}
    with pytest.raises((ValueError, KeyError)):
        validate_result_bundle(bundle)


def test_manifest_and_envelope_tampering_rejected(exported):
    bundle, legacy = exported
    value = json.loads(make_envelope(json.loads(legacy), bundle))
    value["bundle_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        validate_envelope(canonical_json_bytes(value))
    value["bundle_sha256"] = hashlib.sha256(canonical_json_bytes(bundle)).hexdigest()
    value["metrics"]["combined_score"] += 1
    with pytest.raises(ValueError, match="disagree"):
        validate_envelope(canonical_json_bytes(value))


def test_oversize_duplicate_and_nonfinite_wire_are_rejected(exported):
    with pytest.raises(ValueError, match="budget"):
        validate_envelope(b" " * (MAX_WIRE_BYTES + 1))
    with pytest.raises(ValueError, match="Duplicate"):
        validate_envelope(b'{"schema_version":1,"schema_version":2}')
    with pytest.raises(ValueError, match="Nonfinite"):
        validate_envelope(b'{"x":NaN}')
    bundle, _ = exported
    bundle = copy.deepcopy(bundle)
    bundle["metrics"][0]["value"] = float("inf")
    with pytest.raises(ValueError, match="nonfinite"):
        validate_result_bundle(bundle)


def test_validator_import_does_not_load_research_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from packages.tradegrad_adapter.results import validate_envelope; assert not {'akquant','pandas','numpy'} & set(sys.modules)",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_exact_input_hashes_and_symlinks(tmp_path):
    (tmp_path / "strategy.py").write_bytes(b"original bytes\n")
    inventory = artifact_inventory(tmp_path)
    assert inventory == [
        {
            "path": "strategy.py",
            "sha256": hashlib.sha256(b"original bytes\n").hexdigest(),
            "bytes": 15,
        }
    ]
    (tmp_path / "escape").symlink_to(tmp_path / "strategy.py")
    with pytest.raises(ValueError, match="Symlink"):
        artifact_inventory(tmp_path)


@pytest.mark.parametrize(
    "override",
    [
        {"fill_policy": "same_close"},
        {"initial_cash": True},
        {"initial_cash": 0},
        {"lot_size": 0},
        {"timezone": "not/a/timezone"},
        {"commission_rate": float("nan")},
        {"slippage": {"type": "magic", "value": 0}},
        {"risk_config": {"account_mode": "cash", "enable_short_sell": True}},
    ],
)
def test_unknown_or_invalid_override_never_silently_ignored(override):
    with pytest.raises(ValueError):
        validate_engine_overrides(override)


def test_cash_override_reaches_actual_engine(tmp_path):
    from src.evaluate_time import run_backtest

    result = run_backtest(
        str(ROOT / "seed_strategies/us_time.py"),
        str(tmp_path),
        str(FIXTURE / "US/INDEX.csv"),
        "target_symbol",
        "2024-02-02",
        "2024-04-26",
        "US",
        engine_overrides={"initial_cash": 2_000_000.0, "commission_rate": 0.0007},
    )
    assert result.initial_cash == 2_000_000.0
    assert result.resolved_config["commission_policy"]["value"] == 0.0007


@pytest.mark.parametrize("task", ["cn_time", "us_time", "cn_cross", "us_cross"])
def test_all_four_original_seed_bundles_fit_full_baseline_window(
    task, tmp_path, monkeypatch
):
    from src import evaluate, evaluate_time

    monkeypatch.setenv("TRADEGRAD_DATA_WORKERS", "1")
    monkeypatch.setenv("AKQUANT_DATA_CACHE", str(tmp_path / "cache"))
    market = task.split("_")[0].upper()
    seed = ROOT / "seed_strategies" / f"{task}.py"
    seen = []

    def capture(result, performance, kwargs):
        bundle = export_native_result(
            result,
            performance,
            identity={"strategy_sha256": hashlib.sha256(seed.read_bytes()).hexdigest()},
            window={
                "start": "2024-01-02",
                "end": "2024-04-27",
                "interval": "half_open",
            },
            engine_kwargs=kwargs,
            currency="CNY" if market == "CN" else "USD",
        )
        wire = make_envelope(performance, bundle)
        print(
            f"RESULT_BUNDLE_SIZE {task} envelope_bytes={len(wire)} bundle_bytes={len(canonical_json_bytes(bundle))}"
        )
        seen.append(validate_envelope(wire))

    common = {
        "strategy_path": str(seed),
        "result_dir": str(tmp_path / "result"),
        "start_time": "2024-01-02",
        "end_time": "2024-04-26",
        "market": market,
        "result_callback": capture,
    }
    if task.endswith("time"):
        evaluate_time.run_backtest(
            **common,
            data_path=str(FIXTURE / market / "INDEX.csv"),
            symbol="target_symbol",
        )
    else:
        evaluate.run_backtest(
            **common,
            date2symbol_dir=str(FIXTURE / f"{market}_list"),
            data_dir=str(FIXTURE / market),
        )
    assert len(seen) == 1
    assert seen[0]["bundle"]["fills"]["rows"]


def _rehash(bundle):
    # Model an untrusted producer that can update its own hashes. Shape and
    # reconstruction consistency must be checked independently of integrity.
    for item in bundle["artifacts"]:
        data = canonical_json_bytes(bundle[item["section"]])
        item["sha256"] = hashlib.sha256(data).hexdigest()
        item["bytes"] = len(data)
    return bundle


@pytest.mark.parametrize(
    "malformation",
    [
        "fees_missing",
        "curves_empty",
        "future_equity",
        "misaligned_cash",
        "incorrect_drawdown",
        "fake_fee_total",
        "trace_bad_row",
        "trace_wrong_order",
        "wrong_units",
        "fill_missing_columns",
        "fake_execution_count",
        "metric_wrong_unit",
        "unobserved_capability",
    ],
)
def test_rehashed_incomplete_or_inconsistent_bundle_is_rejected(exported, malformation):
    bundle = copy.deepcopy(exported[0])
    if malformation == "fees_missing":
        bundle["fees"] = {}
    elif malformation == "curves_empty":
        for name in ("equity", "drawdown", "cash", "margin"):
            bundle[name]["rows"] = []
    elif malformation == "future_equity":
        bundle["equity"]["rows"][0][0] = "2099-02-04T11:00:00-05:00"
    elif malformation == "misaligned_cash":
        bundle["cash"]["rows"][0][0] = "2024-02-05T11:00:00-05:00"
    elif malformation == "incorrect_drawdown":
        bundle["drawdown"]["rows"][0][1] = 0.5
    elif malformation == "fake_fee_total":
        bundle["fees"]["total_from_fills"] += 1
    elif malformation == "trace_bad_row":
        bundle["decision_trace"]["decisions"][0]["orders_row"] = 999
    elif malformation == "trace_wrong_order":
        bundle["decision_trace"]["decisions"][0]["order_id"] = "unrelated-order"
    elif malformation == "wrong_units":
        bundle["fills"]["columns"][0]["unit"] = "percent"
    elif malformation == "fill_missing_columns":
        bundle["fills"] = {"columns": [], "rows": [[]]}
    elif malformation == "fake_execution_count":
        next(
            item
            for item in bundle["native_metrics"]
            if item["name"] == "execution_count"
        )["value"] += 1
    elif malformation == "metric_wrong_unit":
        next(item for item in bundle["metrics"] if item["name"] == "annualized_return")[
            "unit"
        ] = "fraction"
    elif malformation == "unobserved_capability":
        bundle["capabilities"]["benchmark"] = True
    with pytest.raises(ValueError):
        validate_result_bundle(_rehash(bundle))


def test_actual_source_profile_keeps_implicit_defaults_and_visible_difference(
    exported, tmp_path
):
    from src.evaluate_time import run_backtest

    seed = ROOT / "seed_strategies/us_time.py"
    seen = []

    def capture(result, performance, kwargs):
        seen.append(
            export_native_result(
                result,
                performance,
                identity={
                    "strategy_sha256": hashlib.sha256(seed.read_bytes()).hexdigest()
                },
                window={
                    "start": "2024-02-02",
                    "end": "2024-04-27",
                    "interval": "half_open",
                },
                engine_kwargs=kwargs,
                currency="USD",
            )
        )

    run_backtest(
        str(seed),
        str(tmp_path),
        str(FIXTURE / "US/INDEX.csv"),
        "target_symbol",
        "2024-02-02",
        "2024-04-26",
        "US",
        profile="source_compat",
        result_callback=capture,
    )
    source, corrected = seen[0], exported[0]
    assert "timezone" not in source["engine"]["requested_kwargs"]
    assert "lot_size" not in source["engine"]["requested_kwargs"]
    assert source["engine"]["resolved_config"]["timezone"] == "Asia/Shanghai"
    assert corrected["engine"]["resolved_config"]["timezone"] == "America/New_York"
    assert source["legacy_performance"]["value"]["profile"] == "source_compat"
    assert (
        source["legacy_performance"]["value"]["metrics"]
        != corrected["legacy_performance"]["value"]["metrics"]
    )
    validate_envelope(make_envelope(source["legacy_performance"]["value"], source))
    print(
        "SOURCE_CORRECTED_COMPARISON "
        + json.dumps(
            {
                profile: bundle["legacy_performance"]["value"]
                for profile, bundle in (
                    ("source_compat", source),
                    ("corrected_explicit_config", corrected),
                )
            },
            sort_keys=True,
        )
    )
