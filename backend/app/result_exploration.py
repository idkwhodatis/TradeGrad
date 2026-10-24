"""Read-only result exploration, with no numerical engine or strategy imports."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

from backend.app.result_exploration_schemas import (
    ExplorationContext,
    PriceReplay,
    ResultReturnAnalysis,
    ReturnAnalysis,
)
from backend.app.services import DomainError
from backend.app.storage import read_verified
from packages.tradegrad_adapter.config import canonical_bytes
from packages.tradegrad_adapter.indicators import indicator_time

MAX_BARS = 500
MAX_MARKERS = 2000
MAX_MONTHS = 1200
MAX_REPLAY_ROWS = 40_000
MAX_REPLAY_BYTES = 16_000_000


def next_month(day):
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def return_analysis(bundle, manifest=None):
    """Non-objective display statistics from full persisted, unrounded equity.

    One close per engine-local calendar date (last observation). Never insert an
    initial-capital return, interpolate missing sessions, or use chart samples.
    Session completeness cannot be certified from account observations alone.
    """
    portable = bundle.get("evaluation_mode") == "portable"
    zone = ZoneInfo(
        bundle["manifest"]["timezone"]
        if portable
        else bundle["engine"]["resolved_config"]["timezone"]
    )
    equity_rows = (
        [(row["native_at"], row["equity"]) for row in bundle["account_series"]]
        if portable
        else bundle["equity"]["rows"]
    )
    closes = {}
    for timestamp, value in equity_rows:
        closes[datetime.fromisoformat(timestamp).astimezone(zone).date()] = value
    dates = sorted(closes)
    start = date.fromisoformat(bundle["window"]["start"])
    end = date.fromisoformat(bundle["window"]["end"])
    # Use persisted calendar evidence only, never regenerate a new calendar from
    # today's SDK version. All staged listings must have versioned coverage.
    expected = None
    coverage = (manifest or {}).get("coverage", [])
    listing_ids = set((manifest or {}).get("evaluator_symbol_mapping", {}))
    by_listing = {
        item.get("listing_id"): item for item in coverage if isinstance(item, dict)
    }
    if (
        listing_ids
        and listing_ids <= by_listing.keys()
        and all(
            by_listing[key].get("calendar_version")
            and by_listing[key].get("expected_sessions")
            for key in listing_ids
        )
    ):
        try:
            expected = sorted(
                {
                    date.fromisoformat(day)
                    for key in listing_ids
                    for day in by_listing[key]["expected_sessions"]
                    if str(start) <= day < str(end)
                }
            )
        except (TypeError, ValueError):
            expected = None
    if expected == []:
        expected = None
    expected_index = {day: index for index, day in enumerate(expected or [])}
    missing = sorted(set(expected or []) - set(dates))
    unexpected = sorted(set(dates) - set(expected)) if expected is not None else []
    intervals = []
    excluded = 0

    def ratio(current, base):
        if (
            type(current) not in (int, float)
            or type(base) not in (int, float)
            or not math.isfinite(current)
            or not math.isfinite(base)
            or base <= 0
        ):
            return None
        result = current / base - 1
        return result if math.isfinite(result) else None

    for previous, current in itertools.pairwise(dates):
        value = ratio(closes[current], closes[previous])
        adjacent = expected is None or (
            previous in expected_index
            and current in expected_index
            and expected_index[current] == expected_index[previous] + 1
        )
        if value is None or not adjacent:
            excluded += 1
        else:
            intervals.append(value)
    bins = []
    if intervals:
        low, high = min(intervals), max(intervals)
        count = (
            1 if low == high else min(20, max(1, math.ceil(math.sqrt(len(intervals)))))
        )
        scale = max(abs(low), abs(high), 1)
        normalized_width = (high / scale - low / scale) / count

        def boundary(index):
            return low * (1 - index / count) + high * (index / count)

        bins = [
            {
                "lower": boundary(i),
                "upper": high if i == count - 1 else boundary(i + 1),
                "count": 0,
                "upper_inclusive": i == count - 1,
            }
            for i in range(count)
        ]
        for value in intervals:
            index = (
                0
                if normalized_width == 0
                else min(
                    count - 1,
                    max(0, int((value / scale - low / scale) / normalized_width)),
                )
            )
            bins[index]["count"] += 1
    start = date.fromisoformat(bundle["window"]["start"])
    end = date.fromisoformat(bundle["window"]["end"])
    month = start.replace(day=1)
    months = []
    while month < end:
        if len(months) >= MAX_MONTHS:
            raise DomainError(
                "analysis_too_large",
                "Evaluation exceeds the bounded monthly analysis range",
                409,
            )
        following = next_month(month)
        current_dates = [day for day in dates if month <= day < following]
        previous_dates = [day for day in dates if day < month]
        baseline = (
            previous_dates[-1]
            if previous_dates
            else (current_dates[0] if current_dates else None)
        )
        partial = start > month or end < following or not previous_dates
        status = "partial_period" if partial else "observed_month"
        reason = "Calendar/session completeness is not certified."
        value = None
        if not current_dates or (
            previous_dates and next_month(baseline.replace(day=1)) < month
        ):
            status, reason = (
                "missing_observations",
                "No observations in this month, or the prior observed close skips an entire calendar month; no monthly return is inferred.",
            )
        elif baseline is not None:
            value = ratio(closes[current_dates[-1]], closes[baseline])
            interval_dates = [
                day for day in dates if baseline <= day <= current_dates[-1]
            ]
            if len(interval_dates) < 2:
                status, reason = (
                    "missing_observations",
                    "At least two observed closes are needed; no synthetic initial-capital close is inserted.",
                )
                value = None
            elif value is None or any(
                ratio(closes[b], closes[a]) is None
                for a, b in itertools.pairwise(interval_dates)
            ):
                status, reason = (
                    "invalid_base",
                    "A missing/nonfinite observation or nonpositive return denominator makes this period unavailable.",
                )
                value = None
            elif partial:
                reason = "Partial observed period: evaluation clips the calendar month and/or no preceding-month closing observation exists. Initial capital is not substituted."
        if any(month <= day < following for day in missing):
            status, reason, value = (
                "missing_observations",
                "At least one equity close expected by the pinned dataset calendar is missing in this month; no complete-month return is claimed.",
                None,
            )
        elif expected is not None and (
            any(month <= day < following for day in unexpected)
            or (baseline is not None and baseline not in expected_index)
        ):
            status, reason, value = (
                "missing_observations",
                "An observed close or the return baseline is outside pinned calendar expectations; no monthly return is inferred.",
                None,
            )
        elif expected is not None and status == "observed_month":
            reason = (
                "Observed dates match pinned calendar expectations for this month. "
                + (
                    "Equity is from the verified trusted-engine ledger, not policy-reported accounting."
                    if portable
                    else "Native equity remains untrusted."
                )
            )
        months.append(
            {
                "month": month.strftime("%Y-%m"),
                "value": value,
                "status": status,
                "first_observation": current_dates[0] if current_dates else None,
                "last_observation": current_dates[-1] if current_dates else None,
                "baseline_date": baseline,
                "observations": len(current_dates),
                "reason": reason,
            }
        )
        month = following
    return ReturnAnalysis(
        monthly_definition="Last observed equity in month / last observed equity in preceding calendar month - 1. Without a preceding close, first observed close is the partial-period baseline. Empty months and invalid denominators remain unavailable; no initial capital, fees, or benchmark is added.",
        distribution_definition=(
            "When a complete versioned pinned-session index is available, only consecutive expected sessions enter the distribution; intervals crossing missing sessions and unexpected observations are excluded. "
            if expected is not None
            else "Without versioned pinned-session coverage, missing sessions cannot be identified: these are inter-observation returns, not certified one-trading-day returns. "
        )
        + "Unannualized return E[t] / E[t-1] - 1 between consecutive observed engine-local daily closing equities. Missing sessions are never filled. Equal-width bins: ceil(sqrt(N)), capped at 20; [lower, upper), last upper inclusive; identical returns form one bin.",
        coverage_status=(
            "missing_pinned_sessions"
            if missing or unexpected
            else "matches_pinned_session_dates"
        )
        if expected is not None
        else "exchange_session_completeness_not_verified",
        expected_sessions=len(expected) if expected is not None else None,
        missing_sessions=missing,
        unexpected_observations=unexpected,
        warnings=[
            (
                "Display-only analysis of verified trusted-engine portfolio equity, not policy-reported accounting; "
                if portable
                else "Display-only analysis of native same-process portfolio equity; "
            )
            + "original metrics and optimizer reward are unchanged.",
            "All chart/listing filters are ignored for this full-evaluation portfolio analysis.",
            "Session completeness is checked only against immutable versioned dataset coverage when available, not a live exchange feed or independent ledger. Otherwise gaps may contain holidays, suspensions, or missing snapshots and returns are only inter-observation returns.",
        ],
        months=months,
        bins=bins,
        observations=len(dates),
        valid_returns=len(intervals),
        excluded_returns=excluded,
        collapsed_intraday_observations=len(equity_rows) - len(dates),
        status="available" if intervals else "insufficient_observations",
    )


def result_symbol(result, listing_id):
    mapping = result.dataset.manifest.get("evaluator_symbol_mapping", {})
    symbol = mapping.get(str(listing_id))
    if not isinstance(symbol, str) or not symbol:
        raise DomainError(
            "invalid_result_listing",
            "Listing is not part of this evaluation's immutable dataset",
            422,
        )
    # A time task stages its sole listing under this exact engine alias.
    if result.bundle.get("evaluation_mode") != "portable" and result.bundle["identity"][
        "task"
    ].endswith("_time"):
        if len(mapping) != 1:
            raise DomainError(
                "result_integrity",
                "Time evaluation has ambiguous listing identity",
                409,
            )
        return "target_symbol"
    return symbol


def _indicator_view(bundle, symbol, days, zone):
    section = bundle.get("indicators")
    if section is None:
        return (
            "not_persisted",
            "This older artifact has no recorded indicator section. It is not reconstructed or rerun.",
            [],
        )
    if section["status"] != "available":
        return section["status"], section["reason"], []
    definitions = {item["indicator_key"]: item for item in section["definitions"]}
    observations = {}
    for point in section["points"]:
        observations.setdefault(point["instance_id"], []).append(point)
    output = []
    for instance in section["instances"]:
        if instance["symbol"] != symbol:
            continue
        metadata = definitions[instance["indicator_key"]]
        points, duplicates, warmup, total = {}, 0, 0, 0
        for point in observations.get(instance["instance_id"], []):
            day = indicator_time(point["timestamp"]).astimezone(zone).date().isoformat()
            if day not in days:
                continue
            total += 1
            if point["warmup"]:
                warmup += 1
                continue
            duplicates += int(day in points)
            points[day] = point["value"]
        supported = metadata["render_type"] in {"line", "histogram"}
        status = (
            "unsupported_render"
            if not supported
            else "available"
            if points
            else "no_observations"
        )
        output.append(
            {
                "instance_id": instance["instance_id"],
                "name": metadata["display_name"],
                "symbol": symbol,
                "pane": metadata["pane"],
                "render_type": metadata["render_type"],
                "unit": metadata["unit"] or "undeclared",
                "status": status,
                "reason": "Declared metadata and last recorded non-warmup point per engine-local session only; no indicators inferred. Missing values remain gaps."
                if supported
                else "This declared render type is not supported in V1; persisted values remain available in original JSON.",
                "metadata": {**metadata, "instance": instance},
                "points": [
                    {"session_date": day, "value": value}
                    for day, value in sorted(points.items())
                ],
                "total_points": total,
                "warmup_excluded": warmup,
                "duplicate_session_points": duplicates,
            }
        )
    return (
        "available" if output else "no_listing_observations",
        "Only explicit recorded indicator instances for this listing and price page are projected.",
        output,
    )


def frozen_price_bars(result, settings, *, listing_id, start, end):
    """Shared checksum-bound OHLCV reader; no markers, indicators or provider IO."""
    refs = result.dataset.manifest.get("preview_files", {}).get(str(listing_id))
    if refs is None:
        raise DomainError(
            "price_replay_unavailable",
            "Pinned dataset has no persisted OHLCV preview for this listing; prices are not inferred from fills",
            409,
        )
    if not isinstance(refs, list) or len(refs) > 1200:
        raise DomainError(
            "snapshot_integrity", "Snapshot preview manifest exceeds replay bounds", 409
        )
    bars = []
    scanned_bytes = 0
    scanned_rows = 0
    seen = set()
    for ref in refs:
        if ref["end_date"] <= str(start) or ref["start_date"] >= str(end):
            continue
        raw = read_verified(
            settings.storage_root,
            f"{result.dataset.storage_key}/{ref['path']}",
            ref["sha256"],
            2_000_000,
        )
        scanned_bytes += len(raw)
        if scanned_bytes > MAX_REPLAY_BYTES:
            raise DomainError(
                "price_replay_too_large",
                "Narrow the replay date window; persisted preview exceeds the scan budget",
                422,
            )
        for row in json.loads(raw):
            scanned_rows += 1
            if scanned_rows > MAX_REPLAY_ROWS:
                raise DomainError(
                    "price_replay_too_large",
                    "Narrow the replay date window; persisted preview exceeds the row budget",
                    422,
                )
            day = row["session_date"]
            if row["listing_id"] != str(listing_id):
                raise DomainError(
                    "snapshot_integrity", "Replay listing identity mismatch", 409
                )
            if str(start) <= day < str(end):
                if day in seen:
                    raise DomainError(
                        "snapshot_integrity", "Duplicate replay session", 409
                    )
                seen.add(day)
                bars.append(row)
    bars.sort(key=lambda row: row["session_date"])
    return bars


def price_replay(
    result, settings, *, listing_id, start=None, end=None, offset=0, limit=250
):
    context = exploration_context(result)
    window = result.bundle["window"]
    start = start or date.fromisoformat(window["start"])
    end = end or date.fromisoformat(window["end"])
    if (
        start >= end
        or start < date.fromisoformat(window["start"])
        or end > date.fromisoformat(window["end"])
        or type(offset) is not int
        or not 0 <= offset <= 100000
        or type(limit) is not int
        or not 1 <= limit <= MAX_BARS
    ):
        raise DomainError(
            "invalid_result_query",
            "Replay requires a bounded page within this evaluation's [start, end) window",
            422,
        )
    symbol = result_symbol(result, listing_id)
    bars = frozen_price_bars(
        result, settings, listing_id=listing_id, start=start, end=end
    )
    page = bars[offset : offset + limit]
    days = {bar["session_date"] for bar in page}
    zone = ZoneInfo(context.timezone)
    portable = context.mode.startswith("portable")
    markers = []
    if portable:
        fills = result.bundle["fills"]
    else:
        columns = [item["name"] for item in result.bundle["fills"]["columns"]]
        fills = [dict(zip(columns, row)) for row in result.bundle["fills"]["rows"]]
    for index, item in enumerate(fills):
        if (
            (item["listing_id"] != str(listing_id))
            if portable
            else (item["symbol"] != symbol)
        ):
            continue
        timestamp = item["native_at"] if portable else item["timestamp"]
        day = datetime.fromisoformat(timestamp).astimezone(zone).date()
        if not start <= day < end:
            continue
        # Include absent-bar executions within the displayed page's date extent,
        # never snap them to a neighbouring session or silently invent a candle.
        if bars and not page:
            continue
        if page:
            lower = str(start) if offset == 0 else page[0]["session_date"]
            upper = (
                str(end)
                if offset + limit >= len(bars)
                else bars[offset + limit]["session_date"]
            )
            if not lower <= day.isoformat() < upper:
                continue
        markers.append(
            {
                "id": f"fill:{index}",
                "row_index": index,
                "session_date": day,
                "timestamp": timestamp,
                "symbol": symbol,
                "side": item["side"],
                "price": item["price"],
                "quantity": item["quantity"],
                "commission": item["commission"],
                "order_id": str(item["order_id"]),
                "on_price_bar": day.isoformat() in days,
                "listing_id": str(listing_id),
                **(
                    {
                        key: item[key]
                        for key in (
                            "fill_id",
                            "native_timestamp_ns",
                            "economic_reference_time",
                            "economic_reference",
                        )
                    }
                    if portable
                    else {}
                ),
            }
        )
    markers.sort(
        key=lambda item: (datetime.fromisoformat(item["timestamp"]), item["row_index"])
    )
    status, reason, indicators = (
        (
            "not_persisted",
            "The portable trusted-engine protocol does not persist declared indicator observations. Policy state and decision values are not indicator or accounting evidence; no series is inferred.",
            [],
        )
        if portable
        else _indicator_view(result.bundle, symbol, days, zone)
    )
    return PriceReplay(
        context=context,
        evaluation_id=result.evaluation.id,
        artifact_sha256=result.artifact.sha256,
        dataset_sha256=result.dataset.content_sha256,
        listing_id=listing_id,
        symbol=symbol,
        timezone=str(zone),
        start=start,
        end=end,
        bars=page,
        bar_count=len(bars),
        offset=offset,
        limit=limit,
        markers=markers[:MAX_MARKERS],
        marker_count=len(markers),
        markers_truncated=len(markers) > MAX_MARKERS,
        indicators_status=status,
        indicators_reason=reason,
        indicators=indicators,
        warnings=[
            "OHLCV comes from the checksum-verified pinned dataset, not the engine's fill prices. Adjustment basis remains the dataset's declared semantics.",
            "Markers are actual fill rows, not orders or inferred round trips. Multiple executions on one date stay separate. A fill without a matching OHLCV bar is listed but never snapped to another bar.",
            (
                "Fills and account observations come only from the verified trusted-engine ledger. Native timestamps and economic next-open references remain distinct; policy-reported values are not used. "
                if portable
                else "Native observations are not an independently trusted ledger. "
            )
            + "Only the bounded OHLCV page is charted; missing sessions are not filled.",
        ],
    )


def exploration_context(result):
    """Identity from already-authorized, verified evidence; never a user mode hint."""
    modes = {
        "tradegrad.run.v1": "native_research",
        "tradegrad.final-evaluation.v1": "native_final",
        "tradegrad.portable.run.v1": "portable_research",
        "tradegrad.portable.final-evaluation.v1": "portable_final",
    }
    mode = modes.get(result.run.config.get("schema_version"))
    if mode is None or result.dataset is None:
        raise DomainError(
            "result_integrity", "Missing verified exploration identity", 409
        )
    if (
        hashlib.sha256(canonical_bytes(result.dataset.manifest)).hexdigest()
        != result.dataset.content_sha256
    ):
        raise DomainError(
            "snapshot_integrity",
            "Exploration dataset metadata differs from its pinned checksum",
            409,
        )
    portable = mode.startswith("portable")
    if portable != (result.bundle.get("evaluation_mode") == "portable"):
        raise DomainError(
            "result_integrity", "Exploration mode differs from verified result", 409
        )
    split = result.evaluation.window["split"]
    final = mode.endswith("final")
    selection = result.run.config.get("selection_id")
    if (split == "holdout") != final or final != bool(selection):
        raise DomainError(
            "result_integrity", "Exploration holdout identity differs", 409
        )
    mapping = result.dataset.manifest.get("evaluator_symbol_mapping", {})
    return ExplorationContext(
        mode=mode,
        evaluation_id=result.evaluation.id,
        run_id=result.run.id,
        artifact_sha256=result.artifact.sha256,
        dataset_id=result.dataset.id,
        dataset_sha256=result.dataset.content_sha256,
        evaluation_data_sha256=result.bundle["identity"].get("market_data_sha256")
        if portable
        else None,
        split=split,
        start=result.bundle["window"]["start"],
        end=result.bundle["window"]["end"],
        timezone=result.bundle["manifest"]["timezone"]
        if portable
        else result.bundle["engine"]["resolved_config"]["timezone"],
        result_trust=result.bundle["result_trust"],
        ledger_origin=result.bundle.get("ledger_origin", "native_same_process"),
        selection_id=selection,
        listings=[
            {"listing_id": key, "symbol": value}
            for key, value in sorted(mapping.items())
        ],
    )


def result_return_analysis(result):
    context = exploration_context(result)
    analysis = return_analysis(result.bundle, result.dataset.manifest)
    return ResultReturnAnalysis(**analysis.model_dump(), context=context)
