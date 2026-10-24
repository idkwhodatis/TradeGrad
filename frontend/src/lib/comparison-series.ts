import type { ComparisonRecord } from "@/client/types.gen";
import { objectValue } from "./comparison-evidence";
const numericFields: Record<string, string[]> = {
  candidates: ["score", "rank"],
  features: ["value"],
  predicates: [],
  targets: ["weight", "target_weight"],
  orders: ["quantity", "filled_quantity", "price", "limit_price"],
  fills: ["quantity", "price", "commission"],
  positions: [
    "quantity",
    "short_quantity",
    "long_shares",
    "short_shares",
    "market_value",
    "unrealized_pnl",
  ],
  account: ["equity", "cash", "observed_equity_delta", "observed_cash_delta"],
  turnover: ["value"],
  holding_duration: ["duration_seconds"],
  events: [],
};
export type ComparisonPoint = {
  label: string;
  value: number | null;
  unit: string;
  side: "left" | "right";
  rowId: string;
  pairId: string | null;
  ordinal: number | null;
  rightCensored: boolean;
};
function savedItems(row: ComparisonRecord, side: "left" | "right") {
  const body = row[side];
  if (!body) return [];
  if (Array.isArray(body.items))
    return body.items
      .map(objectValue)
      .filter((value) => value !== null)
      .map((item) => ({
        ordinal: typeof item.ordinal === "number" ? item.ordinal : null,
        record: objectValue(item.record),
      }));
  return [{ ordinal: null, record: body }];
}
export function comparisonNumericFields(rows: ComparisonRecord[]) {
  const sections = [...new Set(rows.map((row) => row.section))];
  if (sections.length !== 1) return [];
  return (numericFields[sections[0]] || []).filter((field) =>
    rows.some((row) =>
      (["left", "right"] as const).some((side) =>
        savedItems(row, side).some(
          ({ record }) => record && Object.hasOwn(record, field),
        ),
      ),
    ),
  );
}
/** Preserve every exact per-side ordinal: numeric plotting is not cross-run row pairing. */
export function comparisonNumericPoints(
  rows: ComparisonRecord[],
  field: string,
): ComparisonPoint[] {
  if (!comparisonNumericFields(rows).includes(field)) return [];
  return rows.flatMap((row) =>
    (["left", "right"] as const).flatMap((side) =>
      savedItems(row, side).map(({ ordinal, record }) => {
        const value = record?.[field];
        const unit =
          typeof record?.unit === "string"
            ? record.unit
            : field === "duration_seconds"
              ? "elapsed seconds"
              : ["weight", "target_weight"].includes(field)
                ? "fraction of equity"
                : field === "rank"
                  ? "rank"
                  : `${side}: saved units not declared; see configuration`;
        return {
          label: `${row.session_date || "undated"} · ${row.instrument_id || "portfolio"} · ${side} #${ordinal ?? "window"} · ${row.row_id.slice(0, 8)}`,
          value:
            typeof value === "number" && Number.isFinite(value) ? value : null,
          unit,
          side,
          rowId: row.row_id,
          pairId: row.event_pair_id ?? null,
          ordinal,
          rightCensored: record?.right_censored === true,
        };
      }),
    ),
  );
}
