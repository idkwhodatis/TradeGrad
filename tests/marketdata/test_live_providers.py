"""Explicit opt-in network acceptance, never conflated with deterministic tests.

Run on an authorized network runner with TRADEGRAD_MARKETDATA_LIVE=1.
Failure is recorded as failure: do not substitute fixtures or another provider.
"""

import os
from datetime import date

import pytest

from packages.marketdata.contracts import BarsRequest
from packages.marketdata.providers.registry import get_provider


@pytest.mark.skipif(
    os.environ.get("TRADEGRAD_MARKETDATA_LIVE") != "1",
    reason="Live upstream probes require explicit network-runner opt-in",
)
@pytest.mark.parametrize(
    "name,symbols,modes",
    [
        ("akshare", ["600000", "000001"], ["raw", "qfq", "hfq"]),
        ("yfinance", ["AAPL", "MSFT"], ["provider_native"]),
    ],
)
def test_live_resolve_fetch_and_snapshot(name, symbols, modes, tmp_path):
    from packages.marketdata.storage import verify_bundle, write_bundle

    provider = get_provider(name, mode="live")
    resolved = provider.resolve_instruments(symbols)
    resolved_count, unresolved_count = len(resolved.data), len(resolved.unresolved)
    assert unresolved_count == 0, {"unresolved_count": unresolved_count}
    assert resolved_count == len(symbols), {"resolved_count": resolved_count}
    for mode in modes:
        request = BarsRequest(
            listings=resolved.data,
            start=date(2024, 1, 2),
            end=date(2024, 1, 6),
            price_mode=mode,
        )
        result = provider.fetch_bars(request)
        live_evidence = all(p.evidence == "live" for p in result.provenance)
        statuses = [c.status for c in result.coverage[:10]]
        coverage_count = len(result.coverage)
        coverage_complete = all(c.status == "complete" for c in result.coverage)
        assert live_evidence
        assert coverage_count == len(symbols), {"coverage_count": coverage_count}
        assert coverage_complete, {
            "statuses": statuses,
            "coverage_count": coverage_count,
            "bar_count": len(result.data),
        }
        directory = tmp_path / mode
        manifest = write_bundle(directory, request, result)
        ready = bool(manifest["ready"])
        assert ready
        verify_bundle(directory, manifest)
