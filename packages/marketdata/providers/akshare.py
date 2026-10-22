"""AKShare/Eastmoney A-share daily adapter; no implicit fallback provider."""

import re
from datetime import timedelta

from ..catalog import documented_capabilities
from ..contracts import (
    Bar,
    BarsRequest,
    PriceSemantics,
    ProviderResult,
    ResolutionResult,
    WarningRecord,
    make_listing,
    utc_now,
)
from ..normalize import finish_result, frame_payload, provenance
from .common import CallPolicy, ProviderError, restore_frame, sdk_call, with_retries


class AKShareProvider:
    def __init__(self, *, mode="live", client=None, policy: CallPolicy | None = None):
        if mode not in {"live", "fixture"} or (
            client is not None and mode != "fixture"
        ):
            raise ValueError("Injected clients require explicit fixture mode")
        self.mode, self.client, self.policy = mode, client, policy or CallPolicy()

    def capabilities(self):
        return documented_capabilities("akshare")

    def _call(self, operation, params):
        if self.client is not None:
            call = lambda: getattr(self.client, operation)(**params)
        elif self.mode == "fixture":
            from . import fixtures

            call = (
                (lambda: fixtures.akshare_resolve())
                if operation == "stock_info_a_code_name"
                else (
                    lambda: fixtures.akshare_bars(
                        params["symbol"],
                        params["adjust"],
                        params["start_date"],
                        params["end_date"],
                    )
                )
            )
        else:
            sdk_operation = (
                "akshare_resolve"
                if operation == "stock_info_a_code_name"
                else "akshare_bars"
            )
            call = lambda: restore_frame(
                sdk_call(sdk_operation, params, timeout=self.policy.timeout + 5)
            )
        return with_retries(
            call, budget_key=f"eastmoney:{operation}", policy=self.policy
        )

    def resolve_instruments(self, symbols: list[str]) -> ResolutionResult:
        if not 1 <= len(symbols) <= 100:
            raise ValueError("Resolve 1 to 100 explicit symbols")
        source = provenance(
            "akshare",
            "sse_szse_bse",
            "stock_info_a_code_name",
            requested_params={"symbols": symbols},
            evidence=self.mode,
            package_names=["akshare"],
        )
        result = ResolutionResult(provenance=[source])
        try:
            frame = self._call("stock_info_a_code_name", {})
            source = source.model_copy(update={"fetched_at": utc_now()})
            result.provenance[-1] = source
            result.raw.append(frame_payload(frame, source.provenance_id))
            if not {"code", "name"} <= set(frame.columns):
                raise ProviderError("upstream_schema_changed")
            lookup = {
                str(row["code"]).zfill(6): str(row["name"])
                for row in frame.to_dict("records")
            }
            for supplied in symbols:
                symbol = supplied.strip()
                if not re.fullmatch(r"(?:6|0|3)\d{5}", symbol) or symbol not in lookup:
                    result.unresolved.append(supplied)
                    continue
                mic = "XSHG" if symbol.startswith("6") else "XSHE"
                result.data.append(
                    make_listing(
                        symbol=symbol,
                        mic=mic,
                        currency="CNY",
                        timezone="Asia/Shanghai",
                        calendar="XSHG",
                        vendor="akshare",
                        name=lookup[symbol],
                        calendar_note="XSHE shares the explicit XSHG mainland equity session schedule; not a native XSHE calendar"
                        if mic == "XSHE"
                        else None,
                    )
                )
        except ProviderError as error:
            result.unresolved.extend(symbols)
            result.warnings.append(
                WarningRecord(
                    code=error.code,
                    message="A-share resolution failed",
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
        mode = "raw" if request.price_mode == "provider_native" else request.price_mode
        if mode not in {"raw", "qfq", "hfq"}:
            raise ValueError("AKShare price_mode must be raw, qfq or hfq")
        adjust = "" if mode == "raw" else mode
        semantics = PriceSemantics(
            native_mode=mode,
            split_basis="unadjusted" if mode == "raw" else "adjusted",
            dividend_basis="unadjusted" if mode == "raw" else "adjusted",
            adjustment_as_of=None,
            algorithm_version="eastmoney-provider-native",
            note="Event provenance is separate; this adapter does not infer events from adjusted prices. "
            "Adjustment-state timestamp is unknown: request.as_of is only a coverage cutoff, "
            "not historical adjustment or point-in-time evidence. Provenance fetched_at is acquisition time.",
        )
        result = ProviderResult(price_semantics=semantics)
        for listing in request.listings:
            try:
                if listing.mic not in {"XSHG", "XSHE"} or listing.currency != "CNY":
                    raise ProviderError("unsupported_listing")
                symbol = listing.vendor_symbol("akshare", request.start)
                params = {
                    "symbol": symbol,
                    "period": "daily",
                    "start_date": request.start.strftime("%Y%m%d"),
                    "end_date": (request.end - timedelta(days=1)).strftime("%Y%m%d"),
                    "adjust": adjust,
                    "timeout": self.policy.timeout,
                }
                source = provenance(
                    "akshare",
                    "eastmoney",
                    "stock_zh_a_hist",
                    request.model_dump(mode="json", exclude={"listings"}),
                    params,
                    self.mode,
                    ["akshare"],
                    note="SDK table preserved; upstream volume lots multiplied by 100 for shares",
                )
                result.provenance.append(source)
                frame = self._call("stock_zh_a_hist", params)
                source = source.model_copy(update={"fetched_at": utc_now()})
                result.provenance[-1] = source
                result.raw.append(
                    frame_payload(frame, source.provenance_id, listing.listing_id)
                )
                if frame.empty:
                    continue
                required = {"日期", "开盘", "最高", "最低", "收盘", "成交量"}
                if not required <= set(frame.columns):
                    raise ProviderError("upstream_schema_changed")
                import pandas as pd

                for row in frame.to_dict("records"):
                    if "股票代码" in row and str(row["股票代码"]).zfill(6) != symbol:
                        raise ProviderError("upstream_symbol_mismatch")
                    result.data.append(
                        Bar(
                            listing_id=listing.listing_id,
                            session_date=pd.Timestamp(row["日期"]).date(),
                            open=row["开盘"],
                            high=row["最高"],
                            low=row["最低"],
                            close=row["收盘"],
                            volume=float(row["成交量"]) * 100,
                            amount=row.get("成交额"),
                            currency=listing.currency,
                            volume_unit="shares",
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - redact SDK failures at the trust boundary
                from .common import classify_error

                error = exc if isinstance(exc, ProviderError) else classify_error(exc)
                result.warnings.append(
                    WarningRecord(
                        code=error.code,
                        message="A-share bars failed for this listing",
                        listing_id=listing.listing_id,
                        severity="error",
                    )
                )
        result.warnings.append(
            WarningRecord(
                code="actions_unavailable",
                message="No separate verified corporate-action feed; strict corporate-action mode unavailable",
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
