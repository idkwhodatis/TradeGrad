import { lazy, Suspense } from "react";
import { z } from "zod";
import { JsonDetails, Metadata } from "./data-common";
const ResearchChart = lazy(() => import("./research-chart"));
const resultSchema = z.object({
  status: z.literal("available"),
  reason: z.string(),
  definition_version: z.string(),
  input: z.record(z.string(), z.unknown()),
  normalization: z.record(z.string(), z.unknown()),
  series: z.object({
    columns: z.array(
      z.object({ name: z.string(), dtype: z.string(), unit: z.string() }),
    ),
    rows: z.array(
      z.array(z.union([z.string(), z.number(), z.boolean(), z.null()])),
    ),
  }),
  metrics: z.array(
    z.object({
      name: z.string(),
      value: z.number(),
      unit: z.string(),
      calculation_basis: z.string(),
      definition_version: z.string(),
    }),
  ),
  series_total_rows: z.number().optional(),
  series_returned_rows: z.number().optional(),
  downsampled: z.boolean().optional(),
  sampling: z.string().optional(),
});
export function BenchmarkResult({ value }: { value: unknown }) {
  if (
    !value ||
    (typeof value === "object" &&
      "status" in value &&
      value.status === "not_configured")
  )
    return (
      <p className="notice mt-5" data-testid="benchmark-unavailable">
        Benchmark: not_configured. No comparison was requested; strategy data is
        not substituted.
      </p>
    );
  const parsed = resultSchema.safeParse(value);
  if (!parsed.success)
    return (
      <p className="field-error" role="alert">
        Persisted benchmark evidence is invalid or incomplete. No curve is
        synthesized.
      </p>
    );
  const data = parsed.data;
  const selected = data.series.columns
    .map((column, index) => ({ column, index }))
    .filter(
      ({ column }) =>
        column.name === "session_date" ||
        ["strategy_observed_return", "benchmark_price_return"].includes(
          column.name,
        ),
    );
  const table = {
    columns: selected.map(({ column }) => column),
    rows: data.series.rows.map((row) =>
      selected.map(({ index }) => row[index]),
    ),
  };
  return (
    <section className="stack mt-5" data-testid="benchmark-result">
      <h3>Persisted close-price benchmark comparison</h3>
      <p className="notice">{data.reason}</p>
      <Metadata
        values={{
          Definition: data.definition_version,
          "Snapshot as of": data.input.source_as_of,
          Currency: data.input.currency,
          "Benchmark classification": data.input.source_classification,
          Timezone: data.input.timezone,
          Calendar: data.input.calendar,
          "Calendar version": data.input.calendar_version,
          "Observed anchor session": data.normalization.session,
          "Returned / complete points": `${data.series_returned_rows ?? data.series.rows.length} / ${data.series_total_rows ?? data.series.rows.length}`,
          Sampling: data.sampling ?? "none",
        }}
      />
      <Metadata
        values={Object.fromEntries(
          data.metrics.map((metric) => [
            metric.name,
            `${metric.value} ${metric.unit}`,
          ]),
        )}
      />
      <p className="muted text-xs">
        Metrics and normalization use the complete scored window, not the
        displayed slice. Portfolio comparison is not filtered by strategy
        listing. Raw persisted observations and all metric definitions are
        included in the original result download.
      </p>
      <Suspense fallback={<p>Loading benchmark chart…</p>}>
        <ResearchChart
          title="Observed strategy return versus benchmark close-price return"
          table={table}
          xColumn="session_date"
        />
      </Suspense>
      <JsonDetails
        title="Returned benchmark observations and definitions"
        value={data}
      />
    </section>
  );
}
