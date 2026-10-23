"""Yahoo OHLC is split-adjusted even when auto_adjust=False."""

from ..catalog import documented_capabilities
from ..contracts import (
    Bar,
    BarsRequest,
    CorporateAction,
    PriceSemantics,
    ProviderResult,
    RawPayload,
    ResolutionResult,
    WarningRecord,
    make_listing,
    utc_now,
)
from ..normalize import finish_result, frame_payload, provenance
from .common import CallPolicy, ProviderError, restore_frame, sdk_call, with_retries

_EXCHANGES = {
    "NMS": "XNAS",
    "NGM": "XNAS",
    "NCM": "XNAS",
    "NAS": "XNAS",
    "NYQ": "XNYS",
    "NYE": "XNYS",
    "ASE": "XASE",
    "AMEX": "XASE",
}


def repair_flags(value):
    import numpy as np

    status = (
        ("yahoo_repaired" if bool(value) else "yahoo_repair_not_flagged")
        if isinstance(value, (bool, np.bool_))
        else "yahoo_repair_status_unknown"
    )
    return ["yahoo_repair_requested", status]


class YFinanceProvider:
    def __init__(self, *, mode="live", client=None, policy: CallPolicy | None = None):
        if mode not in {"live", "fixture"} or (
            client is not None and mode != "fixture"
        ):
            raise ValueError("Injected clients require explicit fixture mode")
        self.mode, self.client, self.policy = mode, client, policy or CallPolicy()

    def capabilities(self):
        return documented_capabilities("yfinance")

    def _call(self, operation, params):
        if self.client is not None:
            call = lambda: getattr(self.client, operation)(**params)
        elif self.mode == "fixture":
            from . import fixtures

            call = lambda: (
                fixtures.yfinance_resolve(params["symbol"])
                if operation == "yfinance_resolve"
                else fixtures.yfinance_bars(
                    params["symbol"],
                    params["start"],
                    params["end"],
                    repair=params["repair"],
                )
            )
        else:
            call = lambda: sdk_call(
                operation, dict(params), timeout=self.policy.timeout + 5
            )
        value = with_retries(call, budget_key=f"yahoo:{operation}", policy=self.policy)
        return (
            restore_frame(value)
            if self.mode == "live" and operation == "yfinance_bars"
            else value
        )

    def resolve_instruments(self, symbols: list[str]) -> ResolutionResult:
        import re

        if not 1 <= len(symbols) <= 100:
            raise ValueError("Resolve 1 to 100 explicit symbols")
        result = ResolutionResult()
        for supplied in symbols:
            symbol = supplied.strip().upper()
            source = provenance(
                "yfinance",
                "yahoo",
                "Search+Ticker.history_metadata",
                requested_params={"symbol": supplied},
                effective_params={"symbol": symbol},
                evidence=self.mode,
                package_names=["yfinance"],
            )
            result.provenance.append(source)
            try:
                if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", symbol):
                    raise ProviderError("unsupported_symbol")
                raw = self._call(
                    "yfinance_resolve",
                    {"symbol": symbol, "timeout": self.policy.timeout},
                )
                result.raw.append(
                    RawPayload(provenance_id=source.provenance_id, content=raw)
                )
                source = source.model_copy(update={"fetched_at": utc_now()})
                result.provenance[-1] = source
                meta = raw["metadata"]
                matching = [
                    q for q in raw["quotes"] if q.get("symbol", "").upper() == symbol
                ]
                mic = _EXCHANGES.get(meta.get("exchangeName"))
                if (
                    not matching
                    or not mic
                    or meta.get("currency") != "USD"
                    or meta.get("instrumentType") != "EQUITY"
                ):
                    raise ProviderError("unsupported_or_unresolved_listing")
                if meta.get("exchangeTimezoneName") != "America/New_York":
                    raise ProviderError("unsupported_timezone")
                listing = make_listing(
                    symbol=symbol,
                    mic=mic,
                    currency="USD",
                    timezone="America/New_York",
                    calendar="XNYS",
                    calendar_note="Explicit XNYS US equity session schedule proxy; MIC retains listing venue",
                    vendor="yfinance",
                    name=matching[0].get("shortname"),
                )
                # firstTradeDate is a vendor history boundary, not authoritative IPO evidence.
                result.data.append(listing)
            except Exception as exc:  # noqa: BLE001 - redact SDK failures at the trust boundary
                from .common import classify_error

                error = exc if isinstance(exc, ProviderError) else classify_error(exc)
                result.unresolved.append(supplied)
                result.warnings.append(
                    WarningRecord(
                        code=error.code,
                        message="Yahoo listing resolution failed",
                        severity="error",
                    )
                )
        if self.mode == "fixture":
            result.warnings.append(
                WarningRecord(
                    code="synthetic_fixture_not_live",
                    message="Fixed synthetic SDK-schema fixture, not live market data",
                )
            )
        return result

    def fetch_bars(self, request: BarsRequest) -> ProviderResult:
        if request.price_mode not in {"provider_native", "repair"}:
            raise ValueError(
                "Yahoo supports provider_native or explicit parent-derived repair only"
            )
        repair = request.price_mode == "repair"
        result = ProviderResult(
            price_semantics=PriceSemantics(
                native_mode="yahoo_split_adjusted_ohlc",
                split_basis="adjusted",
                dividend_basis="unadjusted",
                adjustment_as_of=None,
                adj_close_split_basis="adjusted",
                adj_close_dividend_basis="adjusted",
                algorithm_version=f"yahoo-native;repair={repair}",
                note="OHLC is already split-adjusted, not true unadjusted prices. "
                "Adjustment-state timestamp is unknown: request.as_of is only a coverage cutoff, "
                "not historical adjustment or point-in-time evidence. Provenance fetched_at is acquisition time.",
            )
        )
        if repair:
            result.warnings.append(
                WarningRecord(
                    code="explicit_yahoo_repair_refetch",
                    message="New repair=True SDK acquisition linked to an immutable parent. Upstream revisions may also differ; this is not an offline replay of the parent's raw bytes.",
                )
            )
        for listing in request.listings:
            try:
                if (
                    listing.mic not in {"XNAS", "XNYS", "XASE"}
                    or listing.currency != "USD"
                ):
                    raise ProviderError("unsupported_listing")
                params = {
                    "symbol": listing.vendor_symbol("yfinance", request.start),
                    "start": request.start.isoformat(),
                    "end": request.end.isoformat(),
                    "interval": "1d",
                    "auto_adjust": False,
                    "back_adjust": False,
                    "repair": repair,
                    "actions": True,
                    "keepna": True,
                    "rounding": False,
                    "timeout": self.policy.timeout,
                    "raise_errors": True,
                }
                source = provenance(
                    "yfinance",
                    "yahoo",
                    "Ticker.history",
                    request.model_dump(mode="json", exclude={"listings"}),
                    params,
                    self.mode,
                    ["yfinance"],
                )
                event_source = provenance(
                    "yfinance",
                    "yahoo",
                    "Ticker.history.actions",
                    effective_params=params,
                    evidence=self.mode,
                    package_names=["yfinance"],
                    note="Events from the same Yahoo response, separately attributed",
                )
                result.provenance.extend([source, event_source])
                frame = self._call("yfinance_bars", params)
                stamp = utc_now()
                source = source.model_copy(update={"fetched_at": stamp})
                event_source = event_source.model_copy(update={"fetched_at": stamp})
                result.provenance[-2:] = [source, event_source]
                result.raw.append(
                    frame_payload(frame, source.provenance_id, listing.listing_id)
                )
                if frame.empty:
                    continue
                metadata = frame.attrs.get("history_metadata")
                if metadata is not None and (
                    metadata.get("symbol", "").upper() != params["symbol"].upper()
                    or metadata.get("currency") != listing.currency
                    or metadata.get("exchangeTimezoneName") != listing.timezone
                ):
                    raise ProviderError("upstream_listing_metadata_changed")
                if self.mode == "live" and metadata is None:
                    raise ProviderError("missing_history_metadata")
                if not {"Open", "High", "Low", "Close", "Volume", "Adj Close"} <= set(
                    frame.columns
                ):
                    raise ProviderError("upstream_schema_changed")
                if not {"Dividends", "Stock Splits"} <= set(frame.columns):
                    raise ProviderError("upstream_actions_schema_changed")
                import pandas as pd

                for index, row in frame.iterrows():
                    timestamp = pd.Timestamp(index)
                    day = (
                        timestamp.tz_convert(listing.timezone).date()
                        if timestamp.tzinfo
                        else timestamp.date()
                    )
                    for column, action in [
                        ("Dividends", "dividend"),
                        ("Stock Splits", "split"),
                        ("Capital Gains", "capital_gain"),
                    ]:
                        value = row.get(column, 0)
                        if column in {"Dividends", "Stock Splits"} and pd.isna(value):
                            raise ProviderError("upstream_actions_schema_changed")
                        if not pd.isna(value) and float(value) != 0:
                            result.actions.append(
                                CorporateAction(
                                    listing_id=listing.listing_id,
                                    session_date=day,
                                    action_type=action,
                                    value=float(value),
                                    currency=listing.currency
                                    if action != "split"
                                    else None,
                                    provenance_id=event_source.provenance_id,
                                )
                            )
                    result.data.append(
                        Bar(
                            listing_id=listing.listing_id,
                            session_date=day,
                            open=row["Open"],
                            high=row["High"],
                            low=row["Low"],
                            close=row["Close"],
                            adj_close=row["Adj Close"],
                            volume=row["Volume"],
                            currency=listing.currency,
                            volume_unit="shares",
                            flags=repair_flags(row.get("Repaired?")) if repair else [],
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - redact SDK failures at the trust boundary
                from .common import classify_error

                error = exc if isinstance(exc, ProviderError) else classify_error(exc)
                result.warnings.append(
                    WarningRecord(
                        code=error.code,
                        message="Yahoo bars failed for this listing",
                        listing_id=listing.listing_id,
                        severity="error",
                    )
                )
        if self.mode == "fixture":
            result.warnings.append(
                WarningRecord(
                    code="synthetic_fixture_not_live",
                    message="Fixed synthetic SDK-schema fixture, not live market data",
                )
            )
        return finish_result(request, result)
