import { lazy, Suspense, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Database, RefreshCw, Upload } from "lucide-react";
import type { DatasetPublic, PreviewBar } from "@/client/types.gen";
import * as sdk from "@/client/sdk.gen";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import {
  DataNotice,
  Field,
  JsonDetails,
  Metadata,
  Pagination,
  UploadEvidenceNotice,
} from "@/components/data-common";
import { DataTable } from "@/components/data-table";
import { currentUserOptions } from "@/lib/api";
import {
  coverageOptions,
  datasetOptions,
  previewOptions,
} from "@/lib/data-api";
import {
  asRecord,
  clearDataSubmission,
  dataSubmissionKey,
  dateWindowError,
  records,
  snapshotLabel,
  universeLabels,
  valueText,
} from "@/lib/data-utils";
import { useDataAction } from "@/lib/use-data-action";
const PriceChart = lazy(() => import("@/components/price-chart"));

export function DatasetsPage() {
  const [offset, setOffset] = useState(0);
  const datasets = useQuery({
    queryKey: ["datasets", "page", offset],
    queryFn: async ({ signal }) =>
      (await sdk.listDatasets({ query: { offset, limit: 20 }, signal })).data,
  });
  return (
    <>
      <PageHeader
        title="Dataset snapshots"
        subtitle="Immutable inputs, with their provenance and date boundaries intact."
      >
        <div className="actions">
          <Button variant="outline" asChild>
            <Link to="/datasets/upload">
              <Upload />
              Upload file
            </Link>
          </Button>
          <Button asChild>
            <Link to="/data-sources">Import market data</Link>
          </Button>
        </div>
      </PageHeader>
      <DataNotice />
      <div className="mt-6">
        {datasets.isPending ? (
          <Loading />
        ) : datasets.error ? (
          <ErrorState
            error={datasets.error}
            retry={() => void datasets.refetch()}
          />
        ) : (
          <div className="catalog">
            {datasets.data?.data.map((dataset) => (
              <article className="panel panel-body" key={dataset.id}>
                <div className="flex items-start justify-between gap-3">
                  <div className="catalog-icon">
                    <Database size={21} />
                  </div>
                  <Badge variant="secondary">{snapshotLabel(dataset)}</Badge>
                </div>
                <h2>{dataset.name}</h2>
                <p className="text-xs muted mt-2">
                  {dataset.market} · {dataset.source}
                </p>
                <Metadata
                  values={{
                    "Start · inclusive": dataset.start_date,
                    "End · exclusive": dataset.end_date,
                    Version: dataset.id,
                  }}
                />
                <p className="hash">SHA-256 {dataset.content_sha256}</p>
                <Button asChild variant="outline" className="mt-5">
                  <Link
                    to="/datasets/$datasetId"
                    params={{ datasetId: dataset.id }}
                  >
                    Inspect snapshot
                  </Link>
                </Button>
              </article>
            ))}
          </div>
        )}
      </div>
      <Pagination
        offset={offset}
        limit={20}
        count={datasets.data?.count || 0}
        busy={datasets.isFetching}
        onPage={setOffset}
      />
    </>
  );
}
export function DatasetDetailPage() {
  const { datasetId } = useParams({ strict: false }) as { datasetId: string };
  const dataset = useQuery(datasetOptions(datasetId));
  return (
    <>
      <Link to="/datasets" className="text-link mb-5">
        <ArrowLeft size={14} />
        All snapshots
      </Link>
      {dataset.isPending ? (
        <Loading />
      ) : dataset.error ? (
        <ErrorState
          error={dataset.error}
          retry={() => void dataset.refetch()}
        />
      ) : dataset.data ? (
        <SnapshotContent dataset={dataset.data} key={dataset.data.id} />
      ) : null}
    </>
  );
}
function SnapshotContent({ dataset }: { dataset: DatasetPublic }) {
  const navigate = useNavigate();
  const user = useQuery(currentUserOptions);
  const coverage = useQuery(coverageOptions(dataset.id));
  const listingRecords = records(dataset.manifest.listings);
  const prices = asRecord(dataset.manifest.price_semantics);
  const universe = asRecord(dataset.manifest.universe);
  const compatibility = asRecord(dataset.manifest.evaluation_compatibility);
  const refresh = useDataAction(async () => {
    if (!user.data) return;
    const result = (
      await sdk.refreshDataSnapshot({
        path: { dataset_id: dataset.id },
        headers: {
          "Idempotency-Key": dataSubmissionKey(
            user.data.id,
            `refresh:${dataset.id}`,
            { dataset_id: dataset.id },
          ),
        },
      })
    ).data;
    clearDataSubmission(user.data.id, `refresh:${dataset.id}`);
    await navigate({
      to: "/imports/$importId",
      params: { importId: result.id },
    });
  });
  const repair = useDataAction(async () => {
    if (!user.data) return;
    const scope = `yahoo-repair:${dataset.id}`;
    const result = (
      await sdk.repairYahooSnapshot({
        path: { dataset_id: dataset.id },
        headers: {
          "Idempotency-Key": dataSubmissionKey(user.data.id, scope, {
            dataset_id: dataset.id,
          }),
        },
      })
    ).data;
    clearDataSubmission(user.data.id, scope);
    await navigate({
      to: "/imports/$importId",
      params: { importId: result.id },
    });
  });
  const repairAvailable =
    dataset.source === "yfinance" &&
    prices.algorithm_version === "yahoo-native;repair=False" &&
    Boolean(dataset.manifest.import_request);
  return (
    <>
      <PageHeader
        title={dataset.name}
        subtitle={`${dataset.source} · Immutable version ${dataset.id}`}
      >
        <Badge variant="secondary">{snapshotLabel(dataset)}</Badge>
      </PageHeader>
      <DataNotice />
      <div className="stack mt-6">
        <Panel title="Price basis and provenance">
          <UploadEvidenceNotice evidence={dataset.manifest.data_evidence} />
          <Metadata
            values={{
              "Native mode": prices.native_mode,
              "Split basis": prices.split_basis,
              "Dividend basis": prices.dividend_basis,
              "Adj Close split basis": prices.adj_close_split_basis,
              "Adj Close dividend basis": prices.adj_close_dividend_basis,
              "Adjustment as of": prices.adjustment_as_of,
              "Algorithm version": prices.algorithm_version,
              "Actions snapshot": prices.actions_snapshot_id,
              "Coverage start · inclusive": dataset.start_date,
              "Coverage end · exclusive": dataset.end_date,
              Universe:
                universeLabels[String(universe.state)] ||
                "Unknown · not verified PIT",
            }}
          />
          <p className="text-xs muted">
            {valueText(prices.note)}. Provider-native does not establish raw
            transaction prices. Price and event sources may differ; do not
            double-count dividends or splits.
          </p>
          <p className="hash mt-4">Content SHA-256: {dataset.content_sha256}</p>
          <JsonDetails
            title="Inspect snapshot manifest"
            value={dataset.manifest}
          />
          {repairAvailable && (
            <div className="mt-5">
              <Button
                variant="outline"
                disabled={repair.pending || refresh.pending || !user.data}
                onClick={() => void repair.run()}
              >
                Create Yahoo repair-derived snapshot
              </Button>
              <p className="mt-3 text-xs muted">
                Explicit repair=True re-fetch with the same listings and dates,
                not a replay of old bytes. Preserves this original snapshot and
                pins its raw/provenance hashes. Yahoo OHLC remains
                split-adjusted; per-row repair flags are retained.
              </p>
              {Boolean(repair.error) && <ErrorState error={repair.error} />}
            </div>
          )}
          {Boolean(dataset.manifest.derivation) && (
            <JsonDetails
              title="Immutable repair parent and source hashes"
              value={dataset.manifest.derivation}
            />
          )}
          {Boolean(dataset.manifest.import_request) &&
          dataset.source !== "upload" &&
          !dataset.manifest.derivation ? (
            <div className="mt-5">
              <Button
                variant="outline"
                disabled={refresh.pending || repair.pending || !user.data}
                onClick={() => void refresh.run()}
              >
                <RefreshCw />
                Refresh as new snapshot
              </Button>
              <p className="mt-3 text-xs muted">
                Fetches the full requested history again. This version and all
                runs referencing it remain unchanged.
              </p>
            </div>
          ) : (
            <p className="mt-4 text-xs muted">
              {dataset.source === "upload"
                ? "To revise uploaded data, upload a new file. This version cannot be overwritten."
                : dataset.manifest.derivation
                  ? "To repeat Yahoo repair, open the preserved original parent and explicitly create another derived snapshot."
                  : "This synthetic M1 snapshot has no provider refresh or normalized preview."}
            </p>
          )}
          {Boolean(refresh.error) && (
            <div className="mt-4">
              <ErrorState error={refresh.error} />
            </div>
          )}
          {typeof dataset.manifest.parent_version_id === "string" && (
            <Link
              className="text-link mt-4"
              to="/datasets/$datasetId"
              params={{ datasetId: dataset.manifest.parent_version_id }}
            >
              Open preserved parent version
            </Link>
          )}
        </Panel>
        {listingRecords.length > 0 && (
          <Panel title="Pinned listing identities">
            <DataTable<Record<string, unknown>>
              label="Immutable listing mappings"
              data={listingRecords}
              columns={[
                { accessorKey: "symbol", header: "Security" },
                { accessorKey: "mic", header: "Exchange MIC" },
                { accessorKey: "asset_type", header: "Asset type" },
                { accessorKey: "currency", header: "Currency" },
                { accessorKey: "timezone", header: "Timezone" },
                {
                  id: "calendar",
                  header: "Session calendar / schedule mapping",
                  cell: ({ row }) => (
                    <>
                      <span>{valueText(row.original.calendar)}</span>
                      <span className="cell-sub">
                        {valueText(row.original.calendar_note)}
                      </span>
                    </>
                  ),
                },
                {
                  id: "mapping",
                  header: "Vendor mapping",
                  cell: ({ row }) => (
                    <JsonDetails
                      title={`Vendor symbols for ${valueText(row.original.symbol)}`}
                      value={row.original.vendor_symbols}
                    />
                  ),
                },
              ]}
            />
            <JsonDetails
              title="Stable IDs, listing dates and mapping effective intervals"
              value={listingRecords}
            />
            <p className="notice mt-4">
              The legacy evaluator's INDEX.csv is a compatibility alias, not an
              actual market index or an independent benchmark. Preview selection
              uses pinned listing identities, never that alias.
            </p>
          </Panel>
        )}
        <Panel title="Coverage and quality">
          {coverage.isPending ? (
            <Loading />
          ) : coverage.error ? (
            <ErrorState
              error={coverage.error}
              retry={() => void coverage.refetch()}
            />
          ) : (
            <>
              <DataTable<Record<string, unknown>>
                label="Snapshot coverage by listing"
                data={coverage.data?.coverage || []}
                columns={[
                  {
                    id: "listing",
                    header: "Listing",
                    cell: ({ row }) => {
                      const listing = listingRecords.find(
                        (item) => item.listing_id === row.original.listing_id,
                      );
                      return listing
                        ? `${valueText(listing.symbol)} · ${valueText(listing.mic)}`
                        : valueText(row.original.listing_id);
                    },
                  },
                  { accessorKey: "status", header: "Coverage" },
                  {
                    id: "actual",
                    header: "Actual [start, end)",
                    cell: ({ row }) =>
                      `${valueText(row.original.actual_start)} → ${valueText(row.original.actual_end)}`,
                  },
                  {
                    id: "gaps",
                    header: "Missing / provisional",
                    cell: ({ row }) =>
                      `${Array.isArray(row.original.missing_sessions) ? row.original.missing_sessions.length : "Unknown"} / ${Array.isArray(row.original.provisional_sessions) ? row.original.provisional_sessions.length : "Unknown"}`,
                  },
                  {
                    id: "reason",
                    header: "Reason",
                    cell: ({ row }) => valueText(row.original.missing_reason),
                  },
                ]}
              />
              <JsonDetails
                title="Detailed coverage: holidays, suspensions, listing dates and gaps"
                value={coverage.data?.coverage}
              />
              <JsonDetails
                title="Quality report"
                value={coverage.data?.quality}
              />
              <JsonDetails
                title="Provider, endpoint, parameters, packages and retrieval evidence"
                value={coverage.data?.provenance}
              />
            </>
          )}
        </Panel>
        {listingRecords.length > 0 && (
          <SnapshotPreview dataset={dataset} listings={listingRecords} />
        )}
        {compatibility.compatible === false ? (
          <div className="notice error">
            <div>
              <strong>Not compatible with the fixed-seed evaluator</strong>
              <p>
                {Array.isArray(compatibility.reasons)
                  ? compatibility.reasons.map(valueText).join(" ")
                  : "Inspect the immutable compatibility report."}
              </p>
              <JsonDetails
                title="Evaluation compatibility report"
                value={compatibility}
              />
            </div>
          </div>
        ) : (
          <Button asChild>
            <Link to="/runs/new" search={{ dataset: dataset.id }}>
              Evaluate this immutable snapshot
            </Link>
          </Button>
        )}
      </div>
    </>
  );
}
function SnapshotPreview({
  dataset,
  listings,
}: {
  dataset: DatasetPublic;
  listings: Record<string, unknown>[];
}) {
  const [listing, setListing] = useState(String(listings[0]?.listing_id || ""));
  const [start, setStart] = useState(dataset.start_date);
  const [end, setEnd] = useState(dataset.end_date);
  const [window, setWindow] = useState({
    start: dataset.start_date,
    end: dataset.end_date,
  });
  const [offset, setOffset] = useState(0);
  const [chart, setChart] = useState(false);
  const [error, setError] = useState("");
  const preview = useQuery(
    previewOptions(dataset.id, listing, window.start, window.end, offset),
  );
  const candles = useMemo(
    () =>
      preview.data?.data.map((bar) => ({
        time: bar.session_date,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })) || [],
    [preview.data],
  );
  const volumes = useMemo(
    () =>
      preview.data?.data.map((bar) => ({
        time: bar.session_date,
        value: bar.volume,
      })) || [],
    [preview.data],
  );
  return (
    <Panel title="Daily OHLCV preview">
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          const issue = dateWindowError(start, end);
          setError(issue || "");
          if (!issue) {
            setWindow({ start, end });
            setOffset(0);
          }
        }}
      >
        <Field label="Preview listing" id="preview-listing">
          <select
            id="preview-listing"
            value={listing}
            onChange={(event) => {
              setListing(event.target.value);
              setOffset(0);
            }}
          >
            {listings.map((item) => (
              <option
                key={String(item.listing_id)}
                value={String(item.listing_id)}
              >
                {valueText(item.symbol)} · {valueText(item.mic)} ·{" "}
                {valueText(item.currency)}
              </option>
            ))}
          </select>
        </Field>
        <div className="form-row">
          <Field label="Preview start · inclusive" id="preview-start">
            <Input
              id="preview-start"
              type="date"
              value={start}
              min={dataset.start_date}
              max={dataset.end_date}
              onChange={(event) => setStart(event.target.value)}
            />
          </Field>
          <Field label="Preview end · exclusive" id="preview-end">
            <Input
              id="preview-end"
              type="date"
              value={end}
              min={dataset.start_date}
              max={dataset.end_date}
              onChange={(event) => setEnd(event.target.value)}
            />
          </Field>
        </div>
        <div className="actions">
          <Button type="submit" variant="outline">
            Apply preview window
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => setChart(!chart)}
          >
            {chart ? "Hide OHLC chart" : "Show OHLC chart"}
          </Button>
        </div>
        {error && (
          <p role="alert" className="notice error">
            {error}
          </p>
        )}
      </form>
      <p className="mt-4 mb-4 text-xs muted">
        Bounded to one listing and 100 rows per request. Exchange-local session
        dates; original precision and volume units remain visible.
      </p>
      {preview.isPending ? (
        <Loading text="Loading bounded preview…" />
      ) : preview.error ? (
        <ErrorState
          error={preview.error}
          retry={() => void preview.refetch()}
        />
      ) : (
        <>
          {chart && (
            <Suspense fallback={<Loading text="Loading OHLC chart…" />}>
              <PriceChart candles={candles} volumes={volumes} />
            </Suspense>
          )}
          <DataTable<PreviewBar>
            label="Daily bars"
            data={preview.data?.data || []}
            columns={[
              { accessorKey: "session_date", header: "Session" },
              ...[
                "open",
                "high",
                "low",
                "close",
                "volume",
                "adj_close",
                "currency",
                "volume_unit",
              ].map((key) => ({
                accessorKey: key,
                header: key.replaceAll("_", " "),
                cell: ({ getValue }: { getValue: () => unknown }) =>
                  valueText(getValue()),
              })),
              {
                id: "flags",
                header: "Quality flags",
                cell: ({ row }) =>
                  row.original.flags.join(", ") || "None reported",
              },
            ]}
          />
          <Pagination
            offset={offset}
            limit={100}
            count={preview.data?.count || 0}
            busy={preview.isFetching}
            onPage={setOffset}
          />
        </>
      )}
    </Panel>
  );
}
