"""OpenBB V5's Nasdaq connector, independent of AKShare and yfinance.

Only the provider namespace is called. A subprocess deadline bounds SDK transport
and warning handling; singleton requests avoid Nasdaq's unbounded batch fan-out.
No SDK credentials, TLS settings or global preferences are modified.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from packages.marketdata.contracts import (
    Bar,
    BarsRequest,
    Capability,
    Coverage,
    PriceSemantics,
    ProviderResult,
    RawPayload,
    ResolutionResult,
    WarningRecord,
    make_listing,
    utc_now,
)
from packages.marketdata.normalize import finish_result, provenance

PACKAGES = ("openbb-core", "openbb-nasdaq")
LOCKED_VERSIONS = {"openbb-core": "2.0.1", "openbb-nasdaq": "2.0.0"}
SEARCH_ENDPOINT = "obb.nasdaq.equity.search"
HISTORICAL_ENDPOINT = "obb.nasdaq.equity.historical"
DOCS = "https://docs.openbb.co/odp/python/reference/nasdaq/equity/"
# The calendar proxy is explicit. The directory supplies the exchange, not a
# currency, timezone, calendar, IPO date or historical ticker-validity interval.
EXCHANGES = {
    "Q": ("XNAS", "XNYS"), "N": ("XNYS", "XNYS"), "A": ("XASE", "XNYS"),
    "P": ("ARCX", "XNYS"), "Z": ("BATS", "XNYS"), "V": ("IEXG", "XNYS"),
}
SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,19}\Z")
PRICE_SEMANTICS = PriceSemantics(
    native_mode="provider_native", split_basis="unknown", dividend_basis="unknown",
    note="Nasdaq does not establish the complete split/dividend basis for this endpoint. "
         "No extra adjustment is applied; this is not proof of unadjusted trade prices.",
)


class OpenBBCallError(RuntimeError):
    def __init__(self, message: str, *, code: str = "provider_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _error_code(exc: Exception) -> str:
    # Messages may contain URLs, local paths or user-configured credentials.
    # Only an explicit small adapter code allowlist crosses into API warnings.
    allowed = {"provider_error", "schema_error", "source_mismatch", "sdk_process_error",
               "timeout", "unresolved_symbol", "unsupported_listing", "symbol_mismatch",
               "rate_limited", "upstream_access_denied", "upstream_timeout",
               "upstream_payload_too_large", "sdk_process_failed"}
    code = getattr(exc, "code", "provider_error")
    return code if isinstance(code, str) and code in allowed else "provider_error"


def _versions() -> dict[str, str]:
    result = {}
    for package in PACKAGES:
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = "not-installed"
    return result


def _envelope(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise OpenBBCallError("OpenBB returned an invalid OBBject results envelope", code="schema_error")
    if value.get("provider") != "nasdaq":
        raise OpenBBCallError("OpenBB returned an unexpected upstream provider", code="source_mismatch")
    if not all(isinstance(row, dict) for row in value["results"]):
        raise OpenBBCallError("OpenBB results must contain records", code="schema_error")
    return value


def _warnings(envelope: dict, listing_id: str | None = None) -> list[WarningRecord]:
    return [WarningRecord(code="upstream_warning",
                          message="Nasdaq reported an upstream warning; details are retained in the raw SDK payload.",
                          listing_id=listing_id)
            for w in envelope.get("warnings") or [] if isinstance(w, dict)]


class OpenBBProvider:
    """Formal scope: current Nasdaq directory US equity listings, daily history.

    Fixture mode is synthetic SDK-shaped evidence, not a live security lookup.
    An injected client must expose the actual nasdaq.equity search/historical
    namespace; it is mainly for deterministic real-SDK contract tests.
    """
    def __init__(self, mode: str = "live", client: Any = None, *,
                 timeout_seconds: float = 45, max_attempts: int = 2,
                 block_days: int = 366, max_calls: int = 100,
                 sleep: Any = time.sleep):
        if mode not in {"live", "fixture"}:
            raise ValueError("mode must be live or fixture")
        if client is not None and mode != "fixture":
            raise ValueError("Injected clients require fixture mode; they cannot establish live evidence")
        if not 0 < timeout_seconds <= 120 or not 1 <= max_attempts <= 3:
            raise ValueError("invalid timeout or retry budget")
        if not 1 <= block_days <= 366 or not 1 <= max_calls <= 1000:
            raise ValueError("invalid date block or call budget")
        self.mode, self.client = mode, client
        self.timeout_seconds, self.max_attempts = timeout_seconds, max_attempts
        self.block_days, self.max_calls, self.sleep = block_days, max_calls, sleep

    def capabilities(self) -> list[Capability]:
        common = {"connector": "openbb", "provider": "nasdaq", "package_versions": _versions()}
        # No static fixture or one successful HTTP call upgrades the complete
        # connector. Live probe evidence is recorded separately with its scope.
        return [
            Capability(feature="resolve_instruments", status="documented", **common,
                       endpoint=SEARCH_ENDPOINT, evidence="documentation", source_url=DOCS + "search",
                       note="Current directory, not historical membership/PIT. Fixture mode is not live lookup."),
            Capability(feature="fetch_bars", status="documented", **common,
                       endpoint=HISTORICAL_ENDPOINT, evidence="documentation", source_url=DOCS + "historical",
                       note="US equity daily history; per-listing coverage checked. No complete live gate yet."),
            Capability(feature="fetch_actions", status="unsupported", **common,
                       endpoint="", note="No reconciled split/dividend action contract in this adapter."),
            Capability(feature="strict_corporate_actions", status="unsupported", **common,
                       endpoint=HISTORICAL_ENDPOINT, note="Price split/dividend basis is unknown."),
            Capability(feature="fetch_universe", status="unsupported", **common,
                       endpoint="", note="Current search directory is not historical/PIT membership."),
        ]

    def _call(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.client is not None:
            return _envelope(getattr(self.client.nasdaq.equity, command)(**params))
        if self.mode == "fixture":
            from packages.marketdata.providers.openbb_fixture import fixture_call
            return _envelope(fixture_call(command, params))
        from packages.marketdata.providers.common import (
            ProviderError,
            reserve_budget,
            run_sdk_process,
        )
        for attempt in range(self.max_attempts):
            try:
                reserve_budget("nasdaq:equity." + command)
                response = run_sdk_process(
                    module="packages.marketdata.providers.openbb_sdk",
                    payload={"command": command, "params": params},
                    timeout=self.timeout_seconds,
                )
                if "error" in response:
                    error = response["error"]
                    raise OpenBBCallError("OpenBB SDK request failed",
                                          code=error.get("code", "provider_error"),
                                          retryable=bool(error.get("retryable", False)))
                return _envelope(response)
            except (OpenBBCallError, ProviderError) as exc:
                if not exc.retryable or attempt + 1 == self.max_attempts:
                    raise
            self.sleep(min(2 ** attempt, 4))
        raise AssertionError("unreachable")

    def _provenance(self, endpoint: str, requested: dict, effective: dict):
        p = provenance(connector="openbb", provider="nasdaq", endpoint=endpoint,
                       requested_params=requested, effective_params=effective,
                       evidence="fixture" if self.mode == "fixture" else "live",
                       package_names=PACKAGES)
        return p.model_copy(update={"source_url": DOCS + endpoint.rsplit(".", 1)[-1],
                                   "note": "Synthetic deterministic SDK-shaped fixture, not live data."
                                   if self.mode == "fixture" else
                                   "Uncleaned OBBject preserved; HTTP cache/wire bytes are not exposed by the SDK."})

    def resolve_instruments(self, symbols: list[str]) -> ResolutionResult:
        if not 1 <= len(symbols) <= min(100, self.max_calls):
            raise ValueError("resolve requires 1..100 symbols within the request budget")
        listings, origins, raw, warnings, unresolved = [], [], [], [], []
        seen = set()
        for original in symbols:
            symbol = original.strip().upper()
            if not SYMBOL_RE.fullmatch(symbol):
                unresolved.append(original)
                warnings.append(WarningRecord(code="invalid_symbol", message="Unsupported equity symbol format"))
                continue
            # SDK ignores is_symbol and treats query as a regex; escape and
            # anchor, then independently exact-match returned aliases.
            params = {"query": "^" + re.escape(symbol) + "$", "is_symbol": True,
                      "is_etf": False, "provider": "nasdaq"}
            origin = self._provenance(SEARCH_ENDPOINT, {"symbol": original}, params)
            origins.append(origin)
            try:
                envelope = self._call("search", params)
                origin = origin.model_copy(update={"fetched_at": utc_now()})
                origins[-1] = origin
                raw.append(RawPayload(provenance_id=origin.provenance_id, content=envelope))
                warnings.extend(_warnings(envelope))
                matches = [row for row in envelope["results"] if symbol in {
                    str(row.get(key) or "").upper() for key in ("symbol", "cqs_symbol", "nasdaq_symbol")
                }]
                if len(matches) != 1:
                    raise OpenBBCallError("Symbol lookup returned no unique exact listing", code="unresolved_symbol")
                row = matches[0]
                # 2.0.0 aliases lowercase 'listing Exchange', but the actual
                # directory header is 'Listing Exchange', retained as an extra.
                exchange = row.get("exchange") or row.get("Listing Exchange") or row.get("listing Exchange")
                canonical = str(row.get("symbol") or "").upper()
                if exchange not in EXCHANGES or not SYMBOL_RE.fullmatch(canonical):
                    raise OpenBBCallError("Unmapped Nasdaq directory exchange/symbol", code="unsupported_listing")
                if row.get("test_issue") != "N" or row.get("etf") != "N" or row.get("nasdaq_traded") != "Y":
                    raise OpenBBCallError("Only non-test equity listings are supported", code="unsupported_listing")
                name = row.get("name") or ""
                if re.search(r"\b(warrants?|rights?|units?|notes?|bonds?|debentures?)\b", name, re.IGNORECASE):
                    raise OpenBBCallError("Non-stock directory instrument is outside scope", code="unsupported_listing")
                mic, calendar = EXCHANGES[exchange]
                listing = make_listing(symbol=canonical, mic=mic, currency="USD",
                                       timezone="America/New_York", calendar=calendar,
                                       vendor="nasdaq", name=row.get("name"),
                                       calendar_note="US listing mapping v1: USD/America_New_York; "
                                       "XNYS regular-equity session calendar proxy, not a provider field. "
                                       "Listing/delisting and historical symbol validity are unknown.")
                if listing.listing_id not in seen:
                    listings.append(listing)
                    seen.add(listing.listing_id)
            except Exception as exc:  # noqa: BLE001 - provider boundary preserves per-symbol failures
                unresolved.append(original)
                code = _error_code(exc)
                warnings.append(WarningRecord(code=code,
                                              message=f"{symbol}: Nasdaq resolution failed ({code})", severity="error"))
        if self.mode == "fixture":
            warnings.append(WarningRecord(code="synthetic_fixture_not_live", message="Synthetic fixture lookup, not live directory verification.", severity="info"))
        return ResolutionResult(data=listings, provenance=origins, raw=raw,
                                warnings=warnings, unresolved=unresolved)

    def fetch_bars(self, request: BarsRequest) -> ProviderResult:
        if request.price_mode != "provider_native":
            raise ValueError("OpenBB Nasdaq only supports provider_native; split/dividend basis is unknown")
        blocks = ((request.end - request.start).days + self.block_days - 1) // self.block_days
        if blocks * len(request.listings) > self.max_calls:
            raise ValueError("OpenBB request exceeds call budget; submit smaller date/listing blocks")
        bars, origins, raw, warnings, failures = [], [], [], [], []
        for listing in request.listings:
            try:
                symbol = listing.vendor_symbol("nasdaq", request.start)
                if listing.mic not in {value[0] for value in EXCHANGES.values()} or not SYMBOL_RE.fullmatch(symbol):
                    raise ValueError("unsupported Nasdaq US listing mapping")
                if listing.currency != "USD" or listing.timezone != "America/New_York" or listing.asset_type != "equity":
                    raise ValueError("unsupported currency/timezone/asset type")
            except ValueError as exc:
                warnings.append(WarningRecord(code="unsupported_listing", message=str(exc),
                                              listing_id=listing.listing_id, severity="error"))
                failures.append(self._failure(request, listing, "unsupported_listing", str(exc)))
                continue
            start = request.start
            listing_failed = False
            while start < request.end:
                end = min(start + timedelta(days=self.block_days), request.end)
                params = {"symbol": symbol, "start_date": start.isoformat(),
                          "end_date": (end - timedelta(days=1)).isoformat(), "provider": "nasdaq"}
                origin = self._provenance(HISTORICAL_ENDPOINT,
                                         {"listing_id": listing.listing_id, "start": start.isoformat(),
                                          "end": end.isoformat(), "boundary": "[start,end)",
                                          "price_mode": request.price_mode}, params)
                origins.append(origin)
                try:
                    envelope = self._call("historical", params)
                    origin = origin.model_copy(update={"fetched_at": utc_now()})
                    origins[-1] = origin
                    raw.append(RawPayload(listing_id=listing.listing_id,
                                          provenance_id=origin.provenance_id, content=envelope))
                    warnings.extend(_warnings(envelope, listing.listing_id))
                    for row in envelope["results"]:
                        returned_symbol = row.get("symbol")
                        if returned_symbol and returned_symbol.upper() != symbol.upper():
                            raise OpenBBCallError("Historical response contains a different symbol", code="symbol_mismatch")
                        # Preserve every raw row but never silently coerce missing
                        # volume to zero or include an out-of-window trading bar.
                        session = date.fromisoformat(str(row["date"])[:10])
                        if not start <= session < end:
                            continue
                        bars.append(Bar(listing_id=listing.listing_id, session_date=session,
                                        open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                                        volume=row["volume"], currency=listing.currency, volume_unit="shares"))
                except Exception as exc:  # noqa: BLE001 - provider boundary preserves per-symbol failures
                    listing_failed = True
                    code = _error_code(exc)
                    warnings.append(WarningRecord(code=code,
                                                  message=f"{symbol} [{start},{end}): Nasdaq history failed ({code})",
                                                  listing_id=listing.listing_id, severity="error"))
                start = end
            if listing_failed:
                failures.append(self._failure(request, listing, "provider_error", "One or more date blocks failed; no missing bars were filled."))
        if self.mode == "fixture":
            warnings.append(WarningRecord(code="synthetic_fixture_not_live", message="Synthetic fixture bars, not live market-data verification.", severity="info"))
        result = ProviderResult(data=bars, raw=raw, provenance=origins, warnings=warnings,
                                coverage=failures, price_semantics=PRICE_SEMANTICS)
        return finish_result(request, result)

    @staticmethod
    def _failure(request: BarsRequest, listing: Any, code: str, reason: str) -> Coverage:
        return Coverage(listing_id=listing.listing_id, requested_start=request.start,
                        requested_end=request.end, status="failed", calendar=listing.calendar,
                        error_code=code, missing_reason=reason)
