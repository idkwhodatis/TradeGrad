"""Deterministic adapter contracts and separately the actual generated SDK."""
from __future__ import annotations

import copy
import importlib.metadata
import json
import os
import secrets
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from packages.marketdata.contracts import BarsRequest
from packages.marketdata.providers.openbb import LOCKED_VERSIONS, OpenBBProvider
from packages.marketdata.providers.openbb_fixture import BARS, DIRECTORY


@pytest.fixture(autouse=True)
def isolated_sdk_home(monkeypatch, tmp_path):
    # No user credentials/settings; all SDK-generated cache files stay writable.
    monkeypatch.setenv("HOME", str(tmp_path))


def client(search=None, historical=None):
    return SimpleNamespace(nasdaq=SimpleNamespace(equity=SimpleNamespace(
        search=search or (lambda **kwargs: envelope(DIRECTORY[:1])),
        historical=historical or (lambda **kwargs: envelope(BARS["AAPL"])),
    )))


def envelope(rows, warnings=None):
    return {"results": copy.deepcopy(rows), "provider": "nasdaq", "warnings": warnings or [], "extra": {}}


def request(listings=None, **kwargs):
    listings = listings or OpenBBProvider(mode="fixture").resolve_instruments(["AAPL"]).data
    return BarsRequest(listings=listings, start="2024-01-02", end="2024-01-06",
                       as_of=datetime(2024, 1, 10, tzinfo=UTC), **kwargs)


def test_fixture_honestly_resolves_imports_and_covers_every_listing():
    provider = OpenBBProvider(mode="fixture")
    resolved = provider.resolve_instruments(["AAPL", "MSFT"])
    assert len(resolved.data) == 2
    assert not resolved.unresolved
    assert resolved.data[0].mic == "XNAS"
    assert resolved.data[0].currency == "USD"
    assert resolved.data[0].timezone == "America/New_York"
    assert "proxy" in resolved.data[0].calendar_note
    assert resolved.data[0].listed_on is None
    assert resolved.data[0].vendor_symbols[0].valid_from is None
    result = provider.fetch_bars(request(resolved.data))
    assert len(result.data) == 8
    assert all(c.status == "complete" for c in result.coverage)
    assert all(p.evidence == "fixture" for p in resolved.provenance + result.provenance)
    assert result.price_semantics.native_mode == "provider_native"
    assert result.price_semantics.split_basis == "unknown"
    assert result.price_semantics.dividend_basis == "unknown"
    assert not result.actions
    assert all(c.status != "verified" for c in provider.capabilities())
    assert all(raw.content["extra"]["evidence"] == "synthetic_fixture_not_live" for raw in result.raw)


def test_live_mode_never_silently_uses_static_symbol_lookup(monkeypatch):
    provider = OpenBBProvider()
    def fail(*args, **kwargs):
        raise RuntimeError("upstream is unavailable")
    monkeypatch.setattr(provider, "_call", fail)
    resolved = provider.resolve_instruments(["AAPL"])
    assert resolved.data == [] and resolved.unresolved == ["AAPL"]
    assert any(warning.code == "provider_error" for warning in resolved.warnings)


def test_search_escapes_regex_and_independently_exact_matches():
    calls = []
    def search(**kwargs):
        calls.append(kwargs)
        return envelope([{**DIRECTORY[0], "symbol": "BRK.B", "nasdaq_symbol": "BRK.B"}])
    resolved = OpenBBProvider(mode="fixture", client=client(search=search)).resolve_instruments(["BRK.B"])
    assert resolved.data[0].symbol == "BRK.B"
    assert calls == [{"query": r"^BRK\.B$", "is_symbol": True, "is_etf": False, "provider": "nasdaq"}]
    bad = OpenBBProvider(mode="fixture", client=client()).resolve_instruments(["AA", "AAPL,MSFT", "../AAPL"])
    assert bad.data == [] and len(bad.unresolved) == 3


@pytest.mark.parametrize("field,value", [("Listing Exchange", "?"), ("etf", "Y"), ("test_issue", "Y"),
                                         ("nasdaq_traded", "N"), ("name", "Acme Corp. Warrants")])
def test_unmapped_exchange_and_other_asset_types_are_not_guessed(field, value):
    row = {**DIRECTORY[0], field: value}
    resolved = OpenBBProvider(mode="fixture", client=client(search=lambda **_: envelope([row]))).resolve_instruments(["AAPL"])
    assert not resolved.data and resolved.unresolved == ["AAPL"]


def test_repeated_directory_matches_are_ambiguous_not_first_wins():
    resolved = OpenBBProvider(mode="fixture", client=client(search=lambda **_: envelope([DIRECTORY[0], DIRECTORY[0]]))).resolve_instruments(["AAPL"])
    assert not resolved.data
    assert resolved.warnings[0].code == "unresolved_symbol"


def test_end_exclusive_raw_preserved_warnings_and_upstream_dates():
    calls = []
    rows = BARS["AAPL"] + [{**BARS["AAPL"][-1], "date": "2024-01-06"}]
    warning = {"category": "UserWarning", "message": "Some requested symbol was unavailable"}
    def historical(**kwargs):
        calls.append(kwargs)
        return envelope(rows, [warning])
    result = OpenBBProvider(mode="fixture", client=client(historical=historical)).fetch_bars(request())
    assert calls == [{"symbol": "AAPL", "start_date": "2024-01-02", "end_date": "2024-01-05", "provider": "nasdaq"}]
    assert len(result.data) == 4 and len(result.raw[0].content["results"]) == 5
    assert result.raw[0].content["warnings"] == [warning]
    assert any(w.code == "upstream_warning" for w in result.warnings)
    assert result.provenance[0].endpoint == "obb.nasdaq.equity.historical"
    assert result.provenance[0].requested_params["boundary"] == "[start,end)"
    assert result.provenance[0].effective_params["end_date"] == "2024-01-05"


def test_missing_symbol_is_partial_even_if_sdk_returns_success():
    provider = OpenBBProvider(mode="fixture")
    resolved = provider.resolve_instruments(["AAPL", "MSFT"])
    def historical(**kwargs):
        return envelope(BARS["AAPL"] if kwargs["symbol"] == "AAPL" else [],
                        [{"category": "UserWarning", "message": "No historical prices were returned for MSFT"}])
    result = OpenBBProvider(mode="fixture", client=client(historical=historical)).fetch_bars(request(resolved.data))
    statuses = {c.listing_id: c for c in result.coverage}
    assert statuses[resolved.data[0].listing_id].status == "complete"
    assert statuses[resolved.data[1].listing_id].status == "partial"
    assert len(statuses[resolved.data[1].listing_id].missing_sessions) == 4
    assert len(result.data) == 4


@pytest.mark.parametrize("mutation", [{"volume": None}, {"close": float("nan")}, {"symbol": "MSFT"}, {"date": "garbage"}])
def test_bad_rows_cannot_publish_as_complete(mutation):
    rows = [{**BARS["AAPL"][0], **mutation}] + BARS["AAPL"][1:]
    result = OpenBBProvider(mode="fixture", client=client(historical=lambda **_: envelope(rows))).fetch_bars(request())
    assert result.coverage[0].status == "failed"
    assert result.raw[0].content["results"][0] == rows[0]


def test_serial_bounded_date_blocks_and_call_budget():
    calls = []
    def historical(**kwargs):
        calls.append(kwargs)
        return envelope([row for row in BARS["AAPL"] if kwargs["start_date"] <= row["date"] <= kwargs["end_date"]])
    provider = OpenBBProvider(mode="fixture", client=client(historical=historical), block_days=2)
    assert provider.fetch_bars(request()).coverage[0].status == "complete"
    assert [(c["start_date"], c["end_date"]) for c in calls] == [("2024-01-02", "2024-01-03"), ("2024-01-04", "2024-01-05")]
    with pytest.raises(ValueError, match="call budget"):
        OpenBBProvider(mode="fixture", block_days=2, max_calls=1).fetch_bars(request())
    with pytest.raises(ValueError, match="provider_native"):
        provider.fetch_bars(request(price_mode="unadjusted"))


def test_timeout_retries_are_bounded_but_schema_errors_do_not_retry(monkeypatch):
    from packages.marketdata.providers import common
    calls, sleeps = [], []
    def timeout(*args, **kwargs):
        calls.append(kwargs)
        raise common.ProviderError("upstream_timeout", retryable=True)
    monkeypatch.setattr(common, "run_sdk_process", timeout)
    provider = OpenBBProvider(max_attempts=2, sleep=sleeps.append)
    resolved = provider.resolve_instruments(["AAPL"])
    assert len(calls) == 2 and sleeps == [1]
    assert resolved.warnings[0].code == "upstream_timeout"
    calls.clear()
    def bad_response(*args, **kwargs):
        calls.append(kwargs)
        return {"results": [], "provider": "yfinance"}
    monkeypatch.setattr(common, "run_sdk_process", bad_response)
    assert provider.resolve_instruments(["AAPL"]).warnings[0].code == "source_mismatch"
    assert len(calls) == 1


def _real_sdk():
    for package, expected in LOCKED_VERSIONS.items():
        assert importlib.metadata.version(package) == expected
    from openbb import obb
    assert callable(obb.nasdaq.equity.search)
    assert callable(obb.nasdaq.equity.historical)
    return obb


def test_real_sdk_search_schema_alias_bug_and_generated_namespace(monkeypatch):
    obb = _real_sdk()
    from openbb_nasdaq.utils import helpers
    directory = ("Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares\n"
                 "Y|AAPL|Apple Inc. - Common Stock|Q|Q|N|100|N|N||AAPL|N\n"
                 "Y|MSFT|Microsoft Corporation - Common Stock|Q|Q|N|100|N|N|MSFT|MSFT|N\n"
                 "File Creation Time: 0106202417:03|||||||||||\n")
    monkeypatch.setattr(helpers, "get_nasdaq_directory", lambda *args, **kwargs: directory)
    output = obb.nasdaq.equity.search(query="^AAPL$", provider="nasdaq", is_etf=False)
    assert output.results[0].exchange is None
    assert output.model_dump(mode="json")["results"][0]["Listing Exchange"] == "Q"
    result = OpenBBProvider(mode="fixture", client=obb).resolve_instruments(["AAPL"])
    assert result.data[0].mic == "XNAS"
    assert result.raw[0].content["provider"] == "nasdaq"


def test_real_sdk_historical_schema_partial_batch_warning_and_conversion(monkeypatch):
    obb = _real_sdk()
    from openbb_nasdaq.utils import helpers
    async def response(path, **kwargs):
        if path.startswith("quote/MSFT/"):
            raise RuntimeError("Synthetic transient upstream refusal")
        assert "assetclass=stocks" in path and "fromdate=2024-01-02" in path
        return {"tradesTable": {"rows": [
            {"date": "01/02/2024", "open": "$100.00", "high": "$102.00", "low": "$99.00", "close": "$101.00", "volume": "1,000"},
        ]}}
    monkeypatch.setattr(helpers, "get_nasdaq_data", response)
    output = obb.nasdaq.equity.historical(symbol=["AAPL", "MSFT"], start_date="2024-01-02", end_date="2024-01-05", provider="nasdaq")
    assert len(output.results) == 1
    assert output.results[0].symbol == "AAPL" and output.results[0].volume == 1000
    assert any("MSFT" in warning.message for warning in output.warnings)
    assert output.results[0].date == date(2024, 1, 2)
    result = OpenBBProvider(mode="fixture", client=obb).fetch_bars(request())
    assert result.coverage[0].status == "partial"
    assert len(result.coverage[0].missing_sessions) == 3
    assert result.price_semantics.split_basis == "unknown"


@pytest.mark.skipif(os.environ.get("TRADEGRAD_OPENBB_LIVE") != "1", reason="Optional public-network live probe, not deterministic CI")
def test_optional_live_openbb_probe():
    _real_sdk()
    provider = OpenBBProvider(max_attempts=1)
    resolved = provider.resolve_instruments(["AAPL"])
    resolved_count, unresolved_count = len(resolved.data), len(resolved.unresolved)
    assert unresolved_count == 0 and resolved_count == 1, {"unresolved_count": unresolved_count, "resolved_count": resolved_count}
    result = provider.fetch_bars(request(resolved.data))
    statuses = [c.status for c in result.coverage[:10]]
    coverage_complete = bool(result.coverage) and all(c.status == "complete" for c in result.coverage)
    assert coverage_complete, {"statuses": statuses, "coverage_count": len(result.coverage), "bar_count": len(result.data)}
    live_evidence = all(p.evidence == "live" for p in result.provenance)
    assert live_evidence


def test_generated_fixture_uses_long_fixed_real_calendar_window_not_invented_requested_sessions():
    provider = OpenBBProvider(mode="fixture")
    listing = provider.resolve_instruments(["AAPL"]).data
    result = provider.fetch_bars(BarsRequest(listings=listing, start="2024-01-02", end="2024-07-01"))
    assert len(result.data) >= 84
    assert result.coverage[0].status == "complete"
    assert date(2024, 6, 19) not in result.coverage[0].observed_sessions
    assert all(p.evidence == "fixture" for p in result.provenance)
    outside = provider.fetch_bars(BarsRequest(listings=listing, start="2024-06-28", end="2024-07-03"))
    assert outside.coverage[0].status == "partial"
    assert date(2024, 7, 1) in outside.coverage[0].missing_sessions


def test_every_live_retry_reserves_shared_upstream_budget(monkeypatch):
    from packages.marketdata.providers import common
    from packages.marketdata.providers.common import budget_scope
    reservations = []
    def failure(*args, **kwargs):
        return {"error": {"code": "rate_limited", "message": "Rate limited", "retryable": True}}
    monkeypatch.setattr(common, "run_sdk_process", failure)
    with budget_scope(reservations.append):
        result = OpenBBProvider(max_attempts=2, sleep=lambda _: None).resolve_instruments(["AAPL"])
    assert reservations == ["nasdaq:equity.search", "nasdaq:equity.search"]
    assert result.unresolved == ["AAPL"]


def test_fake_injected_client_cannot_claim_live_evidence():
    with pytest.raises(ValueError, match="fixture mode"):
        OpenBBProvider(mode="live", client=client())


def test_upstream_error_and_warning_content_is_not_reflected_into_public_warnings(monkeypatch):
    marker = secrets.token_hex(32)
    secret_text = f"https://example.invalid?api_key={marker} /home/private/token Authorization: Bearer {marker}"
    def failure(**kwargs):
        raise RuntimeError(secret_text)
    provider = OpenBBProvider(mode="fixture", client=client(search=failure))
    resolved = provider.resolve_instruments(["AAPL"])
    assert marker not in json.dumps([w.model_dump() for w in resolved.warnings])
    warning = {"message": secret_text, "category": marker}
    provider = OpenBBProvider(mode="fixture", client=client(historical=lambda **_: envelope(BARS["AAPL"], [warning])))
    result = provider.fetch_bars(request())
    assert marker not in json.dumps([w.model_dump() for w in result.warnings])
    assert result.raw[0].content["warnings"] == [warning]


def test_actual_openbb_process_boundary_rejects_invalid_command_before_any_network():
    from packages.marketdata.providers.common import run_sdk_process
    output = run_sdk_process(module="packages.marketdata.providers.openbb_sdk",
                             payload={"command": "forbidden", "params": {}}, timeout=10)
    assert output["error"]["code"] == "ValueError"
    assert output["error"]["retryable"] is False


def test_fetched_at_marks_response_completion_but_failures_keep_attempt_time(monkeypatch):
    attempted = datetime(2024, 1, 10, 10, 0, tzinfo=UTC)
    completed = datetime(2024, 1, 10, 10, 1, tzinfo=UTC)
    monkeypatch.setattr("packages.marketdata.normalize.utc_now", lambda: attempted)
    monkeypatch.setattr("packages.marketdata.providers.openbb.utc_now", lambda: completed)
    provider = OpenBBProvider(mode="fixture")
    resolved = provider.resolve_instruments(["AAPL"])
    bars = provider.fetch_bars(request(resolved.data))
    for result in (resolved, bars):
        assert result.provenance[0].fetched_at == completed
        assert result.raw[0].provenance_id == result.provenance[0].provenance_id

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic upstream failure")

    monkeypatch.setattr(provider, "_call", fail)
    for result in (provider.resolve_instruments(["AAPL"]), provider.fetch_bars(request(resolved.data))):
        assert result.provenance[0].fetched_at == attempted
        assert not result.raw
