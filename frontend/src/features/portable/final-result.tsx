import {
  ModeResultExplorer,
  type FillTarget,
} from "@/components/result-exploration";
import {
  ResultTableControls,
  type TableFilters,
} from "@/components/result-table-controls";
import type { ExplorationContext } from "@/client/types.gen";
import { BenchmarkResult } from "@/components/benchmark-result";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { PortableFinalEvaluationDetail } from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import {
  Field,
  JsonDetails,
  Metadata,
  Pagination,
} from "@/components/data-common";
import { Button } from "@/components/ui/button";
import {
  downloadPortableFinalResult,
  portableFinalResultsOptions,
  verifyPortableFinalDownload,
} from "@/lib/portable-final-api";
import { saveResearchDownload } from "@/lib/research-results";
import { portableSections, type PortableSection } from "./protocol";
import {
  acceptedPortableFinalReference,
  portableFinalResultMatches,
} from "./final-protocol";
import { PortableTable, portableChart } from "./result";
import { CreateStrategyPackage } from "@/features/final-tests/strategy-package";
const ResearchChart = lazy(() => import("@/components/research-chart"));
const LIMIT = 50;
export function PortableFinalResult({
  owner,
  job,
}: {
  owner: string;
  job: PortableFinalEvaluationDetail;
}) {
  const evaluationId = acceptedPortableFinalReference(job) || "";
  const [tableFilters, setTableFilters] = useState<TableFilters>({});
  const [exploration, setExploration] = useState<ExplorationContext | null>(
    null,
  );
  const [section, setSection] = useState<PortableSection>("decision_trace"),
    [offset, setOffset] = useState(0),
    [chartOffset, setChartOffset] = useState(0),
    [showCharts, setShowCharts] = useState(false);
  const charts = useQuery(
    portableFinalResultsOptions(
      owner,
      evaluationId,
      "account_series",
      chartOffset,
      LIMIT,
    ),
  );
  const rows = useQuery(
    portableFinalResultsOptions(
      owner,
      evaluationId,
      section,
      offset,
      LIMIT,
      tableFilters,
    ),
  );
  const [fillTarget, setFillTarget] = useState<FillTarget | null>(null);
  const [downloading, setDownloading] = useState(false),
    [downloadError, setDownloadError] = useState<unknown>(null);
  const mounted = useRef(true),
    lock = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function download() {
    if (lock.current || !mounted.current || !evaluationId) return;
    lock.current = true;
    setDownloading(true);
    setDownloadError(null);
    try {
      const blob = await downloadPortableFinalResult(evaluationId);
      const expected = charts.data?.artifact_sha256;
      if (!expected)
        throw new Error(
          "No verified checksum is available for this portable final artifact.",
        );
      await verifyPortableFinalDownload(blob, expected);
      if (mounted.current)
        saveResearchDownload(
          blob,
          `portable-final-evaluation-${evaluationId}.json`,
        );
    } catch (error) {
      if (mounted.current) setDownloadError(error);
    } finally {
      lock.current = false;
      if (mounted.current) setDownloading(false);
    }
  }
  if (!evaluationId)
    return (
      <ErrorState
        error={
          new Error(
            "There is no successful, stopped current-attempt receipt. No result is displayed.",
          )
        }
      />
    );
  if (charts.isPending)
    return (
      <Loading text="Loading integrity-verified persisted portable artifact…" />
    );
  if (charts.error)
    return (
      <Panel title="Portable final evidence unavailable">
        <p className="notice">
          Failed, incomplete or unverified evaluations are not successful
          results.
        </p>
        <ErrorState error={charts.error} retry={() => void charts.refetch()} />
      </Panel>
    );
  const data = charts.data;
  if (!data)
    return (
      <p className="notice">No persisted portable evidence is available.</p>
    );
  if (
    !portableFinalResultMatches(data, job, "account_series", chartOffset, LIMIT)
  )
    return (
      <ErrorState
        error={
          new Error(
            "The persisted artifact does not match this portable evaluation and page. No result is displayed.",
          )
        }
      />
    );
  return (
    <div id="portable-final-result" className="stack mt-6">
      <Panel title="Full-window result identity and metrics">
        <p className="notice" data-testid="portable-final-data-evidence">
          {data.is_synthetic
            ? "Explicit synthetic raw/no-actions fixture. Not market observations, live-provider acceptance or investment evidence."
            : "Non-synthetic upload: raw/no-actions claims are unverified user research assumptions, not provider verification. Inspect pinned declarations, source hashes and calendar evidence."}{" "}
          No optimizer or model calls are performed by this final job. Inspect
          the frozen source run for its model profile and persisted usage.
        </p>
        <Metadata
          values={{
            "Evaluation ID": data.evaluation_id,
            "Frozen selection": data.selection_id,
            "Selection feedback": data.selection_feedback,
            "Final query schema": data.schema_version,
            "Run ID": data.run_id,
            "Result trust": data.result_trust,
            "Ledger origin": data.ledger_origin,
            "Artifact SHA-256": data.artifact_sha256,
            "Trusted engine image ID": data.image_digest,
            "Isolated policy image ID": data.policy_image_digest,
            "Environment SHA-256": data.environment_digest,
            Currency: data.currency,
            "Combined score (return percentage points minus drawdown percentage points)":
              data.combined_score,
            "Runtime certification":
              "Unverified until real-container CI evidence",
            "External frameworks": data.external_framework_equivalence,
          }}
        />
        <Metadata
          values={{
            "Full-window initial cash": data.summary.initial_cash,
            "Full-window final equity": data.summary.final_equity,
            "Full-window total return · percentage points":
              data.summary.total_return_pct,
            "Full-window maximum drawdown · percentage points":
              data.summary.max_drawdown_pct,
            "Full-window executions": data.summary.execution_count,
            "Full-window closed trades": data.summary.closed_trade_count,
            "Full-window commission": data.summary.total_commission,
          }}
        />
        <JsonDetails
          title="Exact evaluation window and original full-window metrics"
          value={{
            window: data.window,
            summary: data.summary,
            identity: data.identity,
          }}
        />
        <p className="text-xs muted mt-4">
          Summary metrics retain their full evaluation window and original
          units. Table paging does not recalculate scores. Native engine
          timestamps are preserved; economic reference timestamps identify
          later-session opens, not rewritten same-bar fills.
        </p>
        {Boolean(downloadError) && <ErrorState error={downloadError} />}
        <Button
          variant="outline"
          className="mt-4"
          disabled={downloading}
          onClick={() => void download()}
        >
          {downloading
            ? "Downloading…"
            : "Download checksum-verified portable final JSON"}
        </Button>
      </Panel>
      <Panel title="Immutable benchmark comparison">
        <BenchmarkResult value={data.benchmark} />
      </Panel>
      <ModeResultExplorer
        mode="portable_final"
        target={fillTarget}
        onContext={setExploration}
        reference={{
          evaluation_id: data.evaluation_id,
          artifact_sha256: data.artifact_sha256,
          window: {
            start: String(data.window.start),
            end: String(data.window.end),
          },
          selection_id: data.selection_id,
          split: "holdout",
        }}
      />
      <Panel title="Persisted account charts · bounded page">
        <p className="notice">
          Only the returned {LIMIT}-row account page is plotted. There is no
          synthetic starting equity, forward-filled gap, fabricated benchmark or
          full-history claim.
        </p>
        <Button
          variant="outline"
          className="mt-4"
          onClick={() => setShowCharts(!showCharts)}
        >
          {showCharts ? "Hide account charts" : "Show persisted account charts"}
        </Button>
        {showCharts && (
          <Suspense fallback={<Loading text="Loading local chart renderer…" />}>
            <ResearchChart
              title="Portable equity and cash"
              table={portableChart(
                data.rows,
                ["equity", "cash"],
                data.currency,
              )}
              xColumn="native_at"
            />
            <ResearchChart
              title="Portable drawdown · fraction"
              table={portableChart(data.rows, ["drawdown"], data.currency)}
              xColumn="native_at"
            />
          </Suspense>
        )}
        <PortableTable
          rows={data.rows}
          label="Persisted portable account series"
        />
        <Pagination
          offset={chartOffset}
          limit={LIMIT}
          count={data.count}
          busy={charts.isFetching}
          onPage={setChartOffset}
        />
      </Panel>
      <Panel title="Decision/state trace and execution tables">
        <Field
          id="portable-final-result-section"
          label="Persisted portable final result section"
        >
          <select
            id="portable-final-result-section"
            value={section}
            onChange={(event) => {
              setSection(event.target.value as PortableSection);
              setTableFilters({});
              setOffset(0);
            }}
          >
            {portableSections.map((name) => (
              <option key={name} value={name}>
                {name.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </Field>
        <p className="text-xs muted mt-3">
          Decision trace exposes warm-up flags, observation and state-chain
          hashes, actual signals/targets, logical-order associations and
          recorded outcomes. Empty decisions distinguish hold, liquidation and
          no decision. No state or signal is reconstructed from prices in this
          UI.
        </p>
        <ResultTableControls
          section={section}
          columns={rows.data?.sortable_columns || []}
          value={tableFilters}
          dates={{
            start: String(data.window.start),
            end: String(data.window.end),
          }}
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
          <Loading text="Loading bounded persisted evidence…" />
        ) : rows.error ? (
          <ErrorState error={rows.error} retry={() => void rows.refetch()} />
        ) : rows.data ? (
          !portableFinalResultMatches(
            rows.data,
            job,
            section,
            offset,
            LIMIT,
          ) ? (
            <ErrorState
              error={
                new Error(
                  "The table response is stale or belongs to a different evaluation. No rows are displayed.",
                )
              }
            />
          ) : (
            <>
              <PortableTable
                rows={rows.data.rows}
                onReviewFill={
                  section === "fills"
                    ? (index) => {
                        const row = rows.data!.rows[index];
                        const original = rows.data!.row_indices?.[index];
                        if (typeof original === "number")
                          setFillTarget({
                            listing: String(row.listing_id),
                            id: `fill:${original}`,
                            timestamp: String(row.native_at),
                          });
                      }
                    : undefined
                }
                rowIndices={rows.data.row_indices || undefined}
                label={`Portable ${section.replaceAll("_", " ")}`}
              />
              <Pagination
                offset={offset}
                limit={LIMIT}
                count={rows.data.count}
                busy={rows.isFetching}
                onPage={setOffset}
              />
            </>
          )
        ) : (
          <p className="empty">No persisted table response is available.</p>
        )}
      </Panel>
      <CreateStrategyPackage
        selectionId={job.final_selection_id}
        evaluationId={evaluationId}
      />
    </div>
  );
}
