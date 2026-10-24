import { describe, expect, it } from "vitest";
import {
  comparisonNumericFields,
  comparisonNumericPoints,
} from "../../src/lib/comparison-series";
import { comparisonNumericChartOptions } from "../../src/features/research-workbench/comparison-chart";
import { eventRow } from "./comparison-fixtures";
describe("bounded actual numeric evidence, not fabricated comparison metrics", () => {
  it("shows observed-window turnover ratio, not annualized or inferred profit", () => {
    const rows = [
      {
        ...eventRow(),
        section: "turnover",
        left: { value: 0, unit: "ratio", annualized: false },
        right: { value: 0.7, unit: "ratio", annualized: false },
      },
    ];
    expect(comparisonNumericFields(rows)).toEqual(["value"]);
    expect(
      comparisonNumericPoints(rows, "value").map((point) => point.value),
    ).toEqual([0, 0.7]);
    expect(comparisonNumericChartOptions(rows, "value").yAxis).toHaveLength(1);
  });
  it("retains duplicate target ordinals separately, not index-based pairings", () => {
    const rows = [
      {
        ...eventRow(),
        section: "targets",
        left: {
          items: [
            { ordinal: 0, record: { weight: 0 } },
            { ordinal: 4, record: { weight: 0.2 } },
          ],
        },
        right: { items: [{ ordinal: 8, record: { weight: 0.4 } }] },
      },
    ];
    const points = comparisonNumericPoints(rows, "weight");
    expect(
      points.map((point) => [point.side, point.ordinal, point.value]),
    ).toEqual([
      ["left", 0, 0],
      ["left", 4, 0.2],
      ["right", 8, 0.4],
    ]);
    expect(points.every((point) => point.pairId === "pair-1")).toBe(true);
  });
  it("keeps unknown right-censored holding durations as gaps, never zero or completed trades", () => {
    const rows = [
      {
        ...eventRow(),
        section: "holding_duration",
        left: {
          items: [
            {
              ordinal: 0,
              record: { duration_seconds: 86400, right_censored: false },
            },
          ],
        },
        right: {
          items: [
            {
              ordinal: 0,
              record: { duration_seconds: null, right_censored: true },
            },
          ],
        },
      },
    ];
    const points = comparisonNumericPoints(rows, "duration_seconds");
    expect(points.map((p) => p.value)).toEqual([86400, null]);
    expect(points[1].rightCensored).toBe(true);
  });
  it("does not coerce booleans, numeric strings, missing or nonfinite fields into numbers", () => {
    for (const value of [false, "0", null, undefined, NaN, Infinity]) {
      const rows = [
        {
          ...eventRow(),
          section: "features",
          left: { items: [{ ordinal: 2, record: { value, unit: "shares" } }] },
          right: null,
        },
      ];
      expect(comparisonNumericPoints(rows, "value")[0].value).toBeNull();
    }
  });
  it("separates undeclared/mismatched source units rather than normalizing currencies", () => {
    const rows = [
      {
        ...eventRow(),
        section: "account",
        left: { items: [{ ordinal: 0, record: { cash: 100 } }] },
        right: { items: [{ ordinal: 0, record: { cash: 100 } }] },
      },
    ];
    expect(
      new Set(comparisonNumericPoints(rows, "cash").map((p) => p.unit)).size,
    ).toBe(2);
    expect(comparisonNumericChartOptions(rows, "cash").yAxis).toHaveLength(2);
  });
  it("refuses unknown field/section combinations and any cross-section combined plot", () => {
    expect(comparisonNumericPoints([eventRow()], "price")).toEqual([]);
    expect(
      comparisonNumericFields([
        eventRow(),
        { ...eventRow(), section: "account" },
      ]),
    ).toEqual([]);
  });
});
