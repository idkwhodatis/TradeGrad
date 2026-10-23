import {
  ModeResultExplorer,
  type FillTarget,
} from "@/components/result-exploration";
import { BenchmarkResult } from "@/components/benchmark-result";
import { lazy, Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ExplorationContext, ResultTable } from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import {
  Field,
  JsonDetails,
  Metadata,
  Pagination,
} from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ResultTableControls,
  type TableFilters,
} from "@/components/result-table-controls";
import { fillNavigation } from "@/lib/result-exploration";
import { ResultMetrics, ResultTableView } from "@/features/backtest-detail";
import { downloadFinalResult, finalResultsOptions } from "@/lib/final-api";
import { label } from "@/lib/format";
import {
  resultSections,
  resultWindowError,
  saveResearchDownload,
  type ResultSection,
} from "@/lib/research-results";
import { useDataAction } from "@/lib/use-data-action";
import { CreateStrategyPackage } from "./strategy-package";
const ResearchChart = lazy(() => import("@/components/research-chart"));
const TABLE_LIMIT = 50;
const MAX_POINTS = 600;

export function HoldoutResult({
  evaluationId,
  selectionId,
}: {
  evaluationId: string;
  selectionId: string;
}) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [filter, setFilter] = useState({ start: "", end: "" });
  const [error, setError] = useState<string | null>(null);
  const [section, setSection] = useState<ResultSection>("positions");
  const [offset, setOffset] = useState(0);
  const [showCharts, setShowCharts] = useState(false);
  const [fillTarget, setFillTarget] = useState<FillTarget | null>(null);
  const [exploration, setExploration] = useState<ExplorationContext | null>(
    null,
  );
  const [tableFilters, setTableFilters] = useState<TableFilters>({});
  const result = useQuery(
    finalResultsOptions(evaluationId, {
      sections: ["equity", "drawdown"],
      max_points: MAX_POINTS,
      start: filter.start || undefined,
      end: filter.end || undefined,
    }),
  );
  const rows = useQuery(
    finalResultsOptions(evaluationId, {
      ...tableFilters,
      sections: [section],
      max_points: MAX_POINTS,
      offset,
      limit: TABLE_LIMIT,
      start: filter.start || undefined,
      end: filter.end || undefined,
    }),
  );
  const download = useDataAction(async () => {
    const blob = await downloadFinalResult(evaluationId);
    saveResearchDownload(blob, `final-evaluation-${evaluationId}.json`);
  });
  const data = result.data;
  if (result.isPending)
    return <Loading text="Loading verified holdout result artifact…" />;
  if (result.error)
    return (
      <ErrorState error={result.error} retry={() => void result.refetch()} />
    );
  if (!data)
    return <p className="notice">No persisted final result is available.</p>;
  if (
    data.selection_id !== selectionId ||
    data.split !== "holdout" ||
    data.selection_feedback !== false
  )
    return (
      <ErrorState
        error={
          new Error(
            "The result does not match this frozen holdout context. No data is displayed.",
          )
        }
      />
    );
  const source = data.config.source_config;
  return (
    <div className="stack" data-testid="holdout-result">
      <Panel title="Actual holdout result · excluded from selection feedback">
        <p className="notice">
          Persisted native holdout observations for [{data.window.start},{" "}
          {data.window.end}). These metrics do not rank, select, mutate or
          re-optimize the frozen candidate.{" "}
          {data.is_synthetic
            ? "Synthetic dataset; not live-provider acceptance evidence."
            : "Dataset marked non-synthetic; inspect pinned provider provenance."}
        </p>
        {data.warnings.map((warning, index) => (
          <p className="notice mt-4" key={index}>
            {warning}
          </p>
        ))}
        <ResultMetrics metrics={data.metrics} />
        {Boolean(download.error) && <ErrorState error={download.error} />}
        <Button
          className="mt-4"
          variant="outline"
          disabled={download.pending}
          onClick={() => void download.run()}
        >
          {download.pending ? "Downloading…" : "Download original holdout JSON"}
        </Button>
        <p className="text-xs muted mt-4">
          Metrics retain their original units and full-window calculation basis.
          Filtering and display rounding do not change these stored metrics.
        </p>
        <Metadata
          values={{
            Evaluation: data.evaluation_id,
            "Frozen selection": data.selection_id,
            "Result context": "holdout",
            "Selection feedback": "disabled",
            "Code SHA-256": data.code_sha256,
            "Final config SHA-256": data.config_sha256,
            "Artifact SHA-256": data.artifact_sha256,
            "Dataset version": source.dataset.id,
            "Dataset SHA-256": source.dataset.sha256,
            "Model provider in original research":
              source.model?.provider || "Unavailable",
            "Initial capital": source.market_profile.initial_cash,
            Currency: data.currency,
            "Native fill fees": data.fees.total_from_fills,
            "Fee unit": data.fees.unit,
          }}
        />
        <JsonDetails
          title="Pinned final config and result capabilities"
          value={{
            config: data.config,
            capabilities: data.capabilities,
            benchmark: data.benchmark,
            fees: data.fees,
          }}
        />
      </Panel>
      <Panel title="Bounded holdout charts and tables">
        <form
          className="form-grid"
          onSubmit={(event) => {
            event.preventDefault();
            const invalid = resultWindowError(start, end);
            if (invalid) {
              setError(invalid);
              return;
            }
            if (
              (start && start < data.window.start) ||
              (end && end > data.window.end)
            ) {
              setError("Choose dates inside the frozen holdout window.");
              return;
            }
            setError(null);
            setFilter({ start, end });
            setOffset(0);
          }}
        >
          <Field id="holdout-start" label="Holdout filter start · inclusive">
            <Input
              id="holdout-start"
              type="date"
              value={start}
              onChange={(event) => setStart(event.target.value)}
            />
          </Field>
          <Field id="holdout-end" label="Holdout filter end · exclusive">
            <Input
              id="holdout-end"
              type="date"
              value={end}
              onChange={(event) => setEnd(event.target.value)}
            />
          </Field>
          <div className="actions">
            <Button
              type="submit"
              disabled={result.isFetching || rows.isFetching}
            >
              Apply holdout filters
            </Button>
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setStart("");
                setEnd("");
                setFilter({ start: "", end: "" });
                setOffset(0);
                setError(null);
              }}
            >
              Reset filters
            </Button>
          </div>
        </form>
        {error && (
          <p className="notice error mt-4" role="alert">
            {error}
          </p>
        )}
        <p className="text-xs muted mt-4">
          At most {MAX_POINTS} server-selected points per chart. Dates are
          [start, end); no fabricated starting equity, benchmark or excess
          return.
        </p>
        <Button
          className="mt-4"
          variant="outline"
          onClick={() => setShowCharts(!showCharts)}
        >
          {showCharts
            ? "Hide holdout charts"
            : "Show holdout equity and drawdown charts"}
        </Button>
        {(["equity", "drawdown"] as const).map((name) => (
          <section className="mt-5" key={name} aria-label={`Holdout ${name}`}>
            <h3>
              {label(name)} · {name === "equity" ? data.currency : "fraction"}
            </h3>
            {data.tables[name] ? (
              <>
                {showCharts && (
                  <Suspense
                    fallback={<Loading text="Loading local holdout chart…" />}
                  >
                    <ResearchChart
                      title={`Holdout ${label(name)}`}
                      table={data.tables[name]}
                    />
                  </Suspense>
                )}
                <details className="mt-3">
                  <summary className="text-link">
                    Returned holdout {name} points
                  </summary>
                  <HoldoutTable table={data.tables[name]} />
                </details>
              </>
            ) : (
              <p className="notice">No persisted {name} series is available.</p>
            )}
          </section>
        ))}
        <BenchmarkResult value={data.benchmark} />
        <div className="mt-5">
          <Field id="holdout-section" label="Holdout result table">
            <select
              id="holdout-section"
              value={section}
              onChange={(event) => {
                setSection(event.target.value as ResultSection);
                setOffset(0);
                setTableFilters({});
              }}
            >
              {resultSections.map((name) => (
                <option key={name} value={name}>
                  {label(name)}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <ResultTableControls
          section={section}
          columns={rows.data?.tables[section]?.sortable_columns || []}
          value={tableFilters}
          listings={exploration?.listings.map((item) => ({
            listing_id: item.listing_id,
            symbol: item.symbol,
          }))}
          onChange={(value) => {
            setTableFilters(value);
            setOffset(0);
          }}
        />
        {rows.isPending ? (
          <Loading />
        ) : rows.error ? (
          <ErrorState error={rows.error} retry={() => void rows.refetch()} />
        ) : rows.data?.tables[section] ? (
          <>
            <ResultTableView
              title={`Holdout ${section}`}
              table={rows.data.tables[section]}
              onReviewFill={
                section === "fills"
                  ? (index) => {
                      const mapping = Object.fromEntries(
                        (exploration?.listings || []).map((item) => [
                          item.listing_id,
                          item.symbol,
                        ]),
                      );
                      setFillTarget(
                        fillNavigation(
                          rows.data!.tables[section]!,
                          index,
                          mapping,
                        ),
                      );
                    }
                  : undefined
              }
            />
            {!["equity", "drawdown", "cash", "margin"].includes(section) && (
              <Pagination
                offset={offset}
                limit={TABLE_LIMIT}
                count={rows.data.tables[section].filtered_rows}
                busy={rows.isFetching}
                onPage={setOffset}
              />
            )}
          </>
        ) : (
          <p className="notice mt-4">
            No records were published for this table. Missing data is not zero.
          </p>
        )}
      </Panel>
      <ModeResultExplorer
        mode="native_final"
        reference={data}
        target={fillTarget}
        onContext={setExploration}
      />
      <CreateStrategyPackage
        selectionId={selectionId}
        evaluationId={evaluationId}
      />
    </div>
  );
}
export function HoldoutTable({ table }: { table: ResultTable }) {
  return table.rows.length ? (
    <ResultTableView table={table} title="Persisted holdout values" />
  ) : (
    <p className="notice">No persisted rows in this window.</p>
  );
}
