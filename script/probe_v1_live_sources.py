"""Hosted, bounded SDK diagnostics. No provider fallback or acceptance promotion.

Only fixed public requests from the failed live gate are repeated. Normal SDK
stdout/stderr, payloads, URLs, headers and exception messages are never exported.
Each child has private SDK HOME and an allowlisted environment; existing pidfd
ownership contains all its descendants before another case can start.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

CASES = {
    "akshare_resolution": ("akshare", "stock_info_a_code_name", ["600000", "000001"]),
    "yahoo_aapl_resolution": ("yfinance", "Search+Ticker.history_metadata", ["AAPL"]),
    "yahoo_msft_resolution": ("yfinance", "Search+Ticker.history_metadata", ["MSFT"]),
    "openbb_aapl_history": ("openbb", "obb.nasdaq.equity.historical", ["AAPL"]),
}
EXCEPTION_CLASSES = {
    "TypeError",
    "ValueError",
    "KeyError",
    "AttributeError",
    "ImportError",
    "ModuleNotFoundError",
    "JSONDecodeError",
    "ValidationError",
    "HTTPError",
    "ClientResponseError",
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "TimeoutError",
    "SSLError",
    "ClientConnectorError",
    "ClientConnectorDNSError",
    "YFRateLimitError",
    "YFDataException",
    "YFPricesMissingError",
    "EmptyDataError",
    "OpenBBError",
    "ProxyError",
    "RemoteDisconnected",
}
PACKAGES = ("akshare", "yfinance", "openbb-core", "openbb-nasdaq")
CASE_SECONDS = 60
TOTAL_SECONDS = 300
CLEANUP_RESERVE_SECONDS = 20
ROOT = Path(__file__).resolve().parents[1]


def exception_projection(error):
    """Types/statuses only; no arbitrary string from an exception is retained."""
    chain, seen = [], set()
    while isinstance(error, BaseException) and id(error) not in seen and len(chain) < 4:
        seen.add(id(error))
        name = type(error).__name__
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if type(status) is not int:
            status = getattr(error, "status", None)
        chain.append(
            {
                "exception_class": name if name in EXCEPTION_CLASSES else "other",
                "http_status": status
                if type(status) is int and 100 <= status <= 599
                else None,
            }
        )
        error = error.__cause__ or error.__context__
    return {"status": "exception", "exception_chain": chain}


def success_projection(case, value):
    """Only counts and fixed semantic predicates leave the SDK process."""
    if case == "akshare_resolution":
        table = value["table"]
        column = table["columns"].index("code")
        symbols = {str(row[column]).zfill(6) for row in table["data"]}
        count = sum(symbol in symbols for symbol in CASES[case][2])
        return {
            "status": "returned",
            "row_count": min(len(table["data"]), 100000),
            "requested_symbols_found": count,
            "expected_symbols": 2,
        }
    if case.startswith("yahoo_"):
        symbol = CASES[case][2][0]
        meta = value["metadata"]
        flags = {
            "exact_quote": any(
                row.get("symbol", "").upper() == symbol for row in value["quotes"]
            ),
            "supported_exchange": meta.get("exchangeName")
            in {"NMS", "NGM", "NCM", "NAS", "NYQ", "NYE", "ASE", "AMEX"},
            "usd": meta.get("currency") == "USD",
            "equity": meta.get("instrumentType") == "EQUITY",
            "new_york_timezone": meta.get("exchangeTimezoneName") == "America/New_York",
        }
        return {"status": "returned", "resolution_predicates": flags}
    if case == "openbb_aapl_history":
        return {"status": "returned", "row_count": min(len(value.results), 100000)}
    raise ValueError("Unsupported fixed case")


def execute_fixed_case(case):
    if case == "openbb_aapl_history":
        from openbb import obb
        from openbb_core.provider.utils.helpers import get_python_request_settings

        if get_python_request_settings().get("verify_ssl") is False:
            raise ValueError("Required TLS verification absent")
        value = obb.nasdaq.equity.historical(
            symbol="AAPL",
            start_date="2024-01-02",
            end_date="2024-01-05",
            provider="nasdaq",
        )
    else:
        from packages.marketdata.providers.sdk_worker import execute

        value = execute(
            "akshare_resolve" if case == "akshare_resolution" else "yfinance_resolve",
            {}
            if case == "akshare_resolution"
            else {"symbol": CASES[case][2][0], "timeout": 20},
        )
    return success_projection(case, value)


def worker(case):
    if os.getenv("TRADEGRAD_DIAGNOSTIC_CHILD") != "1":
        raise ValueError("Owned diagnostic child required")
    with (
        open(os.devnull, "w") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        try:
            result = execute_fixed_case(case)
        except Exception as error:  # noqa: BLE001 - project, never reflect diagnostics
            result = exception_projection(error)
    print(json.dumps(result, allow_nan=False))
    return 0


def validate_projection(case, value):
    """Recheck the tiny child envelope before a public artifact is created."""
    if not isinstance(value, dict):
        raise TypeError("Invalid diagnostic envelope")
    if value.get("status") == "exception":
        if (
            set(value) != {"status", "exception_chain"}
            or not isinstance(value["exception_chain"], list)
            or not 1 <= len(value["exception_chain"]) <= 4
        ):
            raise ValueError("Invalid exception projection")
        for row in value["exception_chain"]:
            if not isinstance(row, dict) or set(row) != {
                "exception_class",
                "http_status",
            }:
                raise ValueError("Invalid exception fields")
            if row["exception_class"] not in EXCEPTION_CLASSES | {"other"}:
                raise ValueError("Unrecognized exception class")
            status = row["http_status"]
            if status is not None and not (
                type(status) is int and 100 <= status <= 599
            ):
                raise ValueError("Invalid HTTP status")
        return value
    if value.get("status") != "returned":
        raise ValueError("Invalid diagnostic status")
    if case.startswith("yahoo_"):
        if set(value) != {"status", "resolution_predicates"}:
            raise ValueError("Invalid Yahoo fields")
        flags = value["resolution_predicates"]
        if (
            not isinstance(flags, dict)
            or set(flags)
            != {
                "exact_quote",
                "supported_exchange",
                "usd",
                "equity",
                "new_york_timezone",
            }
            or any(type(flag) is not bool for flag in flags.values())
        ):
            raise ValueError("Invalid Yahoo predicates")
    else:
        keys = {"status", "row_count"}
        if case == "akshare_resolution":
            keys |= {"requested_symbols_found", "expected_symbols"}
        if (
            set(value) != keys
            or type(value["row_count"]) is not int
            or not 0 <= value["row_count"] <= 100000
        ):
            raise ValueError("Invalid count projection")
        if case == "akshare_resolution" and (
            type(value["requested_symbols_found"]) is not int
            or not 0 <= value["requested_symbols_found"] <= 2
            or value["expected_symbols"] != 2
        ):
            raise ValueError("Invalid fixed symbol count")
    return value


def succeeded(case, result):
    if result["status"] != "returned":
        return False
    if case.startswith("yahoo_"):
        return all(result["resolution_predicates"].values())
    if case == "akshare_resolution":
        return result["requested_symbols_found"] == 2
    return result["row_count"] > 0


def run(report):
    from packages.marketdata.providers.common import sdk_environment
    from script.ci_process_owner import ProcessOwner

    deadline = time.monotonic() + TOTAL_SECONDS - CLEANUP_RESERVE_SECONDS
    outcomes = []
    owner = ProcessOwner(deadline)
    owner.contain()
    with tempfile.TemporaryDirectory(prefix="tradegrad-live-diagnostic-") as directory:
        for case, (provider, endpoint, symbols) in CASES.items():
            if time.monotonic() >= deadline or owner.interrupted:
                break
            home = Path(directory) / case
            home.mkdir(mode=0o700)
            env = sdk_environment(home)
            env["TRADEGRAD_DIAGNOSTIC_CHILD"] = "1"
            result = {"status": "diagnostic_failed"}
            try:
                raw = owner.run(
                    [
                        sys.executable,
                        "-m",
                        "script.probe_v1_live_sources",
                        "--worker-case",
                        case,
                    ],
                    cwd=ROOT,
                    env=env,
                    seconds=CASE_SECONDS,
                    maximum=16384,
                )
                result = validate_projection(case, json.loads(raw))
            except TimeoutError:
                result = {"status": "owned_deadline_exceeded"}
            except Exception:  # noqa: BLE001 - never print command/environment/SDK output
                result = {"status": "diagnostic_failed"}
            finally:
                clean = owner.cleanup()
            outcomes.append(
                {
                    "case": case,
                    "provider": provider,
                    "endpoint": endpoint,
                    "symbols": symbols,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "result": result,
                    "owned_cleanup_confirmed": clean,
                }
            )
            if not clean or owner.interrupted:
                break
    versions = {}
    for package in PACKAGES:
        try:
            value = version(package)
        except PackageNotFoundError:
            value = "unavailable"
        versions[package] = (
            value if re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", value) else "unavailable"
        )
    document = {
        "schema_version": "tradegrad.live-sdk-diagnostic.v1",
        "scope": "fixed SDK root-cause diagnostics, not full adapter/browser acceptance",
        "provider_fallback": False,
        "snapshot_or_seed_acceptance": False,
        "bounds": {
            "cases": 4,
            "seconds_per_case": CASE_SECONDS,
            "total_seconds": TOTAL_SECONDS,
            "cleanup_reserve_seconds": CLEANUP_RESERVE_SECONDS,
            "automatic_retries": 0,
        },
        "history_window": {
            "applies_only_to_case": "openbb_aapl_history",
            "start_inclusive": "2024-01-02",
            "end_exclusive": "2024-01-06",
        },
        "package_versions": versions,
        "unattempted_cases": [
            case for case in CASES if case not in {item["case"] for item in outcomes}
        ],
        "outcomes": outcomes,
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return (
        0
        if len(outcomes) == 4
        and all(
            item["owned_cleanup_confirmed"] and succeeded(item["case"], item["result"])
            for item in outcomes
        )
        else 1
    )


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--worker-case", choices=tuple(CASES))
    args = parser.parse_args()
    if args.worker_case:
        return worker(args.worker_case)
    if (
        not args.report
        or os.getenv("GITHUB_ACTIONS") != "true"
        or os.getenv("TRADEGRAD_MARKETDATA_LIVE") != "1"
        or os.getenv("TRADEGRAD_OPENBB_LIVE") != "1"
    ):
        raise ValueError("Explicit hosted live diagnostic opt-in required")
    return run(args.report)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - no raw exception or SDK diagnostics in logs
        print("Bounded live SDK diagnostic failed; no raw details exported")
        raise SystemExit(1) from None
