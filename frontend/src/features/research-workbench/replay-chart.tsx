import { useEffect, useRef } from "react";
import { init, use } from "echarts/core";
import { CandlestickChart, ScatterChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import type { FeatureRow, PreviewBar, TraceContext } from "@/client/types.gen";
import { savedFeaturePoints } from "@/lib/replay-evidence";
import {
  ReplayTimestampCaveat,
  replayTimestampPresentation,
} from "./replay-timing";
use([
  CandlestickChart,
  ScatterChart,
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  SVGRenderer,
]);

export type ReplayChartProps = {
  bars: PreviewBar[];
  features: FeatureRow[] | null;
  instrument: string;
  cutoff: string;
  decisionTime: string;
  timezone: string;
  context?: TraceContext;
};
export function replayChartOptions({
  bars,
  features,
  instrument,
  cutoff,
  decisionTime,
  timezone,
  context,
}: ReplayChartProps) {
  const timing = replayTimestampPresentation(context);
  const points = savedFeaturePoints(features, instrument, cutoff);
  const units = [...new Set(points.map((p) => p.unit))];
  const grid = [
    { top: 55, height: 220, left: 85, right: 30 },
    ...units.map((_, i) => ({
      top: 300 + i * 115,
      height: 75,
      left: 85,
      right: 30,
    })),
  ];
  return {
    useUTC: true,
    animation: false,
    aria: {
      enabled: true,
      description: `Immutable retrospective OHLC for ${instrument}. ${timing.markerDescription} Market timezone ${timezone}. ${timing.caveat ?? ""} Saved features are separate points in their own units, never recomputed or interpolated.`,
    },
    tooltip: { trigger: "item", renderMode: "richText" },
    legend: { type: "scroll", top: 0 },
    grid,
    xAxis: grid.map((_, index) => ({
      type: "time",
      gridIndex: index,
      name:
        index === 0
          ? timing.instants
            ? "UTC timestamp · market zone shown in evidence"
            : timing.semantics === "native_daily_session_labels"
              ? "UTC session labels · not availability instants"
              : "UTC saved coordinates · availability unverified"
          : "",
      min: Math.min(
        Date.parse(bars[0].session_date),
        Date.parse(cutoff),
        Date.parse(decisionTime),
      ),
      max: Math.max(
        Date.parse(bars.at(-1)!.session_date) + 86400000,
        Date.parse(cutoff),
        Date.parse(decisionTime),
      ),
    })),
    yAxis: [
      { type: "value", gridIndex: 0, scale: true, name: bars[0].currency },
      ...units.map((unit, i) => ({
        type: "value",
        gridIndex: i + 1,
        scale: true,
        name: unit,
      })),
    ],
    dataZoom: [{ type: "inside", xAxisIndex: grid.map((_, i) => i) }],
    series: [
      {
        type: "candlestick",
        name: `Frozen ${instrument} OHLC`,
        xAxisIndex: 0,
        yAxisIndex: 0,
        encode: { x: 0, y: [1, 2, 3, 4] },
        data: bars.map((bar) => [
          Date.parse(bar.session_date),
          bar.open,
          bar.close,
          bar.low,
          bar.high,
        ]),
        markLine: {
          silent: true,
          symbol: "none",
          label: { position: "insideEndTop", formatter: "{b}" },
          data: [
            {
              name: timing.cutoffLabel,
              xAxis: Date.parse(cutoff),
              lineStyle: { color: "#876925", type: "dashed" },
            },
            {
              name: timing.decisionLabel,
              xAxis: Date.parse(decisionTime),
              lineStyle: { color: "#5753a8", type: "solid" },
            },
          ],
        },
      },
      ...points.map((point) => ({
        type: "scatter",
        symbolSize: 10,
        name: `${point.stage_id}/${point.feature_id} (${point.unit})`,
        xAxisIndex: units.indexOf(point.unit) + 1,
        yAxisIndex: units.indexOf(point.unit) + 1,
        data: [[Date.parse(point.time), point.value]],
      })),
    ],
  };
}

export default function ReplayChart({
  bars,
  features,
  instrument,
  cutoff,
  decisionTime,
  timezone,
  context,
}: ReplayChartProps) {
  const timing = replayTimestampPresentation(context);
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!container.current || !bars.length) return;
    const chart = init(container.current, undefined, { renderer: "svg" });
    chart.setOption(
      replayChartOptions({
        bars,
        features,
        instrument,
        cutoff,
        decisionTime,
        timezone,
        context,
      }),
    );
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => chart.resize());
    if (observer) observer.observe(container.current);
    return () => {
      observer?.disconnect();
      chart.dispose();
    };
  }, [bars, features, instrument, cutoff, decisionTime, timezone, context]);
  return (
    <figure>
      <div
        ref={container}
        style={{
          height:
            310 +
            new Set(
              savedFeaturePoints(features, instrument, cutoff).map(
                (point) => point.unit,
              ),
            ).size *
              115,
          width: "100%",
        }}
        role="img"
        aria-label="Retrospective market prices, information cutoff, decision time and actually saved feature points"
        aria-description={`${timing.markerDescription} ${timing.caveat ?? ""}`}
      />
      <figcaption className="text-xs muted">
        Daily OHLC bars use session-date coordinates. {timing.markerDescription}{" "}
        Subsequent prices are hindsight only, not decision inputs. Only numeric
        valid selected-event features whose saved coordinates are at or before
        the saved cutoff are plotted, in separate unit panes. This coordinate
        filter does not independently establish data availability. No
        interpolation, indicator recomputation or inferred explanation.
      </figcaption>
      <ReplayTimestampCaveat context={context} />
    </figure>
  );
}
