"""Candidate-side entry point. This image contains no API/DB/model configuration."""

import json
import base64
from datetime import date, timedelta
from pathlib import Path


def main():
    request_path = Path("/input/request.json")
    if request_path.stat().st_size > 65536:
        raise ValueError("Oversized evaluation request")
    request = json.loads(request_path.read_text())
    task = request["task"]
    if task not in ("cn_time", "cn_cross", "us_time", "us_cross"):
        raise ValueError("Unsupported research task")
    market = task.split("_")[0].upper()
    if request.get("market", market) != market:
        raise ValueError("Task and market do not match")
    profile = request.get("profile", "corrected_explicit_config")
    if profile not in ("source_compat", "corrected_explicit_config"):
        raise ValueError("Unsupported engine profile")
    start = date.fromisoformat(request["start_date"])
    end = date.fromisoformat(request["end_date"])
    if end <= start:
        raise ValueError("Date window must be nonempty and half-open")
    common = dict(
        strategy_path="/input/strategy.py",
        result_dir="/output",
        market=market,
        start_time=start.isoformat(),
        end_time=(end - timedelta(days=1)).isoformat(),
        profile=profile,
    )
    if task.endswith("_time"):
        from src.evaluate_time import run_backtest

        run_backtest(
            **common, data_path="/input/data/INDEX.csv", symbol="target_symbol"
        )
    else:
        from src.evaluate import run_backtest

        run_backtest(
            **common, data_dir="/input/data", date2symbol_dir="/input/universe"
        )
    result = Path("/output/metrics.json")
    if result.is_symlink() or not result.is_file() or result.stat().st_size > 1_048_576:
        raise ValueError("Invalid result file")
    # A bounded wire record avoids exposing a writable host filesystem to code.
    # Native results remain explicitly untrusted; M5 adds a separate trusted ledger.
    print(
        "TRADEGRAD_RESULT_V1:" + base64.b64encode(result.read_bytes()).decode("ascii"),
        flush=True,
    )


if __name__ == "__main__":
    main()
