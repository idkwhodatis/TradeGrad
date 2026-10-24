import type {
  DiagnosticParityEventPage,
  DiagnosticParitySide,
  DiagnosticParityView,
  IndependentEvaluationPublic,
} from "@/client/types.gen";
import type { IndependentSource } from "./verification-draft";
import type { WorkbenchSelection } from "./workbench-selection";

// Keep this list in contract tests aligned with the server's REQUIRED_CHECKS.
export const requiredDiagnosticChecks = [
  "source_identity",
  "fixed_conditions",
  "rng_reconstruction",
  "randomness_scope",
  "audit_coverage",
  "submitted_intents",
  "ordered_status_history",
  "fills",
  "position_trajectory",
  "account_trajectory",
  "result_metrics",
  "original_event_mapping",
  "decision_pre_states",
  "observed_targets",
] as const;
function sideMatches(source: IndependentSource, actual: DiagnosticParitySide) {
  return (
    actual.source_kind === source.source_kind &&
    actual.source_id === source.source_id &&
    actual.binding.evaluation_id === source.evaluation_id &&
    actual.binding.execution_attempt_id === source.execution_attempt_id &&
    actual.binding.result_manifest_sha256 === source.result_manifest_sha256 &&
    actual.binding.code_sha256 === source.code_sha256 &&
    actual.trace_manifest_sha256 === source.trace_manifest_sha256 &&
    actual.spec_revision === source.spec_revision &&
    actual.spec_sha256 === source.spec_sha256
  );
}
export function diagnosticParityProblem(
  source: IndependentSource,
  job: IndependentEvaluationPublic,
  selection: WorkbenchSelection,
  report: DiagnosticParityView,
): string | null {
  const replay = job.result_pin;
  if (
    !replay ||
    !sideMatches(source, report.source) ||
    report.source.binding.run_id !== job.config.source_run_id ||
    report.replay.source_kind !== "strategy_version" ||
    report.replay.source_id !== replay.source_id ||
    report.replay.binding.run_id !== job.id ||
    report.replay.binding.evaluation_id !== replay.evaluation_id ||
    report.replay.binding.execution_attempt_id !==
      replay.execution_attempt_id ||
    report.replay.binding.result_manifest_sha256 !==
      replay.result_manifest_sha256 ||
    report.replay.binding.code_sha256 !== replay.code_sha256 ||
    report.replay.trace_manifest_sha256 !== replay.trace_manifest_sha256 ||
    // Native pins bind the staged evaluation configuration, not the whole run wrapper.
    report.replay.binding.execution_config_sha256 !==
      replay.execution_config_sha256 ||
    (job.config.evaluation_mode === "portable" &&
      replay.execution_config_sha256 !== job.config_sha256)
  )
    return "The semantic report does not match both exact source and diagnostic physical leaves. It is not displayed as verified evidence.";
  if (
    selection.diagnostic_report_sha256 &&
    selection.diagnostic_report_sha256 !== report.report_sha256
  )
    return "The diagnostic report hash differs from the saved immutable reference. No replacement report was loaded.";
  if (
    report.source.binding.evaluation_id ===
      report.replay.binding.evaluation_id ||
    report.source.binding.execution_attempt_id ===
      report.replay.binding.execution_attempt_id
  )
    return "A diagnostic must be an independent execution, not the original physical result reused as a replay.";
  const checks = report.checks;
  if (
    checks.length !== requiredDiagnosticChecks.length ||
    requiredDiagnosticChecks.some(
      (key) => !checks.some((check) => check.key === key),
    ) ||
    new Set(checks.map((check) => check.key)).size !== checks.length
  )
    return "Named semantic checks are missing or duplicated. Equal returns alone cannot establish diagnostic parity.";
  const expected = checks.some((check) => check.status === "mismatch")
    ? "mismatch"
    : checks.some((check) => check.status === "not_verifiable")
      ? "not_verifiable"
      : "matched";
  if (
    expected !== report.outcome ||
    (report.outcome !== "matched" &&
      (report.mapping_counts.verified_original_event ?? 0) > 0)
  )
    return "Diagnostic certification disagrees with its named checks or event mapping. New observations were not promoted to original facts.";
  return null;
}
export function parityPageMatches(
  report: DiagnosticParityView,
  page: DiagnosticParityEventPage,
  offset: number,
) {
  return (
    page.report_sha256 === report.report_sha256 &&
    page.offset === offset &&
    page.limit === 25 &&
    page.items.length <= page.limit &&
    page.total ===
      Object.values(report.mapping_counts).reduce(
        (sum, count) => sum + count,
        0,
      ) &&
    page.items.every(
      (event) =>
        (event.mapping !== "verified_original_event" ||
          (report.outcome === "matched" &&
            Boolean(event.original_decision_id) &&
            Boolean(event.replay_decision_id) &&
            Boolean(event.original_pre_state_sha256) &&
            Boolean(event.replay_pre_state_sha256) &&
            event.pre_state_status === "matched" &&
            event.observation_status === "matched")) &&
        !(
          event.original_decision_id &&
          event.original_decision_id === event.replay_decision_id
        ),
    )
  );
}
export function parityReplayTarget(
  side: DiagnosticParitySide,
  decisionId: string | null,
) {
  if (!decisionId || !side.trace_manifest_sha256) return null;
  const search: WorkbenchSelection = {
    tab: "replay",
    candidate_id: side.source_kind === "candidate" ? side.source_id : undefined,
    code_sha256: side.binding.code_sha256,
    spec_revision: side.spec_revision ?? undefined,
    spec_sha256: side.spec_sha256 ?? undefined,
    evaluation_id: side.binding.evaluation_id,
    execution_attempt_id: side.binding.execution_attempt_id,
    result_manifest_sha256: side.binding.result_manifest_sha256,
    trace_manifest_sha256: side.trace_manifest_sha256,
    trace_status: "available",
    decision_id: decisionId,
  };
  return {
    to: "/strategies/$strategyId/research" as const,
    params: { strategyId: side.binding.strategy_version_id },
    search,
  };
}
export const diagnosticEvidenceClass = (
  outcome: DiagnosticParityView["outcome"],
) =>
  outcome === "matched"
    ? "Verified diagnostic observation within the declared scope"
    : outcome === "mismatch"
      ? "Unverified new observation: semantic mismatch retained"
      : "Unverified new observation: semantic parity not verifiable";
