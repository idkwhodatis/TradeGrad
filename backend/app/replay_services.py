"""Read saved replay evidence, never execute strategies or call a data provider.

Admission belongs to load_owned_trace's four schema-specific immutable loaders.
This layer only joins explicit IDs, reads pinned universe/OHLCV files and projects
bounded pages. A snapshot membership declaration is not historical truth proof.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from functools import wraps
from uuid import UUID

from backend.app.replay_schemas import (
    ExecutionEvidenceRow,
    ReplayExecutionPage,
    ReplayInstrument,
    ReplayInstrumentPage,
    ReplayMarketContext,
    ReplayMarketPage,
)
from backend.app.result_exploration import exploration_context, frozen_price_bars
from backend.app.services import DomainError
from backend.app.storage import read_verified
from backend.app.trace_services import _event, _invalid, _query
from packages.research_trace.contracts import OrderLink, canonical_bytes
from packages.research_trace.storage import MAX_RESPONSE_BYTES, MAX_SCAN_BYTES
from packages.tradegrad_adapter.config import DateWindow
from packages.tradegrad_adapter.windows import CsvLimits, _universe_files

MEMBERSHIP_STATES = {
    "user_fixed": "user_fixed_assumption",
    "current_snapshot": "current_snapshot_only",
    "historical_membership": "historical_intervals_declared",
    "point_in_time_verified": "point_in_time_evidence_declared",
}


def _page(offset, limit):
    if (
        type(offset) is not int
        or not 0 <= offset <= 100000
        or type(limit) is not int
        or not 1 <= limit <= 500
    ):
        raise DomainError("invalid_replay_query", "Replay needs a bounded page", 422)


def _view(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            response = function(*args, **kwargs)
            if (
                len(canonical_bytes(response.model_dump(mode="json")))
                > MAX_RESPONSE_BYTES
            ):
                raise DomainError(
                    "replay_detail_too_large",
                    "This evidence page exceeds 4 MiB; reduce its page size or narrow the selection",
                    422,
                )
            return response
        except DomainError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise _invalid() from None

    return checked


def _membership(result, settings, context, day):
    """Use the same frozen effective universe files as native staging, not rows
    found in orders/candidates, today's listing catalog or same-symbol guesses.
    Portable's actual admitted V1 contract uses all frozen user-fixed listings.
    """
    manifest = result.dataset.manifest
    mapping = manifest.get("evaluator_symbol_mapping", {})
    if (
        not isinstance(mapping, dict)
        or not mapping
        or any(not isinstance(value, str) or not value for value in mapping.values())
        or len(set(mapping.values())) != len(mapping)
    ):
        raise _invalid()
    if context.mode.startswith("portable"):
        ids = {item["listing_id"] for item in manifest.get("listings", [])}
        if ids != set(mapping):
            raise _invalid()
        within_window = context.start <= day < context.end
        return (
            set(mapping.values()),
            set(mapping.values()) if within_window else None,
            "Frozen portable user-fixed basket; all admitted listings belong to the configured evaluation, not a verified historical universe."
            if within_window
            else "This execution observation falls outside the strategy evaluation window; no configured session membership at its later timestamp is inferred.",
        )
    files = manifest.get("files", {})
    refs = {
        name: digest for name, digest in files.items() if name.startswith("universe/")
    }
    if not refs:
        return (
            None,
            None,
            "No immutable effective universe files are retained; dataset listing membership is not substituted for evaluated membership.",
        )
    if len(refs) > 1200:
        raise DomainError(
            "replay_query_too_large",
            "Frozen universe exceeds the bounded file count",
            422,
        )
    raw, total = {}, 0
    for name, digest in sorted(refs.items()):
        content = read_verified(
            settings.storage_root,
            f"{result.dataset.storage_key}/{name}",
            digest,
            min(16 * 1024 * 1024, MAX_SCAN_BYTES - total),
        )
        total += len(content)
        if total >= MAX_SCAN_BYTES:
            raise DomainError(
                "replay_query_too_large",
                "Frozen universe exceeds the 128 MiB scan budget",
                422,
            )
        raw[name] = content
    universe = manifest.get("universe") or {}
    state = universe.get("state", manifest.get("universe_status"))
    if state not in MEMBERSHIP_STATES:
        return (
            None,
            None,
            "The frozen snapshot does not declare supported universe semantics.",
        )
    selected, active = _universe_files(
        raw,
        DateWindow(start=context.start, end=context.end),
        CsvLimits(max_file_bytes=16 * 1024 * 1024, max_total_bytes=MAX_SCAN_BYTES),
        state,
    )
    snapshots = {}
    for content in selected.values():
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
        snapshots[date.fromisoformat(rows[0]["updateDate"])] = {
            row["code"] for row in rows
        }
    if not active <= set(mapping.values()):
        raise _invalid()
    if not context.start <= day < context.end:
        return (
            active,
            None,
            "This execution observation falls outside the strategy evaluation window; no configured session membership at its later timestamp is inferred.",
        )
    effective = max(moment for moment in snapshots if moment <= day)
    return (
        active,
        snapshots[effective],
        "Membership is the frozen configuration's effective snapshot at this session. It does not certify that a strategy evaluated every member or establish independent point-in-time membership.",
    )


def _market_inputs(trace, settings, *, session_date=None, allow_outside=False):
    result = trace.result
    context = exploration_context(result)
    day = session_date or context.start
    if not allow_outside and not context.start <= day < context.end:
        raise DomainError(
            "invalid_replay_query",
            "Session must lie within the leaf evaluation window",
            422,
        )
    source = result.run.config.get("source_config", result.run.config)
    ref = source.get("universe") or {}
    universe = result.dataset.manifest.get("universe") or {}
    for key, expected in (("id", ref.get("id")), ("content_sha256", ref.get("sha256"))):
        if universe.get(key) is not None and universe[key] != expected:
            raise _invalid()
    state = universe.get("state", result.dataset.manifest.get("universe_status"))
    active, effective, reason = _membership(result, settings, context, day)
    time_task = not context.mode.startswith("portable") and result.bundle["identity"][
        "task"
    ].endswith("_time")
    mapping = result.dataset.manifest["evaluator_symbol_mapping"]
    alias_symbols = active if active is not None else set(mapping.values())
    if time_task and len(alias_symbols) != 1:
        raise DomainError(
            "replay_mapping_unavailable",
            "The frozen timing alias cannot be resolved to one evaluated listing",
            409,
        )
    instruments = [
        ReplayInstrument(
            instrument_id=key
            if context.mode.startswith("portable")
            else "target_symbol"
            if time_task and value in alias_symbols
            else value,
            listing_id=UUID(key),
            symbol=value,
            configured_evaluation_member=value in active
            if active is not None
            else None,
            configured_session_member=value in effective
            if effective is not None
            else None,
        )
        for key, value in sorted(mapping.items())
    ]
    if len({item.instrument_id for item in instruments}) != len(instruments):
        raise _invalid()
    market = ReplayMarketContext(
        dataset_version_id=context.dataset_id,
        dataset_sha256=context.dataset_sha256,
        universe_version_id=UUID(ref["id"]) if ref else None,
        universe_sha256=ref.get("sha256"),
        universe_state=state,
        universe_as_of=universe.get("as_of"),
        historical_membership_status=MEMBERSHIP_STATES.get(state, "not_collected"),
        membership_status="available" if active is not None else "not_collected",
        membership_reason=reason,
        timezone=context.timezone,
        window_start=context.start,
        window_end=context.end,
        result_trust=context.result_trust,
        ledger_origin=context.ledger_origin,
        warnings=[
            "Prices are retrospective frozen OHLCV, not proof of information available at the decision cutoff. Saved trace features/predicates alone describe recorded decision inputs; no indicator or strategy is reevaluated.",
            "Outside the configured evaluation universe does not mean outside a genuine historical market universe. Current snapshots and user-fixed baskets are not independent point-in-time evidence.",
            "Native same-process records are not an independently trusted ledger; portable execution records retain their verified trusted-engine provenance but internal policy logic may remain unsupported.",
        ],
    )
    return market, instruments, day


@_view
def replay_instruments(
    trace, settings, source_association, *, session_date=None, offset=0, limit=100
):
    _page(offset, limit)
    market, items, day = _market_inputs(trace, settings, session_date=session_date)
    return ReplayInstrumentPage(
        context=trace.context,
        market=market,
        source_association=source_association,
        session_date=day,
        items=items[offset : offset + limit],
        total=len(items),
        offset=offset,
        limit=limit,
    )


@_view
def replay_market(
    trace,
    settings,
    decision_id,
    *,
    instrument_id,
    start=None,
    end=None,
    offset=0,
    limit=100,
):
    _page(offset, limit)
    event = _event(trace, decision_id) if trace.reader is not None else None
    market, instruments, _ = _market_inputs(
        trace,
        settings,
        session_date=event["start"] if event else None,
        allow_outside=True,
    )
    start, end = start or market.window_start, end or market.window_end
    if not market.window_start <= start < end <= market.window_end:
        raise DomainError(
            "invalid_replay_query",
            "Prices require a bounded [start, end) inside the leaf evaluation",
            422,
        )
    selected = next(
        (item for item in instruments if item.instrument_id == instrument_id), None
    )
    common = {
        "context": trace.context,
        "market": market,
        "decision_id": decision_id,
        "session_date": event["start"] if event else None,
        "instrument_id": instrument_id,
        "instrument": selected,
        "information_cutoff": None,
        "decision_time": None,
        "offset": offset,
        "limit": limit,
        "start": start,
        "end": end,
    }
    if event is None:
        return ReplayMarketPage(
            **common,
            status="not_collected",
            missing_reason=trace.context.missing_reason,
            bars=None,
            total=None,
        )
    saved = _query(trace, "events", **event, limit=2)["items"][0]
    common.update(
        information_cutoff=saved["information_cutoff"],
        decision_time=saved["decision_time"],
    )
    if (
        selected is None
        or selected.configured_evaluation_member is False
        or selected.configured_session_member is False
    ):
        known = market.membership_status == "available"
        return ReplayMarketPage(
            **common,
            status="outside_configured_universe" if known else "unsupported",
            missing_reason="This instrument is not in the frozen configured evaluation universe at this session; no claim about genuine point-in-time market membership is made."
            if known
            else "The instrument cannot be mapped to the frozen dataset; no listing or universe membership is guessed.",
            bars=None,
            total=None,
        )
    if result_refs := trace.result.dataset.manifest.get("preview_files", {}).get(
        str(selected.listing_id)
    ):
        if not isinstance(result_refs, list):
            raise _invalid()
    elif result_refs is None:
        return ReplayMarketPage(
            **common,
            status="not_collected",
            missing_reason="The immutable dataset has no retained OHLCV preview for this listing. Prices are not downloaded or inferred from fills.",
            bars=None,
            total=None,
        )
    bars = frozen_price_bars(
        trace.result, settings, listing_id=selected.listing_id, start=start, end=end
    )
    return ReplayMarketPage(
        **common,
        status="available" if bars else "no_rows",
        missing_reason=None
        if bars
        else "The retained frozen price files contain no rows in this requested range; no candle is interpolated.",
        bars=bars[offset : offset + limit],
        total=len(bars),
    )


def _tables(bundle):
    portable = bundle.get("evaluation_mode") == "portable"
    output = {}
    for kind, section, key in (
        ("logical_order", "logical_orders", "logical_order_id"),
        ("child_order", "orders", "order_id" if portable else "id"),
        ("fill", "fills", "fill_id" if portable else "id"),
    ):
        if kind == "logical_order" and not portable:
            output[kind] = {}
            continue
        rows = bundle[section]
        if not portable:
            names = [column["name"] for column in rows["columns"]]
            rows = [dict(zip(names, row, strict=True)) for row in rows["rows"]]
        indexed = {}
        for index, row in enumerate(rows):
            identifier = str(row[key])
            if identifier in indexed:
                raise _invalid()
            indexed[identifier] = (index, row)
        output[kind] = indexed
    return portable, output


def _linked_rows(trace, stage, link, portable, tables, fills_by_order, unpaired):
    def row(kind, identifier, basis, missing=None):
        pair = tables[kind].get(identifier)
        if pair is not None:
            record = pair[1]
            actual_instrument = str(
                record["listing_id"] if portable else record["symbol"]
            )
            if actual_instrument != link.instrument_id:
                raise _invalid()
            if (
                portable
                and kind != "logical_order"
                and record["logical_order_id"] != link.logical_order_id
            ):
                raise _invalid()
            if kind == "fill" and str(record["order_id"]) not in link.child_order_ids:
                raise _invalid()
        return ExecutionEvidenceRow(
            stage_id=stage,
            logical_order_id=link.logical_order_id,
            instrument_id=link.instrument_id,
            record_kind=kind,
            record_id=identifier,
            record_index=pair[0] if pair else None,
            status="available"
            if pair
            else "not_collected"
            if missing
            else "no_matching_record",
            association_basis="unpaired_native_execution" if unpaired else basis,
            record=pair[1] if pair else None,
            missing_reason=None
            if pair
            else missing
            or "The explicit linked ID has no matching row in this exact ResultBundle. Absence is not a no-intent or no-fill claim.",
            link=link,
        )

    logical = tables["logical_order"].get(link.logical_order_id)
    if logical and not set(link.child_order_ids) <= set(logical[1]["native_order_ids"]):
        raise _invalid()
    yield row(
        "logical_order",
        link.logical_order_id,
        "logical_order_id",
        None
        if portable
        else "Native ResultBundle does not retain an independent logical-order table. The saved OrderLink is the available association, not a reconstructed intent record.",
    )
    for identifier in dict.fromkeys(link.child_order_ids):
        yield row("child_order", identifier, "child_order_id")
    ids = dict.fromkeys(link.fill_ids)
    for identifier in link.child_order_ids:
        for fill in fills_by_order.get(identifier, ()):
            ids.setdefault(fill, None)
    for identifier in ids:
        yield row(
            "fill",
            identifier,
            "fill_id" if identifier in link.fill_ids else "child_order_id",
        )


@_view
def replay_execution(
    trace,
    decision_id,
    *,
    instrument_id=None,
    stage_id=None,
    logical_order_id=None,
    offset=0,
    limit=100,
):
    _page(offset, limit)
    common = {
        "context": trace.context,
        "decision_id": decision_id,
        "instrument_id": instrument_id,
        "stage_id": stage_id,
        "logical_order_id": logical_order_id,
        "offset": offset,
        "limit": limit,
    }
    warnings = [
        "Rows are retrospective persisted execution evidence joined only through explicit logical/child/fill IDs. Later fills do not become information known at the earlier decision cutoff.",
        "Saved link status and final economic order status are distinct observations. No fill row never proves no order intent. Intermediate native order transitions may be uncollected.",
        "Unpaired native execution events have actual engine order IDs but no recovered logical strategy origin or market callback. Native same-process evidence is not an independently trusted ledger.",
    ]
    if trace.reader is None:
        return ReplayExecutionPage(
            **common,
            status="not_collected",
            missing_reason=trace.context.missing_reason,
            items=None,
            total=None,
            warnings=warnings,
        )
    event = _event(trace, decision_id)
    saved = _query(trace, "events", **event, limit=2)["items"][0]
    stages = _query(trace, "stages", **event, stage_id=stage_id, limit=100)
    if stages["total"] > 100:
        raise _invalid()
    portable, tables = _tables(trace.result.bundle)
    fills_by_order = {}
    for identifier, (_, row) in tables["fill"].items():
        fills_by_order.setdefault(str(row["order_id"]), []).append(identifier)
    items, total = [], 0
    for value in stages["items"]:
        for raw in value["payload"]["order_links"]:
            link = OrderLink.model_validate(raw)
            if (instrument_id is not None and link.instrument_id != instrument_id) or (
                logical_order_id is not None
                and link.logical_order_id != logical_order_id
            ):
                continue
            unpaired = (
                not portable
                and saved["data_event_id"].startswith("native_order:")
                and saved["callback_phase"] == "native.execution_result"
            )
            for row in _linked_rows(
                trace,
                value["stage_id"],
                link,
                portable,
                tables,
                fills_by_order,
                unpaired,
            ):
                if offset <= total < offset + limit:
                    items.append(row)
                total += 1
    return ReplayExecutionPage(
        **common,
        status="available" if total else "no_linked_records",
        missing_reason=None
        if total
        else "No explicit execution links were retained for this selection. This does not prove no buy/sell intent or no execution.",
        items=items,
        total=total,
        warnings=warnings,
    )
