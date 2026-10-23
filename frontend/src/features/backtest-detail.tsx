import {
  PriceReplayPanel,
  ReturnAnalysisPanel,
  type FillTarget,
} from "@/components/result-exploration";
import { fillNavigation } from "@/lib/result-exploration";
import { BenchmarkResult } from "@/components/benchmark-result";
import { lazy, Suspense, useMemo, useRef, useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download } from "lucide-react";
import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import type {
  ResultMetric,
  ResultQueryPublic,
  ResultTable,
} from "@/client/types.gen";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import {
  Field,
  JsonDetails,
  Metadata,
  Pagination,
} from "@/components/data-common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { datasetOptions } from "@/lib/data-api";
import { records, snapshotLabel } from "@/lib/data-utils";
import { label, shortId } from "@/lib/format";
import { downloadResearchResult, resultsOptions } from "@/lib/research-api";
import {
  metricText,
  rawCell,
  researchTrustNotice,
  resultSections,
  resultEvidence,
  resultWindowError,
  saveResearchDownload,
  type ResultSection,
} from "@/lib/research-results";
import { useDataAction } from "@/lib/use-data-action";
const ResearchChart = lazy(() => import("@/components/research-chart"));
const TABLE_LIMIT = 50;
const MAX_POINTS = 600;

export function BacktestDetailPage() {
  const { evaluationId } = useParams({ strict: false }) as {
    evaluationId: string;
  };
  // This bounded endpoint reads immutable persisted artifacts. Do not use the
  // legacy evaluation detail response, which embeds the entire result payload.
  const results = useQuery(
    resultsOptions(evaluationId, {
      sections: ["equity", "drawdown"],
      max_points: MAX_POINTS,
    }),
  );
  return results.isPending ? (
    <Loading text="Loading verified persisted result artifact…" />
  ) : results.error ? (
    <>
      <PageHeader
        title="Backtest evidence unavailable"
        subtitle="A missing, invalid or failed evaluation is not a successful backtest."
      />
      <ErrorState error={results.error} retry={() => void results.refetch()} />
      <Link className="text-link mt-5" to="/runs">
        Return to experiments
      </Link>
    </>
  ) : results.data ? (
    <BacktestContent key={evaluationId} result={results.data} />
  ) : null;
}
function BacktestContent({ result }: { result: ResultQueryPublic }) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [listing, setListing] = useState("");
  const [filter, setFilter] = useState({ start: "", end: "", listing: "" });
  const [filterError, setFilterError] = useState<string | null>(null);
  const [section, setSection] = useState<ResultSection>("positions");
  const [offset, setOffset] = useState(0);
  const [sortBy, setSortBy] = useState("");
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc");
  const [side, setSide] = useState<"" | "buy" | "sell">("");
  const [orderStatus, setOrderStatus] = useState("");
  const [replayTarget, setReplayTarget] = useState<FillTarget | null>(null);
  const [showCharts, setShowCharts] = useState(true);
  const snapshot = useQuery(datasetOptions(result.config.dataset.id));
  const listings = snapshot.data
    ? records(snapshot.data.manifest.listings)
    : [];
  const boundedFilter = {
    start: filter.start || undefined,
    end: filter.end || undefined,
    listing_id: filter.listing || undefined,
  };
  const charts = useQuery(
    resultsOptions(result.evaluation_id, {
      sections: ["equity", "drawdown"],
      max_points: MAX_POINTS,
      ...boundedFilter,
    }),
  );
  const rows = useQuery(
    resultsOptions(result.evaluation_id, {
      sections: [section],
      sort_by: sortBy || undefined,
      sort_direction: sortBy ? sortDirection : "asc",
      side: side || undefined,
      order_status: orderStatus || undefined,
      offset,
      limit: TABLE_LIMIT,
      max_points: MAX_POINTS,
      ...boundedFilter,
    }),
  );
  const download = useDataAction(async (format: "json" | "html") => {
    const blob = await downloadResearchResult(result.evaluation_id, format);
    saveResearchDownload(blob, `evaluation-${result.evaluation_id}.${format}`);
  });
  const market = result.config.market_profile;
  const evidence = resultEvidence(result);
  function applyFilters(event: React.FormEvent) {
    event.preventDefault();
    const dateError = resultWindowError(start, end);
    const listingError =
      listing &&
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
        listing,
      )
        ? "Choose a valid listing UUID from the pinned dataset."
        : null;
    if (dateError || listingError) {
      setFilterError(dateError || listingError);
      return;
    }
    if (
      (start && start < result.window.start) ||
      (end && end > result.window.end)
    ) {
      setFilterError(
        "Choose a window inside this persisted evaluation, without changing its original metrics.",
      );
      return;
    }
    setFilterError(null);
    setReplayTarget(null);
    setFilter({ start, end, listing });
    setOffset(0);
  }
  return (
    <>
      <Link
        to="/research-runs/$runId"
        params={{ runId: result.run_id }}
        className="text-link mb-5"
      >
        <ArrowLeft size={14} />
        Research run
      </Link>
      <PageHeader
        eyebrow={`BACKTEST ${shortId(result.evaluation_id)}`}
        title={result.candidate_key}
        subtitle={`${label(result.split)} evaluation · [${result.window.start}, ${result.window.end}) · ${result.currency}`}
      >
        <div className="actions">
          <Button
            variant="outline"
            disabled={download.pending}
            onClick={() => void download.run("json")}
          >
            <Download />
            Original result JSON
          </Button>
          <Button
            variant="outline"
            disabled={download.pending}
            onClick={() => void download.run("html")}
          >
            <Download />
            Download safe report
          </Button>
        </div>
      </PageHeader>
      <p className="notice" data-testid="result-trust">
        {researchTrustNotice}
      </p>
      <p className="notice mt-4" data-testid="result-evidence">
        {evidence.dataset}. {evidence.model}.
      </p>
      {Boolean(download.error) && <ErrorState error={download.error} />}
      {result.warnings.map((warning, index) => (
        <p className="notice mt-4" key={index}>
          {warning}
        </p>
      ))}
      <p className="text-xs muted mt-4">
        The HTML report is downloaded, never embedded in this authenticated
        application. The server escapes report text and excludes scripts.
        Original JSON retains persisted precision and the complete artifact,
        independent of chart filters.
      </p>
      <div className="stack mt-6">
        <Panel title="Window metrics and definitions">
          <ResultMetrics metrics={result.metrics} />
          <p className="text-xs muted mt-4">
            Metrics cover the full persisted evaluation window, not the chart
            subwindow. Display rounding never changes the stored value, reward
            rounding or optimization objective. Inspect each row for its exact
            definition and original value.
          </p>
        </Panel>
        <Panel title="Bounded result explorer">
          <form onSubmit={applyFilters} className="form-grid">
            <Field
              label="Start date (inclusive)"
              id="result-start"
              help="Leave both dates empty for the full evaluation window."
            >
              <Input
                id="result-start"
                type="date"
                value={start}
                onChange={(event) => setStart(event.target.value)}
              />
            </Field>
            <Field label="End date (exclusive)" id="result-end">
              <Input
                id="result-end"
                type="date"
                value={end}
                onChange={(event) => setEnd(event.target.value)}
              />
            </Field>
            <Field
              label="Listing filter"
              id="result-listing"
              help="Portfolio equity and drawdown are never relabeled as single-listing performance."
            >
              {listings.length ? (
                <select
                  id="result-listing"
                  value={listing}
                  onChange={(event) => setListing(event.target.value)}
                >
                  <option value="">All listings</option>
                  {listings.map((item) => {
                    const id = String(item.listing_id || item.id || "");
                    return id ? (
                      <option key={id} value={id}>
                        {String(
                          item.symbol ||
                            item.provider_symbol ||
                            item.name ||
                            id,
                        )}
                      </option>
                    ) : null;
                  })}
                </select>
              ) : (
                <Input
                  id="result-listing"
                  placeholder="Optional listing UUID"
                  value={listing}
                  onChange={(event) => setListing(event.target.value)}
                />
              )}
            </Field>
            <div className="actions">
              <Button
                type="submit"
                disabled={charts.isFetching || rows.isFetching}
              >
                Apply result filters
              </Button>
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  setStart("");
                  setEnd("");
                  setListing("");
                  setReplayTarget(null);
                  setFilter({ start: "", end: "", listing: "" });
                  setOffset(0);
                  setFilterError(null);
                }}
              >
                Reset filters
              </Button>
            </div>
          </form>
          {filterError && (
            <p className="notice error mt-4" role="alert">
              {filterError}
            </p>
          )}
          <p className="text-xs muted mt-4">
            Active chart/table window: [{filter.start || result.window.start},{" "}
            {filter.end || result.window.end}). Listing:{" "}
            {filter.listing || "all"}. At most {MAX_POINTS} chart points per
            series, with server min/max downsampling when required. Raw
            transaction pages request {TABLE_LIMIT} rows.
          </p>
          <Button
            variant="outline"
            className="mt-4"
            onClick={() => setShowCharts((show) => !show)}
          >
            {showCharts ? "Hide charts" : "Show equity and drawdown charts"}
          </Button>
          {charts.isPending ? (
            <Loading />
          ) : charts.error ? (
            <ErrorState
              error={charts.error}
              retry={() => void charts.refetch()}
            />
          ) : (
            charts.data && (
              <div className="stack mt-5">
                {(["equity", "drawdown"] as const).map((name) => (
                  <section key={name} aria-label={`${label(name)} series`}>
                    <h3 className="!text-sm">
                      {label(name)} ·{" "}
                      {name === "equity"
                        ? result.currency
                        : "positive fraction"}
                    </h3>
                    {charts.data.tables[name] ? (
                      <>
                        <p className="text-xs muted mt-2">
                          {name === "drawdown"
                            ? "Drawdown = 1 − equity / cumulative peak; 0.10 means 10%. Null when the peak is zero. No synthetic initial bar."
                            : "Portfolio equity from the native engine; not independently reconciled."}
                        </p>
                        {showCharts && (
                          <Suspense
                            fallback={
                              <Loading text="Loading self-hosted chart…" />
                            }
                          >
                            <ResearchChart
                              title={`${label(name)} (${name === "equity" ? result.currency : "fraction"})`}
                              table={charts.data.tables[name]}
                            />
                          </Suspense>
                        )}
                        <ResultTableView
                          table={charts.data.tables[name]}
                          title={`${label(name)} returned points`}
                          collapsed
                        />
                      </>
                    ) : (
                      <p className="notice mt-2">
                        No persisted {name} series is available.
                      </p>
                    )}
                  </section>
                ))}
              </div>
            )
          )}
          <BenchmarkResult value={charts.data?.benchmark ?? result.benchmark} />
        </Panel>
        <PriceReplayPanel
          result={result}
          listings={listings}
          filter={filter}
          target={replayTarget}
        />
        <ReturnAnalysisPanel reference={result} />
        <Panel title="Positions, orders, fills and account series">
          <Field label="Persisted result table" id="result-section">
            <select
              id="result-section"
              value={section}
              onChange={(event) => {
                setSection(event.target.value as ResultSection);
                setSortBy("");
                setSortDirection("asc");
                setSide("");
                setOrderStatus("");
                setOffset(0);
              }}
            >
              {resultSections.map((name) => (
                <option value={name} key={name}>
                  {label(name)}
                </option>
              ))}
            </select>
          </Field>
          <div className="form-grid mt-4">
            <Field label="Server sort column" id="result-sort">
              <select
                id="result-sort"
                value={sortBy}
                onChange={(event) => {
                  setSortBy(event.target.value);
                  setOffset(0);
                }}
              >
                <option value="">Persisted row order</option>
                {rows.data?.tables[section]?.sortable_columns?.map((column) => (
                  <option key={column} value={column}>
                    {column}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Server sort direction" id="result-sort-direction">
              <select
                id="result-sort-direction"
                value={sortDirection}
                disabled={!sortBy}
                onChange={(event) => {
                  setSortDirection(event.target.value as "asc" | "desc");
                  setOffset(0);
                }}
              >
                <option value="asc">Ascending</option>
                <option value="desc">Descending</option>
              </select>
            </Field>
            {["fills", "orders"].includes(section) && (
              <Field label="Execution side filter" id="result-side">
                <select
                  id="result-side"
                  value={side}
                  onChange={(event) => {
                    setSide(event.target.value as "" | "buy" | "sell");
                    setOffset(0);
                  }}
                >
                  <option value="">All sides</option>
                  <option value="buy">Buy</option>
                  <option value="sell">Sell</option>
                </select>
              </Field>
            )}
            {section === "orders" && (
              <Field label="Order status filter" id="result-order-status">
                <select
                  id="result-order-status"
                  value={orderStatus}
                  onChange={(event) => {
                    setOrderStatus(event.target.value);
                    setOffset(0);
                  }}
                >
                  <option value="">All statuses</option>
                  {[
                    "new",
                    "pending",
                    "submitted",
                    "accepted",
                    "partially_filled",
                    "filled",
                    "cancelled",
                    "canceled",
                    "rejected",
                    "expired",
                  ].map((status) => (
                    <option value={status} key={status}>
                      {label(status)}
                    </option>
                  ))}
                </select>
              </Field>
            )}
          </div>
          <p className="text-xs muted mt-3">
            Sorts and filters run on the complete verified table before bounded
            pagination. Equal values preserve original row order; nulls are
            last. Series stay chronological. Timestamps retain the engine's
            declared column units. Date windows are [start, end). Orders filter
            by created_at, fills by timestamp, positions by date, and closed
            trades by exit_time. An observable order trace is not an internal
            strategy-decision trace.
          </p>
          {rows.isPending ? (
            <Loading />
          ) : rows.error ? (
            <ErrorState error={rows.error} retry={() => void rows.refetch()} />
          ) : rows.data?.tables[section] ? (
            <>
              <ResultTableView
                table={rows.data.tables[section]}
                title={`${label(section)} table`}
                onReviewFill={
                  section === "fills"
                    ? (index) => {
                        const navigation = fillNavigation(
                          rows.data!.tables[section]!,
                          index,
                          (snapshot.data?.manifest.evaluator_symbol_mapping ??
                            {}) as Record<string, unknown>,
                        );
                        if (navigation) {
                          setReplayTarget(navigation);
                          document
                            .getElementById("replay-listing")
                            ?.scrollIntoView({
                              behavior: "smooth",
                              block: "center",
                            });
                        }
                      }
                    : undefined
                }
              />
              {["equity", "drawdown", "cash", "margin"].includes(section) ? (
                <p className="text-xs muted mt-3">
                  Series are bounded to {MAX_POINTS} server-selected points, not
                  transaction-paginated. Download original JSON for full
                  precision/history.
                </p>
              ) : (
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
              This result does not contain the requested table. No records are
              invented.
            </p>
          )}
        </Panel>
        <div className="section-grid">
          <Panel title="Fees and execution cost assumptions">
            <Metadata
              values={{
                "Total fees from fills": `${result.fees.total_from_fills} ${result.fees.unit}`,
                "Native total commission": `${result.fees.native_total_commission} ${result.fees.unit}`,
                "Fee definition": result.fees.definition_version,
                "Calculation basis": result.fees.calculation_basis,
                "Commission rate (fraction)": market.commission_rate,
                "Stamp tax rate (fraction)": market.stamp_tax_rate,
                "Slippage model": market.slippage,
                "Initial capital": `${market.initial_cash} ${market.currency}`,
                "Commission/stamp/transfer breakdown":
                  result.fees.breakdown_status,
                "Separately measured slippage cost":
                  result.fees.slippage_cost_status,
              }}
            />
            <p className="text-xs muted mt-4">
              Rates are historical demonstration assumptions, not current broker
              terms. A 0.0005 fractional rate is 0.05%. Fee components and
              slippage costs are unavailable separately, not zero. No fabricated
              per-component ledger.
            </p>
          </Panel>
          <Panel title="Immutable provenance and limitations">
            <Metadata
              values={{
                Profile: result.config.profile,
                "Data evidence": snapshot.data
                  ? snapshotLabel(snapshot.data)
                  : "Inspect pinned snapshot",
                "Dataset version": result.config.dataset.id,
                "Dataset SHA-256": result.config.dataset.sha256,
                "Universe version": result.config.universe.id,
                "Code SHA-256": result.code_sha256,
                "Config SHA-256": result.config_sha256,
                "Artifact SHA-256": result.artifact_sha256,
                "Price definition": result.price_definition,
                "Selection split": result.split,
                "Warm-up": result.config.warmup,
                Calendar: market.calendar,
                "Calendar limitation": market.calendar_note,
                "Time zone": market.timezone,
                "Trusted ledger": false,
              }}
            />
            {snapshot.error && (
              <ErrorState
                error={snapshot.error}
                retry={() => void snapshot.refetch()}
              />
            )}
            <JsonDetails
              title="Complete pinned configuration and capabilities"
              value={{
                config: result.config,
                capabilities: result.capabilities,
              }}
            />
          </Panel>
        </div>
      </div>
    </>
  );
}
export function ResultMetrics({ metrics }: { metrics: ResultMetric[] }) {
  const columns: ColumnDef<ResultMetric>[] = [
    {
      accessorKey: "name",
      header: "Metric",
      cell: ({ row }) => (
        <>
          <span>{label(row.original.name)}</span>
          {row.original.unavailable_reason && (
            <p className="text-xs muted">{row.original.unavailable_reason}</p>
          )}
        </>
      ),
    },
    {
      id: "display",
      header: "Display",
      cell: ({ row }) => metricText(row.original),
    },
    {
      accessorKey: "value",
      header: "Stored original value",
      cell: ({ getValue }) => rawCell(getValue()),
    },
    { accessorKey: "unit", header: "Unit" },
    { accessorKey: "definition_version", header: "Definition version" },
    { accessorKey: "calculation_basis", header: "Calculation basis" },
  ];
  return (
    <DataTable
      data={metrics}
      columns={columns}
      label="Result metrics with units and definitions"
      empty="No declared metrics are available."
    />
  );
}
export function ResultTableView({
  table,
  title,
  collapsed = false,
  onReviewFill,
}: {
  table: ResultTable;
  title: string;
  collapsed?: boolean;
  onReviewFill?: (index: number) => void;
}) {
  const columns: ColumnDef<ResultTable["rows"][number]>[] = useMemo(
    () => [
      ...(onReviewFill
        ? [
            {
              id: "review-fill",
              header: "Replay",
              cell: ({ row }: { row: { index: number } }) => (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onReviewFill(row.index)}
                >
                  Review fill {table.row_indices?.[row.index] ?? row.index}
                </Button>
              ),
            },
          ]
        : []),
      ...table.columns.map<ColumnDef<ResultTable["rows"][number]>>(
        (column, index) => ({
          id: `${index}:${column.name}`,
          accessorFn: (row) => row[index],
          header: `${column.name} (${column.unit})`,
          cell: ({ getValue }) => rawCell(getValue()),
        }),
      ),
    ],
    [table.columns, table.row_indices, onReviewFill],
  );
  const content = (
    <>
      <p className="text-xs muted my-3">
        {table.returned_rows} returned / {table.filtered_rows} filtered /{" "}
        {table.total_rows} total rows.{" "}
        {table.downsampled
          ? `Downsampled (${table.sampling}); not the raw complete history.`
          : "No downsampling."}{" "}
        Listing filter: {label(table.filter_status)}.
      </p>
      {table.rows.length > 100 ? (
        <VirtualResultTable data={table.rows} columns={columns} title={title} />
      ) : (
        <DataTable
          data={table.rows}
          columns={columns}
          label={title}
          empty="No persisted rows match this selection."
        />
      )}
    </>
  );
  return collapsed ? (
    <details className="mt-3">
      <summary className="text-link">
        {title} · accessible original numeric values
      </summary>
      {content}
    </details>
  ) : (
    <div className="mt-3">{content}</div>
  );
}

function VirtualResultTable({
  data,
  columns,
  title,
}: {
  data: ResultTable["rows"];
  columns: ColumnDef<ResultTable["rows"][number]>[];
  title: string;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const [showAll, setShowAll] = useState(false);
  const table = useReactTable({
    data,
    columns,
    getCoreRowModel: getCoreRowModel(),
    manualPagination: true,
  });
  const rows = table.getRowModel().rows;
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => viewport.current,
    estimateSize: () => 40,
    overscan: 8,
  });
  const visible = virtualizer.getVirtualItems();
  const indices = visible.length
    ? visible.map((item) => item.index)
    : rows.slice(0, 12).map((_, index) => index);
  const top = visible[0]?.start || 0;
  const bottom = Math.max(
    0,
    virtualizer.getTotalSize() - (visible.at(-1)?.end || indices.length * 40),
  );
  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setShowAll((value) => !value)}
      >
        {showAll
          ? "Virtualize returned rows"
          : "Show all returned rows for accessibility"}
      </Button>
      <p className="text-xs muted my-2">
        {data.length} bounded returned rows.{" "}
        {showAll
          ? "All returned rows are rendered."
          : "Scroll inside the table to inspect additional rows; optional all-rows mode supports screen readers and browser search."}
      </p>
      {showAll ? (
        <DataTable data={data} columns={columns} label={title} />
      ) : (
        <div
          ref={viewport}
          className="table-scroll"
          style={{ maxHeight: 440, overflow: "auto" }}
          tabIndex={0}
          role="region"
          aria-label={`${title}; scrollable returned rows`}
        >
          <table aria-rowcount={rows.length + 1}>
            <caption className="sr-only">{title}</caption>
            <thead
              style={{
                position: "sticky",
                top: 0,
                background: "var(--background, white)",
                zIndex: 1,
              }}
            >
              {table.getHeaderGroups().map((group) => (
                <tr key={group.id}>
                  {group.headers.map((header) => (
                    <th key={header.id} scope="col">
                      {flexRender(
                        header.column.columnDef.header,
                        header.getContext(),
                      )}
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody>
              {top > 0 && (
                <tr aria-hidden="true">
                  <td
                    colSpan={columns.length}
                    style={{ height: top, padding: 0, border: 0 }}
                  />
                </tr>
              )}
              {indices.map((index) => {
                const row = rows[index];
                return (
                  <tr
                    key={row.id}
                    data-index={index}
                    ref={virtualizer.measureElement}
                    aria-rowindex={index + 2}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id}>
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext(),
                        )}
                      </td>
                    ))}
                  </tr>
                );
              })}
              {bottom > 0 && (
                <tr aria-hidden="true">
                  <td
                    colSpan={columns.length}
                    style={{ height: bottom, padding: 0, border: 0 }}
                  />
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
