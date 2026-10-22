"""Opt-in LOCAL fixture-adapter -> immutable bundle -> original AKQuant seeds.

Run from the repository root with both existing locked environments available:

    TRADEGRAD_LOCAL_ENGINE_BRIDGE=1 .venv-data/bin/python -m pytest \
        tests/marketdata/test_engine_bridge.py -q

Only the two SHA-256-pinned upstream seeds may run on the host. This is not a
candidate executor or a Docker fallback. Provider transports are synthetic
SDK-schema fixtures, not live SDK requests. There is no API, PostgreSQL, worker,
browser, optimizer, warm-up-correctness or production-isolation evidence here.
"""

import csv
import hashlib
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from packages.marketdata.contracts import BarsRequest
from packages.marketdata.normalize import canonical_json
from packages.marketdata.providers.registry import get_provider
from packages.marketdata.storage import verify_bundle, write_bundle
from src.job import evaluator_environment, load_performance, run_job

ROOT = Path(__file__).resolve().parents[2]
ENGINE_PYTHON = ROOT / ".venv/bin/python"
SOURCE_COMMIT = "baf73ee56353de8996731139f6721b62f4b26da7"
SEED_HASHES = {
    "cn_cross": "486e13c83d179a1a9ba3e9d79614bdff75b001f34c470ce848b4da59b167a78e",
    "us_time": "ecdcc54ffe62c05db0220d6e3e5a48aab10145b7e34b3e0a5aa84e33f4d149e8",
}
START, END = date(2024, 1, 2), date(2024, 7, 1)

local_engine_bridge = pytest.mark.skipif(
    os.environ.get("TRADEGRAD_LOCAL_ENGINE_BRIDGE") != "1",
    reason="Local trusted-seed bridge is opt-in; not a replacement for Docker gates",
)


@pytest.mark.parametrize("adjust,factor", [("", 1.0), ("qfq", 0.9), ("hfq", 1.1)])
def test_cn_generated_fixture_is_valid_distinct_and_date_block_stable(adjust, factor):
    from packages.marketdata.providers.fixtures import akshare_bars

    full = {}
    for symbol in ("600000", "000001"):
        frame = akshare_bars(symbol, adjust)
        full[symbol] = frame
        assert len(frame) == 117 and not frame["日期"].duplicated().any()
        assert (frame["最低"] > 0).all()
        assert (frame["最低"] <= frame[["开盘", "收盘"]].min(axis=1)).all()
        assert (frame["最高"] >= frame[["开盘", "收盘"]].max(axis=1)).all()
        assert (frame["成交量"] > 0).all()
        for field, offset in [("开盘", 10), ("最高", 12), ("最低", 9), ("收盘", 11)]:
            assert frame[field].iloc[:4].tolist() == [
                (offset + i) * factor for i in range(4)
            ]
        pd.testing.assert_frame_equal(frame, akshare_bars(symbol, adjust))
        blocks = []
        for month in range(1, 7):
            first = date(2024, month, 1)
            last = date(2024, month + 1, 1) - timedelta(days=1)
            blocks.append(
                akshare_bars(symbol, adjust, f"{first:%Y%m%d}", f"{last:%Y%m%d}")
            )
        pd.testing.assert_frame_equal(frame, pd.concat(blocks, ignore_index=True))
        # An overlapping range straddles a block boundary and the CN holiday gap.
        overlap = akshare_bars(symbol, adjust, "20240129", "20240308")
        expected = frame[frame["日期"].between("2024-01-29", "2024-03-08")]
        pd.testing.assert_frame_equal(overlap, expected.reset_index(drop=True))
    assert not full["600000"]["收盘"].equals(full["000001"]["收盘"])


@pytest.fixture(scope="module")
def real_engine():
    assert ENGINE_PYTHON.is_file(), "Install tests/baseline/requirements.lock in .venv"
    # Import the actual library in its own interpreter. Do not mix the engine
    # dependencies into the independently locked data runtime.
    probe = subprocess.run(
        [
            str(ENGINE_PYTHON),
            "-c",
            (
                "import akquant, importlib.metadata as m, json; "
                "print(json.dumps({'version': m.version('akquant'), "
                "'module': akquant.__file__}))"
            ),
        ],
        capture_output=True,
        text=True,
        env=evaluator_environment(),
        timeout=20,
        check=True,
    )
    actual = json.loads(probe.stdout)
    assert actual["version"] == "0.3.27"
    assert (ROOT / ".venv").resolve() in Path(actual["module"]).resolve().parents
    return actual


def trusted_seed(task):
    """Never accept candidate source, a configurable path, or an unpinned seed."""
    assert task in SEED_HASHES
    path = ROOT / "seed_strategies" / f"{task}.py"
    assert not path.is_symlink()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SEED_HASHES[task], (
        f"Refusing host execution: {task} is not the original seed at {SOURCE_COMMIT}"
    )
    return path


@local_engine_bridge
@pytest.mark.parametrize(
    "provider_name,symbols,task,expected_rows,expected_symbols,native_mode",
    [
        (
            "akshare",
            ["600000", "000001"],
            "cn_cross",
            234,
            {"sh.600000", "sz.000001"},
            "raw",
        ),
        (
            "yfinance",
            ["AAPL"],
            "us_time",
            124,
            {"INDEX"},
            "yahoo_split_adjusted_ohlc",
        ),
        ("openbb", ["MSFT"], "us_time", 124, {"INDEX"}, "provider_native"),
    ],
    ids=[
        "akshare-shanghai-shenzhen-cn-cross",
        "yahoo-aapl-us-time",
        "openbb-msft-us-time",
    ],
)
def test_fixture_adapter_bundle_executes_original_seed(
    tmp_path,
    monkeypatch,
    real_engine,
    provider_name,
    symbols,
    task,
    expected_rows,
    expected_symbols,
    native_mode,
):
    # Fixture selection is explicit and independent of ambient deployment mode.
    # Block known live entry points, so an accidental transport regression fails
    # without making a provider request. This is a test guard, not a sandbox.
    from packages.marketdata.providers import akshare, common, yfinance

    def forbid_live(*args, **kwargs):
        pytest.fail("Local bridge must not call a live SDK transport")

    monkeypatch.setattr(akshare, "sdk_call", forbid_live)
    monkeypatch.setattr(yfinance, "sdk_call", forbid_live)
    monkeypatch.setattr(common, "run_sdk_process", forbid_live)
    monkeypatch.setenv("AKQUANT_DATA_CACHE", str(tmp_path / "cache"))
    provider = get_provider(provider_name, mode="fixture")
    resolved = provider.resolve_instruments(symbols)
    assert not resolved.unresolved
    assert {item.symbol for item in resolved.data} == set(symbols)
    if provider_name == "akshare":
        assert {item.mic for item in resolved.data} == {"XSHG", "XSHE"}
    request = BarsRequest(
        listings=resolved.data,
        start=START,
        end=END,
        as_of=datetime(2024, 7, 2, tzinfo=UTC),
        price_mode="raw" if provider_name == "akshare" else "provider_native",
    )
    # Real adapters invoke the production normalizer and exchange-session
    # coverage logic. Do not substitute constructed ProviderResult objects.
    result = provider.fetch_bars(request)
    assert len(result.data) == expected_rows
    assert len(result.coverage) == len(symbols)
    assert all(item.status == "complete" for item in result.coverage)
    assert all(not item.missing_sessions for item in result.coverage)
    assert all(START <= bar.session_date < END for bar in result.data)
    assert all(bar.volume_unit == "shares" for bar in result.data)
    assert not any(warning.severity == "error" for warning in result.warnings)
    assert all(p.evidence == "fixture" for p in resolved.provenance + result.provenance)
    assert result.raw
    assert result.price_semantics.native_mode == native_mode
    assert result.price_semantics.adjustment_as_of is None
    if provider_name == "akshare":
        assert result.data[0].volume == 100_000  # Endpoint lots -> shares.
    elif provider_name == "yfinance":
        assert result.price_semantics.split_basis == "adjusted"
        assert {action.action_type for action in result.actions} == {
            "dividend",
            "split",
        }
    else:
        assert result.price_semantics.split_basis == "unknown"
        assert result.price_semantics.dividend_basis == "unknown"

    bundle = tmp_path / "immutable-bundle"
    manifest = write_bundle(bundle, request, result)
    persisted = json.loads((bundle / "manifest.json").read_text())
    assert persisted == manifest
    verify_bundle(bundle, persisted)
    assert manifest["ready"] is True
    assert manifest["row_count"] == expected_rows
    assert (
        manifest["bars_sha256"]
        == hashlib.sha256(
            canonical_json([bar.model_dump(mode="json") for bar in result.data])
        ).hexdigest()
    )
    assert set(manifest["evaluator_symbol_mapping"].values()) == expected_symbols
    assert manifest["universe"]["state"] == "user_fixed"
    assert manifest["universe"]["point_in_time"] is False
    normalized = pq.read_table(bundle / "normalized.parquet")
    assert normalized.num_rows == expected_rows
    assert normalized.to_pylist() == [bar.model_dump() for bar in result.data]
    assert json.loads((bundle / "preview.json").read_text()) == [
        bar.model_dump(mode="json") for bar in result.data
    ]
    rows_by_symbol = {}
    for symbol in expected_symbols:
        with (bundle / "data" / f"{symbol}.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert all(START.isoformat() <= row["date"] < END.isoformat() for row in rows)
        expected_bars = [
            bar
            for bar in result.data
            if manifest["evaluator_symbol_mapping"][bar.listing_id] == symbol
        ]
        assert len(rows) == len(expected_bars)
        for row, bar in zip(rows, expected_bars, strict=True):
            assert row["date"] == bar.session_date.isoformat()
            for field in ("open", "high", "low", "close", "volume"):
                assert float(row[field]) == getattr(bar, field)
            assert (None if row["amount"] == "" else float(row["amount"])) == bar.amount
        rows_by_symbol[symbol] = len(rows)
    assert sum(rows_by_symbol.values()) == expected_rows
    assert len(set(rows_by_symbol.values())) == 1
    with (bundle / "universe/fixed_list_2024-01.csv").open(newline="") as handle:
        universe = list(csv.DictReader(handle))
    assert {row["code"] for row in universe} == expected_symbols
    assert {row["updateDate"] for row in universe} == {START.isoformat()}
    with pytest.raises(FileExistsError):
        write_bundle(bundle, request, result)

    output = tmp_path / "engine-output"
    seed = trusted_seed(task)
    evaluator = (
        ROOT
        / "src"
        / ("evaluate.py" if task.endswith("_cross") else "evaluate_time.py")
    )
    command = [
        str(ENGINE_PYTHON),
        str(evaluator),
        "--strategy_path",
        str(seed),
        "--result_dir",
        str(output),
        "--market",
        task[:2].upper(),
        "--profile",
        "corrected_explicit_config",
        "--start_time",
        START.isoformat(),
        "--end_time",
        (END - timedelta(days=1)).isoformat(),
    ]
    # The source evaluator is inclusive. Convert the immutable half-open window
    # exactly, with no added warm-up rows and no change to seed history handling.
    if task.endswith("_cross"):
        command += [
            "--data_dir",
            str(bundle / "data"),
            "--date2symbol_dir",
            str(bundle / "universe"),
        ]
    else:
        command += [
            "--data_path",
            str(bundle / "data/INDEX.csv"),
            "--symbol",
            "target_symbol",
        ]
    elapsed, error = run_job(output, command, timeout=60)
    assert not error, error
    job = json.loads((output / "job.json").read_text())
    assert job["status"] == "succeeded"
    assert job["returncode"] == 0 and job["timed_out"] is False
    performance = load_performance(output / "metrics.json")
    assert performance["market"] == task[:2].upper()
    assert performance["profile"] == "corrected_explicit_config"
    assert performance["metrics"]["trade_per_day"] > 0
    assert real_engine["version"] == "0.3.27"
    assert elapsed > 0
    # Execution neither rewrites the pinned seed nor mutates any published bytes.
    assert trusted_seed(task) == seed
    verify_bundle(bundle, persisted)
    assert json.loads((bundle / "manifest.json").read_text()) == persisted
