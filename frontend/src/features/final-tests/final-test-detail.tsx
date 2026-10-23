import { useEffect, useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type { FinalEvaluationDetail } from "@/client/types.gen";
import {
  ErrorState,
  Loading,
  PageHeader,
  Panel,
  Status,
} from "@/components/common";
import { JsonDetails, Metadata, Pagination } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { currentUserOptions } from "@/lib/api";
import {
  FINAL_PAGE_SIZE,
  cancelFinalTest,
  finalEvidenceOptions,
  finalNeedsRefresh,
  finalTestOptions,
  refreshFinalTest,
  retryFinalTest,
} from "@/lib/final-api";
import { shortId } from "@/lib/format";
import { useDataAction } from "@/lib/use-data-action";
import { useRunEvents } from "@/lib/use-run-events";
import { useDurableAction } from "./durable-action";
import { ExposurePanel } from "./exposure";
import {
  acceptedFinalReference,
  finalControls,
  finalTrustNotice,
  retrySchema,
} from "./protocol";
import { HoldoutResult } from "./holdout-result";

export function FinalTestDetailPage() {
  const { finalTestId } = useParams({ strict: false }) as {
    finalTestId: string;
  };
  const job = useQuery(finalTestOptions(finalTestId));
  return job.isPending ? (
    <Loading text="Recovering persisted final-test state…" />
  ) : job.error ? (
    <ErrorState error={job.error} retry={() => void job.refetch()} />
  ) : job.data ? (
    <FinalTestContent key={finalTestId} job={job.data} />
  ) : (
    <p className="notice">No final test is available.</p>
  );
}
export function FinalTestContent({ job }: { job: FinalEvaluationDetail }) {
  const active = finalNeedsRefresh(job);
  const events = useRunEvents(job.id, !active);
  const owner = useQuery(currentUserOptions);
  const [offset, setOffset] = useState(0);
  const evidence = useQuery(finalEvidenceOptions(job.id, offset, active));
  const evaluationId = acceptedFinalReference(job);
  useEffect(() => {
    void refreshFinalTest(job.id, job.final_selection_id);
  }, [job.id, job.final_selection_id, job.status, job.next_event_seq]);
  const cancel = useDataAction(async () => {
    try {
      await cancelFinalTest(job.id);
    } finally {
      await refreshFinalTest(job.id, job.final_selection_id);
    }
  });
  const controls = finalControls(job);
  return (
    <>
      <Link
        className="text-link mb-5"
        to="/final-selections/$selectionId"
        params={{ selectionId: job.final_selection_id }}
      >
        Frozen selection
      </Link>
      <PageHeader
        eyebrow={`FINAL HOLDOUT TEST ${shortId(job.id)}`}
        title="Final-test execution"
        subtitle="A distinct persisted holdout job, never an optimizer iteration or validation score."
      >
        <Status status={job.status} />
      </PageHeader>
      <p className="notice" data-testid="final-trust-notice">
        {finalTrustNotice}
      </p>
      <div className="stack mt-6">
        <Panel title="Durable final-job state">
          <Metadata
            values={{
              "Final-test ID": job.id,
              "Frozen selection": job.final_selection_id,
              "Split version": job.split_version_id,
              Generation: job.generation,
              "Active attempt": job.active_attempt_id,
              "Holdout [start, end)": job.config.holdout,
              "Frozen code SHA-256": job.config.selected_strategy.sha256,
              "Environment digest": job.config.environment_digest,
              "Event sequence": job.next_event_seq,
              Updated: job.updated_at,
            }}
          />
          <p className="text-xs muted mt-4">
            Refresh reloads authoritative persisted state. Authenticated events
            are notifications only; polling continues until terminal status and
            confirmed sandbox stop. Final tests cannot pause.
          </p>
          {job.status === "cancel_requested" && (
            <p className="notice mt-4">
              Cancellation requested. This is not a stopped sandbox; retry stays
              blocked until the server confirms stop.
            </p>
          )}
          {job.error && (
            <p className="notice error mt-4" role="alert">
              {job.error}
            </p>
          )}
          {Boolean(cancel.error) && <ErrorState error={cancel.error} />}
          <div className="actions mt-4">
            <Button
              variant="outline"
              disabled={!controls.cancel || cancel.pending}
              onClick={() => void cancel.run()}
            >
              {cancel.pending
                ? "Requesting cancellation…"
                : "Cancel final test"}
            </Button>
          </div>
          {owner.error ? (
            <ErrorState
              error={owner.error}
              retry={() => void owner.refetch()}
            />
          ) : owner.data ? (
            <RetryFinalTest
              key={`${owner.data.id}:${job.id}`}
              owner={owner.data.id}
              job={job}
            />
          ) : (
            <Loading text="Checking owner for retry controls…" />
          )}
          <JsonDetails
            title="Immutable final execution configuration"
            value={job.config}
          />
          <JsonDetails
            title="Attempts and sandbox-stop evidence"
            value={job.attempts}
          />
        </Panel>
        {evaluationId && job.result && (
          <ExposurePanel
            snapshot={job.result.exposure_snapshot}
            title="Exposure recorded for final execution"
          />
        )}
        <Panel title="Holdout evaluation evidence">
          <p className="notice mb-4">
            Historical execution metadata is not a reference result. Only the
            current durably succeeded final job can display metrics or export a
            strategy package.
          </p>
          {evidence.isPending ? (
            <Loading />
          ) : evidence.error ? (
            <ErrorState
              error={evidence.error}
              retry={() => void evidence.refetch()}
            />
          ) : (
            evidence.data && (
              <>
                {!evidence.data.data.length ? (
                  <p className="notice">
                    No holdout evaluation evidence has been published yet.
                    Queued or failed execution is not a result.
                  </p>
                ) : (
                  <ul className="stack">
                    {evidence.data.data.map((row) => (
                      <li key={row.id}>
                        <div className="flex items-center gap-4">
                          <span>Holdout evaluation {shortId(row.id)}</span>
                          <Status status={row.status} />
                          <span className="text-xs muted">
                            Generation {row.generation} · sandbox{" "}
                            {row.sandbox_stopped
                              ? "confirmed stopped"
                              : "not confirmed stopped"}
                          </span>
                        </div>
                        {row.error && (
                          <p className="notice error">{row.error}</p>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                <Pagination
                  offset={offset}
                  limit={FINAL_PAGE_SIZE}
                  count={evidence.data.count}
                  busy={evidence.isFetching}
                  onPage={setOffset}
                />
              </>
            )
          )}
        </Panel>
        {evaluationId && (
          <HoldoutResult
            key={`${job.generation}:${job.active_attempt_id}:${evaluationId}`}
            evaluationId={evaluationId}
            selectionId={job.final_selection_id}
          />
        )}
        <Panel title="Recoverable execution events">
          {events.error ? (
            <>
              <p className="notice">
                Event recovery is temporarily unavailable. Persisted job state
                still refreshes independently.
              </p>
              <ErrorState
                error={events.error}
                retry={() => void events.refetch()}
              />
            </>
          ) : events.isPending ? (
            <Loading text="Recovering event sequence…" />
          ) : (
            <JsonDetails
              title={`${events.data?.length || 0} persisted events`}
              value={events.data || []}
            />
          )}
        </Panel>
      </div>
    </>
  );
}
function RetryFinalTest({
  owner,
  job,
}: {
  owner: string;
  job: FinalEvaluationDetail;
}) {
  const controls = finalControls(job);
  const [confirmed, setConfirmed] = useState(false);
  const retry = useDurableAction({
    owner,
    scope: `retry:${job.id}`,
    schema: retrySchema,
    initial: { final_evaluation_id: job.id, generation: job.generation },
  });
  useEffect(() => {
    if (
      !retry.hasPending &&
      !retry.busy &&
      retry.body?.generation !== job.generation
    ) {
      retry.edit({ final_evaluation_id: job.id, generation: job.generation });
      setConfirmed(false);
    }
  }, [
    job.id,
    job.generation,
    retry.hasPending,
    retry.busy,
    retry.body?.generation,
  ]);
  if (!controls.retryReason && !retry.hasPending) return null;
  return (
    <div className="stack mt-4">
      {controls.retryReason && <p className="notice">{controls.retryReason}</p>}
      {retry.hasPending && (
        <p className="notice">
          Retry response unconfirmed. Recovering the same request key cannot
          create another generation.
        </p>
      )}
      <label className="flex items-start gap-3">
        <input
          type="checkbox"
          checked={confirmed}
          disabled={retry.busy || (!controls.retry && !retry.hasPending)}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>
          Confirm retry of the unchanged frozen holdout, with additional
          recorded exposure.
        </span>
      </label>
      {Boolean(retry.error) && <ErrorState error={retry.error} />}
      <Button
        variant="outline"
        disabled={
          !confirmed || retry.busy || (!controls.retry && !retry.hasPending)
        }
        onClick={() =>
          void retry.run(
            (body, key) => retryFinalTest(body.final_evaluation_id, key),
            () => {
              setConfirmed(false);
              void refreshFinalTest(job.id, job.final_selection_id);
            },
          )
        }
      >
        {retry.busy
          ? "Retrying…"
          : retry.hasPending
            ? "Recover same retry request"
            : "Retry complete final test"}
      </Button>
    </div>
  );
}
