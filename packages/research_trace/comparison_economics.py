"""Observed economic bundles and explicit ID graphs, never strategy reruns.

Local IDs establish associations inside one admitted result. Cross-run equality
uses exact documented field projections and graph topology, not IDs, dates alone,
or a global recursive 'drop every id' transformation.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

from .comparison_contracts import ComparisonError
from .comparison_semantics import group_record
from .contracts import OrderLink, canonical_bytes

# The only identity exclusions are schema-specific local row/edge identifiers.
# Every other persisted economic field remains part of the semantic observation.
_ID_FIELDS = {
    "native": {"orders": {"id"}, "fills": {"id", "order_id"}},
    "portable": {
        "orders": {"order_id", "logical_order_id"},
        "fills": {"fill_id", "order_id", "logical_order_id"},
    },
}


class EconomicGraph(dict):
    """Bounded explicit local edges; detail capture is not a memory exemption."""

    def __init__(self, limits, control):
        super().__init__()
        self.limits, self.control = limits, control
        self.edges = self.size_bytes = 0


def register_links(graph, event, stages, pair_id, *, paired):
    for stage, value in stages.items():
        for ordinal, raw in enumerate(value["order_links"]):
            link = OrderLink.model_validate(raw)
            association = {
                "decision_id": event["decision_id"],
                "stage_id": stage,
                "link_ordinal": ordinal,
                "logical_order_id": link.logical_order_id,
                "event_pair_id": pair_id,
                "source_event_paired": paired,
                "data_event_id": event["data_event_id"],
                "callback_phase": event["callback_phase"],
                "event_seq": event["event_seq"],
                "scope": event["scope"],
                "instrument_id": link.instrument_id,
                "link": link.model_dump(mode="json"),
            }
            size = len(canonical_bytes(association))
            for child in link.child_order_ids:
                graph.control.check()
                graph.edges += 1
                graph.size_bytes += size
                if (
                    graph.edges > graph.limits.max_graph_edges
                    or graph.size_bytes > graph.limits.max_graph_bytes
                ):
                    raise ComparisonError(
                        "comparison_capacity",
                        "Explicit execution associations exceed the fixed graph row/byte budget",
                    )
                graph.setdefault(child, []).append(association)


def _table(bundle, section):
    value = bundle.get(section)
    if value is None:
        return None
    if bundle.get("evaluation_mode") == "portable":
        if not isinstance(value, list) or any(
            not isinstance(row, dict) for row in value
        ):
            raise ComparisonError()
        return value
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("columns"), list)
        or not isinstance(value.get("rows"), list)
    ):
        raise ComparisonError()
    names = [column["name"] for column in value["columns"]]
    if len(names) != len(set(names)):
        raise ComparisonError()
    return [dict(zip(names, row, strict=True)) for row in value["rows"]]


def _instrument(row, portable):
    return str(row["listing_id"] if portable else row["symbol"])


def _time(row, section, portable):
    if portable:
        return (
            row.get("native_created_at")
            if section == "orders"
            else row.get("native_at")
        )
    return (
        row.get("created_at")
        if section == "orders"
        else row.get("timestamp")
        if section == "fills"
        else row.get("date")
    )


def _moment(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _day(value):
    moment = _moment(value)
    return moment.date().isoformat() if moment else None


def _semantic(row, section, portable):
    omitted = _ID_FIELDS["portable" if portable else "native"].get(section, set())
    return {key: value for key, value in row.items() if key not in omitted}


def _indexed(rows, key):
    output = {}
    for ordinal, row in enumerate(rows or []):
        identifier = row.get(key)
        if not isinstance(identifier, str) or not identifier or identifier in output:
            raise ComparisonError()
        output[identifier] = (ordinal, row)
    return output


def _economic_input(value, trace_graph):
    bundle = value.economic_bundle
    portable = bundle.get("evaluation_mode") == "portable"
    sections = {
        name: _table(bundle, name)
        for name in ("orders", "fills", "positions", "closed_trades")
    }
    orders = _indexed(sections["orders"], "order_id" if portable else "id")
    fills = _indexed(sections["fills"], "fill_id" if portable else "id")
    logical = (
        _indexed(bundle.get("logical_orders"), "logical_order_id") if portable else {}
    )
    by_order = defaultdict(list)
    for identifier, (_, fill) in fills.items():
        child = str(fill.get("order_id"))
        if child not in orders or _instrument(fill, portable) != _instrument(
            orders[child][1], portable
        ):
            raise ComparisonError()
        if portable and fill.get("logical_order_id") != orders[child][1].get(
            "logical_order_id"
        ):
            raise ComparisonError()
        by_order[child].append(identifier)
    # Validate every retained explicit edge against the actual local bundle.
    # Missing native logical tables remain unavailable, never fabricated.
    for child, references in trace_graph.items():
        for association in references:
            link = association["link"]
            for other in link["child_order_ids"]:
                if (
                    other in orders
                    and _instrument(orders[other][1], portable) != link["instrument_id"]
                ):
                    raise ComparisonError()
                if (
                    portable
                    and other in orders
                    and orders[other][1]["logical_order_id"] != link["logical_order_id"]
                ):
                    raise ComparisonError()
            for identifier in link["fill_ids"]:
                if identifier in fills:
                    fill = fills[identifier][1]
                    if (
                        str(fill["order_id"]) not in link["child_order_ids"]
                        or _instrument(fill, portable) != link["instrument_id"]
                    ):
                        raise ComparisonError()
            if portable and link["logical_order_id"] in logical:
                parent = logical[link["logical_order_id"]][1]
                if not set(link["child_order_ids"]) <= set(parent["native_order_ids"]):
                    raise ComparisonError()
    return {
        "portable": portable,
        "sections": sections,
        "orders": orders,
        "fills": fills,
        "logical": logical,
        "by_order": by_order,
        "trace_graph": trace_graph,
        "account": _account(bundle, portable),
        "control": trace_graph.control,
        "limits": trace_graph.limits,
    }


def _order_graph(data, identifier):
    portable, orders, fills = data["portable"], data["orders"], data["fills"]
    references = data["trace_graph"].get(identifier, [])
    actual = orders[identifier][1]
    graph_semantics = []
    missing = not references
    graph_bytes = 0
    for reference in references:
        data["control"].check()
        link = reference["link"]
        if not reference["source_event_paired"]:
            missing = True
            # Generic native IDs do not establish a cross-run event association.
            continue
        children = []
        for child in link["child_order_ids"]:
            if child not in orders:
                missing = True
                children.append({"availability": "no_matching_record"})
            else:
                children.append(
                    {
                        "order": _semantic(orders[child][1], "orders", portable),
                        "fills": sorted(
                            [
                                _semantic(fills[fill][1], "fills", portable)
                                for fill in data["by_order"].get(child, [])
                            ],
                            key=canonical_bytes,
                        ),
                    }
                )
        # Explicit fill-edge topology is retained independently of derived
        # child->fill edges, including multiplicity and unresolved references.
        linked_fills = []
        for fill in link["fill_ids"]:
            if fill not in fills:
                missing = True
                linked_fills.append({"availability": "no_matching_record"})
            else:
                linked_fills.append(_semantic(fills[fill][1], "fills", portable))
        graph_bytes += (
            len(canonical_bytes(children)) + len(canonical_bytes(linked_fills)) + 4096
        )
        if graph_bytes > data["limits"].max_record_bytes:
            raise ComparisonError(
                "comparison_capacity",
                "One explicit economic graph exceeds the fixed record byte budget",
            )
        graph_semantics.append(
            {
                "source_event": [
                    reference[key]
                    for key in ("data_event_id", "callback_phase", "scope", "event_seq")
                ],
                "stage_id": reference["stage_id"],
                "instrument_id": link["instrument_id"],
                "status": link["status"],
                "submitted_at": link["submitted_at"],
                "fill_times": sorted(link["fill_times"]),
                "rejection_reason": link["rejection_reason"],
                "children": sorted(children, key=canonical_bytes),
                "explicit_fills": sorted(linked_fills, key=canonical_bytes),
            }
        )
    if portable:
        logical_id = actual["logical_order_id"]
        parent = data["logical"].get(logical_id)
        if parent is None:
            missing = True
        else:
            parent = parent[1]
            children = []
            for child in parent["native_order_ids"]:
                if child not in orders:
                    raise ComparisonError()
                children.append(
                    {
                        "order": _semantic(orders[child][1], "orders", portable),
                        "fills": sorted(
                            [
                                _semantic(fills[fill][1], "fills", portable)
                                for fill in data["by_order"].get(child, [])
                            ],
                            key=canonical_bytes,
                        ),
                    }
                )
            graph_semantics.append(
                {
                    "logical_order": {
                        key: val
                        for key, val in parent.items()
                        if key
                        not in {"logical_order_id", "signal_id", "native_order_ids"}
                    },
                    "children": sorted(children, key=canonical_bytes),
                }
            )
    return sorted(graph_semantics, key=canonical_bytes), references, missing


def _economic_rows(data, section):
    rows = data["sections"][section]
    if rows is None:
        return None
    portable = data["portable"]
    groups = defaultdict(list)
    total_bytes = 0
    for ordinal, record in enumerate(rows):
        data["control"].check()
        instrument = _instrument(record, portable)
        moment = _time(record, section, portable)
        key = (
            instrument,
            moment,
            record.get("side") if section in {"orders", "fills"} else None,
        )
        semantic = _semantic(record, section, portable)
        item = {"ordinal": ordinal, "record": record, "semantic": {"record": semantic}}
        if section in {"orders", "fills"}:
            identifier = str(
                record["order_id"] if portable or section == "fills" else record["id"]
            )
            graph, associations, missing = _order_graph(data, identifier)
            item["associations"] = associations
            item["graph_status"] = "incomplete_coverage" if missing else "explicit"
            item["logical_order_table_status"] = (
                "available" if portable else "not_collected"
            )
            item["semantic"]["order_graph"] = graph
            item["semantic"]["graph_availability"] = (
                "incomplete_coverage" if missing else "explicit"
            )
            if section == "fills":
                item["semantic"]["parent_order"] = _semantic(
                    data["orders"][identifier][1], "orders", portable
                )
        total_bytes += len(canonical_bytes(item))
        if total_bytes > data["limits"].max_event_bytes:
            raise ComparisonError(
                "comparison_capacity",
                "Economic semantic projection exceeds its fixed materialization budget",
            )
        groups[key].append(item)
    return groups


def _account(bundle, portable):
    if portable:
        rows = bundle.get("account_series")
        if rows is None:
            return None
        return [
            {
                "timestamp": row["native_at"],
                "equity": row["equity"],
                "cash": row["cash"],
                "record": row,
            }
            for row in rows
        ]
    equity, cash = bundle.get("equity"), bundle.get("cash")
    if equity is None and cash is None:
        return None

    def values(table):
        if table is None:
            return {}
        rows = table["rows"]
        output = {}
        for row in rows:
            if len(row) != 2 or row[0] in output:
                raise ComparisonError()
            output[row[0]] = row[1]
        return output

    equities, cashes = values(equity), values(cash)
    return [
        {
            "timestamp": moment,
            "equity": equities.get(moment),
            "cash": cashes.get(moment),
            "record": {
                "timestamp": moment,
                "equity": equities.get(moment),
                "cash": cashes.get(moment),
            },
        }
        for moment in sorted(equities.keys() | cashes.keys())
    ]


def _account_rows(data):
    if data["account"] is None:
        return None
    groups, previous = {}, None
    for ordinal, row in enumerate(data["account"]):

        def delta(field, row=row, previous=previous):
            return (
                row[field] - previous[field]
                if previous is not None
                and _finite(row[field])
                and _finite(previous[field])
                else None
            )

        record = {
            **row["record"],
            "observed_equity_delta": delta("equity"),
            "observed_cash_delta": delta("cash"),
            "delta_baseline_timestamp": previous["timestamp"] if previous else None,
        }
        key = (None, row["timestamp"], None)
        if key in groups:
            raise ComparisonError()
        groups[key] = [
            {
                "ordinal": ordinal,
                "record": record,
                "semantic": record,
                "account_status": "available"
                if row["equity"] is not None and row["cash"] is not None
                else "incomplete_coverage",
            }
        ]
        previous = row
    return groups


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _turnover(value, data):
    fills, account = data["sections"]["fills"], data["account"]
    reason = None
    numerator = denominator = result = None
    if fills is None or account is None or not account:
        reason = "Actual fills or observed account equity are unavailable."
    elif any(
        not _finite(row.get("quantity"))
        or not _finite(row.get("price"))
        or row["price"] <= 0
        for row in fills
    ):
        reason = "A fill has unavailable/nonfinite quantity or nonpositive price."
    elif any(not _finite(row["equity"]) or row["equity"] <= 0 for row in account):
        reason = "The observed equity denominator includes missing, nonfinite or nonpositive values; no observations were silently discarded."
    else:
        numerator = math.fsum(abs(row["quantity"] * row["price"]) for row in fills)
        denominator = math.fsum(row["equity"] for row in account) / len(account)
        if (
            not math.isfinite(numerator)
            or not math.isfinite(denominator)
            or denominator <= 0
        ):
            numerator = denominator = None
            reason = (
                "The gross notional or mean observed equity is not finite and positive."
            )
        else:
            result = numerator / denominator
            if not math.isfinite(result):
                result = None
                reason = "The turnover ratio is nonfinite."
    return {
        "title": "Gross observed-window turnover",
        "value": result,
        "unit": "ratio",
        "gross_filled_notional": numerator,
        "denominator": denominator,
        "denominator_definition": "Arithmetic mean of all positive observed account-equity snapshots; any invalid/nonpositive snapshot makes the measure unavailable.",
        "formula": "sum(abs(actual_fill_quantity * actual_fill_price)) / mean(observed_account_equity)",
        "window": value.window.model_dump(mode="json"),
        "account_observations": len(account) if account is not None else None,
        "fill_observations": len(fills) if fills is not None else None,
        "session_completeness": "not_certified",
        "annualized": False,
        "missing_reason": reason,
    }


def _holdings(data):
    groups = defaultdict(list)
    trades, positions, account = (
        data["sections"]["closed_trades"],
        data["sections"]["positions"],
        data["account"],
    )
    portable = data["portable"]
    if trades is None and positions is None:
        return None
    for ordinal, trade in enumerate(trades or []):
        instrument = _instrument(trade, portable)
        entry = trade.get("entry_native_at" if portable else "entry_time")
        end = trade.get("exit_native_at" if portable else "exit_time")
        start_time, end_time = _moment(entry), _moment(end)
        duration = (
            (end_time - start_time).total_seconds()
            if start_time and end_time and end_time >= start_time
            else None
        )
        record = {
            "kind": "closed_trade",
            "entry_time": entry,
            "exit_time": end,
            "duration_seconds": duration,
            "right_censored": False,
            "quantity": trade.get("quantity"),
            "instrument_id": instrument,
            "missing_reason": None
            if duration is not None
            else "A true saved entry/exit timestamp is unavailable; fill or session dates were not substituted.",
            "closed_trade": trade,
        }
        groups[(instrument, entry, "closed_trade")].append(
            {
                "ordinal": ordinal,
                "record": record,
                "semantic": record,
                "duration_status": "available"
                if duration is not None
                else "incomplete_coverage",
            }
        )
    # Only a real terminal snapshot establishes that a position is still open.
    # A sparse earlier position row is not silently forward-filled to the end.
    terminal = account[-1]["timestamp"] if account else None
    for ordinal, position in enumerate(positions or []):
        moment = _time(position, "positions", portable)
        if _moment(moment) is None or _moment(moment) != _moment(terminal):
            continue
        long = position.get("quantity" if portable else "long_shares")
        short = position.get("short_quantity" if portable else "short_shares")
        if not _finite(long) or not _finite(short) or (long == 0 and short == 0):
            continue
        instrument = _instrument(position, portable)
        record = {
            "kind": "open_position",
            "instrument_id": instrument,
            "quantity": long,
            "short_quantity": short,
            "entry_time": None,
            "exit_time": None,
            "observed_until": terminal,
            "duration_seconds": None,
            "right_censored": True,
            "missing_reason": "This actual terminal position is still open. A lot-specific entry timestamp is unavailable, so its censored duration remains null, never zero.",
        }
        groups[(instrument, terminal, "open_position")].append(
            {
                "ordinal": ordinal,
                "record": record,
                "semantic": record,
                "duration_status": "incomplete_coverage",
            }
        )
    return groups


def compare_economics(left, right, graphs, append, control, *, common_namespace):
    inputs = [left, right]
    data = [
        _economic_input(value, graph)
        for value, graph in zip(inputs, graphs, strict=True)
    ]
    for section in ("orders", "fills", "positions", "account", "holding_duration"):
        control.check()
        groups = [
            (
                _account_rows(item)
                if section == "account"
                else _holdings(item)
                if section == "holding_duration"
                else _economic_rows(item, section)
            )
            for item in data
        ]
        # No table is not an observed empty table. Empty order/fill tables are
        # explicit observations and can establish non-trading economic history.
        for side, group in enumerate(groups):
            if group == {} and section in {"orders", "fills"}:
                group[(None, None, "observed_empty_table")] = [
                    {
                        "ordinal": None,
                        "record": {"observed_rows": 0},
                        "semantic": {"observed_rows": 0},
                    }
                ]
        keys = (groups[0] or {}).keys() | (groups[1] or {}).keys()
        for key in sorted(keys, key=canonical_bytes):
            control.check()
            a, b = (group.get(key, []) if group is not None else [] for group in groups)

            def absence(side, groups=groups, section=section):
                if groups[side] is None:
                    return "not_collected"
                # Account/position gaps at a timestamp never prove zero state.
                if section in {"positions", "account", "holding_duration"}:
                    return "incomplete_coverage"
                return "observed_absence"

            # A timestamp/instrument equality is an observation coordinate, not
            # strategy-event identity. Different namespaces are never aligned.
            combinations = (
                [(a, b, None)]
                if common_namespace
                else [(a, [], "left"), ([], b, "right")]
            )
            for la, rb, side in combinations:
                if not la and not rb:
                    continue
                # Missing graph evidence is not a changed execution mechanism.
                # Compare retained economic fields, while leaving graph equality
                # unknown and preserving all raw associations in the output.
                if section in {"orders", "fills"} and any(
                    item.get("graph_status") == "incomplete_coverage"
                    for item in [*la, *rb]
                ):

                    def observed_only(items):
                        return [
                            {
                                **item,
                                "semantic": {
                                    name: value
                                    for name, value in item["semantic"].items()
                                    if name not in {"order_graph", "graph_availability"}
                                },
                            }
                            for item in items
                        ]

                    la, rb = observed_only(la), observed_only(rb)
                row = group_record(
                    section,
                    [key, side],
                    la,
                    rb,
                    left_state=absence(0) if common_namespace else "unpaired",
                    right_state=absence(1) if common_namespace else "unpaired",
                    session_date=_day(key[1]),
                    instrument_id=key[0],
                    control=control,
                    graph_semantics=section in {"orders", "fills"},
                    basis="observed_timestamp"
                    if section in {"positions", "account"}
                    else "semantic_multiset",
                )
                associations = []
                for label, items in (("left", la), ("right", rb)):
                    for item in items:
                        for association in item.get("associations", []):
                            associations.append(
                                {
                                    "side": label,
                                    **{
                                        key: association[key]
                                        for key in (
                                            "event_pair_id",
                                            "decision_id",
                                            "stage_id",
                                            "logical_order_id",
                                            "instrument_id",
                                            "link_ordinal",
                                            "source_event_paired",
                                        )
                                    },
                                    "child_order_ids": association["link"][
                                        "child_order_ids"
                                    ],
                                    "fill_ids": association["link"]["fill_ids"],
                                }
                            )
                    if any(
                        item.get("graph_status") == "incomplete_coverage"
                        or item.get("account_status") == "incomplete_coverage"
                        or item.get("duration_status") == "incomplete_coverage"
                        for item in items
                    ):
                        row[f"{label}_state"] = "incomplete_coverage"
                unique = {canonical_bytes(item): item for item in associations}
                row["associations"] = list(unique.values())
                paired_ids = {
                    item["event_pair_id"]
                    for item in associations
                    if item["source_event_paired"]
                }
                if len(paired_ids) == 1 and all(
                    item["source_event_paired"] for item in associations
                ):
                    row["event_pair_id"] = next(iter(paired_ids))
                append(row)
    values = [_turnover(value, item) for value, item in zip(inputs, data, strict=True)]
    # An unavailable scalar remains null and is not an equal observed zero.
    row = group_record(
        "turnover",
        "gross_observed_window",
        [{"ordinal": 0, "record": values[0], "semantic": values[0]}],
        [{"ordinal": 0, "record": values[1], "semantic": values[1]}],
        left_state="observed",
        right_state="observed",
        basis="window_metric",
        control=control,
    )
    if any(value["value"] is None for value in values):
        row["status"] = "unavailable"
        row["left_state"] = (
            "observed" if values[0]["value"] is not None else "not_collected"
        )
        row["right_state"] = (
            "observed" if values[1]["value"] is not None else "not_collected"
        )
        row["missing_reason"] = (
            "At least one observed-window turnover is unavailable; null is not zero or equality."
        )
    append(row)
