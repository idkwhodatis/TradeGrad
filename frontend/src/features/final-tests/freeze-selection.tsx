import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type { CheckpointDetail, ResearchRunDetail } from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { Field, Metadata } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { currentUserOptions, queryClient } from "@/lib/api";
import { candidateOptions, checkpointOptions } from "@/lib/research-api";
import { finalSelectionsOptions, freezeFinalSelection } from "@/lib/final-api";
import { useDurableAction } from "./durable-action";
import {
  eligibleCandidates,
  exposureNotice,
  freezeSchema,
  freezeUnavailable,
} from "./protocol";

export function FreezeCandidatePanel({ run }: { run: ResearchRunDetail }) {
  const reason = freezeUnavailable(run);
  return (
    <Panel title="Freeze a final candidate">
      {reason ? (
        <>
          <p className="notice" data-testid="freeze-unavailable">
            {reason}
          </p>
          {(!run.experiment_id || !run.split_version_id) && (
            <Link className="text-link mt-4" to="/experiments/new">
              Create a new bound experiment
            </Link>
          )}
        </>
      ) : (
        <FreezeInputs key={run.id} run={run} />
      )}
    </Panel>
  );
}
function FreezeInputs({ run }: { run: ResearchRunDetail }) {
  const owner = useQuery(currentUserOptions);
  const checkpoint = useQuery(checkpointOptions(run.latest_checkpoint_id!));
  const existing = useQuery(finalSelectionsOptions(run.split_version_id!));
  const retry = () => {
    void owner.refetch();
    void checkpoint.refetch();
    void existing.refetch();
  };
  if (owner.isPending || checkpoint.isPending || existing.isPending)
    return (
      <Loading text="Checking immutable checkpoint and existing selection…" />
    );
  if (owner.error || checkpoint.error || existing.error)
    return (
      <ErrorState
        error={owner.error || checkpoint.error || existing.error}
        retry={retry}
      />
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
          to="/final-selections/$selectionId"
          params={{ selectionId: selected.id }}
        >
          Open frozen selection
        </Link>
      </>
    );
  if (!owner.data || !checkpoint.data)
    return <p className="notice">No verified checkpoint is available.</p>;
  return (
    <FreezeForm
      key={`${owner.data.id}:${run.id}`}
      owner={owner.data.id}
      run={run}
      checkpoint={checkpoint.data}
    />
  );
}
function FreezeForm({
  owner,
  run,
  checkpoint,
}: {
  owner: string;
  run: ResearchRunDetail;
  checkpoint: CheckpointDetail;
}) {
  const navigate = useNavigate();
  const draft = useDurableAction({
    owner,
    scope: `freeze:${run.id}`,
    schema: freezeSchema,
    initial: null,
  });
  const [confirmed, setConfirmed] = useState(false);
  const eligible = eligibleCandidates(checkpoint.summary);
  const selected = eligible.find(
    (item) => item.id === draft.body?.source_candidate_id,
  );
  const candidate = useQuery(candidateOptions(selected?.id || ""));
  const exact = Boolean(
    selected &&
    draft.body?.source_run_id === run.id &&
    draft.body.split_version_id === run.split_version_id &&
    draft.body.source_checkpoint_id === checkpoint.id,
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
        Freeze exactly one candidate, not necessarily the highest-scoring one.
        This pins its source code and original selection evidence.{" "}
        {exposureNotice}
      </p>
      <Field
        id="freeze-candidate"
        label="Exact valid candidate"
        help="Only candidates marked valid and complete in the published checkpoint are offered; the server re-verifies their artifacts."
      >
        <select
          id="freeze-candidate"
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
          candidate.isFetching ||
          Boolean(candidate.error) ||
          draft.busy
        }
        onClick={() =>
          void draft.run(freezeFinalSelection, (selection) => {
            void queryClient.invalidateQueries({
              queryKey: ["final-selections", run.split_version_id],
            });
            void navigate({
              to: "/final-selections/$selectionId",
              params: { selectionId: selection.id },
            });
          })
        }
      >
        {draft.busy
          ? "Freezing…"
          : draft.hasPending
            ? "Retry same freeze request"
            : "Freeze selected candidate"}
      </Button>
    </div>
  );
}
