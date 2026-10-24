"""Candidate-side entry point. This image contains no API/DB/model configuration."""

import base64
import json
from datetime import date, timedelta
from pathlib import Path


def _evaluation_config(value, start, end):
    from packages.tradegrad_adapter.results import validate_engine_overrides

    keys = {"engine_overrides", "window", "warmup", "benchmark", "run_config_sha256"}
    if (
        not isinstance(value, dict)
        or set(value) - {"strategy_contract", "research_capture"} != keys
    ):
        raise ValueError("Invalid evaluation_config schema")
    window = value["window"]
    if not isinstance(window, dict) or set(window) != {"start", "end", "role"}:
        raise ValueError("Invalid evaluation window")
    if (
        window["start"] != start.isoformat()
        or window["end"] != end.isoformat()
        or window["role"] not in ("train", "validation", "holdout", "seed")
    ):
        raise ValueError("Evaluation window does not match request")
    warmup = value["warmup"]
    if not isinstance(warmup, dict) or set(warmup) != {"sessions", "history_policy"}:
        raise ValueError("Invalid warmup config")
    if (
        type(warmup["sessions"]) is not int
        or not 0 <= warmup["sessions"] <= 100000
        or warmup["history_policy"] not in ("require", "source_cold_start")
    ):
        raise ValueError("Invalid warmup config")
    if warmup["history_policy"] == "source_cold_start" and warmup["sessions"] != 0:
        raise ValueError("Source cold-start cannot claim staged warmup")
    if value["benchmark"] is not None:
        raise ValueError(
            "Configured benchmarks are not yet supported; no proxy is substituted"
        )
    digest = value["run_config_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("Invalid immutable RunConfig hash")
    if "strategy_contract" in value:
        from packages.tradegrad_adapter.native_contract import NativeStrategyContract

        NativeStrategyContract.model_validate(value["strategy_contract"])
    if "research_capture" in value:
        from packages.research_trace.contracts import CaptureConfig

        CaptureConfig.model_validate(value["research_capture"])
    validate_engine_overrides(value["engine_overrides"])
    return value


def main(input_dir=Path("/input"), output_dir=Path("/output")):
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    request_path = input_dir / "request.json"
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
    common = {
        "strategy_path": str(input_dir / "strategy.py"),
        "result_dir": str(output_dir),
        "market": market,
        "start_time": start.isoformat(),
        "end_time": (end - timedelta(days=1)).isoformat(),
        "profile": profile,
    }
    captured = {}
    recorder = trace_writer = native_capture = None
    if "evaluation_config" in request:
        from packages.tradegrad_adapter.results import (
            artifact_inventory,
            canonical_json_bytes,
            export_native_result,
            sha256_bytes,
        )

        config = _evaluation_config(request["evaluation_config"], start, end)
        from packages.research_trace.contracts import CaptureConfig, canonical_bytes
        from packages.research_trace.native import NativeCapture
        from packages.research_trace.recorder import Recorder
        from packages.research_trace.transport import TRACE_PREFIX, CaptureWriter

        capture_config = CaptureConfig.model_validate(
            config.get("research_capture", {})
        )
        trace_writer = CaptureWriter(
            capture_config,
            lambda value: print(
                TRACE_PREFIX + canonical_bytes(value).decode("utf-8"), flush=True
            ),
        )
        recorder = Recorder(
            code_sha256=sha256_bytes((input_dir / "strategy.py").read_bytes()),
            config=capture_config,
        )
        native_capture = NativeCapture(recorder, trace_writer.append)
        recorder._on_record = native_capture.append
        inputs = artifact_inventory(input_dir)
        identity = {
            "strategy_sha256": sha256_bytes((input_dir / "strategy.py").read_bytes()),
            "run_config_sha256": config["run_config_sha256"],
            "evaluation_config_sha256": sha256_bytes(canonical_json_bytes(config)),
            "input_manifest_sha256": sha256_bytes(canonical_json_bytes(inputs)),
            "inputs": inputs,
            "task": task,
            "market": market,
            "profile": profile,
        }
        common["engine_overrides"] = config["engine_overrides"]
        if "strategy_contract" in config:
            common["strategy_contract"] = config["strategy_contract"]

        def capture(result, performance, engine_kwargs):
            from src.market import MARKETS

            native_capture.finalize(
                result,
                market_timezone="Asia/Shanghai"
                if market == "CN"
                else "America/New_York",
            )
            captured["bundle"] = export_native_result(
                result,
                performance,
                identity=identity,
                window={
                    **config["window"],
                    "interval": "half_open",
                    "warmup": config["warmup"],
                    "engine_start_time": common["start_time"],
                    "engine_end_time": common["end_time"],
                    "engine_boundary_adapter": "inclusive_daily_end_previous_calendar_date.v1",
                },
                engine_kwargs=engine_kwargs,
                currency="CNY" if market == "CN" else "USD",
                reward_parameters={
                    "ar_weight": evaluator.AR_WEIGHT,
                    "ar_reference": 20,
                    "mdd_reference": 15,
                    "freq_floor": MARKETS[market]["freq_floor"][
                        "time" if task.endswith("_time") else "cross"
                    ],
                    "freq_sharpness": evaluator.FREQ_SHARPNESS,
                },
            )

        common["result_callback"] = capture
    from packages.research_trace.recorder import recording

    with recording(recorder):
        if task.endswith("_time"):
            from src import evaluate_time as evaluator

            evaluator.run_backtest(
                **common,
                data_path=str(input_dir / "data/INDEX.csv"),
                symbol="target_symbol",
            )
        else:
            from src import evaluate as evaluator

            evaluator.run_backtest(
                **common,
                data_dir=str(input_dir / "data"),
                date2symbol_dir=str(input_dir / "universe"),
            )
    if trace_writer is not None:
        trace_writer.finish(recorder.summary())
    result = output_dir / "metrics.json"
    if result.is_symlink() or not result.is_file() or result.stat().st_size > 1_048_576:
        raise ValueError("Invalid result file")
    if "evaluation_config" in request:
        from packages.tradegrad_adapter.results import wire_record

        # Exact-one bounded envelope. Oversize fails rather than silently dropping
        # orders, fills or equity. The coordinator validates before persistence.
        print(
            wire_record(json.loads(result.read_bytes()), captured["bundle"]), flush=True
        )
    else:
        # Old requests retain their original metrics-only wire bytes.
        print(
            "TRADEGRAD_RESULT_V1:"
            + base64.b64encode(result.read_bytes()).decode("ascii"),
            flush=True,
        )


if __name__ == "__main__":
    main()
