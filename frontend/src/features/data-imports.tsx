import { useEffect, useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, RefreshCw, Square } from "lucide-react";
import type {
  BlockPublic,
  ImportDetail,
  ImportPublic,
} from "@/client/types.gen";
import * as sdk from "@/client/sdk.gen";
import { Button } from "@/components/ui/button";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import {
  DataNotice,
  DataStatus,
  JsonDetails,
  Metadata,
  Pagination,
} from "@/components/data-common";
import { DataTable } from "@/components/data-table";
import { queryClient } from "@/lib/api";
import { importOptions, importsOptions } from "@/lib/data-api";
import { importTerminal, valueText } from "@/lib/data-utils";
import { formatDate, shortId } from "@/lib/format";
import { useDataAction } from "@/lib/use-data-action";

export function ImportsPage() {
  const [offset, setOffset] = useState(0);
  const imports = useQuery(importsOptions(offset));
  return (
    <>
      <PageHeader
        title="Import history"
        subtitle="Durable jobs survive navigation, reloads and worker restarts."
      >
        <Button asChild>
          <Link to="/data-sources">New import</Link>
        </Button>
      </PageHeader>
      <DataNotice />
      <div className="mt-6">
        <Panel title="Import and resolution jobs" body={false}>
          {imports.isPending ? (
            <Loading />
          ) : imports.error ? (
            <ErrorState
              error={imports.error}
              retry={() => void imports.refetch()}
            />
          ) : (
            <DataTable<ImportPublic>
              label="Import jobs"
              data={imports.data?.data || []}
              columns={[
                {
                  id: "name",
                  header: "Job",
                  cell: ({ row }) => (
                    <Link
                      className="text-link"
                      to="/imports/$importId"
                      params={{ importId: row.original.id }}
                    >
                      {valueText(
                        row.original.request.name || row.original.kind,
                      )}{" "}
                      · {shortId(row.original.id)}
                    </Link>
                  ),
                },
                { accessorKey: "provider", header: "Provider" },
                {
                  id: "status",
                  header: "State",
                  cell: ({ row }) => (
                    <DataStatus status={row.original.status} />
                  ),
                },
                { accessorKey: "generation", header: "Generation" },
                {
                  id: "updated",
                  header: "Updated · UTC",
                  cell: ({ row }) => formatDate(row.original.updated_at),
                },
              ]}
            />
          )}
        </Panel>
        <Pagination
          offset={offset}
          limit={25}
          count={imports.data?.count || 0}
          onPage={setOffset}
          busy={imports.isFetching}
        />
      </div>
    </>
  );
}
export function ImportDetailPage() {
  const { importId } = useParams({ strict: false }) as { importId: string };
  const [blockOffset, setBlockOffset] = useState(0);
  useEffect(() => setBlockOffset(0), [importId]);
  const result = useQuery(importOptions(importId, blockOffset));
  return (
    <>
      <Link to="/imports" className="text-link mb-5">
        <ArrowLeft size={14} />
        All imports
      </Link>
      {result.isPending ? (
        <Loading text="Reading durable import progress…" />
      ) : result.error ? (
        <ErrorState error={result.error} retry={() => void result.refetch()} />
      ) : result.data ? (
        <ImportContent
          job={result.data}
          onPage={setBlockOffset}
          busy={result.isFetching}
        />
      ) : null}
    </>
  );
}
function ImportContent({
  job,
  onPage,
  busy,
}: {
  job: ImportDetail;
  onPage: (offset: number) => void;
  busy: boolean;
}) {
  const action = useDataAction(async (action: "cancel" | "retry") => {
    try {
      if (action === "cancel")
        await sdk.cancelDataImport({ path: { import_id: job.id } });
      else await sdk.retryDataImport({ path: { import_id: job.id } });
    } finally {
      // A lost mutation response is not proof that the server rejected it.
      // Reconcile durable state, including after reconnect, before another action.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["data-import", job.id] }),
        queryClient.invalidateQueries({ queryKey: ["data-imports"] }),
      ]);
    }
  });
  const completed = job.blocks.filter(
    (block) => block.status === "ready",
  ).length;
  return (
    <>
      <PageHeader
        title="Import progress"
        subtitle={`${job.kind} · ${job.provider} · ${job.id}`}
      >
        <DataStatus status={job.status} />
      </PageHeader>
      <DataNotice />
      <div className="data-grid mt-6">
        <div className="stack">
          <Panel title="Durable progress">
            <Metadata
              values={{
                Generation: job.generation,
                "Published dataset": job.dataset_version_id || "Not published",
                "Last update · UTC": formatDate(job.updated_at),
              }}
            />
            <JsonDetails title="Worker progress" value={job.progress} />
            {job.blocks.length > 0 && (
              <div className="mt-4">
                <label htmlFor="import-progress" className="text-xs muted">
                  {completed} / {job.blocks.length} completed blocks on this
                  page ({job.blocks_total} total blocks)
                </label>
                <progress
                  id="import-progress"
                  className="data-progress mt-2"
                  value={completed}
                  max={job.blocks.length}
                />
              </div>
            )}
            <p className="mt-4 text-xs muted">
              {job.status === "ready"
                ? job.kind === "resolve"
                  ? "Resolution finished. Select the explicit listing identities to create a data snapshot."
                  : "Validation finished. A published version is immutable."
                : job.status === "partial"
                  ? "Partial is not a published dataset. Inspect each symbol's failure or missing sessions, then retry incomplete blocks."
                  : job.status === "cancelled"
                    ? "Cancelled. Completed block evidence is retained; no incomplete snapshot is published."
                    : "Only validated blocks can contribute to a published dataset. Closing this page does not cancel the job."}
            </p>
            {job.error && (
              <p role="alert" className="notice error mt-4">
                {job.error}
              </p>
            )}
            {Boolean(action.error) && (
              <div className="mt-4">
                <ErrorState error={action.error} />
              </div>
            )}
            <div className="actions mt-5">
              {!importTerminal.has(job.status) && (
                <Button
                  variant="outline"
                  disabled={action.pending || job.cancel_requested}
                  onClick={() => void action.run("cancel")}
                >
                  <Square />
                  Cancel import
                </Button>
              )}
              {["failed", "partial", "cancelled"].includes(job.status) && (
                <Button
                  disabled={action.pending}
                  onClick={() => void action.run("retry")}
                >
                  <RefreshCw />
                  Retry incomplete blocks
                </Button>
              )}
              {job.dataset_version_id && (
                <Button asChild>
                  <Link
                    to="/datasets/$datasetId"
                    params={{ datasetId: job.dataset_version_id }}
                  >
                    Open published snapshot
                  </Link>
                </Button>
              )}
              {job.kind === "resolve" && (
                <Button asChild variant="outline">
                  <Link to="/data-sources">Select resolved listings</Link>
                </Button>
              )}
            </div>
            <p className="text-xs muted mt-4">
              Retry reuses completed content-addressed blocks, not a new data
              revision. Use snapshot refresh to fetch a new immutable revision.
            </p>
          </Panel>
          <Panel title="Per-symbol and date-block results" body={false}>
            <DataTable<BlockPublic>
              label="Import blocks"
              data={job.blocks}
              columns={[
                { accessorKey: "symbol", header: "Symbol" },
                {
                  id: "window",
                  header: "[start, end)",
                  cell: ({ row }) =>
                    `${row.original.start_date} → ${row.original.end_date}`,
                },
                { accessorKey: "status", header: "State" },
                { accessorKey: "attempts", header: "Fetch attempts" },
                {
                  id: "detail",
                  header: "Evidence / error",
                  cell: ({ row }) => (
                    <>
                      <span className="data-warning">
                        {row.original.error || "No error reported"}
                      </span>
                      <span className="cell-sub hash">
                        {row.original.content_sha256 ||
                          "No completed content hash"}
                      </span>
                      <JsonDetails
                        title={`Coverage for ${row.original.symbol}`}
                        value={row.original.coverage}
                      />
                    </>
                  ),
                },
              ]}
              empty={
                job.kind === "resolve"
                  ? "Resolution jobs have no price blocks."
                  : "The data worker has not created blocks yet."
              }
            />
          </Panel>
          {job.blocks_total > job.blocks_limit && (
            <Pagination
              offset={job.blocks_offset}
              limit={job.blocks_limit}
              count={job.blocks_total}
              onPage={onPage}
              busy={busy}
            />
          )}
        </div>
        <div className="stack">
          <Panel title="Request and provenance">
            <Metadata
              values={{
                Provider: job.provider,
                "Created · UTC": formatDate(job.created_at),
                "Resolved listing count": job.listing_ids.length,
              }}
            />
            <JsonDetails title="Immutable import request" value={job.request} />
            <JsonDetails title="Resolved listing IDs" value={job.listing_ids} />
            {job.parent_version_id && (
              <Link
                className="text-link mt-5"
                to="/datasets/$datasetId"
                params={{ datasetId: job.parent_version_id }}
              >
                View preserved parent snapshot
              </Link>
            )}
          </Panel>
          <Panel title="Warnings and completeness">
            <p className="text-xs muted">
              No silent fallback to another provider. Unknown gaps are not
              automatically called holidays or suspensions.
            </p>
            {job.warnings.length ? (
              <ul className="mt-4 space-y-3 pl-4 text-xs">
                {job.warnings.map((warning, index) => (
                  <li className="data-warning" key={index}>
                    {valueText(warning.message || warning)}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs mt-4 muted">No warnings recorded.</p>
            )}
            <JsonDetails
              title="Inspect complete warning evidence"
              value={job.warnings}
            />
          </Panel>
        </div>
      </div>
    </>
  );
}
