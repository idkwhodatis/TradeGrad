import { z } from "zod";
import type {
  FinalEvaluationDetail,
  ResearchRunDetail,
} from "@/client/types.gen";
import { asRecord } from "@/lib/data-utils";

export const freezeSchema = z.strictObject({
  split_version_id: z.uuid(),
  source_run_id: z.uuid(),
  source_candidate_id: z.uuid(),
  source_checkpoint_id: z.uuid(),
});
export const finalTestSchema = z.strictObject({
  selection_id: z.uuid(),
  max_runtime_seconds: z.number().int().min(1).max(3600),
});
export const retrySchema = z.strictObject({
  final_evaluation_id: z.uuid(),
  generation: z.number().int().min(0),
});
export const packageSchema = z.strictObject({
  final_selection_id: z.uuid(),
  reference_evaluation_id: z.uuid(),
  profile: z.literal("reference_only.v1"),
});
export const finalTerminal = new Set([
  "failed",
  "cancelled",
  "interrupted",
  "succeeded",
]);
export const finalTrustNotice =
  "Native · UNTRUSTED. Strategy and engine share a process; these holdout observations are not an independently trusted ledger or evidence of portable external-framework equivalence. No live trading is performed.";
export const exposureNotice =
  "Historical tracking incomplete/external use unknown. No tracked overlap does not establish an independent or previously unseen holdout. Recorded exposures are a lower bound, not a complete history.";
export function freezeUnavailable(
  run: Pick<
    ResearchRunDetail,
    | "status"
    | "execution_mode"
    | "experiment_id"
    | "split_version_id"
    | "latest_checkpoint_id"
  >,
) {
  if (!run.experiment_id || !run.split_version_id)
    return "This historical research run has no immutable experiment/split binding. Recreate research with a bound split before freezing; an old run cannot be retroactively certified.";
  if (run.execution_mode !== "optimization")
    return "Only a native optimization run can provide a final selection.";
  if (run.status !== "succeeded")
    return "Finish research successfully before freezing a final candidate.";
  if (!run.latest_checkpoint_id)
    return "No published complete checkpoint is available for freezing.";
  return null;
}
export function eligibleCandidates(summary: Record<string, unknown>) {
  return Object.entries(asRecord(summary.candidates)).flatMap(
    ([key, value]) => {
      const row = asRecord(value);
      return row.valid === true &&
        row.complete === true &&
        z.uuid().safeParse(row.id).success
        ? [
            {
              key,
              id: row.id as string,
              score:
                typeof row.combined_score === "number" &&
                Number.isFinite(row.combined_score)
                  ? row.combined_score
                  : null,
            },
          ]
        : [];
    },
  );
}
export function finalControls(
  run: Pick<FinalEvaluationDetail, "status" | "attempts">,
) {
  const stopped = run.attempts.every((attempt) => attempt.sandbox_stopped);
  const retryable = ["failed", "cancelled", "interrupted"].includes(run.status);
  return {
    cancel: !finalTerminal.has(run.status) && run.status !== "cancel_requested",
    retry: retryable && stopped,
    retryReason:
      retryable && !stopped
        ? "Waiting for the server to confirm every previous execution sandbox has stopped."
        : retryable
          ? "Retry uses the same frozen inputs and reruns the complete holdout. It does not resume optimizer state."
          : null,
  };
}

export function acceptedFinalReference(
  job: Pick<
    FinalEvaluationDetail,
    | "status"
    | "result"
    | "final_selection_id"
    | "active_attempt_id"
    | "generation"
    | "attempts"
  >,
): string | null {
  // Physical evaluation history is diagnostic, never a selectable reference.
  // A newer lifecycle response must unmount even an already cached artifact.
  if (
    job.status !== "succeeded" ||
    !job.result?.holdout_executed ||
    !job.result.evaluation_id ||
    job.result.selection_id !== job.final_selection_id ||
    !job.active_attempt_id
  )
    return null;
  const attempt = job.attempts.find(
    (item) => item.id === job.active_attempt_id,
  );
  return attempt?.generation === job.generation &&
    attempt.status === "succeeded" &&
    attempt.sandbox_stopped &&
    Boolean(attempt.finished_at) &&
    attempt.exit_code === 0
    ? job.result.evaluation_id
    : null;
}
