import {
  NamedListControls,
  type NamedListFilters,
} from "@/components/named-list-controls";
import { FreezeCandidatePanel } from "@/features/final-tests/freeze-selection";
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ArrowLeft, Download, Pause, RotateCcw, Square } from "lucide-react";
import type {
  ArtifactPublic,
  CandidatePublic,
  CheckpointPublic,
  EvaluationPublic,
  ModelCallPublic,
  ResearchRunDetail,
} from "@/client/types.gen";
import {
  ErrorState,
  Loading,
  PageHeader,
  Panel,
  Status,
} from "@/components/common";
import { JsonDetails, Metadata, Pagination } from "@/components/data-common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { getArtifact } from "@/lib/api";
import { datasetOptions } from "@/lib/data-api";
import { asRecord, snapshotLabel } from "@/lib/data-utils";
import {
  formatBytes,
  formatDate,
  label,
  shortId,
  taskLabels,
} from "@/lib/format";
import {
  RESEARCH_PAGE_SIZE,
  candidateOptions,
  candidatesOptions,
  checkpointOptions,
  checkpointsOptions,
  evaluationsOptions,
  modelCallOptions,
  modelCallsOptions,
  refreshResearch,
  researchCommand,
  researchArtifactsOptions,
  researchRunOptions,
  researchUsageOptions,
} from "@/lib/research-api";
import {
  rawCell,
  researchControls,
  researchTerminal,
  researchTrustNotice,
  saveResearchDownload,
} from "@/lib/research-results";
import { useDataAction } from "@/lib/use-data-action";
import { useRunEvents } from "@/lib/use-run-events";
const CodeDiff = lazy(() => import("@/components/code-diff"));
const ResearchChart = lazy(() => import("@/components/research-chart"));

export function ResearchDetailPage() {
  const { runId } = useParams({ strict: false }) as { runId: string };
  const run = useQuery(researchRunOptions(runId));
  return (
    <>
      <Link to="/runs" className="text-link mb-5">
        <ArrowLeft size={14} />
        All experiments
      </Link>
      {run.isPending ? (
        <Loading text="Loading persisted research state…" />
      ) : run.error ? (
        <ErrorState error={run.error} retry={() => void run.refetch()} />
      ) : run.data ? (
        <ResearchContent key={runId} run={run.data} />
      ) : null}
    </>
  );
}
function ResearchContent({ run }: { run: ResearchRunDetail }) {
  const [candidateOffset, setCandidateOffset] = useState(0);
  const [candidateFilters, setCandidateFilters] = useState<NamedListFilters>(
    {},
  );
  const [evaluationOffset, setEvaluationOffset] = useState(0);
  const [checkpointOffset, setCheckpointOffset] = useState(0);
  const [callOffset, setCallOffset] = useState(0);
  const [artifactOffset, setArtifactOffset] = useState(0);
  const [selectedCandidate, setSelectedCandidate] = useState("");
  const [selectedCheckpoint, setSelectedCheckpoint] = useState("");
  const [selectedCall, setSelectedCall] = useState("");
  const [compare, setCompare] = useState<string[]>([]);
  const [showProgressChart, setShowProgressChart] = useState(false);
  const stable = researchTerminal.has(run.status);
  const controls = researchControls(run);
  const events = useRunEvents(run.id, stable);
  const candidates = useQuery(
    candidatesOptions(run.id, candidateOffset, !stable, candidateFilters),
  );
  const evaluations = useQuery(
    evaluationsOptions(run.id, evaluationOffset, !stable),
  );
  const checkpoints = useQuery(
    checkpointsOptions(run.id, checkpointOffset, !stable),
  );
  // Fetch only the last known bounded checkpoint page to show current durable
  // progress, rather than downloading the latest checkpoint's whole state map.
  const latestCheckpointPage = useQuery({
    ...checkpointsOptions(
      run.id,
      Math.max(0, (checkpoints.data?.count || 1) - 1),
      !stable,
    ),
    enabled: Boolean(checkpoints.data?.count),
  });
  const latestCheckpoint = latestCheckpointPage.data?.data.find(
    (item) => item.id === run.latest_checkpoint_id,
  );
  const progressTable = useMemo(
    () => ({
      columns: [
        { name: "timestamp", dtype: "datetime", unit: "UTC timestamp" },
        { name: "completed_iteration", dtype: "integer", unit: "iteration" },
      ],
      rows: (checkpoints.data?.data || []).map((checkpoint) => [
        checkpoint.created_at,
        checkpoint.iteration,
      ]),
    }),
    [checkpoints.data],
  );
  const calls = useQuery(modelCallsOptions(run.id, callOffset, !stable));
  const usage = useQuery(researchUsageOptions(run.id, !stable));
  const dataset = useQuery(datasetOptions(run.dataset_version_id));
  const artifacts = useQuery(
    researchArtifactsOptions(run.id, artifactOffset, !stable),
  );
  const command = useDataAction(
    async (action: "pause" | "cancel" | "resume") => {
      try {
        await researchCommand(run.id, action);
      } finally {
        await refreshResearch(run.id);
      }
    },
  );
  // SSE is a notification stream, not the source of truth. Re-load persisted
  // resources at durable sequence/status changes, on reload and on reconnect.
  useEffect(() => {
    void refreshResearch(run.id);
  }, [run.id, run.status, run.next_event_seq]);
  const result = asRecord(run.result);
  const budgets = asRecord(run.config.budgets);
  const optimizer = asRecord(run.config.optimizer);
  const model = asRecord(run.config.model);
  const windows = asRecord(run.config.windows);
  const candidateColumns: ColumnDef<CandidatePublic>[] = [
    {
      accessorKey: "candidate_key",
      header: "Immutable candidate",
      cell: ({ row }) => (
        <Button
          variant="ghost"
          onClick={() => setSelectedCandidate(row.original.id)}
        >
          {row.original.candidate_key}
        </Button>
      ),
    },
    { accessorKey: "iteration", header: "Iteration" },
    { accessorKey: "kind", header: "Kind" },
    { accessorKey: "repair_index", header: "Repair index" },
    { accessorKey: "generation", header: "Generation" },
    {
      accessorKey: "parent_id",
      header: "Parent version",
      cell: ({ row }) =>
        row.original.parent_id ? (
          <Button
            variant="ghost"
            onClick={() => setSelectedCandidate(row.original.parent_id!)}
          >
            {shortId(row.original.parent_id)}
          </Button>
        ) : (
          "Seed (no parent)"
        ),
    },
    {
      accessorKey: "code_sha256",
      header: "Code SHA-256",
      cell: ({ getValue }) => (
        <span className="hash" title={String(getValue())}>
          {String(getValue()).slice(0, 16)}…
        </span>
      ),
    },
  ];
  const evaluationColumns: ColumnDef<EvaluationPublic>[] = [
    {
      id: "compare",
      header: "Compare",
      cell: ({ row }) => (
        <input
          type="checkbox"
          aria-label={`Compare evaluation ${row.original.id}`}
          checked={compare.includes(row.original.id)}
          disabled={
            row.original.status !== "succeeded" ||
            (!compare.includes(row.original.id) && compare.length >= 4)
          }
          onChange={(event) =>
            setCompare((previous) =>
              event.target.checked
                ? [...previous, row.original.id]
                : previous.filter((id) => id !== row.original.id),
            )
          }
        />
      ),
    },
    { accessorKey: "candidate_key", header: "Candidate" },
    { accessorKey: "split", header: "Selection split" },
    {
      accessorKey: "window",
      header: "Window [start, end)",
      cell: ({ row }) =>
        `${rawCell(row.original.window.start)} → ${rawCell(row.original.window.end)}`,
    },
    {
      accessorKey: "status",
      header: "Status",
      cell: ({ row }) => (
        <>
          <Status status={row.original.status} />
          {row.original.error && (
            <p className="text-xs text-[#a85b46] mt-2">{row.original.error}</p>
          )}
        </>
      ),
    },
    {
      id: "result",
      header: "Persisted evidence",
      cell: ({ row }) =>
        row.original.status === "succeeded" ? (
          <Link
            className="text-link"
            to="/backtests/$evaluationId"
            params={{ evaluationId: row.original.id }}
          >
            Inspect backtest
          </Link>
        ) : (
          <span className="text-xs muted">
            {row.original.sandbox_stopped
              ? "Sandbox stopped; failure retained"
              : "Awaiting sandbox stop confirmation"}
          </span>
        ),
    },
  ];
  const checkpointColumns: ColumnDef<CheckpointPublic>[] = [
    {
      accessorKey: "sequence",
      header: "Durable sequence",
      cell: ({ row }) => (
        <Button
          variant="ghost"
          onClick={() => setSelectedCheckpoint(row.original.id)}
        >
          #{row.original.sequence}
        </Button>
      ),
    },
    { accessorKey: "iteration", header: "Iteration" },
    { accessorKey: "generation", header: "Generation" },
    { accessorKey: "next_stage", header: "Next stage" },
    {
      accessorKey: "complete",
      header: "Publication",
      cell: ({ row }) =>
        row.original.complete
          ? "Complete manifest"
          : "Incomplete: not resumable",
    },
    {
      accessorKey: "created_at",
      header: "Published UTC",
      cell: ({ getValue }) => formatDate(String(getValue())),
    },
  ];
  const callColumns: ColumnDef<ModelCallPublic>[] = [
    {
      accessorKey: "logical_call_id",
      header: "Logical call",
      cell: ({ row }) => (
        <Button
          variant="ghost"
          onClick={() => setSelectedCall(row.original.id)}
        >
          {row.original.logical_call_id}
        </Button>
      ),
    },
    {
      id: "model",
      header: "Provider / model",
      cell: ({ row }) => `${row.original.provider} / ${row.original.model}`,
    },
    { accessorKey: "status", header: "Status" },
    {
      id: "tokens",
      header: "Input / output tokens",
      cell: ({ row }) =>
        `${rawCell(row.original.usage.input_tokens)} / ${rawCell(row.original.usage.output_tokens)}`,
    },
    {
      id: "usage",
      header: "Usage evidence",
      cell: ({ row }) => rawCell(row.original.usage.usage_source),
    },
    { accessorKey: "latency_seconds", header: "Latency (seconds)" },
    {
      accessorKey: "adopted",
      header: "Adopted",
      cell: ({ row }) => (row.original.adopted ? "Yes" : "No"),
    },
    {
      accessorKey: "error_category",
      header: "Failure",
      cell: ({ getValue }) =>
        getValue() ? rawCell(getValue()) : "None recorded",
    },
  ];
  return (
    <>
      <PageHeader
        eyebrow={`RESEARCH ${shortId(run.id)}`}
        title={taskLabels[String(run.config.task)] || "Research run"}
        subtitle={`Generation ${run.generation} · ${formatDate(run.created_at)} UTC · persisted optimization state`}
      >
        <div className="actions">
          <Status status={run.status} />
          {controls.pause && (
            <Button
              variant="outline"
              disabled={command.pending}
              onClick={() => void command.run("pause")}
            >
              <Pause />
              Pause at checkpoint
            </Button>
          )}
          {controls.cancel && (
            <Button
              variant="outline"
              disabled={command.pending}
              onClick={() => void command.run("cancel")}
            >
              <Square />
              Cancel run
            </Button>
          )}
          {controls.resume && (
            <Button
              variant="outline"
              disabled={command.pending}
              onClick={() => void command.run("resume")}
            >
              <RotateCcw />
              Resume checkpoint
            </Button>
          )}
        </div>
      </PageHeader>
      <p className="notice" data-testid="research-trust">
        {researchTrustNotice}
      </p>
      {model.provider === "fixture" && (
        <p className="notice mt-4">
          Deterministic model fixture, not a paid or live model. Synthetic usage
          remains explicitly zero when recorded; missing usage remains
          unavailable.
        </p>
      )}
      {Boolean(command.error) && <ErrorState error={command.error} />}
      {run.error && (
        <p role="alert" className="notice error mt-4">
          {run.error}
        </p>
      )}
      {run.error?.includes("model process termination receipt") && (
        <p className="notice error mt-4" role="alert">
          Model-process stop evidence is missing. Recovery stays blocked and no
          request is automatically retried. An operator must investigate; a
          timeout or dead worker alone does not prove provider billing stopped.
        </p>
      )}
      {String(
        run.config.model &&
          (run.config.model as Record<string, unknown>).provider,
      ) !== "fixture" && (
        <p className="notice mt-4">
          Failed or uncertain model calls, including HTTP 429/5xx, are not
          automatically retried on resume. A deliberately new experiment is a
          separate potentially billed action. Usage may be actual tokens or a
          conservative reservation; no dollar cost is inferred.
        </p>
      )}
      {Boolean(result.stop_reason) && (
        <p className="notice mt-4" role="status">
          Stop reason: {label(String(result.stop_reason))}.
          {result.stop_reason === "no_valid_candidate"
            ? " No usable strategy was produced. Finishing the loop is not success."
            : ""}
        </p>
      )}
      {run.status === "interrupted" && (
        <p className="notice mt-4">
          Execution was interrupted. Published candidates and complete
          checkpoints remain available; unfinished work is not shown as
          completed.
        </p>
      )}
      {run.status === "failed" && !result.stop_reason && (
        <p className="notice mt-4">
          Research failed. Retained candidate code and evaluation errors below
          are evidence, not a successful strategy.
        </p>
      )}
      {(run.status === "pause_requested" ||
        run.status === "cancel_requested") && (
        <p className="notice mt-4" role="status">
          {run.status === "pause_requested"
            ? "Waiting for a complete checkpoint and confirmed evaluator/model process stops. Pause is not final yet."
            : "Cancellation requested. Waiting for bounded model request completion and durable process stop confirmation."}
        </p>
      )}
      {controls.resumeReason && (
        <p className="notice mt-4">
          {controls.resumeReason} Completed model responses are reused; future
          live responses would not be guaranteed bit-for-bit deterministic.
        </p>
      )}
      <div className="section-grid mt-6">
        <div className="stack">
          <Panel
            title="Persisted progress"
            action={
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void refreshResearch(run.id)}
              >
                Refresh persisted state
              </Button>
            }
          >
            <Metadata
              values={{
                "Run status": run.status,
                "Current generation": run.generation,
                "Latest complete checkpoint": run.latest_checkpoint_id,
                "Latest completed iteration":
                  latestCheckpoint?.iteration ??
                  "Awaiting published checkpoint metadata",
                "Next optimizer stage":
                  latestCheckpoint?.next_stage ??
                  "Unavailable until checkpoint is published",
                "Resume checkpoint": run.resume_checkpoint_id,
                "Best candidate": result.best_candidate_key,
                "Stop reason": result.stop_reason,
              }}
            />
            <p className="text-xs muted mt-3">
              Progress and recovery load from the database and verified
              artifacts, not an in-memory event stream. Training and validation
              are selection data. A configured final-test window has not been
              executed and is not a verified unused holdout.
            </p>
            {events.isPending ? (
              <Loading text="Recovering ordered durable events…" />
            ) : events.error ? (
              <ErrorState
                error={events.error}
                retry={() => void events.refetch()}
              />
            ) : (
              <ol className="events" aria-label="Research run events">
                {events.data?.slice(-100).map((event) => (
                  <li
                    className="event"
                    key={event.seq}
                    data-event-seq={event.seq}
                  >
                    <time dateTime={event.occurred_at}>
                      {formatDate(event.occurred_at)}
                    </time>
                    <div>
                      <strong>
                        #{event.seq} {label(event.type)}
                      </strong>
                      <p>
                        {Object.entries(event.payload)
                          .map(
                            ([key, value]) =>
                              `${label(key)}: ${rawCell(value)}`,
                          )
                          .join(" · ")}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
            {!events.isPending && !events.data?.length && !events.error && (
              <p className="text-xs muted">No durable events yet.</p>
            )}
            <p className="text-xs muted mt-3">
              Latest 100 cached events shown in sequence order. UTC timestamps.{" "}
              {stable
                ? "Stable state loaded from persistent storage."
                : "Status and persisted pages refresh independently every 2 seconds."}
            </p>
          </Panel>
          <Panel title="Model usage and budget">
            {usage.isPending ? (
              <Loading text="Loading full persisted usage…" />
            ) : usage.error ? (
              <ErrorState
                error={usage.error}
                retry={() => void usage.refetch()}
              />
            ) : (
              usage.data && (
                <Metadata
                  values={{
                    "Persisted logical calls": usage.data.calls,
                    "Adopted calls": usage.data.adopted_calls,
                    "Input tokens": usage.data.input_tokens,
                    "Output tokens": usage.data.output_tokens,
                    "Budget-charged tokens": usage.data.charged_tokens,
                    "Calls with unknown usage": usage.data.unknown_usage_calls,
                    "Usage sources": usage.data.usage_sources,
                    "Currency cost": "Unavailable: no verified price",
                    "Maximum calls": budgets.max_model_calls,
                    "Maximum tokens": budgets.max_tokens,
                    "Maximum runtime (seconds)": budgets.max_runtime_seconds,
                    "Maximum iterations": optimizer.max_iterations,
                    "Maximum repairs": budgets.max_repairs,
                  }}
                />
              )
            )}
            <p className="text-xs muted mt-3">
              Usage covers all persisted logical calls, not just the visible
              page. Budget-charged reservations are not measured provider tokens
              or money. No token-to-currency estimate is invented.
            </p>
          </Panel>
        </div>
        <div className="stack">
          <Panel title="Immutable execution assumptions">
            <Metadata
              values={{
                Profile: run.config.profile,
                "Profile meaning":
                  run.config.profile === "source_compat"
                    ? "Specified source-behavior compatibility, not a paper replication claim"
                    : "Corrected explicit market and execution configuration",
                "Dataset version": run.dataset_version_id,
                "Data evidence": dataset.data
                  ? snapshotLabel(dataset.data)
                  : "Loading pinned provenance",
                "Strategy version": run.strategy_version_id,
                "Model provider": model.provider,
                Model: model.model,
                "Model profile": model.profile_id ?? "Deterministic fixture",
                "Model profile SHA-256":
                  model.profile_sha256 ?? "Not applicable",
                "Model temperature": model.temperature,
                "Maximum output tokens per call": model.max_output_tokens,
                "Train [start, end)": windows.train,
                "Validation [start, end)": windows.validation,
                "Final test (not executed)": windows.holdout,
                "Image digest": run.research_image_digest,
              }}
            />
            {dataset.error && (
              <ErrorState
                error={dataset.error}
                retry={() => void dataset.refetch()}
              />
            )}
            <JsonDetails
              title="Warm-up, costs, execution rules and full RunConfig"
              value={run.config}
            />
          </Panel>
          <Panel title="Execution attempts">
            {!run.attempts.length && (
              <p className="text-xs muted">
                Waiting for a worker to claim this run.
              </p>
            )}
            {run.attempts.map((attempt) => (
              <div key={attempt.id} className="mb-5">
                <h3 className="!text-xs">
                  Generation {attempt.generation} · {label(attempt.status)}
                </h3>
                <p className="hash mt-2">{attempt.id}</p>
                <p className="text-xs muted mt-2">
                  Sandbox{" "}
                  {attempt.sandbox_stopped
                    ? "confirmed stopped"
                    : "not confirmed stopped"}{" "}
                  · exit {rawCell(attempt.exit_code)}
                </p>
                {attempt.error && (
                  <p className="text-xs mt-2">{attempt.error}</p>
                )}
              </div>
            ))}
          </Panel>
          <Panel title="Artifacts and logs">
            {artifacts.isPending ? (
              <Loading />
            ) : artifacts.error ? (
              <ErrorState
                error={artifacts.error}
                retry={() => void artifacts.refetch()}
              />
            ) : artifacts.data?.data.length ? (
              artifacts.data.data.map((artifact) => (
                <ResearchArtifact key={artifact.id} artifact={artifact} />
              ))
            ) : (
              <p className="text-xs muted">
                Published artifacts appear here when durable. No placeholder
                files.
              </p>
            )}
            <Pagination
              offset={artifactOffset}
              limit={RESEARCH_PAGE_SIZE}
              count={artifacts.data?.count || 0}
              busy={artifacts.isFetching}
              onPage={setArtifactOffset}
            />
          </Panel>
        </div>
      </div>
      <FreezeCandidatePanel run={run} />
      <div className="stack mt-6">
        <Panel title="Candidate server filters and sorting">
          <NamedListControls
            kind="candidate"
            value={candidateFilters}
            onChange={(value) => {
              setCandidateFilters(value);
              setCandidateOffset(0);
            }}
          />
        </Panel>
        <PagedResearch
          title="Candidate lineage and immutable repairs"
          query={candidates}
          columns={candidateColumns}
          offset={candidateOffset}
          onPage={setCandidateOffset}
          empty="No candidates have been published yet."
        />
        <p className="text-xs muted">
          Every generation and repair is an immutable version. A repair does not
          replace failed source code; select any candidate or its parent to
          inspect the diff.
        </p>
        {selectedCandidate && (
          <CandidateInspector
            key={selectedCandidate}
            id={selectedCandidate}
            close={() => setSelectedCandidate("")}
          />
        )}
        <PagedResearch
          title="Training and validation evaluations"
          query={evaluations}
          columns={evaluationColumns}
          offset={evaluationOffset}
          onPage={setEvaluationOffset}
          empty="No completed or failed evaluations have been persisted yet."
        />
        <div className="actions">
          <span className="text-xs muted">
            {compare.length} of 4 evaluations selected across pages.
          </span>
          {compare.length >= 2 && (
            <Button asChild>
              <Link to="/compare" search={{ evaluations: compare.join(",") }}>
                Compare selected evaluations
              </Link>
            </Button>
          )}
          {compare.length > 0 && (
            <Button variant="ghost" onClick={() => setCompare([])}>
              Clear selection
            </Button>
          )}
        </div>
        <Panel title="Checkpoint iteration progress">
          <p className="text-xs muted">
            Only the current checkpoint page is plotted, in durable sequence
            order. This is optimizer progress, not a reward curve or a claim
            that a usable strategy exists.
          </p>
          <Button
            variant="outline"
            className="mt-3"
            onClick={() => setShowProgressChart((show) => !show)}
          >
            {showProgressChart
              ? "Hide checkpoint progress chart"
              : "Show checkpoint progress chart"}
          </Button>
          {showProgressChart && (
            <Suspense
              fallback={<Loading text="Loading checkpoint progress chart…" />}
            >
              <ResearchChart
                table={progressTable}
                title="Completed optimizer iteration by checkpoint time"
              />
            </Suspense>
          )}
        </Panel>
        <PagedResearch
          title="Complete checkpoint history"
          query={checkpoints}
          columns={checkpointColumns}
          offset={checkpointOffset}
          onPage={setCheckpointOffset}
          empty="No complete checkpoint is published. Unfinished staging files are not recovery points."
        />
        {selectedCheckpoint && (
          <CheckpointInspector
            key={selectedCheckpoint}
            id={selectedCheckpoint}
            close={() => setSelectedCheckpoint("")}
          />
        )}
        <PagedResearch
          title="Logical model calls"
          query={calls}
          columns={callColumns}
          offset={callOffset}
          onPage={setCallOffset}
          empty="No persisted model calls. A seed-only run can correctly have zero calls and zero tokens."
        />
        {selectedCall && (
          <ModelCallInspector
            key={selectedCall}
            id={selectedCall}
            runId={run.id}
            active={!stable}
            close={() => setSelectedCall("")}
          />
        )}
      </div>
    </>
  );
}
function PagedResearch<T>({
  title,
  query,
  columns,
  offset,
  onPage,
  empty,
}: {
  title: string;
  query: UseQueryResult<{ data: T[]; count: number }>;
  columns: ColumnDef<T>[];
  offset: number;
  onPage: (offset: number) => void;
  empty: string;
}) {
  return (
    <Panel title={title}>
      {query.isPending ? (
        <Loading />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : (
        <DataTable
          data={query.data?.data || []}
          columns={columns}
          label={title}
          empty={empty}
        />
      )}
      <Pagination
        offset={offset}
        limit={RESEARCH_PAGE_SIZE}
        count={query.data?.count || 0}
        busy={query.isFetching}
        onPage={onPage}
      />
    </Panel>
  );
}
function CandidateInspector({ id, close }: { id: string; close: () => void }) {
  const candidate = useQuery(candidateOptions(id));
  const parent = useQuery(candidateOptions(candidate.data?.parent_id || ""));
  const [showDiff, setShowDiff] = useState(false);
  return (
    <Panel
      title="Immutable candidate code"
      action={
        <Button variant="ghost" onClick={close}>
          Close candidate
        </Button>
      }
    >
      {candidate.isPending ? (
        <Loading />
      ) : candidate.error ? (
        <ErrorState
          error={candidate.error}
          retry={() => void candidate.refetch()}
        />
      ) : (
        candidate.data && (
          <>
            <Metadata
              values={{
                Candidate: candidate.data.candidate_key,
                "Candidate ID": candidate.data.id,
                Kind: candidate.data.kind,
                "Code SHA-256": candidate.data.code_sha256,
                "Parent ID": candidate.data.parent_id,
                "Repair index": candidate.data.repair_index,
              }}
            />
            <details className="mt-4">
              <summary className="text-link">
                Candidate source (read-only plain text)
              </summary>
              <pre className="preview mt-3" tabIndex={0}>
                {candidate.data.code}
              </pre>
            </details>
            {candidate.data.parent_id && parent.isPending ? (
              <Loading text="Loading immutable parent source…" />
            ) : parent.error ? (
              <ErrorState
                error={parent.error}
                retry={() => void parent.refetch()}
              />
            ) : (
              <>
                {!candidate.data.parent_id && (
                  <p className="text-xs muted mt-3">
                    Seed candidate has no parent. The diff compares an empty
                    source to this seed.
                  </p>
                )}
                <Button
                  variant="outline"
                  className="mt-4"
                  onClick={() => setShowDiff((value) => !value)}
                >
                  {showDiff
                    ? "Hide code diff"
                    : "Load parent / candidate code diff"}
                </Button>
                {showDiff && (
                  <Suspense
                    fallback={<Loading text="Loading self-hosted code diff…" />}
                  >
                    <CodeDiff
                      original={parent.data?.code || ""}
                      modified={candidate.data.code}
                    />
                  </Suspense>
                )}
              </>
            )}
          </>
        )
      )}
    </Panel>
  );
}
function CheckpointInspector({ id, close }: { id: string; close: () => void }) {
  const checkpoint = useQuery(checkpointOptions(id));
  return (
    <Panel
      title="Published checkpoint evidence"
      action={
        <Button variant="ghost" onClick={close}>
          Close checkpoint
        </Button>
      }
    >
      {checkpoint.isPending ? (
        <Loading />
      ) : checkpoint.error ? (
        <ErrorState
          error={checkpoint.error}
          retry={() => void checkpoint.refetch()}
        />
      ) : (
        checkpoint.data && (
          <>
            <Metadata
              values={{
                Sequence: checkpoint.data.sequence,
                Complete: checkpoint.data.complete,
                "Next stage": checkpoint.data.next_stage,
                "Schema version": checkpoint.data.schema_version,
                "Input digest": checkpoint.data.input_digest,
                "Config SHA-256": checkpoint.data.config_sha256,
                "Manifest SHA-256": checkpoint.data.manifest_sha256,
              }}
            />
            <JsonDetails
              title="Persisted candidate validity and scores"
              value={checkpoint.data.summary}
            />
          </>
        )
      )}
    </Panel>
  );
}
function ModelCallInspector({
  id,
  runId,
  active,
  close,
}: {
  id: string;
  runId: string;
  active: boolean;
  close: () => void;
}) {
  const call = useQuery(modelCallOptions(id, runId, active));
  return (
    <Panel
      title="Persisted model request and response"
      action={
        <Button variant="ghost" onClick={close}>
          Close model call
        </Button>
      }
    >
      {call.isPending ? (
        <Loading />
      ) : call.error ? (
        <ErrorState error={call.error} retry={() => void call.refetch()} />
      ) : (
        call.data && (
          <>
            <Metadata
              values={{
                "Logical call": call.data.logical_call_id,
                Provider: call.data.provider,
                Model: call.data.model,
                "Prompt version": call.data.prompt_version,
                "Request SHA-256": call.data.request_sha256,
                "Response SHA-256": call.data.response_sha256,
                Adopted: call.data.adopted,
                "Error category": call.data.error_category,
              }}
            />
            <JsonDetails
              title="Usage and provider price metadata, if recorded"
              value={call.data.usage}
            />
            <JsonDetails
              title="Sanitized persisted request / response evidence"
              value={call.data.payload}
            />
          </>
        )
      )}
    </Panel>
  );
}
function ResearchArtifact({ artifact }: { artifact: ArtifactPublic }) {
  const download = useDataAction(async () =>
    saveResearchDownload(await getArtifact(artifact.id), artifact.name),
  );
  return (
    <div className="mb-4">
      <div className="artifact">
        <div className="artifact-info">
          <p className="artifact-name">{artifact.name}</p>
          <p className="artifact-meta">
            {formatBytes(artifact.size_bytes)} · SHA-256{" "}
            {artifact.sha256.slice(0, 16)}…
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={download.pending}
          aria-label={`Download ${artifact.name}`}
          onClick={() => void download.run()}
        >
          <Download />
        </Button>
      </div>
      {Boolean(download.error) && <ErrorState error={download.error} />}
    </div>
  );
}
