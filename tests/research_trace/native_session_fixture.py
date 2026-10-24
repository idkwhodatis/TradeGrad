"""Real maintained-source native daily-label probe, not a substitute engine.

Only invoked through the opt-in, pinned native interpreter. Prices remain the
literal I0 tape; shifting its dates tests session mapping, not market calendars.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path


def run_case(name, market, days):
    import akquant as aq

    from packages.research_trace.contracts import CaptureConfig
    from packages.research_trace.recorder import Recorder, recording
    from packages.research_trace.samples import (
        CONTROLLED_SOURCE_SHA256,
        CROSS_SECTION_TAPE,
        TIMING_TAPE,
    )
    from packages.tradegrad_adapter.config import DateWindow
    from packages.tradegrad_adapter.windows import stage_csv_window
    from src import evaluate, evaluate_time
    from src.market import backtest_kwargs

    assert version("akquant") == "0.3.27"
    root = Path(__file__).resolve().parents[2]
    cross = name == "cross_sectional"
    source = (
        root
        / "seed_strategies"
        / ("research_cross.py" if cross else "research_time.py")
    )
    expected = CONTROLLED_SOURCE_SHA256[name]
    assert source.is_file() and not source.is_symlink()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
    tape = CROSS_SECTION_TAPE if cross else TIMING_TAPE
    zone = "Asia/Shanghai" if market == "CN" else "America/New_York"
    start, end = days[2], (date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat()
    window = DateWindow(start=start, end=end)
    overrides = {
        "initial_cash": 10000,
        "commission_rate": 0,
        "stamp_tax_rate": 0,
        "slippage": {"type": "percent", "value": 0},
    }

    def table(frame):
        # Random engine IDs and pandas index labels are not economic values.
        ignored = {"id", "order_id", "trade_id", "execution_id"}
        return json.loads(
            frame.drop(columns=[c for c in frame.columns if c in ignored])
            .reset_index(drop=True)
            .to_json(orient="split")
        )

    def once(directory, profile, mode, *, mutate=False, warmup=2, direct=False):
        directory.mkdir()
        data_dir, universe = directory / "data", directory / "universe"
        data_dir.mkdir()
        universe.mkdir()
        staged = {}
        for instrument in tape.instruments:
            symbol = instrument.instrument_id
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            for index, day in enumerate(days):
                close = instrument.closes[index]
                if mutate and index == len(days) - 1:
                    close *= 3
                writer.writerow(
                    [
                        day,
                        close,
                        close + 0.1,
                        close - 0.1,
                        close,
                        instrument.volumes[index],
                    ]
                )
            # An extreme end-exclusive source bar must never reach the engine.
            writer.writerow([end, 999, 1000, 998, 999, 1000])
            raw = stream.getvalue().encode()
            content = stage_csv_window(
                raw,
                listing_id=symbol,
                window=window,
                warmup_sessions=warmup,
                history_policy="require" if warmup else "source_cold_start",
                expected_sessions=days,
            ).content
            (data_dir / f"{symbol}.csv").write_bytes(content)
            staged[symbol] = content.decode()
        initial_day = (date.fromisoformat(days[0]) - timedelta(days=10)).isoformat()
        (universe / "initial_list_2024-01.csv").write_text(
            "updateDate,code,code_name\n"
            + "".join(
                f"{initial_day},{item.instrument_id},Frozen\n"
                for item in tape.instruments
            )
        )
        if cross:
            (universe / "later_list_2024-02.csv").write_text(
                "updateDate,code,code_name\n"
                + "".join(
                    f"{days[5]},{item.instrument_id},Frozen\n"
                    for item in tape.instruments
                    if item.instrument_id != "B"
                )
            )
        recorder = Recorder(code_sha256=expected, config=CaptureConfig(mode=mode))
        kwargs = backtest_kwargs(market, profile)
        kwargs.update(overrides)
        with recording(recorder):
            if direct:
                # Exact pre-fix reference: old raw loader + unchanged engine.
                # No adapter localization, source patching or fake engine.
                data = (
                    evaluate.load_data(str(data_dir))
                    if cross
                    else evaluate_time.load_data(
                        str(data_dir / "target_symbol.csv"), "target_symbol"
                    )
                )
                context = (
                    {
                        "all_symbols": sorted(data),
                        "date2symbols": evaluate.Date2Symbols.from_date2symbol_dir(
                            universe
                        ),
                    }
                    if cross
                    else {"target_symbol": "target_symbol"}
                )
                result = aq.run_backtest(
                    data=data,
                    strategy_source=str(source),
                    context=context,
                    start_time=start,
                    end_time=days[-1],
                    **kwargs,
                )
            else:
                evaluator = evaluate if cross else evaluate_time
                inputs = (
                    {"data_dir": str(data_dir), "date2symbol_dir": str(universe)}
                    if cross
                    else {
                        "data_path": str(data_dir / "target_symbol.csv"),
                        "symbol": "target_symbol",
                    }
                )
                result = evaluator.run_backtest(
                    strategy_path=str(source),
                    result_dir=str(directory / "output"),
                    start_time=start,
                    end_time=days[-1],
                    market=market,
                    profile=profile,
                    engine_overrides=overrides,
                    independent_observation=True,
                    **inputs,
                )
        after = {
            symbol: (data_dir / f"{symbol}.csv").read_bytes().decode()
            for symbol in staged
        }
        assert after == staged
        return {
            "records": [record.model_dump(mode="json") for record in recorder.records],
            "summary": recorder.summary(),
            "staged": staged,
            "economics": {
                "orders": table(result.orders_df),
                "fills": table(result.executions_df),
                "positions": table(result.positions_df),
                "trades": table(result.trades_df),
                "equity": json.loads(result.equity_curve.to_json()),
                "metrics": json.loads(result.metrics_df.to_json()),
            },
            "equity_times": [value.isoformat() for value in result.equity_curve.index],
            "zone": result.resolved_config["timezone"],
        }

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        os.environ["AKQUANT_DATA_CACHE"] = str(directory / "cache")
        result = {}
        for label, profile, mode, extra in (
            ("corrected", "corrected_explicit_config", "research", {}),
            ("off", "corrected_explicit_config", "off", {}),
            ("future", "corrected_explicit_config", "research", {"mutate": True}),
            ("cold", "corrected_explicit_config", "research", {"warmup": 0}),
            ("legacy", "source_compat", "research", {}),
            ("legacy_reference", "source_compat", "research", {"direct": True}),
        ):
            result[label] = once(directory / label, profile, mode, **extra)
        result["zone"] = zone
        return result


if __name__ == "__main__":
    import sys

    print(
        "SESSION_RESULT:"
        + json.dumps(run_case(*json.loads(sys.argv[1])), allow_nan=False)
    )
