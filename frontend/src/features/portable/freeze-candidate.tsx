import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type { CheckpointDetail, PortableRunDetail } from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { Field, Metadata } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import {
  portableCandidateOptions,
  portableCheckpointOptions,
} from "@/lib/portable-api";
import {
  portableFinalSelectionsOptions,
  freezePortableSelection,
  refreshPortableFinal,
} from "@/lib/portable-final-api";
import { useDurableAction } from "@/features/final-tests/durable-action";
import { exposureNotice } from "@/features/final-tests/protocol";
import {
  eligibleCandidates,
  freezeSchema,
  portableFreezeUnavailable,
  portableCheckpointMatches,
} from "./final-protocol";

export function PortableFreezeCandidatePanel({
  owner,
  run,
}: {
  owner: string;
  run: PortableRunDetail;
}) {
  const reason = portableFreezeUnavailable(run);
  return (
    <Panel title="Freeze a portable final candidate">
      {reason ? (
        <>
          <p className="notice" data-testid="freeze-unavailable">
            {reason}
          </p>
          {(!run.experiment_id || !run.split_version_id) && (
            <Link className="text-link mt-4" to="/portable/new">
              Create bound portable research
            </Link>
          )}
        </>
      ) : (
        <FreezeInputs key={`${owner}:${run.id}`} owner={owner} run={run} />
      )}
    </Panel>
  );
}
function FreezeInputs({
  owner,
  run,
}: {
  owner: string;
  run: PortableRunDetail;
}) {
  const checkpoint = useQuery(
    portableCheckpointOptions(owner, run.latest_checkpoint_id!),
  );
  const existing = useQuery(
    portableFinalSelectionsOptions(owner, run.split_version_id!),
  );
  const retry = () => {
    void checkpoint.refetch();
    void existing.refetch();
  };
  if (checkpoint.isPending || existing.isPending)
    return (
      <Loading text="Checking immutable checkpoint and existing selection…" />
    );
  if (checkpoint.error || existing.error)
    return (
      <ErrorState error={checkpoint.error || existing.error} retry={retry} />
    );
  const selected = existing.data?.data[0];
  if (selected)
    return (
      <>
        <p className="notice">
          This split already has a frozen selection. Choosing a different
          candidate requires new research with a new split; this selection
          cannot be overwritten.
        </p>
        <Link
          className="text-link mt-4"
          to="/portable/final-selections/$selectionId"
          params={{ selectionId: selected.id }}
        >
          Open frozen selection
        </Link>
      </>
    );
  if (!checkpoint.data)
    return <p className="notice">No verified checkpoint is available.</p>;
  return (
    <PortableFreezeForm
      key={`${owner}:${run.id}`}
      owner={owner}
      run={run}
      checkpoint={checkpoint.data}
    />
  );
}
function PortableFreezeForm({
  owner,
  run,
  checkpoint,
}: {
  owner: string;
  run: PortableRunDetail;
  checkpoint: CheckpointDetail;
}) {
  const navigate = useNavigate();
  const draft = useDurableAction({
    owner,
    scope: `portable-freeze:${run.id}`,
    schema: freezeSchema,
    initial: null,
  });
  const [confirmed, setConfirmed] = useState(false);
  const eligible = eligibleCandidates(checkpoint.summary);
  const selected = eligible.find(
    (item) => item.id === draft.body?.source_candidate_id,
  );
  const candidate = useQuery(
    portableCandidateOptions(owner, selected?.id || ""),
  );
  const exact = Boolean(
    portableCheckpointMatches(run, checkpoint) &&
    selected &&
    draft.body?.source_run_id === run.id &&
    draft.body.split_version_id === run.split_version_id &&
    draft.body.source_checkpoint_id === checkpoint.id,
  );
  if (!portableCheckpointMatches(run, checkpoint))
    return (
      <ErrorState
        error={
          new Error(
            "The published checkpoint does not match this successful portable research run. No candidate can be frozen.",
          )
        }
      />
    );
  if (!eligible.length)
    return (
      <p className="notice">
        No valid, complete candidate is recorded in the published checkpoint.
        Failed or partial candidates cannot be frozen.
      </p>
    );
  return (
    <div className="stack">
      <p className="notice">
        Freeze exactly one portable candidate, not necessarily the
        highest-scoring one. This pins its source code and original selection
        evidence. {exposureNotice}
      </p>
      <Field
        id="portable-freeze-candidate"
        label="Exact valid portable candidate"
        help="Only candidates marked valid and complete in the published checkpoint are offered; the server re-verifies their artifacts."
      >
        <select
          id="portable-freeze-candidate"
          value={selected?.id || ""}
          disabled={draft.busy}
          onChange={(event) => {
            setConfirmed(false);
            draft.edit(
              event.target.value
                ? {
                    split_version_id: run.split_version_id!,
                    source_run_id: run.id,
                    source_candidate_id: event.target.value,
                    source_checkpoint_id: checkpoint.id,
                  }
                : null,
            );
          }}
        >
          <option value="">Choose a candidate explicitly</option>
          {eligible.map((item) => (
            <option key={item.id} value={item.id}>
              {item.key} · selection score {item.score ?? "unavailable"}
            </option>
          ))}
        </select>
      </Field>
      {selected &&
        (candidate.isPending ? (
          <Loading text="Loading exact candidate source…" />
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
                  "Candidate ID": candidate.data.id,
                  "Candidate code SHA-256": candidate.data.code_sha256,
                  "Source checkpoint": checkpoint.id,
                  "Checkpoint SHA-256": checkpoint.manifest_sha256,
                }}
              />
              <details>
                <summary className="text-link">
                  Review exact candidate source
                </summary>
                <pre className="preview" tabIndex={0}>
                  {candidate.data.code}
                </pre>
              </details>
            </>
          )
        ))}
      <label className="flex items-start gap-3">
        <input
          type="checkbox"
          checked={confirmed}
          disabled={!exact || draft.busy}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>
          I reviewed this exact candidate and confirm freezing it permanently
          for this split. Freezing does not run the holdout.
        </span>
      </label>
      {draft.storageWarning && (
        <p role="alert" className="notice error">
          Tab storage is unavailable. Submission stays blocked until a durable
          retry key can be saved.
        </p>
      )}
      {draft.hasPending && (
        <p className="notice">
          A previous response is unconfirmed. Retry keeps the exact body and
          key; changing the candidate replaces the draft, not any selection the
          server already froze.
        </p>
      )}
      {Boolean(draft.error) && <ErrorState error={draft.error} />}
      <Button
        disabled={
          !confirmed ||
          !exact ||
          !candidate.data ||
          candidate.data.id !== selected?.id ||
          candidate.data.run_id !== run.id ||
          candidate.isFetching ||
          Boolean(candidate.error) ||
          draft.busy
        }
        onClick={() =>
          void draft.run(freezePortableSelection, (selection) => {
            void refreshPortableFinal(owner);
            void navigate({
              to: "/portable/final-selections/$selectionId",
              params: { selectionId: selection.id },
            });
          })
        }
      >
        {draft.busy
          ? "Freezing…"
          : draft.hasPending
            ? "Retry same freeze request"
            : "Freeze selected portable candidate"}
      </Button>
    </div>
  );
}
