import { describe, expect, it } from "vitest";
import type { PriceReplay, ResultTable } from "../../src/client/types.gen";
import {
  fillNavigation,
  localSession,
  navigationWindow,
  replaySelection,
} from "../../src/lib/result-exploration";

const table = {
  columns: ["symbol", "timestamp", "price"].map((name) => ({
    name,
    dtype: "string",
    unit: "test",
  })),
  rows: [["target_symbol", "2024-02-05T10:00:00+08:00", 10.123456789]],
  row_indices: [83],
} as ResultTable;

describe("exact persisted execution navigation", () => {
  it("uses original server row index after filtered sorting, not visible index or price", () => {
    expect(fillNavigation(table, 0, { listingA: "INDEX" })).toEqual({
      listing: "listingA",
      id: "fill:83",
      timestamp: "2024-02-05T10:00:00+08:00",
    });
  });
  it("does not guess a listing for an ambiguous time alias", () => {
    expect(
      fillNavigation(table, 0, { listingA: "A", listingB: "B" }),
    ).toBeNull();
  });
  it("maps cross-sectional engine symbols through immutable listing identities", () => {
    expect(
      fillNavigation(
        { ...table, rows: [["BBB", "2024-02-05T10:00:00Z", 10]] },
        0,
        { first: "AAA", second: "BBB" },
      )?.listing,
    ).toBe("second");
  });
  it("does not fabricate a row identity when the server omits it", () => {
    expect(
      fillNavigation({ ...table, row_indices: [] }, 0, { listingA: "INDEX" }),
    ).toBeNull();
  });
  it("uses the actual engine timezone at a UTC-midnight boundary", () => {
    expect(localSession("2024-03-10T03:30:00Z", "America/New_York")).toBe(
      "2024-03-09",
    );
    expect(localSession("2024-03-10T03:30:00Z", "Asia/Shanghai")).toBe(
      "2024-03-10",
    );
  });
  it("focus window remains half-open inside the persisted evaluation", () => {
    expect(navigationWindow("2024-02-02", "2024-02-01", "2024-02-10")).toEqual({
      start: "2024-02-01",
      end: "2024-02-10",
    });
  });
  it("previous/next are actual returned fills, and unknown selection is explicit", () => {
    const data = {
      markers: [{ id: "fill:4" }, { id: "fill:9" }, { id: "fill:14" }],
    } as PriceReplay;
    expect(replaySelection(data, "fill:9")).toEqual({
      selected: data.markers[1],
      previous: "fill:4",
      next: "fill:14",
    });
    expect(replaySelection(data, "fill:404").selected).toBeNull();
    expect(replaySelection(data, "fill:4").previous).toBeNull();
    expect(replaySelection(data, "fill:14").next).toBeNull();
  });
});
