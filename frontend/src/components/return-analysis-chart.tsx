import { useEffect, useRef } from "react";
import { init, use } from "echarts/core";
import { HeatmapChart, BarChart } from "echarts/charts";
import {
  AriaComponent,
  GridComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import type { ReturnAnalysis } from "@/client/types.gen";
use([
  HeatmapChart,
  BarChart,
  AriaComponent,
  GridComponent,
  TooltipComponent,
  VisualMapComponent,
  SVGRenderer,
]);

export default function ReturnAnalysisChart({
  value,
  kind,
}: {
  value: ReturnAnalysis;
  kind: "monthly" | "distribution";
}) {
  const element = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!element.current) return;
    const chart = init(element.current, undefined, { renderer: "svg" });
    const years = [
      ...new Set(value.months.map((item) => item.month.slice(0, 4))),
    ];
    const magnitude = Math.max(
      0.01,
      ...value.months.map((item) => Math.abs(item.value ?? 0)),
    );
    chart.setOption(
      kind === "monthly"
        ? {
            animation: false,
            aria: { enabled: true },
            tooltip: {
              renderMode: "richText",
              formatter: (params: {
                value: [number, number, number, string];
              }) =>
                `${years[params.value[1]]}-${String(params.value[0] + 1).padStart(2, "0")}: ${(params.value[2] * 100).toFixed(2)}% (${params.value[3]})`,
            },
            grid: { left: 65, right: 35, top: 25, bottom: 90 },
            xAxis: {
              type: "category",
              data: [
                "Jan",
                "Feb",
                "Mar",
                "Apr",
                "May",
                "Jun",
                "Jul",
                "Aug",
                "Sep",
                "Oct",
                "Nov",
                "Dec",
              ],
              splitArea: { show: true },
            },
            yAxis: { type: "category", data: years, splitArea: { show: true } },
            visualMap: {
              min: -magnitude,
              max: magnitude,
              calculable: false,
              orient: "horizontal",
              left: "center",
              bottom: 0,
              inRange: { color: ["#b35748", "#f3f3e9", "#367653"] },
              formatter: (v: number) => `${(v * 100).toFixed(1)}%`,
            },
            series: [
              {
                type: "heatmap",
                data: value.months
                  .filter((item) => item.value !== null)
                  .map((item) => ({
                    value: [
                      Number(item.month.slice(5)) - 1,
                      years.indexOf(item.month.slice(0, 4)),
                      item.value,
                      item.status,
                    ],
                    itemStyle: {
                      borderColor:
                        item.status === "partial_period" ? "#5a408d" : "#fff",
                      borderWidth: item.status === "partial_period" ? 3 : 1,
                    },
                  })),
                label: {
                  show: true,
                  formatter: (params: {
                    value: [number, number, number, string];
                  }) =>
                    `${(params.value[2] * 100).toFixed(1)}%${params.value[3] === "partial_period" ? "*" : ""}`,
                },
              },
            ],
          }
        : {
            animation: false,
            aria: { enabled: true },
            tooltip: { trigger: "axis", renderMode: "richText" },
            grid: { left: 55, right: 25, top: 25, bottom: 100 },
            xAxis: {
              type: "category",
              data: value.bins.map(
                (item) =>
                  `${(item.lower * 100).toFixed(2)} to ${(item.upper * 100).toFixed(2)}%`,
              ),
              axisLabel: { rotate: 30 },
            },
            yAxis: {
              type: "value",
              minInterval: 1,
              name: "observed intervals",
            },
            series: [
              {
                type: "bar",
                data: value.bins.map((item) => item.count),
                itemStyle: { color: "#6d8660" },
              },
            ],
          },
    );
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(element.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [value, kind]);
  return (
    <div
      ref={element}
      style={{
        height:
          kind === "monthly"
            ? Math.max(
                220,
                new Set(value.months.map((m) => m.month.slice(0, 4))).size *
                  45 +
                  130,
              )
            : 330,
      }}
      role="img"
      aria-label={
        kind === "monthly"
          ? "Monthly observed equity returns heatmap; asterisk means partial period"
          : "Observed close-to-close return distribution; not certified daily returns"
      }
    />
  );
}
