import { useEffect, useRef, useState } from "react";
import { init, use } from "echarts/core";
import { ScatterChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  TooltipComponent,
} from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import {
  comparisonNumericFields,
  comparisonNumericPoints,
} from "@/lib/comparison-series";
import type { ComparisonRecord } from "@/client/types.gen";
use([
  ScatterChart,
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  TooltipComponent,
  SVGRenderer,
]);
export function comparisonChartOptions(rows: ComparisonRecord[]) {
  const states = [
    "equal",
    "changed",
    "left_only",
    "right_only",
    "ambiguous",
    "unavailable",
  ];
  return {
    animation: false,
    aria: {
      enabled: true,
      description:
        "Bounded observed comparison rows. Each point is one exact server row, not a date/symbol match or a numeric performance estimate. Select a point to inspect its explicit event pair.",
    },
    tooltip: { trigger: "item", renderMode: "richText" },
    grid: { left: 110, right: 30, top: 20, bottom: 100 },
    xAxis: {
      type: "category",
      data: rows.map(
        (row, i) =>
          `${i + 1}: ${row.session_date || "undated"} · ${row.row_id.slice(0, 8)}`,
      ),
      axisLabel: { rotate: 35 },
    },
    yAxis: { type: "category", data: states },
    dataZoom: [{ type: "inside" }, { type: "slider", bottom: 0 }],
    series: [
      {
        type: "scatter",
        symbolSize: 12,
        data: rows.map((row, i) => ({
          value: [i, states.indexOf(row.status)],
          name: `${row.row_id} · ${row.status} · ${row.event_pair_id || "no explicit event pair"}`,
        })),
      },
    ],
  };
}
export function comparisonNumericChartOptions(
  rows: ComparisonRecord[],
  field: string,
) {
  const points = comparisonNumericPoints(rows, field);
  const units = [...new Set(points.map((point) => point.unit))];
  return {
    animation: false,
    aria: {
      enabled: true,
      description: `Exact saved ${field} observations. No ordinal pairing, averaging, interpolation or zero filling. Unknown units remain side-specific; currencies are not normalized.`,
    },
    tooltip: { trigger: "item", renderMode: "richText" },
    grid: units.map((_, i) => ({
      left: 110,
      right: 35,
      top: 30 + i * 220,
      height: 130,
    })),
    xAxis: units.map((_, i) => ({
      type: "category",
      gridIndex: i,
      data: points.map((point) => point.label),
      axisLabel: { show: false },
    })),
    yAxis: units.map((unit, i) => ({
      type: "value",
      gridIndex: i,
      scale: true,
      name: unit,
      nameLocation: "middle",
      nameGap: 70,
    })),
    series: units.map((unit, i) => ({
      type: "scatter",
      symbolSize: 10,
      xAxisIndex: i,
      yAxisIndex: i,
      data: points.map((point, index) => ({
        value: [index, point.unit === unit ? point.value : null],
        name: `${point.label} · ${field} · ${point.unit}${point.rightCensored ? " · right-censored, not a completed holding" : ""}`,
      })),
    })),
  };
}
export default function ComparisonChart({
  rows,
  onPair,
}: {
  rows: ComparisonRecord[];
  onPair: (id: string) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [field, setField] = useState("");
  const fields = comparisonNumericFields(rows);
  const activeField = fields.includes(field) ? field : "";
  const points = activeField ? comparisonNumericPoints(rows, activeField) : [];
  useEffect(() => {
    if (!container.current || !rows.length) return;
    const chart = init(container.current, undefined, { renderer: "svg" });
    chart.setOption(
      activeField
        ? comparisonNumericChartOptions(rows, activeField)
        : comparisonChartOptions(rows),
    );
    chart.on("click", (event) => {
      const index = (event as { dataIndex?: number }).dataIndex;
      const pair =
        index === undefined
          ? null
          : activeField
            ? comparisonNumericPoints(rows, activeField)[index]?.pairId
            : rows[index]?.event_pair_id;
      if (pair) onPair(pair);
    });
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => chart.resize());
    if (container.current) observer?.observe(container.current);
    return () => {
      observer?.disconnect();
      chart.dispose();
    };
  }, [rows, onPair, activeField]);
  return (
    <figure>
      <label className="workbench-field">
        Plot saved field
        <select
          value={activeField}
          onChange={(event) => setField(event.target.value)}
        >
          <option value="">Recorded difference categories</option>
          {fields.map((value) => (
            <option key={value} value={value}>
              {value} (saved values)
            </option>
          ))}
        </select>
      </label>
      {activeField && (
        <p className="notice">
          Only exact saved {activeField} values are plotted. Duplicate ordinals
          are separate per-side points, not inferred matches. Missing/nonfinite
          values and unknown open-position duration remain gaps. Units without a
          declaration stay on separate side-specific axes; currencies are not
          normalized.
        </p>
      )}
      <div
        ref={container}
        role="img"
        aria-label="Bounded event-pair differences; exact selectable rows below"
        style={{
          height: activeField
            ? Math.max(
                300,
                new Set(points.map((point) => point.unit)).size * 220,
              )
            : 350,
          width: "100%",
        }}
      />
      <figcaption className="text-xs muted">
        Only this server page is plotted. Same-day events remain distinct. Zoom
        does not load other history; points without an explicit pair have no
        replay link. Exact values and missing states are below.
      </figcaption>
    </figure>
  );
}
