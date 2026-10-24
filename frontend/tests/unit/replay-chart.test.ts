import { describe, expect, it } from "vitest";
import { init } from "echarts/core";
import { replayChartOptions } from "../../src/features/research-workbench/replay-chart";
import { detail, ids } from "./workbench-fixtures";
const props = {
  bars: [
    {
      listing_id: ids.strategy,
      session_date: "2024-01-02",
      open: 5,
      high: 7,
      low: 4,
      close: 6,
      volume: 0,
      volume_unit: "shares",
      currency: "CNY",
      flags: [],
    },
  ],
  features: detail().features.items,
  instrument: "B",
  cutoff: "2024-01-02T07:00:00Z",
  decisionTime: "2024-01-02T07:01:00Z",
  timezone: "Asia/Shanghai",
};
describe("saved-evidence chart projection", () => {
  it("pins UTC rendering regardless of browser/host timezone", () => {
    const options = replayChartOptions(props);
    expect(options.useUTC).toBe(true);
    expect(options.xAxis[0].name).toContain("UTC");
    expect(
      "markLine" in options.series[0]
        ? options.series[0].markLine.data.map((marker) => marker.xAxis)
        : [],
    ).toEqual([Date.parse(props.cutoff), Date.parse(props.decisionTime)]);
  });
  it("keeps actual zero-valued recorded feature points on separate unit panes", () => {
    const options = replayChartOptions(props);
    expect(options.series[0].data).toEqual([
      [Date.parse("2024-01-02"), 5, 6, 4, 7],
    ]);
    expect(options.yAxis.map((axis) => axis.name)).toEqual(["CNY", "shares"]);
    expect(options.series[1].type).toBe("scatter");
    expect(options.series[1].data).toEqual([[Date.parse(props.cutoff), 0]]);
    expect(options.series[1].xAxisIndex).toBe(1);
    expect(options.series[1].name).toContain("liquidity/volume");
  });
  it("does not overlay missing, future or another instrument's values", () => {
    const rows = detail().features.items!;
    expect(
      replayChartOptions({
        ...props,
        features: [
          {
            ...rows[0],
            observation: {
              ...rows[0].observation,
              validity: "not_sampled",
              value: null,
            },
          },
        ],
      }).series,
    ).toHaveLength(1);
    expect(
      replayChartOptions({ ...props, instrument: "C" }).series,
    ).toHaveLength(1);
    expect(
      replayChartOptions({ ...props, cutoff: "2024-01-01T07:00:00Z" }).series,
    ).toHaveLength(1);
  });
  it("renders actual ECharts SVG without a browser, socket or recalculation", () => {
    const chart = init(null, undefined, {
      renderer: "svg",
      ssr: true,
      width: 900,
      height: 450,
    });
    try {
      chart.setOption(replayChartOptions(props));
      const svg = chart.renderToSVGString();
      expect(svg).toContain("Information cutoff");
      expect(svg).toContain("Decision time");
      expect(svg).toContain("volume");
      expect(svg).not.toContain("NaN");
    } finally {
      chart.dispose();
    }
  });
});
