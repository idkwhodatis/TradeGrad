import {
  NamedListControls,
  type NamedListFilters,
} from "@/components/named-list-controls";
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import type {
  CandidatePublic,
  CheckpointPublic,
  ModelCallPublic,
  PortableEvaluationPublic,
  PortableRunDetail,
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
import { currentUserOptions } from "@/lib/api";
import {
  portableCandidateOptions,
  portableCandidatesOptions,
  portableCheckpointOptions,
  portableCheckpointsOptions,
  portableCommand,
  portableEvaluationsOptions,
  portableModelCallOptions,
  portableModelCallsOptions,
  portableNeedsRefresh,
  portableRunOptions,
  portableUsageOptions,
  refreshPortable,
  PORTABLE_PAGE_SIZE,
} from "@/lib/portable-api";
import { rawCell } from "@/lib/research-results";
import { portableControls, portableNotice } from "./protocol";
import { PortableFreezeCandidatePanel } from "./freeze-candidate";

export function PortableResearchPage() {
  const { runId } = useParams({ strict: false }) as { runId: string };
  const user = useQuery(currentUserOptions);
  return user.isPending ? (
    <Loading text="Verifying portable research owner…" />
  ) : user.error ? (
    <ErrorState error={user.error} retry={() => void user.refetch()} />
  ) : user.data ? (
    <PortableRunLoader
      key={`${user.data.id}:${runId}`}
      owner={user.data.id}
      runId={runId}
    />
  ) : null;
}
function PortableRunLoader({ owner, runId }: { owner: string; runId: string }) {
  const run = useQuery(portableRunOptions(owner, runId));
  return (
    <>
      <Link to="/portable/new" className="text-link mb-5">
        Portable experiments
      </Link>
      {run.isPending ? (
        <Loading text="Loading persisted portable state…" />
      ) : run.error ? (
        <ErrorState error={run.error} retry={() => void run.refetch()} />
      ) : run.data ? (
        <PortableResearchContent
          key={`${owner}:${runId}`}
          owner={owner}
          run={run.data}
        />
      ) : (
        <p className="notice">No persisted portable run is available.</p>
      )}
    </>
  );
}
export function PortableResearchContent({
  owner,
  run,
}: {
  owner: string;
  run: PortableRunDetail;
}) {
  const [candidateFilters, setCandidateFilters] = useState<NamedListFilters>(
    {},
  );
  const [candidateOffset, setCandidateOffset] = useState(0),
    [evaluationOffset, setEvaluationOffset] = useState(0),
    [checkpointOffset, setCheckpointOffset] = useState(0),
    [callOffset, setCallOffset] = useState(0);
  const [candidateId, setCandidateId] = useState(""),
    [checkpointId, setCheckpointId] = useState(""),
    [callId, setCallId] = useState("");
  const active = portableNeedsRefresh(run),
    controls = portableControls(run);
  const candidates = useQuery(
    portableCandidatesOptions(
      owner,
      run.id,
      candidateOffset,
      active,
      candidateFilters,
    ),
  );
  const evaluations = useQuery(
    portableEvaluationsOptions(owner, run.id, evaluationOffset, active),
  );
  const checkpoints = useQuery(
    portableCheckpointsOptions(owner, run.id, checkpointOffset, active),
  );
  const calls = useQuery(
    portableModelCallsOptions(owner, run.id, callOffset, active),
  );
  const usage = useQuery(portableUsageOptions(owner, run.id, active));
  const candidate = useQuery(portableCandidateOptions(owner, candidateId));
  const checkpoint = useQuery(portableCheckpointOptions(owner, checkpointId));
  const call = useQuery(portableModelCallOptions(owner, callId, active));
  const [pending, setPending] = useState(false),
    [failure, setFailure] = useState<unknown>(null);
  const mounted = useRef(true),
    locked = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    void refreshPortable(owner);
  }, [owner, run.id, run.status, run.next_event_seq]);
  async function command(action: "pause" | "cancel" | "resume") {
    if (locked.current || !mounted.current || !controls[action]) return;
    locked.current = true;
    setPending(true);
    setFailure(null);
    try {
      await portableCommand(run.id, action);
    } catch (error) {
      if (mounted.current) setFailure(error);
    } finally {
      await refreshPortable(owner);
      locked.current = false;
      if (mounted.current) setPending(false);
    }
  }
  const candidateColumns: ColumnDef<CandidatePublic>[] = [
    {
      accessorKey: "candidate_key",
      header: "Immutable candidate",
      cell: ({ row }) => (
        <Button variant="ghost" onClick={() => setCandidateId(row.original.id)}>
          {row.original.candidate_key}
        </Button>
      ),
    },
    {
      accessorKey: "parent_id",
      header: "Parent lineage",
      cell: ({ row }) =>
        row.original.parent_id ? (
          <Button
            variant="ghost"
            onClick={() => setCandidateId(row.original.parent_id!)}
          >
            {row.original.parent_id}
          </Button>
        ) : (
          "Seed / no parent"
        ),
    },
    { accessorKey: "kind", header: "Kind" },
    { accessorKey: "iteration", header: "Iteration" },
    { accessorKey: "repair_index", header: "Repair" },
    { accessorKey: "code_sha256", header: "Code SHA-256" },
  ];
  const evaluationColumns: ColumnDef<PortableEvaluationPublic>[] = [
    { accessorKey: "candidate_key", header: "Candidate" },
    { accessorKey: "split", header: "Split" },
    {
      accessorKey: "window",
      header: "Exact window",
      cell: ({ row }) => rawCell(row.original.window),
    },
    { accessorKey: "status", header: "Status" },
    {
      accessorKey: "sandbox_stopped",
      header: "Both containers stopped",
      cell: ({ row }) =>
        row.original.sandbox_stopped ? "Confirmed" : "Not confirmed",
    },
    {
      id: "result",
      header: "Persisted evidence",
      cell: ({ row }) =>
        row.original.status === "succeeded" &&
        row.original.sandbox_stopped &&
        row.original.finished_at ? (
          <Link
            to="/portable/evaluations/$evaluationId"
            params={{ evaluationId: row.original.id }}
            className="text-link"
          >
            Open portable result
          </Link>
        ) : (
          row.original.error || "No successful stopped result"
        ),
    },
  ];
  const checkpointColumns: ColumnDef<CheckpointPublic>[] = [
    {
      accessorKey: "sequence",
      header: "Sequence",
      cell: ({ row }) => (
        <Button
          variant="ghost"
          onClick={() => setCheckpointId(row.original.id)}
        >
          {row.original.sequence}
        </Button>
      ),
    },
    { accessorKey: "iteration", header: "Iteration" },
    { accessorKey: "next_stage", header: "Next stage" },
    {
      accessorKey: "complete",
      header: "Published complete",
      cell: ({ row }) => String(row.original.complete),
    },
    { accessorKey: "manifest_sha256", header: "Manifest SHA-256" },
  ];
  const callColumns: ColumnDef<ModelCallPublic>[] = [
    {
      accessorKey: "logical_call_id",
      header: "Logical call",
      cell: ({ row }) => (
        <Button variant="ghost" onClick={() => setCallId(row.original.id)}>
          {row.original.logical_call_id}
        </Button>
      ),
    },
    { accessorKey: "status", header: "Status" },
    {
      accessorKey: "adopted",
      header: "Adopted",
      cell: ({ row }) => String(row.original.adopted),
    },
    { accessorKey: "model", header: "Model" },
    {
      accessorKey: "usage",
      header: "Token usage",
      cell: ({ row }) => rawCell(row.original.usage),
    },
  ];
  return (
    <>
      <PageHeader
        eyebrow={`PORTABLE RUN · ${run.id}`}
        title={`${run.config.strategy_kind === "portfolio" ? "Momentum portfolio" : "Trend event policy"} · ${run.config.task}`}
        subtitle={`${run.execution_mode} · generation ${run.generation} · persisted event sequence ${run.next_event_seq}`}
      >
        <Status status={run.status} />
      </PageHeader>
      <p className="notice" data-testid="portable-trust">
        {portableNotice}
      </p>
      <div className="stack mt-6">
        <Panel title="Run control and durable progress">
          <div className="actions">
            {controls.pause && (
              <Button
                variant="outline"
                disabled={pending}
                onClick={() => void command("pause")}
              >
                Pause portable run
              </Button>
            )}
            {controls.cancel && (
              <Button
                variant="outline"
                disabled={pending}
                onClick={() => void command("cancel")}
              >
                Cancel portable run
              </Button>
            )}
            {["paused", "failed", "cancelled", "interrupted"].includes(
              run.status,
            ) && (
              <Button
                disabled={pending || !controls.resume}
                onClick={() => void command("resume")}
              >
                Resume from portable checkpoint
              </Button>
            )}
            <Button
              variant="outline"
              onClick={() => void refreshPortable(owner)}
            >
              Refresh persisted state
            </Button>
          </div>
          {controls.resumeReason && (
            <p className="notice mt-4">{controls.resumeReason}</p>
          )}
          {["pause_requested", "cancel_requested"].includes(run.status) && (
            <p className="notice mt-4">
              This request is not proof of stopped execution. Wait for both
              policy and trusted engine containers to be confirmed stopped.
            </p>
          )}
          {run.attempts.some((item) => !item.sandbox_stopped) && (
            <p className="notice mt-4">
              An execution environment is still not confirmed stopped. Results
              and resume remain subject to server verification.
            </p>
          )}
          {Boolean(failure) && <ErrorState error={failure} />}{" "}
          {run.error && (
            <p className="notice error mt-4" role="alert">
              {run.error}
            </p>
          )}
          <Metadata
            values={{
              "Latest checkpoint": run.latest_checkpoint_id,
              "Resume checkpoint": run.resume_checkpoint_id,
              "Trusted engine image ID":
                run.research_image_digest || "Not pinned yet",
              "Isolated policy image ID":
                run.portable_policy_image_digest || "Not pinned yet",
              "Environment SHA-256": run.config.environment_digest,
              Conformance: "interface_only",
              "Runtime certification":
                "Unverified until actual real-container CI",
              "External frameworks": "not_certified",
              "Experiment family": run.experiment_id,
              "Split version": run.split_version_id,
              Updated: run.updated_at,
            }}
          />
          <JsonDetails
            title="Execution attempts and stop evidence"
            value={run.attempts}
          />
        </Panel>
        <Panel title="Portable outcome, not native or unseen-holdout evidence">
          {run.result ? (
            <>
              <p className="notice">
                {run.result.is_synthetic
                  ? "Synthetic dataset, not market-performance evidence."
                  : "Non-synthetic upload: raw/no-actions claims are unverified user research assumptions, not provider evidence."}{" "}
                {run.config.model?.provider === "fixture"
                  ? "Deterministic fixture model; no provider request."
                  : "Operator model profile; inspect persisted calls, usage and stop receipts. Provider charges cannot be inferred from tokens."}{" "}
                Holdout execution is excluded from this research run.
              </p>
              <Metadata
                values={{
                  "Stop reason": run.result.stop_reason,
                  "Best candidate": run.result.best_candidate_key,
                  "Combined score": run.result.combined_score,
                  Objective: run.result.objective_definition,
                  "Result trust": run.result.result_trust,
                  "Ledger origin": run.result.ledger_origin,
                }}
              />
              <JsonDetails
                title="Persisted portable summary"
                value={run.result}
              />
            </>
          ) : (
            <p className="empty">
              No completed portable summary is published. Missing metrics are
              not zero and an accepted run is not a successful evaluation.
            </p>
          )}
          <JsonDetails
            title="Immutable portable configuration and windows"
            value={run.config}
          />
        </Panel>
        <PortableFreezeCandidatePanel owner={owner} run={run} />
        <Panel title="Candidate lineage">
          <NamedListControls
            kind="candidate"
            value={candidateFilters}
            onChange={(value) => {
              setCandidateFilters(value);
              setCandidateOffset(0);
            }}
          />
          <Paged
            query={candidates}
            columns={candidateColumns}
            label="Portable candidate lineage"
            offset={candidateOffset}
            onPage={setCandidateOffset}
          />
          {candidateId && (
            <div className="mt-4">
              <Button variant="ghost" onClick={() => setCandidateId("")}>
                Close candidate details
              </Button>
              {candidate.isPending ? (
                <Loading text="Loading immutable candidate…" />
              ) : candidate.error ? (
                <ErrorState
                  error={candidate.error}
                  retry={() => void candidate.refetch()}
                />
              ) : candidate.data ? (
                <>
                  <Metadata
                    values={{
                      Candidate: candidate.data.candidate_key,
                      Parent: candidate.data.parent_id,
                      "Code SHA-256": candidate.data.code_sha256,
                      Generation: candidate.data.generation,
                    }}
                  />
                  <pre className="preview mt-3" tabIndex={0}>
                    {candidate.data.code}
                  </pre>
                </>
              ) : null}
            </div>
          )}
        </Panel>
        <Panel title="Training and validation evaluations">
          <Paged
            query={evaluations}
            columns={evaluationColumns}
            label="Portable evaluation evidence"
            offset={evaluationOffset}
            onPage={setEvaluationOffset}
          />
        </Panel>
        <Panel title="Complete checkpoints">
          <Paged
            query={checkpoints}
            columns={checkpointColumns}
            label="Portable checkpoints"
            offset={checkpointOffset}
            onPage={setCheckpointOffset}
          />
          {checkpointId && (
            <div className="mt-4">
              <Button variant="ghost" onClick={() => setCheckpointId("")}>
                Close checkpoint details
              </Button>
              {checkpoint.isPending ? (
                <Loading text="Loading checkpoint…" />
              ) : checkpoint.error ? (
                <ErrorState
                  error={checkpoint.error}
                  retry={() => void checkpoint.refetch()}
                />
              ) : checkpoint.data ? (
                <JsonDetails
                  title="Persisted checkpoint identity and optimizer state"
                  value={checkpoint.data}
                />
              ) : null}
            </div>
          )}
        </Panel>
        <Panel title="Model calls and usage">
          <p className="notice">
            {run.config.model?.provider === "fixture"
              ? "Token counts are deterministic fixture accounting, not paid-model bills."
              : "Token counts are provider-reported or conservatively reserved when usage is unknown. Failed or uncertain requests are not automatically retried; local stop receipts do not undo provider processing or charges."}{" "}
            Cost is unavailable, not zero. Neither usage nor a completed run
            proves model quality.
          </p>
          {usage.isPending ? (
            <Loading text="Loading persisted usage…" />
          ) : usage.error ? (
            <ErrorState
              error={usage.error}
              retry={() => void usage.refetch()}
            />
          ) : usage.data ? (
            <Metadata
              values={{
                "Logical calls": usage.data.calls,
                "Adopted calls": usage.data.adopted_calls,
                "Input tokens": usage.data.input_tokens,
                "Output tokens": usage.data.output_tokens,
                "Charged or reserved tokens": usage.data.charged_tokens,
                "Unknown usage calls": usage.data.unknown_usage_calls,
                Cost: "Unavailable",
                "Usage evidence": usage.data.usage_sources,
              }}
            />
          ) : null}
          <Paged
            query={calls}
            columns={callColumns}
            label="Portable model calls"
            offset={callOffset}
            onPage={setCallOffset}
          />
          {callId && (
            <div className="mt-4">
              <Button variant="ghost" onClick={() => setCallId("")}>
                Close model call details
              </Button>
              {call.isPending ? (
                <Loading text="Loading persisted model call…" />
              ) : call.error ? (
                <ErrorState
                  error={call.error}
                  retry={() => void call.refetch()}
                />
              ) : call.data ? (
                <JsonDetails
                  title="Model request, response and adoption"
                  value={call.data}
                />
              ) : null}
            </div>
          )}
        </Panel>
      </div>
    </>
  );
}
function Paged<T>({
  query,
  columns,
  label,
  offset,
  onPage,
}: {
  query: UseQueryResult<{ data: T[]; count: number }, unknown>;
  columns: ColumnDef<T>[];
  label: string;
  offset: number;
  onPage: (value: number) => void;
}) {
  return query.isPending ? (
    <Loading text={`Loading ${label.toLowerCase()}…`} />
  ) : query.error ? (
    <ErrorState error={query.error} retry={() => void query.refetch()} />
  ) : query.data ? (
    <>
      <DataTable
        data={query.data.data}
        columns={columns}
        label={label}
        empty="No persisted records in this page."
      />
      <Pagination
        offset={offset}
        limit={PORTABLE_PAGE_SIZE}
        count={query.data.count}
        busy={query.isFetching}
        onPage={onPage}
      />
    </>
  ) : null;
}
