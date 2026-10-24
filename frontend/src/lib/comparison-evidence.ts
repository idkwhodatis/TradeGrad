import type {
  ComparisonCreate,
  ComparisonPublic,
  ComparisonResult,
  ComparisonSideCreate,
  ComparisonSidePins,
  ComparisonRecord,
  WorkbenchEvaluation,
  WorkbenchSpec,
} from "@/client/types.gen";
import { stableJson } from "./data-utils";
import { leafBindingProblem } from "./workbench-evidence";
import {
  comparisonSidePatch,
  comparisonSideSelection,
  type ComparisonSide,
  type ComparisonSideSelection,
  type WorkbenchSelection,
} from "./workbench-selection";

export const sideTitle = (side: ComparisonSide) =>
  side === "left" ? "Left" : "Right";
export const comparisonCaution =
  "Both decision pre-states must be read before interpreting differences. Earlier paths can change cash, holdings, indicators and pending orders. This compares complete frozen evaluations, not a local counterfactual, causal validation or diagnostic parity certification.";
export const comparisonTerminal = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "paused",
]);
export function sideFromEvidence(
  spec: WorkbenchSpec,
  leaf?: WorkbenchEvaluation,
): ComparisonSideSelection {
  const pins: ComparisonSideSelection = {
    source_kind: spec.resource_kind,
    source_id: spec.resource_id,
    strategy_version_id: spec.strategy_version_id,
    code_sha256: spec.code_sha256,
    spec_revision: spec.revision,
    spec_sha256: spec.spec_sha256,
  };
  if (leaf) {
    const problem = comparisonLeafProblem(pins, leaf);
    if (problem) throw new Error(problem);
    Object.assign(pins, {
      evaluation_id: leaf.evaluation_id,
      execution_attempt_id: leaf.execution_attempt_id,
      result_manifest_sha256: leaf.result_manifest_sha256 ?? undefined,
      trace_manifest_sha256: leaf.trace_manifest_sha256 ?? undefined,
      trace_status: leaf.trace_manifest_sha256
        ? "registered"
        : "historical_absence",
    });
  }
  return pins;
}
export function comparisonLeafProblem(
  pins: ComparisonSideSelection,
  leaf: WorkbenchEvaluation,
): string | null {
  if (!pins.source_kind || !pins.source_id || !pins.strategy_version_id)
    return "Choose and verify an owned source before selecting its leaf.";
  return (
    leafBindingProblem(
      {
        candidate_id:
          pins.source_kind === "candidate" ? pins.source_id : undefined,
        code_sha256: pins.code_sha256,
        evaluation_id: pins.evaluation_id,
        execution_attempt_id: pins.execution_attempt_id,
        result_manifest_sha256: pins.result_manifest_sha256,
        trace_manifest_sha256: pins.trace_manifest_sha256,
      },
      pins.strategy_version_id,
      leaf,
    ) ||
    (pins.trace_status === "historical_absence" &&
    leaf.trace_manifest_sha256 !== null
      ? "Historical absence was pinned, but this leaf has a registered trace. It was not silently replaced."
      : pins.evaluation_id && pins.evaluation_id !== leaf.evaluation_id
        ? "The returned leaf differs from the selected Evaluation."
        : null)
  );
}
export function sideSelectionProblem(
  pins: ComparisonSideSelection,
): string | null {
  if (Boolean(pins.source_kind) !== Boolean(pins.source_id))
    return "A comparison source requires both its resource kind and exact ID.";
  if (Boolean(pins.spec_revision) !== Boolean(pins.spec_sha256))
    return "Both description revision and hash must remain pinned.";
  if (pins.trace_status === "historical_absence" && pins.trace_manifest_sha256)
    return "Historical absence cannot have a registered trace hash.";
  if (pins.trace_status === "registered" && !pins.trace_manifest_sha256)
    return "A registered trace needs its exact hash, including capture-off traces.";
  if (
    (pins.execution_attempt_id ||
      pins.result_manifest_sha256 ||
      pins.trace_manifest_sha256) &&
    !(
      pins.evaluation_id &&
      pins.execution_attempt_id &&
      pins.result_manifest_sha256
    )
  )
    return "Incomplete leaf, execution attempt or result pins; no replacement is loaded.";
  return null;
}
export function sideRequest(
  pins: ComparisonSideSelection,
): ComparisonSideCreate {
  const problem = sideSelectionProblem(pins);
  if (problem) throw new Error(problem);
  if (
    !pins.source_kind ||
    !pins.source_id ||
    !pins.strategy_version_id ||
    !pins.code_sha256 ||
    !pins.spec_revision ||
    !pins.spec_sha256 ||
    !pins.evaluation_id ||
    !pins.execution_attempt_id ||
    !pins.result_manifest_sha256 ||
    !pins.trace_status
  )
    throw new Error(
      "Explicitly select both owned sources, exact descriptions and successful leaf Evaluations before comparing.",
    );
  return {
    source_kind: pins.source_kind,
    source_id: pins.source_id,
    code_sha256: pins.code_sha256,
    spec_revision: pins.spec_revision,
    spec_sha256: pins.spec_sha256,
    evaluation_id: pins.evaluation_id,
    execution_attempt_id: pins.execution_attempt_id,
    result_manifest_sha256: pins.result_manifest_sha256,
    trace_manifest_sha256: pins.trace_manifest_sha256 ?? null,
  };
}
export function comparisonRequest(
  selection: WorkbenchSelection,
): ComparisonCreate {
  return {
    left: sideRequest(comparisonSideSelection(selection, "left")),
    right: sideRequest(comparisonSideSelection(selection, "right")),
    options: { max_runtime_seconds: 300 },
  };
}
export function comparisonFingerprint(body: ComparisonCreate) {
  return stableJson(body);
}
export function comparisonStatusProblem(
  selection: WorkbenchSelection,
  status: ComparisonPublic,
): string | null {
  if (status.id !== selection.comparison_id)
    return "The comparison status belongs to another job. No result was substituted.";
  try {
    if (
      comparisonFingerprint(comparisonRequest(selection)) !==
      comparisonFingerprint(status.request)
    )
      return "The comparison job does not match the ordered immutable source selections. Restore its exact pins or explicitly create a new comparison.";
  } catch (error) {
    return (error as Error).message;
  }
  if (
    selection.comparison_manifest_sha256 &&
    status.manifest_sha256 !== selection.comparison_manifest_sha256
  )
    return "The saved comparison manifest is no longer the published artifact. The old reference was not retargeted.";
  return null;
}
function sideMatches(
  request: ComparisonSideCreate,
  actual: ComparisonSidePins,
): boolean {
  return (
    request.source_kind === actual.source_kind &&
    request.source_id === actual.source_id &&
    request.code_sha256 === actual.binding.code_sha256 &&
    request.evaluation_id === actual.binding.evaluation_id &&
    request.execution_attempt_id === actual.binding.execution_attempt_id &&
    request.result_manifest_sha256 === actual.binding.result_manifest_sha256 &&
    request.trace_manifest_sha256 === actual.trace_manifest_sha256 &&
    (request.spec_revision ?? null) === actual.spec_revision &&
    (request.spec_sha256 ?? null) === actual.spec_sha256
  );
}
export function comparisonResultProblem(
  selection: WorkbenchSelection,
  status: ComparisonPublic,
  result: ComparisonResult,
): string | null {
  const statusProblem = comparisonStatusProblem(selection, status);
  if (statusProblem) return statusProblem;
  if (
    result.comparison_id !== status.id ||
    result.input_sha256 !== status.input_sha256 ||
    result.manifest_sha256 !== status.manifest_sha256 ||
    result.manifest_artifact_id !== status.manifest_artifact_id ||
    result.manifest.publication.comparison_id !== status.id ||
    result.manifest.publication.run_id !== status.run_id ||
    result.manifest.publication.execution_attempt_id !==
      status.active_attempt_id ||
    result.manifest.input_hash !== status.engine_input_sha256 ||
    result.manifest.algorithm_version !== status.algorithm_version
  )
    return "Comparison response identity differs from the pinned job and artifact. It is not displayed.";
  for (const side of ["left", "right"] as const) {
    const actual = result.manifest[side];
    const source = result[`${side}_source`];
    const pins = comparisonSideSelection(selection, side);
    if (
      !sideMatches(status.request[side], actual) ||
      source.source_kind !== actual.source_kind ||
      source.source_id !== actual.source_id ||
      source.code_sha256 !== actual.binding.code_sha256 ||
      source.spec_sha256 !== actual.spec_sha256 ||
      source.source_strategy_version_id !== pins.strategy_version_id ||
      (actual.spec_revision !== null && !source.spec) ||
      (source.spec &&
        (source.spec.code_sha256 !== source.code_sha256 ||
          source.spec.revision !== actual.spec_revision))
    )
      return `${side} source, description or execution differs from the immutable comparison selection.`;
  }
  return null;
}
export function selectionFromRequest(
  status: ComparisonPublic,
  strategyIds: { left: string; right: string },
): Partial<WorkbenchSelection> {
  return {
    ...comparisonSidePatch("left", {
      ...status.request.left,
      strategy_version_id: strategyIds.left,
      spec_revision: status.request.left.spec_revision ?? undefined,
      spec_sha256: status.request.left.spec_sha256 ?? undefined,
      trace_manifest_sha256:
        status.request.left.trace_manifest_sha256 ?? undefined,
      trace_status: status.request.left.trace_manifest_sha256
        ? "registered"
        : "historical_absence",
    }),
    ...comparisonSidePatch("right", {
      ...status.request.right,
      strategy_version_id: strategyIds.right,
      spec_revision: status.request.right.spec_revision ?? undefined,
      spec_sha256: status.request.right.spec_sha256 ?? undefined,
      trace_manifest_sha256:
        status.request.right.trace_manifest_sha256 ?? undefined,
      trace_status: status.request.right.trace_manifest_sha256
        ? "registered"
        : "historical_absence",
    }),
  };
}
export function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}
export function comparisonReplayTarget(
  result: ComparisonResult,
  side: ComparisonSide,
  event: ComparisonRecord,
  observation?: {
    record_kind: WorkbenchSelection["record_kind"];
    record_id: string;
    record_ordinal?: number;
    stage_id?: string;
    instrument_id?: string;
  },
) {
  const item = event[side];
  if (event.section !== "events" || typeof item?.decision_id !== "string")
    return null;
  return immutableReplayTarget(
    result,
    side,
    item.decision_id,
    observation,
    typeof item.session_date === "string" ? item.session_date : undefined,
  );
}
export function immutableReplayTarget(
  result: ComparisonResult,
  side: ComparisonSide,
  decisionId: string,
  observation?: {
    record_kind: WorkbenchSelection["record_kind"];
    record_id: string;
    record_ordinal?: number;
    stage_id?: string;
    instrument_id?: string;
  },
  sessionDate?: string,
) {
  const pins = result.manifest[side];
  if (pins.trace_state !== "registered" || !pins.trace_manifest_sha256)
    return null;
  const source = result[`${side}_source`];
  const selection: WorkbenchSelection = {
    tab: "replay",
    candidate_id: pins.source_kind === "candidate" ? pins.source_id : undefined,
    code_sha256: pins.binding.code_sha256,
    evaluation_id: pins.binding.evaluation_id,
    execution_attempt_id: pins.binding.execution_attempt_id,
    result_manifest_sha256: pins.binding.result_manifest_sha256,
    trace_manifest_sha256: pins.trace_manifest_sha256 ?? undefined,
    trace_status: "available",
    spec_revision: pins.spec_revision ?? undefined,
    spec_sha256: pins.spec_sha256 ?? undefined,
    decision_id: decisionId,
    // Event IDs are authoritative. Dates only narrow the event list display.
    session_date: sessionDate,
    ...observation,
  };
  return {
    to: "/strategies/$strategyId/research" as const,
    params: { strategyId: source.source_strategy_version_id },
    search: selection,
  };
}
