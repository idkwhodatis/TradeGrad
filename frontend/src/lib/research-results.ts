import type { ResearchRunDetail } from "@/client/types.gen";
import { dateWindowError } from "./data-utils";

export const researchTerminal = new Set([
  "paused",
  "failed",
  "cancelled",
  "interrupted",
  "succeeded",
]);
export const resultSections = [
  "equity",
  "drawdown",
  "cash",
  "margin",
  "positions",
  "orders",
  "fills",
  "closed_trades",
] as const;
export type ResultSection = (typeof resultSections)[number];
export const researchTrustNotice =
  "Native · UNTRUSTED. Strategy and engine share a process; isolation is not an independently trusted ledger. These are research observations, not live-trading or holdout performance claims.";
export function researchControls(
  run: Pick<ResearchRunDetail, "status" | "attempts" | "latest_checkpoint_id">,
) {
  const resumable = ["paused", "failed", "cancelled", "interrupted"].includes(
    run.status,
  );
  return {
    pause: ["queued", "preparing", "running"].includes(run.status),
    cancel:
      !researchTerminal.has(run.status) && run.status !== "cancel_requested",
    resume:
      resumable &&
      Boolean(run.latest_checkpoint_id) &&
      run.attempts.every((attempt) => attempt.sandbox_stopped),
    resumeReason: !resumable
      ? null
      : !run.latest_checkpoint_id
        ? "No complete checkpoint is published. Recovery cannot invent optimizer state."
        : run.attempts.some((attempt) => !attempt.sandbox_stopped)
          ? "Waiting for the previous execution environment to be confirmed stopped."
          : "The server verifies checkpoint integrity, compatible inputs and that every evaluation sandbox has stopped before resuming.",
  };
}
export function comparisonIds(value: unknown): {
  ids: string[];
  error: string | null;
} {
  if (typeof value !== "string" || !value.trim())
    return { ids: [], error: "Choose two to four evaluation IDs to compare." };
  const ids = value.split(",").map((id) => id.trim());
  if (ids.length < 2 || ids.length > 4)
    return { ids: [], error: "Compare requires two to four evaluation IDs." };
  if (new Set(ids).size !== ids.length)
    return {
      ids: [],
      error: "Choose distinct evaluations, not the same evaluation twice.",
    };
  if (
    ids.some(
      (id) =>
        !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
          id,
        ),
    )
  )
    return { ids: [], error: "Each evaluation must have a valid UUID." };
  return { ids, error: null };
}
export function resultWindowError(start: string, end: string) {
  if (!start && !end) return null;
  return dateWindowError(start, end);
}
export function rawCell(value: unknown): string {
  return value === null || value === undefined
    ? "Unavailable"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
}
// Metric values retain their declared unit. A stored percentage point is never
// multiplied by 100, and absent/non-finite values never become zero.
export function metricText(metric: { value: number | null; unit: string }) {
  if (metric.value === null || !Number.isFinite(metric.value))
    return "Unavailable";
  return `${metric.value.toLocaleString("en", { maximumFractionDigits: 6 })}${metric.unit === "percent" || metric.unit === "percentage_points" ? "%" : metric.unit === "ratio" || metric.unit === "dimensionless" ? "" : ` ${metric.unit}`}`;
}
export type ChartTable = {
  columns: { name: string; dtype: string; unit?: string | null }[];
  rows: (string | number | boolean | null)[][];
};
export function chartSeries(table: ChartTable, xColumn?: string) {
  const time = xColumn
    ? table.columns.findIndex((column) => column.name === xColumn)
    : table.columns.findIndex(
        (column) =>
          /^(timestamp|date|datetime|session_date|time|index)$/i.test(
            column.name,
          ) || /datetime|timestamp|date/.test(column.dtype),
      );
  if (time < 0) return null;
  const numeric = table.columns
    .map((column, index) => ({ column, index }))
    .filter(
      ({ column, index }) =>
        index !== time &&
        !/(id|quantity|volume)$/i.test(column.name) &&
        table.rows.some(
          (row) =>
            typeof row[index] === "number" && Number.isFinite(row[index]),
        ),
    );
  if (!numeric.length) return null;
  return {
    labels: table.rows.map((row) => rawCell(row[time])),
    series: numeric.map(({ column, index }) => ({
      name: column.name,
      unit: column.unit || "unknown",
      values: table.rows.map((row) =>
        typeof row[index] === "number" && Number.isFinite(row[index])
          ? (row[index] as number)
          : null,
      ),
    })),
  };
}
export function safeDownloadName(name: string) {
  return (
    name.replace(/[\\/\u0000-\u001f\u007f]/g, "_").slice(0, 160) ||
    "research-artifact"
  );
}
export function saveResearchDownload(blob: Blob, name: string) {
  // Never navigate to HTML or embed arbitrary artifacts in the authenticated app.
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = safeDownloadName(name);
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function resultEvidence(result: {
  is_synthetic: boolean;
  config: { model?: { provider?: string } };
}) {
  const provider = result.config.model?.provider;
  return {
    dataset: result.is_synthetic
      ? "Synthetic dataset; not live-provider acceptance evidence"
      : "Dataset marked non-synthetic; inspect pinned provider provenance",
    model:
      provider === "fixture"
        ? "Deterministic model fixture; not live-model acceptance evidence"
        : provider
          ? `Declared provider: ${provider}; inspect persisted call evidence`
          : "Model provider unavailable",
  };
}
