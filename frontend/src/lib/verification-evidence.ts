import type { WorkbenchEvaluation, WorkbenchSpec } from "@/client/types.gen";
import { sideFromEvidence, sideRequest } from "./comparison-evidence";
import { stableJson } from "./data-utils";
import { leafBindingProblem } from "./workbench-evidence";
import type { WorkbenchSelection } from "./workbench-selection";
import {
  independentSourceSchema,
  type IndependentSource,
} from "./verification-draft";

export const independentTerminal = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "paused",
]);
export const independentEvidenceNotice =
  "Original execution evidence remains immutable. A new diagnostic is unverified observation until every required semantic check passes. Equal returns alone never establish parity, and native shared-process observations are not an independent ledger or no-lookahead proof.";
export function verificationSource(
  selection: WorkbenchSelection,
  spec: WorkbenchSpec,
  leaf?: WorkbenchEvaluation,
  verified = false,
): IndependentSource {
  if (
    !verified ||
    !leaf ||
    leaf.status !== "succeeded" ||
    !leaf.result_manifest_sha256
  )
    throw new Error(
      "Select an admitted successful physical leaf Evaluation before independent verification.",
    );
  const problem = leafBindingProblem(selection, spec.strategy_version_id, leaf);
  if (problem) throw new Error(problem);
  if (
    selection.evaluation_id !== leaf.evaluation_id ||
    selection.spec_revision !== spec.revision ||
    selection.spec_sha256 !== spec.spec_sha256 ||
    selection.code_sha256 !== spec.code_sha256 ||
    selection.execution_attempt_id !== leaf.execution_attempt_id ||
    selection.result_manifest_sha256 !== leaf.result_manifest_sha256 ||
    (selection.trace_manifest_sha256 ?? null) !== leaf.trace_manifest_sha256
  )
    throw new Error(
      "Verification source, description and physical evidence pins must all match exactly. No latest source was substituted.",
    );
  return independentSourceSchema.parse(
    sideRequest(sideFromEvidence(spec, leaf)),
  );
}
export function sameIndependentSource(left: unknown, right: unknown) {
  const a = independentSourceSchema.safeParse(left),
    b = independentSourceSchema.safeParse(right);
  return a.success && b.success && stableJson(a.data) === stableJson(b.data);
}
/** Parameter-only claims are deliberately conservative; code changes are not causal proof. */
export function previewArmChanges(
  originalCode: string,
  original: Record<string, unknown> | null,
  code: string,
  parameters: Record<string, unknown> | null,
) {
  const before = original ?? {},
    after = parameters ?? {};
  const fields = [...new Set([...Object.keys(before), ...Object.keys(after)])]
    .sort()
    .filter((key) => stableJson(before[key]) !== stableJson(after[key]))
    .map((key) => `parameters.${key}`);
  if (originalCode !== code) fields.unshift("code");
  return {
    fields,
    interpretation:
      fields.length === 0
        ? "unchanged"
        : fields.length === 1 &&
            fields[0] !== "code" &&
            Object.keys(before).length === Object.keys(after).length &&
            Object.keys(before).every((key) => Object.hasOwn(after, key))
          ? "single_parameter_change_not_causal_proof"
          : "not_individually_validated",
  };
}
export function fieldConditions(
  value: Record<string, unknown>,
  prefix = "",
): { field: string; value: unknown }[] {
  return Object.entries(value)
    .sort(([a], [b]) => a.localeCompare(b))
    .flatMap(([key, item]) => {
      const field = prefix ? `${prefix}.${key}` : key;
      return item !== null &&
        typeof item === "object" &&
        !Array.isArray(item) &&
        Object.keys(item).length > 0
        ? fieldConditions(item as Record<string, unknown>, field)
        : [{ field, value: item }];
    });
}

export function independentStatusProblem(
  source: IndependentSource,
  status: import("@/client/types.gen").IndependentEvaluationPublic,
  expected: {
    id: string;
    purpose: "diagnostic" | "controlled";
    config_sha256?: string;
    result_pin?: import("@/client/types.gen").IndependentResultPin | null;
  },
) {
  if (
    status.id !== expected.id ||
    status.purpose !== expected.purpose ||
    !sameIndependentSource(source, status.source) ||
    !sameIndependentSource(source, status.config.source) ||
    status.config.independent_id !== status.independent_id ||
    status.config.purpose !== status.purpose ||
    status.id === status.config.source_run_id
  )
    return "The independent job does not belong to this exact original source and purpose. No result was substituted.";
  if (expected.config_sha256 && expected.config_sha256 !== status.config_sha256)
    return "The diagnostic configuration hash differs from the saved reference. No newer job was substituted.";
  if (
    expected.result_pin &&
    stableJson(expected.result_pin) !== stableJson(status.result_pin)
  )
    return "The saved independent physical result changed. It was not retargeted to a later attempt.";
  if (
    status.result_pin &&
    (status.status !== "succeeded" ||
      !status.result_ready ||
      status.result_pin.evaluation_id !== status.evaluation_id ||
      status.result_pin.execution_attempt_id !== status.active_attempt_id ||
      status.result_pin.source_id !== status.config.selected_strategy.id ||
      status.result_pin.code_sha256 !==
        status.config.selected_strategy.sha256 ||
      status.result_pin.evaluation_id === source.evaluation_id ||
      status.result_pin.execution_attempt_id === source.execution_attempt_id ||
      (status.config.evaluation_mode === "portable" &&
        status.result_pin.execution_config_sha256 !== status.config_sha256))
  )
    return "The independent result publication disagrees with its durable physical execution and selected source.";
  if (
    status.purpose === "diagnostic" &&
    status.config.selected_strategy.sha256 !== source.code_sha256
  )
    return "Diagnostic source code changed. This cannot be presented as a replay of the original source.";
  return null;
}
export function diagnosticResultPins(
  selection: WorkbenchSelection,
  result: import("@/client/types.gen").IndependentResultPin | null,
): string | null {
  for (const field of [
    "evaluation_id",
    "execution_attempt_id",
    "result_manifest_sha256",
    "trace_manifest_sha256",
  ] as const) {
    const expected = selection[`diagnostic_${field}`];
    if (expected !== undefined && expected !== result?.[field])
      return "The selected diagnostic leaf or artifact differs from the saved immutable pins. No newer attempt was substituted.";
  }
  return null;
}
export function controlledStatusProblem(
  source: IndependentSource,
  selection: WorkbenchSelection,
  value: import("@/client/types.gen").ControlledExperimentPublic,
): string | null {
  if (
    selection.controlled_experiment_id !== value.id ||
    !sameIndependentSource(source, value.source) ||
    !sameIndependentSource(source, value.request.source)
  )
    return "The controlled experiment belongs to another immutable source. No experiment was substituted.";
  if (
    (selection.controlled_request_sha256 &&
      selection.controlled_request_sha256 !== value.request_sha256) ||
    (selection.controlled_protocol_sha256 &&
      selection.controlled_protocol_sha256 !== value.protocol_sha256)
  )
    return "The controlled request or protocol hash differs from the saved reference.";
  if (
    value.arms.length < 2 ||
    value.arms.length > 3 ||
    value.arms.some((arm, index) => arm.ordinal !== index) ||
    new Set(value.arms.map((arm) => arm.run_id)).size !== value.arms.length ||
    new Set(value.arms.map((arm) => arm.strategy_version_id)).size !==
      value.arms.length ||
    value.arms[0].code_sha256 !== source.code_sha256 ||
    value.arms[0].interpretation !== "baseline"
  )
    return "Controlled arms are not the exact ordered copied baseline plus one or two immutable variants.";
  if (selection.controlled_arm !== undefined) {
    const arm = value.arms[selection.controlled_arm];
    if (!arm || selection.controlled_arm_run_id !== arm.run_id)
      return "The selected controlled ordinal does not match its immutable run.";
    for (const field of [
      "evaluation_id",
      "execution_attempt_id",
      "result_manifest_sha256",
      "trace_manifest_sha256",
    ] as const) {
      const expected = selection[`controlled_arm_${field}`];
      if (expected !== undefined && expected !== arm.result_pin?.[field])
        return "The selected controlled result differs from the saved physical leaf. No later attempt was substituted.";
    }
  }
  return null;
}
