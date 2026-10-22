import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import type {
  ComparisonDifference,
  ResultComparePublic,
  ResultMetric,
} from "@/client/types.gen";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import { Field, JsonDetails, Metadata } from "@/components/data-common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { comparisonOptions } from "@/lib/research-api";
import {
  comparisonIds,
  metricText,
  rawCell,
  researchTrustNotice,
  resultEvidence,
} from "@/lib/research-results";
import { label, shortId } from "@/lib/format";
const ResearchChart = lazy(() => import("@/components/research-chart"));

export function ComparePage() {
  const search = useSearch({ strict: false }) as { evaluations?: string };
  const navigate = useNavigate();
  const value = search.evaluations || "";
  const parsed = comparisonIds(value);
  const [input, setInput] = useState(value);
  const [inputError, setInputError] = useState<string | null>(null);
  useEffect(() => {
    setInput(value);
    setInputError(null);
  }, [value]);
  const comparison = useQuery(comparisonOptions(parsed.ids));
  function submit(event: React.FormEvent) {
    event.preventDefault();
    const next = comparisonIds(input);
    setInputError(next.error);
    if (!next.error)
      void navigate({
        to: "/compare",
        search: { evaluations: next.ids.join(",") },
      });
  }
  return (
    <>
      <PageHeader
        title="Compare evaluations"
        subtitle="Explicit windows, metric definitions and configuration differences. No silent apples-to-oranges ranking."
      />
      <p className="notice">{researchTrustNotice}</p>
      <Panel title="Choose two to four persisted evaluations">
        <form onSubmit={submit} className="mt-3">
          <Field
            id="comparison-evaluations"
            label="Evaluation IDs (comma-separated)"
            help="Select successful evaluations on a research run, or paste their IDs here. Selections stay in the URL for refresh and Back/Forward navigation."
          >
            <Input
              id="comparison-evaluations"
              value={input}
              onChange={(event) => setInput(event.target.value)}
            />
          </Field>
          <Button type="submit" className="mt-4">
            Compare evaluations
          </Button>
        </form>
        {(inputError || parsed.error) && (
          <p className="notice mt-4" role={inputError ? "alert" : "status"}>
            {inputError || parsed.error}
          </p>
        )}
        <Link to="/runs" className="text-link mt-4">
          Choose from experiments
        </Link>
      </Panel>
      {!parsed.error &&
        (comparison.isPending ? (
          <Loading text="Checking immutable comparison basis…" />
        ) : comparison.error ? (
          <ErrorState
            error={comparison.error}
            retry={() => void comparison.refetch()}
          />
        ) : comparison.data ? (
          <ComparisonContent result={comparison.data} />
        ) : null)}
    </>
  );
}
function ComparisonContent({ result }: { result: ResultComparePublic }) {
  const compatibleMetrics = useMemo(
    () =>
      result.evaluations[0]?.metrics.filter(
        (metric) =>
          metric.value !== null &&
          result.evaluations.every((evaluation) =>
            evaluation.metrics.some(
              (other) =>
                other.name === metric.name &&
                other.unit === metric.unit &&
                other.definition_version === metric.definition_version &&
                other.calculation_basis === metric.calculation_basis &&
                other.value !== null,
            ),
          ),
      ) || [],
    [result],
  );
  const [metricName, setMetricName] = useState("");
  const metric =
    compatibleMetrics.find((item) => item.name === metricName) ||
    compatibleMetrics[0];
  const chartTable = useMemo(
    () =>
      metric
        ? {
            columns: [
              { name: "evaluation", dtype: "string", unit: "identifier" },
              { name: metric.name, dtype: "float64", unit: metric.unit },
            ],
            rows: result.evaluations.map((evaluation) => [
              shortId(evaluation.evaluation_id),
              evaluation.metrics.find((item) => item.name === metric.name)
                ?.value ?? null,
            ]),
          }
        : null,
    [metric, result],
  );
  const differenceColumns: ColumnDef<ComparisonDifference>[] = [
    { accessorKey: "field", header: "Configuration / basis field" },
    {
      accessorKey: "affects_comparability",
      header: "Changes comparison basis?",
      cell: ({ row }) =>
        row.original.affects_comparability
          ? "Yes: not directly comparable"
          : "No: intentional candidate/run difference",
    },
    ...result.evaluations.map((evaluation) => ({
      id: evaluation.evaluation_id,
      header: shortId(evaluation.evaluation_id),
      accessorFn: (difference: ComparisonDifference) =>
        difference.values.find(
          (value) => value.evaluation_id === evaluation.evaluation_id,
        )?.value,
      cell: ({ getValue }: { getValue: () => unknown }) => rawCell(getValue()),
    })),
  ];
  return (
    <div className="stack mt-6">
      <p
        className={`notice ${result.comparable ? "" : "error"}`}
        role="status"
        data-testid="comparison-basis"
      >
        {result.comparable
          ? "The selected evaluations share the server-checked comparison basis. They are shown in your selection order, never auto-ranked."
          : "Comparison basis differs. Ranking and comparison charts are blocked: inspect windows, currency, inputs, definitions and costs below before drawing conclusions."}{" "}
        No winner or investment recommendation is inferred.
      </p>
      {result.warnings.map((warning, index) => (
        <p className="notice" key={index}>
          {warning}
        </p>
      ))}
      <Panel title="Configuration and comparability differences">
        <DataTable
          data={result.differences}
          columns={differenceColumns}
          label="Evaluation configuration differences"
          empty="No configuration or comparison-basis differences were found."
        />
        <p className="text-xs muted mt-4">
          Different strategy code is expected. Different data versions, time
          windows, execution costs, metric definitions or assumptions can make
          metric values incomparable. Source compatibility is not full paper
          replication.
        </p>
      </Panel>
      {result.comparable && metric && chartTable && (
        <Panel title="Matched metric comparison (not a ranking)">
          <Field
            label="Metric with matching units and definition"
            id="comparison-metric"
          >
            <select
              id="comparison-metric"
              value={metric.name}
              onChange={(event) => setMetricName(event.target.value)}
            >
              {compatibleMetrics.map((item) => (
                <option key={item.name} value={item.name}>
                  {label(item.name)} · {item.unit} · {item.definition_version}
                </option>
              ))}
            </select>
          </Field>
          <Suspense
            fallback={<Loading text="Loading bounded comparison chart…" />}
          >
            <ResearchChart
              table={chartTable}
              title={`${label(metric.name)} (${metric.unit})`}
              kind="bar"
              xColumn="evaluation"
            />
          </Suspense>
          <p className="text-xs muted">
            The bars preserve selection order and original units; they do not
            select a best candidate. {metric.calculation_basis}
          </p>
        </Panel>
      )}
      <div className="catalog">
        {result.evaluations.map((evaluation) => (
          <Panel
            key={evaluation.evaluation_id}
            title={`${evaluation.candidate_key} · ${shortId(evaluation.evaluation_id)}`}
          >
            <Link
              to="/backtests/$evaluationId"
              params={{ evaluationId: evaluation.evaluation_id }}
              className="text-link"
            >
              Inspect complete backtest evidence
            </Link>
            <Metadata
              values={{
                "Evaluation ID": evaluation.evaluation_id,
                "Window [start, end)": `${evaluation.window.start} → ${evaluation.window.end}`,
                Split: evaluation.split,
                Currency: evaluation.currency,
                Profile: evaluation.config.profile,
                "Dataset evidence": resultEvidence(evaluation).dataset,
                "Model evidence": resultEvidence(evaluation).model,
                "Price definition": evaluation.price_definition,
                "Dataset SHA-256": evaluation.config.dataset.sha256,
                "Code SHA-256": evaluation.code_sha256,
                "Config SHA-256": evaluation.config_sha256,
                "Artifact SHA-256": evaluation.artifact_sha256,
              }}
            />
            <dl className="details">
              {evaluation.metrics.map((item: ResultMetric) => (
                <div key={item.name}>
                  <dt>{label(item.name)}</dt>
                  <dd>
                    {metricText(item)}
                    <p className="text-[10px] muted">
                      {item.unit} · {item.definition_version}
                    </p>
                    {item.unavailable_reason && (
                      <p className="text-[10px] muted">
                        {item.unavailable_reason}
                      </p>
                    )}
                  </dd>
                </div>
              ))}
            </dl>
            <JsonDetails
              title="Raw metrics and calculation definitions"
              value={evaluation.metrics}
            />
            <JsonDetails
              title="Full immutable configuration"
              value={evaluation.config}
            />
          </Panel>
        ))}
      </div>
    </div>
  );
}
