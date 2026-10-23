import { lazy, Suspense, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ResultQueryPublic, TradeMarker } from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import {
  Field,
  JsonDetails,
  Metadata,
  Pagination,
} from "@/components/data-common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { priceReplayOptions, returnAnalysisOptions } from "@/lib/research-api";
import {
  localSession,
  navigationWindow,
  replaySelection,
} from "@/lib/result-exploration";
import { label } from "@/lib/format";
const TradePriceChart = lazy(() => import("@/components/trade-price-chart"));
const ReturnAnalysisChart = lazy(
  () => import("@/components/return-analysis-chart"),
);

export type FillTarget = { listing: string; id: string; timestamp: string };

export function PriceReplayPanel({
  result,
  listings,
  filter,
  target,
}: {
  result: ResultQueryPublic;
  listings: Record<string, unknown>[];
  filter: { start: string; end: string; listing: string };
  target: FillTarget | null;
}) {
  const [chosen, setChosen] = useState("");
  const [offset, setOffset] = useState(0);
  const [markerOffset, setMarkerOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [focus, setFocus] = useState<{ start: string; end: string } | null>(
    null,
  );
  const listing =
    filter.listing ||
    chosen ||
    String(listings[0]?.listing_id || listings[0]?.id || "");
  useEffect(() => {
    setOffset(0);
    setMarkerOffset(0);
    setFocus(null);
    setSelectedId(null);
  }, [filter]);
  useEffect(() => {
    if (!target) return;
    setChosen(target.listing);
    setOffset(0);
    setMarkerOffset(0);
    setSelectedId(target.id);
    const day = localSession(target.timestamp, result.engine_timezone);
    setFocus(navigationWindow(day, result.window.start, result.window.end));
  }, [target, result]);
  const replay = useQuery(
    priceReplayOptions(result.evaluation_id, {
      listing_id: target && focus ? target.listing : listing,
      start: focus?.start || filter.start || result.window.start,
      end: focus?.end || filter.end || result.window.end,
      offset,
      limit: 250,
    }),
  );
  const data = replay.data;
  const selection = data ? replaySelection(data, selectedId) : null;
  const select = (id: string | null) => setSelectedId(id);
  return (
    <Panel title="OHLCV and execution replay">
      <Field
        label="Replay listing"
        id="replay-listing"
        help="A single pinned listing is required. Portfolio equity is not converted into listing performance."
      >
        <select
          id="replay-listing"
          value={target && focus ? target.listing : listing}
          disabled={Boolean(filter.listing)}
          onChange={(event) => {
            setChosen(event.target.value);
            setFocus(null);
            setSelectedId(null);
            setOffset(0);
            setMarkerOffset(0);
          }}
        >
          {!listings.length && (
            <option value="">No pinned listing available</option>
          )}
          {listings.map((item) => {
            const id = String(item.listing_id || item.id || "");
            return (
              <option value={id} key={id}>
                {String(item.symbol || item.provider_symbol || item.name || id)}
              </option>
            );
          })}
        </select>
      </Field>
      {!listing ? (
        <p className="notice mt-3">
          Select a listing with persisted OHLCV. No prices or indicators are
          fabricated.
        </p>
      ) : replay.isPending ? (
        <Loading />
      ) : replay.error ? (
        <ErrorState error={replay.error} retry={() => void replay.refetch()} />
      ) : (
        data && (
          <div className="stack mt-4">
            <p className="text-xs muted" data-testid="replay-window">
              Replay window [{data.start}, {data.end}) · {data.timezone}.{" "}
              {data.bars.length} / {data.bar_count} OHLCV bars. Actual fill
              markers: {data.markers.length} / {data.marker_count} in this
              page's date extent.
            </p>
            {focus && (
              <Button
                variant="outline"
                onClick={() => {
                  setFocus(null);
                  setSelectedId(null);
                  setOffset(0);
                }}
              >
                Reset trade focus
              </Button>
            )}
            {data.warnings.map((warning) => (
              <p className="notice" key={warning}>
                {warning}
              </p>
            ))}
            {data.markers_truncated && (
              <p className="notice error">
                Execution marker budget reached. Narrow the date window; only
                the first {data.markers.length} of {data.marker_count} fills are
                displayed, not a complete execution history.
              </p>
            )}
            {data.bars.length ? (
              <Suspense
                fallback={<Loading text="Loading persisted price replay…" />}
              >
                <TradePriceChart
                  data={data}
                  selectedId={selectedId}
                  onSelect={select}
                />
              </Suspense>
            ) : (
              <p className="notice">
                No persisted OHLCV bars match this window. Fill prices are not
                substituted as candles.
              </p>
            )}
            <Pagination
              offset={offset}
              limit={250}
              count={data.bar_count}
              busy={replay.isFetching}
              onPage={(page) => {
                setOffset(page);
                setSelectedId(null);
                setMarkerOffset(0);
              }}
            />
            <section aria-label="Selected execution details">
              <h3 className="!text-sm">Selected execution</h3>
              <div className="actions mt-2">
                <Button
                  variant="outline"
                  disabled={!selection?.previous}
                  onClick={() => select(selection?.previous ?? null)}
                >
                  Previous execution
                </Button>
                <Button
                  variant="outline"
                  disabled={!selection?.next}
                  onClick={() => select(selection?.next ?? null)}
                >
                  Next execution
                </Button>
                <Button
                  variant="ghost"
                  disabled={!selectedId}
                  onClick={() => select(null)}
                >
                  Clear execution selection
                </Button>
              </div>
              {selection?.selected ? (
                <>
                  <Metadata
                    values={{
                      "Fill reference": selection.selected.id,
                      "Order ID": selection.selected.order_id,
                      "Exact timestamp": selection.selected.timestamp,
                      "Engine-local session": selection.selected.session_date,
                      Side: selection.selected.side,
                      "Original fill price": selection.selected.price,
                      "Original quantity": selection.selected.quantity,
                      "Original commission": selection.selected.commission,
                    }}
                  />
                  {!selection.selected.on_price_bar && (
                    <p className="notice error">
                      This fill has no matching persisted OHLCV bar. It is
                      listed but not drawn on a neighbouring session.
                    </p>
                  )}
                </>
              ) : (
                <p className="text-xs muted mt-2">
                  {selectedId
                    ? "The selected fill is outside this returned price page. Narrow the window or use the result table's Review fill action."
                    : "Choose an actual fill below. This selects an execution, not an inferred entry/exit round trip."}
                </p>
              )}
            </section>
            <DataTable<TradeMarker>
              data={data.markers.slice(markerOffset, markerOffset + 50)}
              label="Actual execution navigation"
              empty="No persisted fills in this price page's date extent."
              columns={[
                {
                  id: "select",
                  header: "Review",
                  cell: ({ row }) => (
                    <Button
                      variant={
                        selectedId === row.original.id ? "default" : "outline"
                      }
                      size="sm"
                      onClick={() => select(row.original.id)}
                      aria-pressed={selectedId === row.original.id}
                    >
                      Select {row.original.id}
                    </Button>
                  ),
                },
                { accessorKey: "session_date", header: "Engine-local session" },
                { accessorKey: "side", header: "Side" },
                { accessorKey: "price", header: "Original price" },
                { accessorKey: "quantity", header: "Quantity" },
                { accessorKey: "commission", header: "Commission" },
                {
                  id: "bar",
                  header: "OHLCV match",
                  cell: ({ row }) =>
                    row.original.on_price_bar
                      ? "Exact session"
                      : "Missing bar, not plotted",
                },
              ]}
            />
            <Pagination
              offset={markerOffset}
              limit={50}
              count={data.markers.length}
              busy={replay.isFetching}
              onPage={setMarkerOffset}
            />
            <section aria-label="Persisted indicator availability">
              <h3 className="!text-sm">
                Declared and recorded indicators ·{" "}
                {label(data.indicators_status)}
              </h3>
              <p className="notice mt-2">{data.indicators_reason}</p>
              {data.indicators.map((indicator) => (
                <div key={indicator.instance_id} className="mt-3">
                  <p>
                    {indicator.name} · {indicator.unit} · declared pane{" "}
                    {indicator.pane} · {label(indicator.status)}
                  </p>
                  <p className="text-xs muted">
                    {indicator.reason} {indicator.total_points} recorded points
                    in this page; {indicator.warmup_excluded} warm-up points
                    excluded; {indicator.duplicate_session_points} same-session
                    points collapsed to the last recorded value.
                  </p>
                  <JsonDetails
                    title={`${indicator.name} recorded metadata and original returned values`}
                    value={indicator}
                  />
                </div>
              ))}
            </section>
            <JsonDetails
              title="OHLCV page original values and quality flags"
              value={data.bars}
            />
          </div>
        )
      )}
    </Panel>
  );
}

export function ReturnAnalysisPanel({
  evaluationId,
}: {
  evaluationId: string;
}) {
  const query = useQuery(returnAnalysisOptions(evaluationId));
  const [offset, setOffset] = useState(0);
  const data = query.data;
  return (
    <Panel title="Monthly returns and observed-return distribution">
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        data && (
          <div className="stack">
            <p className="notice">
              Full persisted evaluation portfolio only. Chart, listing and table
              filters do not change this analysis or optimizer metrics.
            </p>
            <p className="text-xs muted">
              Definition: {data.definition_version}. Units: fraction (0.10 =
              10%). Coverage:{" "}
              {label(
                data.coverage_status ||
                  "exchange_session_completeness_not_verified",
              )}
              .
            </p>
            {data.warnings.map((warning) => (
              <p className="text-xs muted" key={warning}>
                {warning}
              </p>
            ))}
            <p className="text-xs muted">
              Pinned expected sessions:{" "}
              {data.expected_sessions ?? "Unavailable"}; missing equity
              sessions: {data.missing_sessions?.length ?? 0}; unexpected
              observed dates: {data.unexpected_observations?.length ?? 0}.
            </p>
            {Boolean(
              data.missing_sessions?.length ||
              data.unexpected_observations?.length,
            ) && (
              <JsonDetails
                title="Missing and unexpected equity session dates"
                value={{
                  missing: data.missing_sessions,
                  unexpected: data.unexpected_observations,
                }}
              />
            )}
            <section aria-label="Monthly return analysis">
              <p className="text-sm">{data.monthly_definition}</p>
              <p className="notice mt-2">
                * Purple border means a partial observed period, not a full
                calendar-month return. Missing months have no colored cell.
                Coverage is compared with pinned calendar evidence when
                available; matching dates are not an independently trusted
                ledger.
              </p>
              {data.months.some((item) => item.value !== null) ? (
                <Suspense fallback={<Loading />}>
                  <ReturnAnalysisChart value={data} kind="monthly" />
                </Suspense>
              ) : (
                <p className="notice">
                  Monthly returns unavailable: no valid pair of observed closing
                  equities.
                </p>
              )}
              <DataTable
                data={data.months.slice(offset, offset + 24)}
                label="Monthly return values and coverage"
                empty="No persisted calendar months."
                columns={[
                  { accessorKey: "month", header: "Month" },
                  {
                    accessorKey: "value",
                    header: "Original fraction",
                    cell: ({ getValue }) =>
                      getValue() == null ? "Unavailable" : String(getValue()),
                  },
                  { accessorKey: "status", header: "Period status" },
                  {
                    accessorKey: "baseline_date",
                    header: "Baseline observation",
                  },
                  {
                    accessorKey: "last_observation",
                    header: "Closing observation",
                  },
                  { accessorKey: "reason", header: "Coverage / reason" },
                ]}
              />
              <Pagination
                offset={offset}
                limit={24}
                count={data.months.length}
                onPage={setOffset}
                busy={query.isFetching}
              />
            </section>
            <section aria-label="Observed return distribution">
              <p className="text-sm">{data.distribution_definition}</p>
              <p className="text-xs muted mt-2">
                {data.observations} observed daily closes; {data.valid_returns}{" "}
                valid intervals; {data.excluded_returns} invalid or calendar-gap
                intervals excluded; {data.collapsed_intraday_observations}{" "}
                intraday observations collapsed.
              </p>
              {data.status === "available" ? (
                <Suspense fallback={<Loading />}>
                  <ReturnAnalysisChart value={data} kind="distribution" />
                </Suspense>
              ) : (
                <p className="notice">
                  Distribution unavailable: fewer than two valid observed
                  closing equities.
                </p>
              )}
              <DataTable
                data={data.bins}
                label="Return distribution exact bin boundaries"
                empty="No valid observed returns."
                columns={[
                  {
                    accessorKey: "lower",
                    header: "Lower fraction (inclusive)",
                  },
                  { accessorKey: "upper", header: "Upper fraction" },
                  {
                    id: "interval",
                    header: "Upper boundary",
                    cell: ({ row }) =>
                      row.original.upper_inclusive ? "Inclusive" : "Exclusive",
                  },
                  { accessorKey: "count", header: "Observed interval count" },
                ]}
              />
            </section>
          </div>
        )
      )}
    </Panel>
  );
}
