"""Bounded projections of verified portable tables, never policy accounting."""

from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.services import DomainError

TIME_FIELDS = {
    "account_series": "native_at",
    "positions": "native_at",
    "orders": "native_created_at",
    "fills": "native_at",
    "closed_trades": "exit_native_at",
    "decision_trace": "decision_time",
    "logical_orders": "decision_time",
}
SORT_FIELDS = {
    "positions": {
        "native_at",
        "listing_id",
        "quantity",
        "close",
        "market_value",
        "unrealized_pnl",
    },
    "orders": {
        "native_created_at",
        "native_updated_at",
        "listing_id",
        "side",
        "quantity",
        "filled_quantity",
        "commission",
        "status",
    },
    "fills": {"native_at", "listing_id", "side", "quantity", "price", "commission"},
    "closed_trades": {
        "entry_native_at",
        "exit_native_at",
        "listing_id",
        "quantity",
        "pnl",
        "net_pnl",
        "commission",
    },
}


def query_portable_table(
    bundle,
    section,
    offset=0,
    limit=100,
    *,
    start=None,
    end=None,
    listing_id=None,
    sort_by=None,
    sort_direction="asc",
    side=None,
):
    allowed = SORT_FIELDS.get(section, set())
    if (
        section not in {*TIME_FIELDS, "native_metrics"}
        or type(offset) is not int
        or not 0 <= offset <= 100000
        or type(limit) is not int
        or not 1 <= limit <= 500
        or (sort_by is not None and sort_by not in allowed)
        or sort_direction not in {"asc", "desc"}
        or (sort_by is None and sort_direction != "asc")
        or (start is not None and end is not None and start >= end)
        or ((start is not None or end is not None) and section not in TIME_FIELDS)
        or (listing_id is not None and section not in {*SORT_FIELDS, "logical_orders"})
        or (
            side is not None
            and (
                section not in {"fills", "orders", "logical_orders"}
                or side not in {"buy", "sell"}
            )
        )
    ):
        raise DomainError(
            "invalid_result_query",
            "Unsupported portable table filter/sort or page",
            422,
        )
    zone = ZoneInfo(bundle["manifest"]["timezone"])
    indexed = []
    for index, row in enumerate(bundle[section]):
        if start is not None or end is not None:
            day = (
                datetime.fromisoformat(row[TIME_FIELDS[section]])
                .astimezone(zone)
                .date()
            )
            if (start is not None and day < start) or (end is not None and day >= end):
                continue
        if listing_id is not None and row["listing_id"] != str(listing_id):
            continue
        if side is not None and row["side"] != side:
            continue
        indexed.append((index, row))
    if sort_by is not None:
        present = [pair for pair in indexed if pair[1][sort_by] is not None]
        missing = [pair for pair in indexed if pair[1][sort_by] is None]

        def key(pair):
            value = pair[1][sort_by]
            return datetime.fromisoformat(value) if sort_by.endswith("_at") else value

        indexed = sorted(present, key=key, reverse=sort_direction == "desc") + missing
    page = indexed[offset : offset + limit]
    return {
        "rows": [row for _, row in page],
        "row_indices": [index for index, _ in page],
        "count": len(indexed),
        "total_rows": len(bundle[section]),
        "sortable_columns": sorted(allowed),
        "sort_by": sort_by,
        "sort_direction": sort_direction,
        "side": side,
    }
