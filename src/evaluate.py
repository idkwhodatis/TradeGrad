import argparse
import bisect
import datetime
import json
import math
import os
import re
from pathlib import Path

import akquant as aq
import pandas as pd

try:
    from .data_cache import load_data, localize_market_dates
    from .market import MARKETS, backtest_kwargs
except ImportError:  # run as a script by the loss layer
    from data_cache import load_data, localize_market_dates
    from market import MARKETS, backtest_kwargs


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy_path", type=str, help="Path to the trading strategy source code.")
    parser.add_argument("--result_dir", type=str, help="Directory to save strategy performance results.")
    parser.add_argument("--date2symbol_dir", type=str, help="Path to date2symbol mapping file.")
    parser.add_argument("--data_dir", type=str, help="Path to market data directory.")
    parser.add_argument("--start_time", type=str, help="Start time for backtesting.")
    parser.add_argument("--end_time", type=str, help="End time for backtesting.")
    parser.add_argument("--market", required=True, choices=sorted(MARKETS))
    parser.add_argument("--profile", choices=("source_compat", "corrected_explicit_config"), default="corrected_explicit_config")
    parser.add_argument("--freq_floor", type=float, default=None)
    return parser.parse_args()



class Date2Symbols(dict[int, set[str]]):
    """
    dict: {update_date -> set(symbols)}
    - key uses int form yyyymmdd (e.g. 20151130)
    - query: d[any_date] returns the constituent set as of the most recent rebalance <= any_date
    """

    def __init__(self, *args, **kwargs):
        super().__init__()
        self._sorted_keys: list[int] | None = None
        if args or kwargs:
            self.update(*args, **kwargs)

    @staticmethod
    def _norm_date(value) -> int:
        """Normalize input into int yyyymmdd.

        Query supports: str, datetime.date.
        Also tolerates: datetime.datetime, pandas.Timestamp, int.
        """
        if isinstance(value, int):
            # Prefer treating as yyyymmdd if it looks like a date.
            if 19000101 <= value <= 21000101:
                return value

            # Otherwise treat as epoch timestamp (ns/ms/s heuristic)
            abs_v = abs(value)
            if abs_v >= 10**14:
                ts = pd.to_datetime(value, unit="ns", utc=True)
            elif abs_v >= 10**11:
                ts = pd.to_datetime(value, unit="ms", utc=True)
            else:
                ts = pd.to_datetime(value, unit="s", utc=True)
            if getattr(ts, "tz", None) is not None:
                ts = ts.tz_convert(None)
            d = ts.normalize().date()
            return d.year * 10000 + d.month * 100 + d.day

        if isinstance(value, datetime.datetime):
            d = value.date()
            return d.year * 10000 + d.month * 100 + d.day
        if isinstance(value, datetime.date):
            return value.year * 10000 + value.month * 100 + value.day

        ts = pd.Timestamp(value)
        if getattr(ts, "tz", None) is not None:
            ts = ts.tz_convert(None)
        d = ts.normalize().date()
        return d.year * 10000 + d.month * 100 + d.day

    def _ensure_sorted_keys(self) -> list[int]:
        if self._sorted_keys is None:
            self._sorted_keys = sorted(self.keys())
        return self._sorted_keys

    def effective_date(self, query_date) -> int:
        q = self._norm_date(query_date)
        keys = self._ensure_sorted_keys()
        if not keys:
            raise KeyError("Date2Symbols is empty")

        i = bisect.bisect_right(keys, q) - 1

        return None if i < 0 else keys[i]

    def as_of(self, query_date) -> set[str]:
        k = self.effective_date(query_date)
        return dict.__getitem__(self, k)

    def __missing__(self, key):
        return self.as_of(key)

    def __setitem__(self, key, value) -> None:
        k = self._norm_date(key)
        dict.__setitem__(self, k, set(value))
        self._sorted_keys = None

    def update(self, *args, **kwargs) -> None:
        other = dict(*args, **kwargs)
        for k, v in other.items():
            self[k] = v
        self._sorted_keys = None

    @classmethod
    def from_date2symbol_dir(cls, date2symbol_dir: str | Path) -> "Date2Symbols":
        """
        Reads data/<index>_list/*.csv into {updateDate -> set(code)}. Expected columns:
        updateDate, code, code_name.
        """
        date2symbol_dir = Path(date2symbol_dir)
        paths = sorted(date2symbol_dir.glob("*_list_*.csv"))
        merged = {}

        for path in paths:
            df = pd.read_csv(path, dtype={"code": str})

            # Which column carries the snapshot date differs by market. The HS300 files stamp every
            # row with that file's rebalance date, so `updateDate` is the snapshot. The S&P files
            # stamp each row with the date that company joined the index -- grouping on it would
            # produce hundreds of "snapshots" holding one or two tickers each. There the snapshot
            # is the month in the file name, and the whole file is the membership on that date.
            stamped = pd.to_datetime(df["updateDate"], errors="coerce").dt.normalize()
            codes = df["code"].astype(str).sort_values()
            if stamped.nunique(dropna=True) > 1:
                m = re.search(r"_(\d{4})-(\d{2})", path.name)
                if m is None:
                    raise ValueError(f"cannot read a snapshot date from {path.name}")
                snapshot = pd.Timestamp(int(m.group(1)), int(m.group(2)), 1).normalize()
                stamped = pd.Series(snapshot, index=df.index)

            for d, group_codes in codes.groupby(stamped):
                if pd.isna(d):
                    continue
                dd_ts = pd.Timestamp(d)
                if getattr(dd_ts, "tz", None) is not None:
                    dd_ts = dd_ts.tz_convert(None)
                dd = dd_ts.normalize().date()
                key = dd.year * 10000 + dd.month * 100 + dd.day
                group_codes = sorted(set(group_codes.tolist()))
                merged.setdefault(key, set()).update(group_codes)

        for k, v in merged.items():
            merged[k] = list(v)

        return cls(merged)


# Trade-frequency floor for the cross-sectional task: 0.25x the initial strategy's own trade rate
# (see market.py's freq_floor). Loose enough to leave room for genuinely lower-turnover designs,
# tight enough to kill the reward-hacking route of trading less to flatten drawdown.
FREQ_SHARPNESS = float(os.environ.get("AKQUANT_FREQ_SHARPNESS", 1.0))
# Weight on the AR term vs MDD in reward_function's geometric mean; 0.5 = equal weight, raising it trades less emphasis on drawdown for more on return.
AR_WEIGHT = float(os.environ.get("AKQUANT_AR_WEIGHT", 0.5))


def reward_function(
    metrics: dict,
    ar_weight: float = AR_WEIGHT,
    ar_reference: float = 20,
    mdd_reference: float = 15,
    *,
    market: str,
    freq_floor: float | None = None,
) -> float:
    ar_score = 1.0 / (
        1.0 + math.exp(-0.11 * (metrics['annualized_return'] - ar_reference))
    )
    mdd_score = 1.0 / (
        1.0 + math.exp(0.22 * (metrics['max_drawdown'] - mdd_reference))
    )
    base = ar_score ** ar_weight * mdd_score ** (1 - ar_weight)

    gate = 1.0
    floor = MARKETS[market]["freq_floor"]["cross"] if freq_floor is None else freq_floor
    if floor > 0:
        gate = min(1.0, metrics['trade_per_day'] / floor) ** FREQ_SHARPNESS

    return 100 * base * gate


def run_backtest(
    strategy_path: str,
    result_dir: str,
    date2symbol_dir: str,
    data_dir: str,
    start_time: str,
    end_time: str,
    market: str,
    freq_floor: float | None = None,
    profile: str = "corrected_explicit_config",
    *,
    engine_overrides: dict | None = None,
    result_callback=None,
    strategy_contract: dict | None = None,
    runtime_observer=None,
    rng_restore=None,
    rng_callback=None,
    independent_observation: bool = False,
):
    date2symbols = Date2Symbols.from_date2symbol_dir(date2symbol_dir)
    all_symbols = set()
    for symbols in date2symbols.values():
        all_symbols.update(symbols)
    all_symbols = list(sorted(all_symbols))

    data_map = load_data(data_dir, symbols=all_symbols)
    data_map = {symbol: data_map[symbol] for symbol in all_symbols if symbol in data_map}

    engine_kwargs = backtest_kwargs(market, profile)
    if engine_overrides is not None:
        from packages.tradegrad_adapter.results import validate_engine_overrides

        engine_kwargs.update(validate_engine_overrides(engine_overrides))

    if profile == "corrected_explicit_config":
        data_map = localize_market_dates(data_map, engine_kwargs["timezone"])

    context = {"all_symbols": all_symbols, "date2symbols": date2symbols}
    if strategy_contract is not None:
        from packages.tradegrad_adapter.native_contract import native_context

        context = native_context(context, strategy_contract)

    observer_kwargs = {}
    if runtime_observer is not None:
        observer_kwargs = {"on_event": runtime_observer, "stream_mode": "audit"}
    # This seam is after trusted data/context preparation and immediately before
    # AKQuant loads candidate source. Ordinary capture never reseeds either RNG.
    if rng_callback is not None or rng_restore is not None:
        from packages.tradegrad_adapter.runtime_observation import (
            capture_rng_state,
            restore_rng_state,
        )

        if rng_restore is not None:
            restore_rng_state(rng_restore, include_numpy=True)
        rng_state = capture_rng_state(include_numpy=True)
        if rng_callback is not None:
            rng_callback(rng_state)
    result = aq.run_backtest(
        data=data_map,
        strategy_source=strategy_path,
        context=context,
        start_time=start_time,
        end_time=end_time,
        **engine_kwargs,
        **observer_kwargs,
    )

    if independent_observation:
        from packages.tradegrad_adapter.independent_results import (
            observation_performance,
        )

        performance = observation_performance(result, market=market, profile=profile)
        # Snapshot the genuine successful engine result before legacy reward and
        # positive-closed-trade admission. Engine exceptions still propagate.
        if result_callback is not None:
            result_callback(result, performance, engine_kwargs)
        os.makedirs(result_dir, exist_ok=True)
        with open(os.path.join(result_dir, "metrics.json"), "w") as stream:
            json.dump(performance, stream, indent=4, allow_nan=False)
        return result

    metrics_df = result.metrics_df

    metrics = {
        'sharpe_ratio':  metrics_df.loc['sharpe_ratio', 'value'],
        'annualized_return': metrics_df.loc['annualized_return', 'value'] * 100,
        'max_drawdown': metrics_df.loc['max_drawdown_pct', 'value'],
        'trade_per_day': metrics_df.loc['closed_trade_count', 'value'] / (metrics_df.loc["duration", "value"].days / 365 * 243),
        'win_rate': metrics_df.loc['win_rate', 'value'],
        'return_per_trade': metrics_df.loc['avg_return_pct', 'value'],
    }

    if metrics['trade_per_day'] == 0:
        raise ValueError("No trades executed, invalid strategy.")

    for k, v in metrics.items():
        if isinstance(v, (int, float)):
            metrics[k] = round(v, 2)

    performance = {
        "combined_score": round(reward_function(metrics, market=market, freq_floor=freq_floor), 2),
        "metrics": metrics,
        "market": market,
        "profile": profile,
    }

    os.makedirs(result_dir, exist_ok=True)
    with open(os.path.join(result_dir, "metrics.json"), 'w') as f:
        json.dump(performance, f, indent=4, allow_nan=False)

    # Snapshot while the real native object is alive. This is explicitly opt-in:
    # original callers retain byte-identical metrics.json and reward semantics.
    if result_callback is not None:
        result_callback(result, performance, engine_kwargs)
    return result


if __name__ == "__main__":
    args = parse_args()
    run_backtest(**vars(args))
