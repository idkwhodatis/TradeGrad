import { BenchmarkResult } from "@/components/benchmark-result";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type { PortableResultQuery } from "@/client/types.gen";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import {
  Field,
  JsonDetails,
  Metadata,
  Pagination,
} from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { currentUserOptions } from "@/lib/api";
import {
  downloadPortableResult,
  portableResultsOptions,
} from "@/lib/portable-api";
import {
  rawCell,
  saveResearchDownload,
  type ChartTable,
} from "@/lib/research-results";
import {
  portableNotice,
  portableSections,
  type PortableSection,
} from "./protocol";
const ResearchChart = lazy(() => import("@/components/research-chart"));
const LIMIT = 50;
export function PortableResultPage() {
  const { evaluationId } = useParams({ strict: false }) as {
    evaluationId: string;
  };
  const user = useQuery(currentUserOptions);
  return user.isPending ? (
    <Loading text="Verifying portable result owner…" />
  ) : user.error ? (
    <ErrorState error={user.error} retry={() => void user.refetch()} />
  ) : user.data ? (
    <PortableResultContent
      key={`${user.data.id}:${evaluationId}`}
      owner={user.data.id}
      evaluationId={evaluationId}
    />
  ) : null;
}
export function portableChart(
  rows: PortableResultQuery["rows"],
  fields: string[],
  currency: string,
): ChartTable {
  return {
    columns: [
      {
        name: "native_at",
        dtype: "datetime",
        unit: "Native engine UTC timestamp",
      },
      ...fields.map((name) => ({
        name,
        dtype: "number",
        unit: name === "drawdown" ? "fraction" : currency,
      })),
    ],
    rows: rows.map((row) => [
      typeof row.native_at === "string" ? row.native_at : null,
      ...fields.map((name) =>
        typeof row[name] === "number" && Number.isFinite(row[name])
          ? (row[name] as number)
          : null,
      ),
    ]),
  };
}
export function portableResultContextMatches(
  result: PortableResultQuery,
  evaluationId: string,
  section: PortableSection,
  offset: number,
) {
  return (
    result.evaluation_id === evaluationId &&
    result.evaluation_mode === "portable" &&
    result.schema_version === "tradegrad.portable-result.v1" &&
    result.result_trust === "isolated_policy_trusted_akquant_engine" &&
    result.ledger_origin === "trusted_engine" &&
    result.runtime_certified === false &&
    result.external_framework_equivalence === "not_certified" &&
    result.section === section &&
    result.offset === offset
  );
}
export function PortableResultContent({
  owner,
  evaluationId,
}: {
  owner: string;
  evaluationId: string;
}) {
  const [section, setSection] = useState<PortableSection>("decision_trace"),
    [offset, setOffset] = useState(0),
    [chartOffset, setChartOffset] = useState(0),
    [showCharts, setShowCharts] = useState(false);
  const charts = useQuery(
    portableResultsOptions(
      owner,
      evaluationId,
      "account_series",
      chartOffset,
      LIMIT,
    ),
  );
  const rows = useQuery(
    portableResultsOptions(owner, evaluationId, section, offset, LIMIT),
  );
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
    if (lock.current || !mounted.current) return;
    lock.current = true;
    setDownloading(true);
    setDownloadError(null);
    try {
      const blob = await downloadPortableResult(evaluationId);
      if (mounted.current)
        saveResearchDownload(blob, `portable-evaluation-${evaluationId}.json`);
    } catch (error) {
      if (mounted.current) setDownloadError(error);
    } finally {
      lock.current = false;
      if (mounted.current) setDownloading(false);
    }
  }
  if (charts.isPending)
    return (
      <Loading text="Loading integrity-verified persisted portable artifact…" />
    );
  if (charts.error)
    return (
      <>
        <PageHeader
          title="Portable evidence unavailable"
          subtitle="Failed, incomplete or unverified evaluations are not successful results."
        />
        <ErrorState error={charts.error} retry={() => void charts.refetch()} />
        <Link to="/portable/new" className="text-link mt-5">
          Return to portable experiments
        </Link>
      </>
    );
  const data = charts.data;
  if (!data)
    return (
      <p className="notice">No persisted portable evidence is available.</p>
    );
  if (
    !portableResultContextMatches(
      data,
      evaluationId,
      "account_series",
      chartOffset,
    )
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
    <>
      <Link
        to="/portable/runs/$runId"
        params={{ runId: data.run_id }}
        className="text-link mb-5"
      >
        Portable research run
      </Link>
      <PageHeader
        eyebrow={`PORTABLE EVALUATION · ${evaluationId}`}
        title="Persisted portable execution evidence"
        subtitle="An isolated policy contract and independently persisted trusted-engine ledger, not external-runtime certification."
      />
      <p className="notice" data-testid="portable-trust">
        {portableNotice}
      </p>
      <div className="stack mt-6">
        <Panel title="Full-window result identity and metrics">
          <p className="notice" data-testid="portable-data-evidence">
            {data.is_synthetic
              ? "Explicit synthetic raw/no-actions fixture. Not market observations, live-provider acceptance or investment evidence."
              : "Non-synthetic upload: raw/no-actions claims are unverified user research assumptions, not provider verification. Inspect pinned declarations, source hashes and calendar evidence."}{" "}
            Model provenance and usage belong to the immutable source run; this
            ledger does not certify model quality or billing.
          </p>
          <Metadata
            values={{
              "Evaluation ID": data.evaluation_id,
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
              : "Download original portable result JSON"}
          </Button>
        </Panel>
        <Panel title="Immutable benchmark comparison">
          <BenchmarkResult value={data.benchmark} />
        </Panel>
        <Panel title="Persisted account charts · bounded page">
          <p className="notice">
            Only the returned {LIMIT}-row account page is plotted. There is no
            synthetic starting equity, forward-filled gap, fabricated benchmark
            or full-history claim.
          </p>
          <Button
            variant="outline"
            className="mt-4"
            onClick={() => setShowCharts(!showCharts)}
          >
            {showCharts
              ? "Hide account charts"
              : "Show persisted account charts"}
          </Button>
          {showCharts && (
            <Suspense
              fallback={<Loading text="Loading local chart renderer…" />}
            >
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
            id="portable-result-section"
            label="Persisted portable result section"
          >
            <select
              id="portable-result-section"
              value={section}
              onChange={(event) => {
                setSection(event.target.value as PortableSection);
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
          {rows.isPending ? (
            <Loading text="Loading bounded persisted evidence…" />
          ) : rows.error ? (
            <ErrorState error={rows.error} retry={() => void rows.refetch()} />
          ) : rows.data ? (
            !portableResultContextMatches(
              rows.data,
              evaluationId,
              section,
              offset,
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
      </div>
    </>
  );
}
export function PortableTable({
  rows,
  label,
}: {
  rows: PortableResultQuery["rows"];
  label: string;
}) {
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  return (
    <div
      className="table-scroll mt-4"
      role="region"
      tabIndex={0}
      aria-label={label}
    >
      {rows.length ? (
        <table>
          <caption className="sr-only">{label}</caption>
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column} scope="col">
                  {column.replaceAll("_", " ")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                {columns.map((column) => (
                  <td key={column}>{rawCell(row[column])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="empty">
          No persisted rows in this page. No zero-valued records or decisions
          are invented.
        </p>
      )}
    </div>
  );
}
