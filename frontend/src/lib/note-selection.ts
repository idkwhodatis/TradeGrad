import {
  workbenchSearch,
  selectionProblem,
  type WorkbenchSelection,
} from "./workbench-selection";

export const noteStatusLabels = {
  pending: "Pending validation",
  supported_under_current_tests: "Supported under current tests",
  mixed: "Mixed results",
  refuted_under_current_tests: "Refuted under current tests",
} as const;
export const noteEvidenceNotice =
  "Status is maintained by you, never inferred from profit. Human notes and explanations do not replace observed records. Source labels identify provenance, not a confidence score. Full backtest differences are not isolated causal proof; missing evidence stays missing.";

/** Saved restore state contains evidence, never a recursive note or draft identity. */
export function noteRestoreSelection(
  selection: WorkbenchSelection,
): WorkbenchSelection {
  const original = workbenchSearch(selection);
  const result: WorkbenchSelection = Object.fromEntries(
    Object.entries(original).filter(
      ([key, value]) => !key.startsWith("note_") && value !== undefined,
    ),
  );
  result.tab =
    selection.note_origin_tab ??
    (selection.tab === "notes"
      ? selection.controlled_experiment_id || selection.diagnostic_run_id
        ? "verification"
        : selection.comparison_id
          ? "comparison"
          : selection.decision_id
            ? "replay"
            : "description"
      : (selection.tab ?? "description"));
  const problem = selectionProblem(result);
  if (problem) throw new Error(problem);
  return result;
}

/** No API-provided HTML, external URL or unvalidated route is used as a link. */
export function safeNoteEvidenceHref(
  href: string | null | undefined,
): string | null {
  if (!href || !href.startsWith("/strategies/") || href.startsWith("//"))
    return null;
  try {
    const url = new URL(href, "https://notes.invalid");
    if (
      url.origin !== "https://notes.invalid" ||
      url.hash ||
      !/^\/strategies\/[a-f0-9-]{36}\/research$/.test(url.pathname)
    )
      return null;
    const entries = [...url.searchParams.entries()];
    if (entries.some(([key]) => key.startsWith("note_"))) return null;
    if (new Set(entries.map(([key]) => key)).size !== entries.length)
      return null;
    const selection = workbenchSearch(Object.fromEntries(entries));
    if (selectionProblem(selection)) return null;
    // Dropping an unknown field could silently change the selected evidence.
    if (entries.some(([key]) => !(key in selection))) return null;
    if (
      !selection.code_sha256 ||
      !selection.spec_revision ||
      !selection.spec_sha256
    )
      return null;
    return `${url.pathname}${url.search}`;
  } catch {
    return null;
  }
}

/** A completed action may affect only the owner and exact route that started it. */
export function noteOperationCurrent(
  captured: string,
  current: string,
  active: boolean,
) {
  return active && captured === current;
}

/** Historical controls have a note-owned locator, not a mutable current-run route. */
export function noteHistoricalHref(
  strategyId: string,
  note: { note_id: string; revision: number; revision_sha256: string },
  index: number,
  arm: number,
) {
  const selection = workbenchSearch({
    tab: "notes",
    note_id: note.note_id,
    note_revision: note.revision,
    note_revision_sha256: note.revision_sha256,
    note_evidence_index: index,
    note_arm: arm,
  });
  if (selectionProblem(selection) || !/^[a-f0-9-]{36}$/.test(strategyId))
    return null;
  return `/strategies/${strategyId}/research?${new URLSearchParams(
    Object.entries(selection)
      .filter(([, value]) => value !== undefined)
      .map(([key, value]) => [key, String(value)]),
  )}`;
}

export function noteArmResultHref(
  arm: import("@/client/types.gen").NoteControlledArm,
) {
  const pin = arm.result;
  if (
    !pin ||
    pin.source_kind !== "strategy_version" ||
    !pin.spec_revision ||
    !pin.spec_sha256
  )
    return null;
  const selection = {
    tab: "replay",
    code_sha256: pin.code_sha256,
    spec_revision: pin.spec_revision,
    spec_sha256: pin.spec_sha256,
    evaluation_id: pin.evaluation_id,
    execution_attempt_id: pin.execution_attempt_id,
    result_manifest_sha256: pin.result_manifest_sha256,
    ...(pin.trace_manifest_sha256
      ? {
          trace_manifest_sha256: pin.trace_manifest_sha256,
          trace_status: "available",
        }
      : { trace_status: "not_collected" }),
  };
  return safeNoteEvidenceHref(
    `/strategies/${pin.source_id}/research?${new URLSearchParams(Object.entries(selection).map(([key, value]) => [key, String(value)]))}`,
  );
}
