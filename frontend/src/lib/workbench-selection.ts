/** URL state is the research evidence identity, not merely display preferences. */
export const workbenchTabs = [
  "description",
  "replay",
  "comparison",
  "verification",
  "notes",
] as const;
export type WorkbenchTab = (typeof workbenchTabs)[number];
export type WorkbenchSelection = {
  tab?: WorkbenchTab;
  candidate_id?: string;
  code_sha256?: string;
  evaluation_id?: string;
  execution_attempt_id?: string;
  result_manifest_sha256?: string;
  trace_manifest_sha256?: string;
  trace_status?: "available" | "not_collected";
  decision_id?: string;
  instrument_id?: string;
  stage_id?: string;
  rule_id?: string;
  spec_revision?: number;
  spec_sha256?: string;
  left_evaluation_id?: string;
  right_evaluation_id?: string;
  comparison_id?: string;
  event_pair_id?: string;
};
const textKeys = [
  "candidate_id",
  "code_sha256",
  "evaluation_id",
  "execution_attempt_id",
  "result_manifest_sha256",
  "trace_manifest_sha256",
  "decision_id",
  "instrument_id",
  "stage_id",
  "rule_id",
  "spec_sha256",
  "left_evaluation_id",
  "right_evaluation_id",
  "comparison_id",
  "event_pair_id",
] as const;
/** Reject malformed pins rather than discarding them and loading newer evidence. */
export function workbenchSearch(
  search: Record<string, unknown>,
): WorkbenchSelection {
  const result: WorkbenchSelection = {};
  const input: Record<string, unknown> = {
    ...search,
    evaluation_id: search.evaluation_id ?? search.eval_id,
  };
  for (const key of textKeys) {
    const value = input[key];
    if (value === undefined) continue;
    if (typeof value !== "string" || !value || value.length > 500)
      throw new Error(
        `Invalid research selection: ${key}. The evidence was not retargeted.`,
      );
    result[key] = value;
  }
  if (search.trace_status !== undefined) {
    if (
      search.trace_status !== "available" &&
      search.trace_status !== "not_collected"
    )
      throw new Error("Invalid trace selection status.");
    result.trace_status = search.trace_status;
  }
  if (search.tab !== undefined) {
    if (!workbenchTabs.includes(search.tab as WorkbenchTab))
      throw new Error(
        "Unknown research tab. The saved evidence selection was not changed.",
      );
    result.tab = search.tab as WorkbenchTab;
  }
  if (search.spec_revision !== undefined) {
    const value = Number(search.spec_revision);
    if (!Number.isSafeInteger(value) || value < 1)
      throw new Error(
        "Invalid description revision. The saved revision was not replaced.",
      );
    result.spec_revision = value;
  }
  for (const key of textKeys.filter((key) => key.endsWith("sha256"))) {
    if (result[key] && !/^[a-f0-9]{64}$/.test(result[key]!))
      throw new Error(
        `Invalid research hash: ${key}. The saved evidence was not replaced.`,
      );
  }
  return result;
}
export function selectionProblem(value: WorkbenchSelection): string | null {
  if (Boolean(value.spec_revision) !== Boolean(value.spec_sha256))
    return "A saved description needs both its revision and hash. No latest revision was substituted.";
  if (value.trace_status === "available" && !value.trace_manifest_sha256)
    return "An available trace selection needs its exact trace hash. No newer trace was substituted.";
  if (value.trace_status === "not_collected" && value.trace_manifest_sha256)
    return "A missing trace selection cannot include a trace hash. No replacement was loaded.";
  const partialPins = Boolean(
    value.result_manifest_sha256 || value.trace_manifest_sha256,
  );
  if (
    (value.execution_attempt_id && !value.evaluation_id) ||
    (partialPins &&
      !(
        value.evaluation_id &&
        value.execution_attempt_id &&
        value.result_manifest_sha256
      ))
  )
    return "Incomplete execution pins. Evaluation, execution attempt and result hash must be supplied together; no replacement was loaded.";
  if ((value.decision_id || value.instrument_id) && !value.evaluation_id)
    return "Choose an actual leaf Evaluation before opening decision evidence.";
  return null;
}
export function resourceSelection(
  value: WorkbenchSelection,
  candidateId?: string,
): WorkbenchSelection {
  return { tab: value.tab, candidate_id: candidateId };
}
export function tracePins(value: WorkbenchSelection) {
  return {
    execution_attempt_id: value.execution_attempt_id,
    result_manifest_sha256: value.result_manifest_sha256,
    trace_manifest_sha256: value.trace_manifest_sha256,
  };
}
/** A tab or rule change must retain every immutable identity and observation. */
export function changeSelection(
  value: WorkbenchSelection,
  patch: Partial<WorkbenchSelection>,
): WorkbenchSelection {
  return { ...value, ...patch };
}
