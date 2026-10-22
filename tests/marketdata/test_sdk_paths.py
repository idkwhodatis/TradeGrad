"""Execute installed SDK parsers against synthetic transport responses, offline."""

import json
import secrets
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from packages.marketdata.providers import common, sdk_worker


def test_installed_akshare_parser_preserves_modes_and_documented_lots(monkeypatch):
    import akshare as ak
    import requests

    seen = []

    def response(url, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            json=lambda: {
                "data": {
                    "klines": [
                        "2024-01-02,10,11,12,9,1000,1000000,3,2,1,0.5",
                    ]
                }
            }
        )

    monkeypatch.setattr(requests, "get", response)
    frames = [
        ak.stock_zh_a_hist(
            symbol="600000",
            start_date="20240102",
            end_date="20240102",
            adjust=mode,
            timeout=2,
        )
        for mode in ["", "qfq", "hfq"]
    ]
    assert all(frame.iloc[0]["成交量"] == 1000 for frame in frames)
    assert [call["params"]["fqt"] for call in seen] == ["0", "1", "2"]
    assert all(call["timeout"] == 2 for call in seen)
    assert frames[0].iloc[0]["日期"].isoformat() == "2024-01-02"


def test_installed_yfinance_history_parser_preserves_adjclose_events(monkeypatch):
    from yfinance.scrapers.history import PriceHistory

    times = [
        int(pd.Timestamp(x, tz="America/New_York").timestamp())
        for x in ["2024-01-02 09:30", "2024-01-03 09:30"]
    ]
    raw = {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": "AAPL",
                        "exchangeTimezoneName": "America/New_York",
                        "instrumentType": "EQUITY",
                        "priceHint": 2,
                        "validRanges": ["1d", "5d", "1mo"],
                    },
                    "timestamp": times,
                    "indicators": {
                        "quote": [
                            {
                                "open": [100, 101],
                                "high": [102, 103],
                                "low": [99, 100],
                                "close": [101, 102],
                                "volume": [1000, 1100],
                            }
                        ],
                        "adjclose": [{"adjclose": [100.5, 101.5]}],
                    },
                    "events": {
                        "dividends": {
                            str(times[1]): {"date": times[1], "amount": 0.25}
                        },
                        "splits": {},
                    },
                }
            ],
        }
    }
    seen = []

    class Data:
        def get(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(text="synthetic fixture", json=lambda: raw)

        cache_get = get

    history = PriceHistory(Data(), "AAPL", "America/New_York")
    frame = history.history(
        start="2024-01-02",
        end="2024-01-04",
        auto_adjust=False,
        back_adjust=False,
        repair=False,
        actions=True,
        timeout=2,
        raise_errors=True,
    )
    assert frame["Close"].tolist() == [101, 102]
    assert frame["Adj Close"].tolist() == [100.5, 101.5]
    assert frame["Dividends"].tolist() == [0, 0.25]
    assert seen[0]["params"]["events"] == "div,splits,capitalGains"


def test_sdk_worker_calls_real_public_ticker_history_interface(monkeypatch):
    import yfinance

    seen = []

    class Ticker:
        def __init__(self, symbol):
            assert symbol == "AAPL"

        def get_history_metadata(self):
            return {
                "symbol": "AAPL",
                "currency": "USD",
                "exchangeTimezoneName": "America/New_York",
            }

        def history(self, **kwargs):
            seen.append(kwargs)
            return pd.DataFrame({"Close": [1.2345678901234567]})

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    result = sdk_worker.execute(
        "yfinance_bars",
        {
            "symbol": "AAPL",
            "auto_adjust": False,
            "back_adjust": False,
            "repair": False,
            "actions": True,
            "start": "2024-01-02",
            "end": "2024-01-03",
        },
    )
    assert seen[0]["auto_adjust"] is False and seen[0]["actions"] is True
    restored = common.restore_frame(result)
    assert restored.iloc[0]["Close"] == 1.2345678901234567
    assert restored.attrs["history_metadata"]["symbol"] == "AAPL"
    assert result["attributes"]["history_metadata"]["currency"] == "USD"


def test_sdk_child_excludes_ambient_secrets_and_uses_private_home(
    monkeypatch, tmp_path
):
    marker = secrets.token_hex(32)
    for key in [
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "YF_API_KEY",
        "OBB_PAT",
        "HOME",
        "PYTHONPATH",
    ]:
        monkeypatch.setenv(key, marker)
    monkeypatch.setenv("SSL_CERT_FILE", "/existing/verified/ca.pem")
    env = common.sdk_environment(tmp_path)
    assert marker not in json.dumps(env)
    assert env["HOME"] == str(tmp_path)
    assert env["SSL_CERT_FILE"] == "/existing/verified/ca.pem"
    assert "verify" not in env


def test_actual_sdk_process_round_trip_error_does_not_echo_input():
    marker = secrets.token_hex(32)
    with pytest.raises(common.ProviderError, match="upstream_schema_changed") as error:
        common.sdk_call("invalid-operation", {"token": marker}, timeout=5)
    assert marker not in str(error.value)


def test_actual_process_timeout_kills_sdk_and_descendant(monkeypatch, tmp_path):
    actual = subprocess.Popen
    child_pid_file = tmp_path / "child.pid"
    parent_pids = []

    def process(command, **kwargs):
        code = (
            "import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(p.pid)); "
            "time.sleep(60)"
        )
        instance = actual([sys.executable, "-c", code], **kwargs)
        parent_pids.append(instance.pid)
        return instance

    monkeypatch.setattr(common.subprocess, "Popen", process)
    with pytest.raises(common.ProviderError, match="upstream_timeout"):
        common.run_sdk_process(
            "packages.marketdata.providers.sdk_worker", {}, timeout=0.4
        )
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    for pid in [parent_pids[0], child_pid]:
        stat = Path(f"/proc/{pid}/stat")
        # The container init may reap a dead orphan asynchronously; zombie isn't running.
        assert not stat.exists() or stat.read_text().split()[2] == "Z"


def test_process_output_growth_is_hard_bounded_before_read(monkeypatch):
    actual = subprocess.Popen

    def process(command, **kwargs):
        output, maximum = command[-3:-1]
        code = (
            "import pathlib,resource; "
            f"resource.setrlimit(resource.RLIMIT_FSIZE,({maximum},{maximum})); "
            f"pathlib.Path({output!r}).write_bytes(b'x'*1000000)"
        )
        return actual([sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr(common.subprocess, "Popen", process)
    with pytest.raises(common.ProviderError, match="upstream_payload_too_large"):
        common.run_sdk_process(
            "packages.marketdata.providers.sdk_worker",
            {},
            timeout=5,
            max_output_bytes=4096,
        )


def test_module_and_limits_cannot_be_supplied_as_arbitrary_code():
    with pytest.raises(ValueError):
        common.run_sdk_process("os", {}, timeout=5)
    with pytest.raises(ValueError):
        common.run_sdk_process(
            "packages.marketdata.providers.sdk_worker", {}, timeout=500
        )


def test_worker_death_before_sdk_initialization_fails_closed(tmp_path):
    import os
    import signal
    import time

    output = tmp_path / "must-not-run.json"
    pidfile = tmp_path / "sdk.pid"
    child = (
        "import runpy,sys,time; time.sleep(0.6); "
        "runpy.run_module('packages.marketdata.providers.process_entry',run_name='__main__')"
    )
    launcher = (
        "import os,pathlib,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r},"
        f"'packages.marketdata.providers.sdk_worker',{str(output)!r},'4096',str(os.getpid())],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    owner = subprocess.Popen([sys.executable, "-c", launcher], start_new_session=True)
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists()
        child_pid = int(pidfile.read_text())
        # Child has started Python but deliberately has not imported process_entry.
        owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{child_pid}/stat")
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("SDK process survived creator death before initialization")
        assert not output.exists(), "Provider module must never run after creator death"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
