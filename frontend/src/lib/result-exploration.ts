import type { PriceReplay, ResultTable } from "@/client/types.gen";

export function fillNavigation(
  table: ResultTable,
  rowIndex: number,
  mapping: Record<string, unknown>,
) {
  const row = table.rows[rowIndex];
  const read = (column: string) =>
    row?.[table.columns.findIndex((item) => item.name === column)];
  const symbol = read("symbol");
  const listing =
    symbol === "target_symbol" && Object.keys(mapping).length === 1
      ? Object.keys(mapping)[0]
      : Object.entries(mapping).find(([, mapped]) => mapped === symbol)?.[0];
  const originalIndex = table.row_indices?.[rowIndex];
  return listing && typeof originalIndex === "number"
    ? {
        listing,
        id: `fill:${originalIndex}`,
        timestamp: String(read("timestamp")),
      }
    : null;
}

export function replaySelection(data: PriceReplay, id: string | null) {
  const index = data.markers.findIndex((marker) => marker.id === id);
  return {
    selected: index < 0 ? null : data.markers[index],
    previous: index > 0 ? data.markers[index - 1].id : null,
    next: index + 1 < data.markers.length ? data.markers[index + 1].id : null,
  };
}

export function localSession(timestamp: string, timezone: string) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(timestamp));
  return ["year", "month", "day"]
    .map((key) => parts.find((part) => part.type === key)?.value)
    .join("-");
}

export function navigationWindow(day: string, start: string, end: string) {
  const shift = (days: number) =>
    new Date(Date.parse(day + "T00:00:00Z") + days * 86400000)
      .toISOString()
      .slice(0, 10);
  return {
    start: shift(-15) < start ? start : shift(-15),
    end: shift(16) > end ? end : shift(16),
  };
}
