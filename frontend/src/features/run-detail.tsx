import { useEffect, useRef, useState } from "react";
import { Link, Navigate, useParams } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Download,
  Eye,
  FileCode2,
  Info,
  LoaderCircle,
  RotateCcw,
  Square,
  X,
} from "lucide-react";
import type { ArtifactPublic, RunDetail } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import {
  DemoNotice,
  NativeWindowNotice,
  ErrorState,
  Loading,
  PageHeader,
  Panel,
  Status,
} from "@/components/common";
import {
  artifactsOptions,
  cancelRun,
  getArtifact,
  queryClient,
  resumeRun,
  runOptions,
} from "@/lib/api";
import { datasetOptions } from "@/lib/data-api";
import { snapshotLabel } from "@/lib/data-utils";
import { useRunEvents } from "@/lib/use-run-events";
import { researchConfig } from "@/lib/navigation";
import {
  formatBytes,
  formatDate,
  label,
  shortId,
  taskLabels,
  terminalStatuses,
} from "@/lib/format";

export function RunDetailPage() {
  const { runId } = useParams({ strict: false }) as { runId: string };
  const run = useQuery(runOptions(runId));
  if (run.data && researchConfig(run.data.config)) {
    return <Navigate to="/research-runs/$runId" params={{ runId }} replace />;
  }
  return (
    <>
      <Link to="/runs" className="text-link mb-5">
        <ArrowLeft size={14} />
        All experiments
      </Link>
      {run.isPending ? (
        <Loading text="Loading durable run state…" />
      ) : run.error ? (
        <ErrorState error={run.error} retry={() => void run.refetch()} />
      ) : run.data ? (
        <RunContent key={runId} run={run.data} />
      ) : null}
    </>
  );
}
function RunContent({ run }: { run: RunDetail }) {
  const dataset = useQuery(datasetOptions(run.dataset_version_id));
  const terminal = terminalStatuses.has(run.status);
  const events = useRunEvents(run.id, terminal);
  const artifacts = useQuery({
    ...artifactsOptions(run.id),
    refetchInterval: terminal ? false : 3000,
  });
  const previousStatus = useRef(run.status);
  useEffect(() => {
    if (previousStatus.current !== run.status) {
      void queryClient.invalidateQueries({ queryKey: ["events", run.id] });
      void queryClient.invalidateQueries({ queryKey: ["artifacts", run.id] });
      previousStatus.current = run.status;
    }
  }, [run.status, run.id]);
  const command = useMutation({
    mutationFn: (action: "cancel" | "resume") =>
      action === "cancel" ? cancelRun(run.id) : resumeRun(run.id),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["run", run.id] }),
        queryClient.invalidateQueries({ queryKey: ["runs"] }),
        queryClient.invalidateQueries({ queryKey: ["events", run.id] }),
      ]);
    },
  });
  const canResume =
    ["failed", "cancelled", "interrupted"].includes(run.status) &&
    run.attempts.every((attempt) => attempt.sandbox_stopped);
  const stages = ["queued", "preparing", "running", "finalizing", "succeeded"];
  const stageIndex = stages.indexOf(run.status);
  const metrics =
    run.result?.metrics && typeof run.result.metrics === "object"
      ? Object.entries(run.result.metrics as Record<string, unknown>).filter(
          ([, value]) => typeof value === "number" && Number.isFinite(value),
        )
      : [];
  const percentMetrics = new Set([
    "annualized_return",
    "max_drawdown",
    "return_per_trade",
  ]);
  return (
    <>
      <PageHeader
        eyebrow={`EXPERIMENT ${shortId(run.id)}`}
        title={taskLabels[String(run.config.task)] || "Seed evaluation"}
        subtitle={`Created ${formatDate(run.created_at)} UTC · Fixed seed · Generation ${run.generation}`}
      >
        <div className="actions">
          <Status status={run.status} />
          {!terminal && (
            <Button
              variant="outline"
              disabled={command.isPending || run.status === "cancel_requested"}
              onClick={() => command.mutate("cancel")}
            >
              <Square size={13} />
              {run.status === "cancel_requested"
                ? "Cancellation requested"
                : "Cancel run"}
            </Button>
          )}
          {canResume && (
            <Button
              variant="outline"
              disabled={command.isPending}
              onClick={() => command.mutate("resume")}
            >
              {command.isPending ? (
                <LoaderCircle className="spinning" />
              ) : (
                <RotateCcw />
              )}
              Resume evaluation
            </Button>
          )}
        </div>
      </PageHeader>
      <DemoNotice />
      <NativeWindowNotice />
      {command.error && (
        <div className="mt-4">
          <ErrorState error={command.error} />
        </div>
      )}
      {run.error && (
        <div className="notice error mt-4" role="alert">
          {run.error}
        </div>
      )}
      {run.status === "cancel_requested" && (
        <div className="notice mt-4" role="status">
          <Info />
          <p>
            Waiting for the evaluator to confirm it has stopped. This run is not
            cancelled until that confirmation is durable.
          </p>
        </div>
      )}
      {canResume && (
        <div className="notice mt-4">
          <Info />
          <p>
            Resume creates a new attempt and evaluates the same seed from
            scratch. Optimizer checkpoint recovery is not part of M1.
          </p>
        </div>
      )}
      {terminal && !canResume && run.status !== "succeeded" && (
        <div className="notice mt-4">
          <Info />
          <p>
            Recovery is unavailable until the previous execution environment is
            confirmed stopped. Contact your administrator if reconciliation does
            not complete.
          </p>
        </div>
      )}
      <div className="section-grid mt-6">
        <div className="stack">
          <Panel
            title="Execution timeline"
            action={
              <span className="text-[10px] muted">Durable events · UTC</span>
            }
          >
            <div className="stage-list" aria-label="Run stages">
              {stages.map((stage, index) => (
                <div
                  key={stage}
                  className={`stage ${index < stageIndex ? "done" : index === stageIndex ? "current" : ""}`}
                >
                  {stage === "succeeded" ? "Complete" : label(stage)}
                </div>
              ))}
            </div>
            {events.isPending ? (
              <Loading text="Recovering ordered events…" />
            ) : events.error ? (
              <ErrorState
                error={events.error}
                retry={() => void events.refetch()}
              />
            ) : (
              <ol className="events" aria-label="Run events">
                {events.data?.map((event) => (
                  <li
                    key={event.seq}
                    className="event"
                    data-event-seq={event.seq}
                  >
                    <time dateTime={event.occurred_at}>
                      {new Date(event.occurred_at).toISOString().slice(11, 19)}
                    </time>
                    <div>
                      <strong>{label(event.type)}</strong>
                      <p>
                        {Object.entries(event.payload)
                          .map(
                            ([key, value]) =>
                              `${label(key)}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`,
                          )
                          .join(" · ") || `Event ${event.seq}`}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
            <p className="text-[10px] muted mt-4" role="status">
              {terminal
                ? "Final state loaded from durable storage."
                : "Refreshing status and resuming events from the last sequence every 2 seconds."}
            </p>
          </Panel>
          {run.status === "succeeded" && (
            <Panel
              title="Evaluation results"
              action={<span className="chip">Native · untrusted result</span>}
            >
              <p className="text-xs muted mb-5">
                Fixed-seed native AKQuant metrics. Strategy and engine share a
                process, so container isolation is not an independently trusted
                ledger. Display rounding does not change the stored values.
                These are not live or holdout performance claims.
              </p>
              <dl className="metric-grid">
                {metrics.map(([name, value]) => (
                  <div className="metric" key={name}>
                    <dt>{label(name)}</dt>
                    <dd>
                      {(value as number).toLocaleString("en", {
                        maximumFractionDigits: 3,
                      })}
                      {percentMetrics.has(name) ? "%" : ""}
                    </dd>
                  </div>
                ))}
              </dl>
              {!metrics.length && (
                <p className="notice">
                  No numeric summary metrics are available. Inspect the
                  persisted result bundle below.
                </p>
              )}
              <p className="text-[10px] muted mt-4">
                M0 engine metric basis; percent-suffixed values are already
                percentage points. Other values retain their raw engine units.
                Versioned result definitions and native charts arrive in M3/M4.
              </p>
              <details className="mt-5">
                <summary className="text-link cursor-pointer">
                  Inspect stored result bundle
                </summary>
                <pre className="preview mt-3" data-testid="result-bundle">
                  {JSON.stringify(run.result, null, 2)}
                </pre>
              </details>
            </Panel>
          )}
          <Panel
            title="Artifacts"
            action={
              <span className="text-xs muted">
                {artifacts.data?.count ?? 0} files
              </span>
            }
          >
            {artifacts.isPending ? (
              <Loading text="Loading artifact manifest…" />
            ) : artifacts.error ? (
              <ErrorState
                error={artifacts.error}
                retry={() => void artifacts.refetch()}
              />
            ) : artifacts.data?.data.length ? (
              <div className="space-y-3">
                {artifacts.data.data.map((artifact) => (
                  <ArtifactRow key={artifact.id} artifact={artifact} />
                ))}
              </div>
            ) : (
              <p className="text-xs muted leading-6">
                Published artifacts appear here once an attempt has produced
                them. No placeholder results are shown.
              </p>
            )}
          </Panel>
        </div>
        <div className="stack">
          <Panel title="Immutable configuration">
            <dl className="details !mt-0">
              <div>
                <dt>Run ID</dt>
                <dd className="hash">{run.id}</dd>
              </div>
              <div>
                <dt>Dataset version</dt>
                <dd className="hash">
                  <Link
                    className="text-link"
                    to="/datasets/$datasetId"
                    params={{ datasetId: run.dataset_version_id }}
                  >
                    {run.dataset_version_id}
                  </Link>
                </dd>
              </div>
              <div>
                <dt>Data evidence</dt>
                <dd>
                  {dataset.data
                    ? snapshotLabel(dataset.data)
                    : "Inspect pinned dataset"}
                </dd>
              </div>
              <div>
                <dt>Strategy version</dt>
                <dd className="hash">{run.strategy_version_id}</dd>
              </div>
              <div>
                <dt>Start · inclusive</dt>
                <dd>{String(run.config.start_date || "—")}</dd>
              </div>
              <div>
                <dt>End · exclusive</dt>
                <dd>{String(run.config.end_date || "—")}</dd>
              </div>
              <div>
                <dt>Profile</dt>
                <dd className="hash">{String(run.config.profile || "—")}</dd>
              </div>
              <div>
                <dt>Execution</dt>
                <dd>Seed evaluation</dd>
              </div>
            </dl>
            <details className="mt-5">
              <summary className="text-link cursor-pointer">
                Full configuration
              </summary>
              <pre className="preview mt-3">
                {JSON.stringify(run.config, null, 2)}
              </pre>
            </details>
          </Panel>
          <Panel title="Execution attempts">
            {run.attempts.length ? (
              <div className="space-y-5">
                {run.attempts.map((attempt) => (
                  <div key={attempt.id}>
                    <div className="flex justify-between gap-3">
                      <h3 className="!text-xs">
                        Generation {attempt.generation}
                      </h3>
                      <span className="text-[10px] muted">
                        {label(attempt.status)}
                      </span>
                    </div>
                    <p className="hash muted mt-2">{attempt.id}</p>
                    <p className="mt-2 text-[11px] muted">
                      Sandbox{" "}
                      {attempt.sandbox_stopped
                        ? "confirmed stopped"
                        : "not yet confirmed stopped"}
                      {attempt.exit_code !== null
                        ? ` · exit ${attempt.exit_code}`
                        : ""}
                    </p>
                    {attempt.error && (
                      <p className="text-[11px] text-[#a85b46] mt-2">
                        {attempt.error}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs muted leading-6">
                Waiting for a worker to claim this run.
              </p>
            )}
          </Panel>
        </div>
      </div>
    </>
  );
}
function ArtifactRow({ artifact }: { artifact: ArtifactPublic }) {
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const canPreview =
    artifact.size_bytes <= 1_000_000 &&
    /\.(json|txt|log|py|csv)$/.test(artifact.name);
  async function open(previewOnly: boolean) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const blob = await getArtifact(artifact.id);
      if (previewOnly) setPreview(await blob.text());
      else {
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = artifact.name;
        anchor.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div>
      <div className="artifact">
        <FileCode2 size={20} />
        <div className="artifact-info">
          <div className="artifact-name">{artifact.name}</div>
          <div className="artifact-meta">
            {formatBytes(artifact.size_bytes)} · SHA-256{" "}
            {artifact.sha256.slice(0, 12)}…
          </div>
        </div>
        {canPreview && (
          <Button
            size="icon-sm"
            variant="ghost"
            disabled={busy}
            onClick={() => void open(true)}
            aria-label={`Preview ${artifact.name}`}
          >
            <Eye size={16} />
          </Button>
        )}
        <Button
          size="icon-sm"
          variant="ghost"
          disabled={busy}
          onClick={() => void open(false)}
          aria-label={`Download ${artifact.name}`}
        >
          {busy ? (
            <LoaderCircle className="spinning" />
          ) : (
            <Download size={16} />
          )}
        </Button>
      </div>
      {Boolean(error) && (
        <div className="mt-3">
          <ErrorState error={error} />
        </div>
      )}
      {preview !== null && (
        <div className="mt-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs muted">
              Read-only preview · {artifact.name}
            </span>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Close artifact preview"
              onClick={() => setPreview(null)}
            >
              <X />
            </Button>
          </div>
          <pre className="preview" tabIndex={0}>
            {preview}
          </pre>
        </div>
      )}
    </div>
  );
}
