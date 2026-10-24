import type {
  DecisionDetail,
  Rule,
  TraceContext,
  WorkbenchEvaluation,
} from "@/client/types.gen";
import {
  clearedReplaySelection,
  type WorkbenchSelection,
} from "./workbench-selection";

export function evidenceMatches(
  selection: WorkbenchSelection,
  context: TraceContext,
): boolean {
  return (
    selection.evaluation_id === context.evaluation_id &&
    selection.execution_attempt_id === context.execution_attempt_id &&
    selection.result_manifest_sha256 === context.result_manifest_sha256 &&
    (!selection.trace_manifest_sha256 ||
      selection.trace_manifest_sha256 === context.trace_manifest_sha256) &&
    (!selection.code_sha256 ||
      (context.binding
        ? selection.code_sha256 === context.binding.code_sha256
        : context.status === "not_collected" &&
          context.trace_manifest_sha256 === null))
  );
}
/** Never infer a branch outcome from a template, absent trade, or missing stage. */
export function ruleObservation(rule: Rule, detail?: DecisionDetail) {
  if (!detail?.decision || !rule.stage_id || detail.stage_id !== rule.stage_id)
    return { status: "No selected recorded stage", stage: null };
  const stage = detail.stages?.find(
    (value) => value.stage_id === rule.stage_id,
  );
  if (!stage)
    return {
      status: "Stage not recorded; branch outcome unknown",
      stage: null,
    };
  return {
    status: `Recorded stage · ${stage.coverage}${stage.missing_reason ? ` · ${stage.missing_reason}` : ""}`,
    stage,
  };
}
export function recordedValue(value: unknown): string {
  if (value === null || value === undefined) return "Not recorded";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function leafSelection(
  selection: WorkbenchSelection,
  leaf: WorkbenchEvaluation,
): WorkbenchSelection {
  return {
    ...selection,
    code_sha256: leaf.code_sha256,
    evaluation_id: leaf.evaluation_id,
    execution_attempt_id: leaf.execution_attempt_id,
    result_manifest_sha256: leaf.result_manifest_sha256 ?? undefined,
    trace_manifest_sha256: leaf.trace_manifest_sha256 ?? undefined,
    trace_status: leaf.trace_manifest_sha256 ? "available" : "not_collected",
    ...clearedReplaySelection(),
  };
}
export function leafBindingProblem(
  selection: WorkbenchSelection,
  strategyId: string,
  leaf?: WorkbenchEvaluation,
): string | null {
  if (!leaf) return null;
  const association = leaf.source_association;
  if (
    !association ||
    (selection.candidate_id
      ? association.candidate_id !== selection.candidate_id ||
        association.candidate_basis === "unavailable"
      : association.strategy_version_id !== strategyId ||
        association.strategy_basis === "unavailable") ||
    (selection.code_sha256 && leaf.code_sha256 !== selection.code_sha256)
  )
    return "This leaf Evaluation does not belong to the selected exact source. No seed or candidate was substituted.";
  if (
    (selection.execution_attempt_id &&
      leaf.execution_attempt_id !== selection.execution_attempt_id) ||
    (selection.result_manifest_sha256 &&
      leaf.result_manifest_sha256 !== selection.result_manifest_sha256) ||
    (selection.trace_manifest_sha256 &&
      leaf.trace_manifest_sha256 !== selection.trace_manifest_sha256)
  )
    return "The saved execution, result or trace pin no longer matches this leaf. The reference was not silently retargeted.";
  return null;
}

export function workbenchRunTarget(leaf: WorkbenchEvaluation) {
  if (leaf.run_kind === "native_final")
    return leaf.final_test_id
      ? {
          to: "/final-tests/$finalTestId" as const,
          params: { finalTestId: leaf.final_test_id },
        }
      : null;
  if (leaf.run_kind === "portable_final")
    return leaf.final_test_id
      ? {
          to: "/portable/final-tests/$finalTestId" as const,
          params: { finalTestId: leaf.final_test_id },
        }
      : null;
  if (leaf.run_kind === "portable_research")
    return {
      to: "/portable/runs/$runId" as const,
      params: { runId: leaf.run_id },
    };
  if (leaf.run_kind === "native_research")
    return {
      to: "/research-runs/$runId" as const,
      params: { runId: leaf.run_id },
    };
  return { to: "/runs/$runId" as const, params: { runId: leaf.run_id } };
}
