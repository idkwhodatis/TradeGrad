import { useEffect, useRef } from "react";
import { init, use } from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import { chartSeries, type ChartTable } from "@/lib/research-results";
use([
  BarChart,
  LineChart,
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  SVGRenderer,
]);

export default function ResearchChart({
  table,
  title,
  kind = "line",
  xColumn,
}: {
  table: ChartTable;
  title: string;
  kind?: "line" | "bar";
  xColumn?: string;
}) {
  const element = useRef<HTMLDivElement>(null);
  const data = chartSeries(table, xColumn);
  useEffect(() => {
    if (!element.current || !data) return;
    const chart = init(element.current, undefined, { renderer: "svg" });
    chart.setOption({
      animation: false,
      aria: {
        enabled: true,
        description: `${title}. Only the requested bounded series is shown. Original precision is available in the data table.`,
      },
      tooltip: { trigger: "axis", renderMode: "richText" },
      legend: { type: "scroll", top: 0 },
      grid: { top: 55, right: 30, bottom: 75, left: 80 },
      xAxis: { type: "category", data: data.labels },
      yAxis: {
        type: "value",
        scale: true,
        name: [...new Set(data.series.map((series) => series.unit))].join(
          " / ",
        ),
      },
      dataZoom: [{ type: "inside" }, { type: "slider", bottom: 5 }],
      series: data.series.map((series) => ({
        name: `${series.name} (${series.unit})`,
        type: kind,
        showSymbol: false,
        connectNulls: false,
        data: series.values,
      })),
    });
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => chart.resize());
    if (observer && element.current) observer.observe(element.current);
    return () => {
      observer?.disconnect();
      chart.dispose();
    };
  }, [table, title, kind, xColumn]);
  if (!data)
    return (
      <p className="notice">
        No plottable dated numeric series is present. Inspect the persisted
        table below; no curve is invented.
      </p>
    );
  return (
    <figure className="m-0">
      <div
        ref={element}
        style={{ height: 340, width: "100%" }}
        role="img"
        aria-label={`${title}; accessible numeric evidence below`}
      />
      <figcaption className="text-xs muted mt-2">
        Bounded persisted series only. Zoom does not fetch additional history.
        Missing values remain gaps; the evidence below preserves returned
        numeric precision.
      </figcaption>
    </figure>
  );
}
