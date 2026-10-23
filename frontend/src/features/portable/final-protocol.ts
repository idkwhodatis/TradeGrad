import type {
  CheckpointDetail,
  FinalSelectionPublic,
  PortableFinalEvaluationDetail,
  PortableFinalEvaluationPreflight,
  PortableFinalResultQuery,
  PortableRunDetail,
} from "@/client/types.gen";
import { asRecord, stableJson } from "@/lib/data-utils";
import { portableNotice, type PortableSection } from "./protocol";
export {
  freezeSchema,
  finalTestSchema,
  retrySchema,
  eligibleCandidates,
} from "@/features/final-tests/protocol";
export const portableFinalNotice = `${portableNotice} Final evaluation is a separate frozen holdout job: no optimizer, model calls, candidate selection feedback or clean-holdout certification.`;
export const portableFinalTerminal = new Set([
  "failed",
  "cancelled",
  "interrupted",
  "succeeded",
]);
export function portableFreezeUnavailable(run: PortableRunDetail) {
  if (!run.experiment_id || !run.split_version_id)
    return "This portable run has no immutable experiment/split binding. Create new bound portable research; historical runs cannot be retroactively certified.";
  if (
    run.config.schema_version !== "tradegrad.portable.run.v1" ||
    run.config.evaluation_mode !== "portable" ||
    run.execution_mode !== "optimization"
  )
    return "Only portable optimization can provide this frozen selection. Native or seed-only execution is not converted.";
  if (!run.config.windows.holdout)
    return "Declare a distinct metadata-only holdout before running new portable research.";
  if (run.status !== "succeeded")
    return "Finish portable research successfully before freezing a candidate.";
  if (run.attempts.some((attempt) => !attempt.sandbox_stopped))
    return "Wait until both execution environments are confirmed stopped.";
  if (!run.latest_checkpoint_id)
    return "No published complete checkpoint is available for freezing.";
  return null;
}
export function portableCheckpointMatches(
  run: PortableRunDetail,
  checkpoint: CheckpointDetail,
) {
  return (
    checkpoint.complete === true &&
    checkpoint.id === run.latest_checkpoint_id &&
    checkpoint.run_id === run.id &&
    checkpoint.generation <= run.generation
  );
}
export function portableSelectionMatches(selection: FinalSelectionPublic) {
  const source = asRecord(selection.selection_evidence.source_config);
  return (
    source.schema_version === "tradegrad.portable.run.v1" &&
    source.evaluation_mode === "portable" &&
    source.execution_mode === "optimization"
  );
}
export function portableFinalControls(
  run: Pick<PortableFinalEvaluationDetail, "status" | "attempts">,
) {
  const retryable = ["failed", "cancelled", "interrupted"].includes(run.status);
  const stopped = run.attempts.every((attempt) => attempt.sandbox_stopped);
  return {
    cancel:
      !portableFinalTerminal.has(run.status) &&
      run.status !== "cancel_requested",
    retry: retryable && stopped,
    retryReason:
      retryable && !stopped
        ? "Waiting for the server to confirm both previous execution environments have stopped."
        : retryable
          ? "Retry reruns the complete unchanged frozen holdout and records more exposure. It never resumes optimizer state."
          : null,
  };
}
export function portableFinalPreflightMatches(
  result: PortableFinalEvaluationPreflight,
  selection: FinalSelectionPublic,
) {
  const config = result.config;
  const source = asRecord(selection.selection_evidence.source_config);
  const windows = asRecord(source.windows);
  return (
    result.selection_id === selection.id &&
    config.selection_id === selection.id &&
    config.schema_version === "tradegrad.portable.final-evaluation.v1" &&
    config.evaluation_mode === "portable" &&
    config.execution_mode === "final_evaluation" &&
    config.split_version_id === selection.split_version_id &&
    config.selected_strategy.id === selection.strategy_version_id &&
    config.selected_strategy.sha256 === selection.source_code_sha256 &&
    config.source_config_sha256 === selection.source_config_sha256 &&
    stableJson(config.source_config) === stableJson(source) &&
    config.environment_digest === source.environment_digest &&
    stableJson(config.holdout) === stableJson(windows.holdout)
  );
}
export function acceptedPortableFinalReference(
  job: PortableFinalEvaluationDetail,
): string | null {
  // History is diagnostic, not a selectable accepted receipt. A changed
  // lifecycle must hide even an already cached artifact from an older attempt.
  if (
    job.status !== "succeeded" ||
    !job.result?.holdout_executed ||
    !job.result.evaluation_id ||
    job.result.selection_id !== job.final_selection_id ||
    !job.active_attempt_id ||
    !job.attempts.every((attempt) => attempt.sandbox_stopped)
  )
    return null;
  const attempt = job.attempts.find(
    (item) => item.id === job.active_attempt_id,
  );
  return attempt?.generation === job.generation &&
    attempt.status === "succeeded" &&
    Boolean(attempt.finished_at) &&
    attempt.exit_code === 0
    ? job.result.evaluation_id
    : null;
}
export function portableFinalResultMatches(
  result: PortableFinalResultQuery,
  job: PortableFinalEvaluationDetail,
  section: PortableSection,
  offset: number,
  limit: number,
) {
  return (
    acceptedPortableFinalReference(job) !== null &&
    result.evaluation_id === acceptedPortableFinalReference(job) &&
    result.run_id === job.id &&
    result.selection_id === job.final_selection_id &&
    result.schema_version === "tradegrad.portable.final-result-query.v1" &&
    result.result_schema_version === "tradegrad.portable-result.v1" &&
    result.evaluation_mode === "portable" &&
    result.result_trust === "isolated_policy_trusted_akquant_engine" &&
    result.ledger_origin === "trusted_engine" &&
    result.runtime_certified === false &&
    result.external_framework_equivalence === "not_certified" &&
    result.selection_feedback === false &&
    /^sha256:[a-f0-9]{64}$/.test(result.image_digest) &&
    /^sha256:[a-f0-9]{64}$/.test(result.policy_image_digest) &&
    result.image_digest === job.research_image_digest &&
    result.policy_image_digest === job.portable_policy_image_digest &&
    result.identity.strategy_sha256 === job.config.selected_strategy.sha256 &&
    result.identity.parameters_sha256 ===
      job.config.selected_parameters.sha256 &&
    result.environment_digest === job.config.environment_digest &&
    stableJson(result.config) === stableJson(job.config) &&
    stableJson(result.window) ===
      stableJson({ ...job.config.holdout, interval: "half_open" }) &&
    /^[a-f0-9]{64}$/.test(result.artifact_sha256) &&
    result.section === section &&
    result.offset === offset &&
    result.limit === limit &&
    Number.isSafeInteger(result.count) &&
    result.count >= 0 &&
    result.rows.length <= limit
  );
}
