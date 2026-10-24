import type { CandidateObservation } from "@/client/types.gen";

/** URL state is the research evidence identity, not merely display preferences. */
export const workbenchTabs = [
  "description",
  "replay",
  "comparison",
  "verification",
  "notes",
] as const;
export type WorkbenchTab = (typeof workbenchTabs)[number];
export const comparisonSides = ["left", "right"] as const;
export type ComparisonSide = (typeof comparisonSides)[number];
export const comparisonSections = [
  "events",
  "candidates",
  "features",
  "predicates",
  "targets",
  "orders",
  "fills",
  "positions",
  "account",
  "turnover",
  "holding_duration",
] as const;
export type ComparisonSection = (typeof comparisonSections)[number];
export type ComparisonSideSelection = {
  source_kind?: "candidate" | "strategy_version";
  source_id?: string;
  strategy_version_id?: string;
  code_sha256?: string;
  spec_revision?: number;
  spec_sha256?: string;
  evaluation_id?: string;
  execution_attempt_id?: string;
  result_manifest_sha256?: string;
  trace_manifest_sha256?: string;
  trace_status?: "registered" | "historical_absence";
};
type ComparisonRoutePins = {
  [
    Side in ComparisonSide as `${Side}_${keyof ComparisonSideSelection}`
  ]?: never;
};
type ComparisonPins = {
  [
    Key in keyof ComparisonRoutePins
  ]?: Key extends `${ComparisonSide}_${infer Field extends keyof ComparisonSideSelection}`
    ? ComparisonSideSelection[Field]
    : never;
};
export type WorkbenchSelection = ComparisonPins & {
  tab?: WorkbenchTab;
  note_id?: string;
  note_revision?: number;
  note_revision_sha256?: string;
  note_query?: string;
  note_offset?: number;
  note_origin_tab?: Exclude<WorkbenchTab, "notes">;
  note_evidence_index?: number;
  note_arm?: number;
  candidate_id?: string;
  code_sha256?: string;
  evaluation_id?: string;
  execution_attempt_id?: string;
  result_manifest_sha256?: string;
  trace_manifest_sha256?: string;
  trace_status?: "available" | "not_collected";
  session_date?: string;
  event_offset?: number;
  instrument_offset?: number;
  candidate_offset?: number;
  detail_offset?: number;
  execution_offset?: number;
  market_offset?: number;
  market_start?: string;
  market_end?: string;
  eligibility?: CandidateObservation["eligibility"];
  reason?: CandidateObservation["reason"];
  rank_min?: number;
  rank_max?: number;
  score_min?: number;
  score_max?: number;
  record_kind?: "candidate" | "feature" | "predicate" | "order";
  record_id?: string;
  record_ordinal?: number;
  decision_id?: string;
  instrument_id?: string;
  stage_id?: string;
  rule_id?: string;
  spec_revision?: number;
  spec_sha256?: string;
  comparison_id?: string;
  comparison_manifest_sha256?: string;
  comparison_section?: ComparisonSection;
  comparison_offset?: number;
  comparison_event_offset?: number;
  comparison_instrument_id?: string;
  mechanism_index?: number;
  event_pair_id?: string;
  diagnostic_run_id?: string;
  diagnostic_config_sha256?: string;
  diagnostic_report_sha256?: string;
  diagnostic_event_offset?: number;
  diagnostic_evaluation_id?: string;
  diagnostic_execution_attempt_id?: string;
  diagnostic_result_manifest_sha256?: string;
  diagnostic_trace_manifest_sha256?: string;
  controlled_experiment_id?: string;
  controlled_request_sha256?: string;
  controlled_protocol_sha256?: string;
  controlled_arm?: number;
  controlled_arm_run_id?: string;
  controlled_arm_evaluation_id?: string;
  controlled_arm_execution_attempt_id?: string;
  controlled_arm_result_manifest_sha256?: string;
  controlled_arm_trace_manifest_sha256?: string;
};
const textKeys = [
  "note_id",
  "note_revision_sha256",
  "note_query",
  "candidate_id",
  "code_sha256",
  "evaluation_id",
  "execution_attempt_id",
  "result_manifest_sha256",
  "trace_manifest_sha256",
  "decision_id",
  "session_date",
  "market_start",
  "market_end",
  "record_id",
  "instrument_id",
  "stage_id",
  "rule_id",
  "spec_sha256",

  "comparison_id",
  "comparison_manifest_sha256",
  "comparison_instrument_id",
  "event_pair_id",
  "diagnostic_run_id",
  "diagnostic_config_sha256",
  "diagnostic_report_sha256",
  "diagnostic_evaluation_id",
  "diagnostic_execution_attempt_id",
  "diagnostic_result_manifest_sha256",
  "diagnostic_trace_manifest_sha256",
  "controlled_experiment_id",
  "controlled_request_sha256",
  "controlled_protocol_sha256",
  "controlled_arm_run_id",
  "controlled_arm_evaluation_id",
  "controlled_arm_execution_attempt_id",
  "controlled_arm_result_manifest_sha256",
  "controlled_arm_trace_manifest_sha256",
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
  for (const side of comparisonSides) {
    const pins: ComparisonSideSelection = {};
    for (const field of [
      "source_id",
      "strategy_version_id",
      "code_sha256",
      "spec_sha256",
      "evaluation_id",
      "execution_attempt_id",
      "result_manifest_sha256",
      "trace_manifest_sha256",
    ] as const) {
      const value = search[`${side}_${field}`];
      if (value === undefined) continue;
      if (
        typeof value !== "string" ||
        !value ||
        value.length > 500 ||
        (field.endsWith("sha256") && !/^[a-f0-9]{64}$/.test(value))
      )
        throw new Error(
          `Invalid ${side} comparison pin: ${field}. No newer evidence was substituted.`,
        );
      pins[field] = value;
    }
    for (const [field, choices] of [
      ["source_kind", ["candidate", "strategy_version"]],
      ["trace_status", ["registered", "historical_absence"]],
    ] as const) {
      const value = search[`${side}_${field}`];
      if (value === undefined) continue;
      if (
        typeof value !== "string" ||
        !(choices as readonly string[]).includes(value)
      )
        throw new Error(`Invalid ${side} comparison ${field}.`);
      Object.assign(pins, { [field]: value });
    }
    const revision = search[`${side}_spec_revision`];
    if (revision !== undefined) {
      const value =
        typeof revision === "number" ||
        (typeof revision === "string" && revision.trim())
          ? Number(revision)
          : NaN;
      if (!Number.isSafeInteger(value) || value < 1)
        throw new Error(`Invalid ${side} comparison description revision.`);
      pins.spec_revision = value;
    }
    Object.assign(result, comparisonSidePatch(side, pins));
  }
  if (search.comparison_section !== undefined) {
    if (
      !comparisonSections.includes(
        search.comparison_section as ComparisonSection,
      )
    )
      throw new Error(
        "Invalid comparison section. No other evidence was substituted.",
      );
    result.comparison_section = search.comparison_section as ComparisonSection;
  }
  for (const key of textKeys) {
    const value = input[key];
    if (value === undefined) continue;
    if (typeof value !== "string" || !value || value.length > 500)
      throw new Error(
        `Invalid research selection: ${key}. The evidence was not retargeted.`,
      );
    result[key] = value;
  }
  if (search.controlled_arm !== undefined) {
    const raw = search.controlled_arm;
    const value =
      typeof raw === "number" || (typeof raw === "string" && raw.trim())
        ? Number(raw)
        : NaN;
    if (!Number.isSafeInteger(value) || value < 0 || value > 2)
      throw new Error(
        "Invalid controlled arm ordinal. No different arm was selected.",
      );
    result.controlled_arm = value;
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
  if (search.note_origin_tab !== undefined) {
    if (
      !["description", "replay", "comparison", "verification"].includes(
        String(search.note_origin_tab),
      )
    )
      throw new Error(
        "Invalid note origin. No different evidence was selected.",
      );
    result.note_origin_tab =
      search.note_origin_tab as WorkbenchSelection["note_origin_tab"];
  }
  for (const [key, maximum] of [
    ["note_evidence_index", 31],
    ["note_arm", 2],
  ] as const) {
    if (search[key] === undefined) continue;
    const value = Number(search[key]);
    if (
      (typeof search[key] !== "number" && typeof search[key] !== "string") ||
      (typeof search[key] === "string" && !search[key].trim()) ||
      !Number.isSafeInteger(value) ||
      value < 0 ||
      value > maximum
    )
      throw new Error(
        "Invalid historical note evidence locator. No different arm was selected.",
      );
    result[key] = value;
  }
  if (search.note_revision !== undefined) {
    const value = Number(search.note_revision);
    if (
      (typeof search.note_revision !== "number" &&
        typeof search.note_revision !== "string") ||
      !Number.isSafeInteger(value) ||
      value < 1 ||
      value > 10000
    )
      throw new Error(
        "Invalid note revision. No latest revision was substituted.",
      );
    result.note_revision = value;
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
  for (const key of ["session_date", "market_start", "market_end"] as const) {
    const value = result[key];
    if (
      value &&
      (!/^\d{4}-\d{2}-\d{2}$/.test(value) ||
        !Number.isFinite(Date.parse(value)) ||
        new Date(value).toISOString().slice(0, 10) !== value)
    )
      throw new Error(
        `Invalid replay date: ${key}. The evidence was not retargeted.`,
      );
  }
  for (const key of [
    "note_offset",
    "diagnostic_event_offset",
    "record_ordinal",
    "mechanism_index",
    "comparison_offset",
    "comparison_event_offset",
    "event_offset",
    "instrument_offset",
    "candidate_offset",
    "detail_offset",
    "execution_offset",
    "market_offset",
    "rank_min",
    "rank_max",
    "score_min",
    "score_max",
  ] as const) {
    const raw = search[key];
    if (raw === undefined) continue;
    const value =
      typeof raw === "number" || (typeof raw === "string" && raw.trim() !== "")
        ? Number(raw)
        : NaN;
    if (
      !Number.isFinite(value) ||
      ((key.includes("offset") ||
        key === "record_ordinal" ||
        key === "mechanism_index") &&
        (!Number.isSafeInteger(value) ||
          value < 0 ||
          value >
            (key === "record_ordinal" ? Number.MAX_SAFE_INTEGER : 100000))) ||
      (key.startsWith("rank_") && (!Number.isSafeInteger(value) || value < 1))
    )
      throw new Error(
        `Invalid replay filter: ${key}. No unfiltered evidence was substituted.`,
      );
    result[key] = value;
  }
  for (const [key, values] of [
    ["eligibility", eligibilityValues],
    ["reason", candidateReasons],
    ["record_kind", ["candidate", "feature", "predicate", "order"]],
  ] as const) {
    const value = search[key];
    if (value === undefined) continue;
    if (
      typeof value !== "string" ||
      !(values as readonly string[]).includes(value)
    )
      throw new Error(`Invalid replay selection: ${key}.`);
    Object.assign(result, { [key]: value });
  }
  return result;
}
export const eligibilityValues = [
  "eligible",
  "ineligible",
  "unknown",
  "not_sampled",
] as const;
export const candidateReasons = [
  "outside_universe",
  "feature_unavailable",
  "eligibility_filter",
  "rank_not_selected",
  "holding_retained",
  "position_slots",
  "portfolio_constraint",
  "cash_constraint",
  "timing_not_triggered",
  "order_rejected",
  "order_unfilled",
  "partial_fill",
  "not_evaluated",
  "not_collected",
  "not_sampled",
  "selected",
  "exit",
] as const;
export function selectionProblem(value: WorkbenchSelection): string | null {
  if (
    (value.note_id || value.note_revision || value.note_revision_sha256) &&
    !(value.note_id && value.note_revision && value.note_revision_sha256)
  )
    return "A saved note needs its exact ID, revision and hash. No latest note was substituted.";
  if (
    (value.note_evidence_index !== undefined && !value.note_id) ||
    (value.note_arm !== undefined && value.note_evidence_index === undefined)
  )
    return "Historical arm evidence requires its exact note revision and evidence ordinal.";
  const verificationProblem = verificationSelectionProblem(value);
  if (verificationProblem) return verificationProblem;
  if (
    value.record_ordinal !== undefined &&
    (!value.stage_id ||
      !["candidate", "feature", "predicate", "order"].includes(
        value.record_kind ?? "",
      ))
  )
    return "An exact observation ordinal requires its recorded stage and observation kind.";
  if (Boolean(value.record_kind) !== Boolean(value.record_id))
    return "A selected replay record needs its actual kind and ID. No replacement record was selected.";
  if (value.record_id && !value.decision_id)
    return "Choose a specific decision event before opening a recorded condition or order.";
  if (
    value.rank_min !== undefined &&
    value.rank_max !== undefined &&
    value.rank_min > value.rank_max
  )
    return "Minimum rank cannot exceed maximum rank.";
  if (
    value.score_min !== undefined &&
    value.score_max !== undefined &&
    value.score_min > value.score_max
  )
    return "Minimum score cannot exceed maximum score.";
  if (
    value.market_start &&
    value.market_end &&
    value.market_start >= value.market_end
  )
    return "Retrospective chart end must follow start (exclusive).";
  if (Boolean(value.spec_revision) !== Boolean(value.spec_sha256))
    return "A saved description needs both its revision and hash. No latest revision was substituted.";
  if (value.trace_status === "available" && !value.trace_manifest_sha256)
    return "An available trace selection needs its exact trace hash. No newer trace was substituted.";
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
  const next = { ...value, ...patch };
  if (
    !Object.hasOwn(patch, "record_kind") &&
    ["decision_id", "stage_id", "instrument_id"].some(
      (key) =>
        Object.hasOwn(patch, key) &&
        value[key as "decision_id" | "stage_id" | "instrument_id"] !==
          patch[key as "decision_id" | "stage_id" | "instrument_id"],
    )
  ) {
    next.record_kind = undefined;
    next.record_id = undefined;
    next.record_ordinal = undefined;
  }

  if (
    Object.hasOwn(patch, "record_kind") &&
    patch.record_kind !== "feature" &&
    patch.record_kind !== "predicate" &&
    patch.record_kind !== "candidate" &&
    !(patch.record_kind === "order" && Object.hasOwn(patch, "record_ordinal"))
  ) {
    next.record_ordinal = undefined;
  }
  return next;
}

/** Clear all leaf-dependent observations without losing exact source/spec pins. */
export function clearedReplaySelection(): Partial<WorkbenchSelection> {
  return {
    session_date: undefined,
    event_offset: undefined,
    instrument_offset: undefined,
    candidate_offset: undefined,
    detail_offset: undefined,
    execution_offset: undefined,
    market_offset: undefined,
    market_start: undefined,
    market_end: undefined,
    eligibility: undefined,
    reason: undefined,
    rank_min: undefined,
    rank_max: undefined,
    score_min: undefined,
    score_max: undefined,
    record_kind: undefined,
    record_id: undefined,
    record_ordinal: undefined,
    decision_id: undefined,
    instrument_id: undefined,
    stage_id: undefined,
    rule_id: undefined,
  };
}

/** A side is always explicit; there is no fallback to the currently open source. */
export function comparisonSideSelection(
  value: WorkbenchSelection,
  side: ComparisonSide,
): ComparisonSideSelection {
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => key.startsWith(`${side}_`))
      .map(([key, item]) => [key.slice(side.length + 1), item]),
  ) as ComparisonSideSelection;
}
export function comparisonSidePatch(
  side: ComparisonSide,
  pins: ComparisonSideSelection,
): Partial<WorkbenchSelection> {
  return Object.fromEntries(
    Object.entries(pins).map(([key, item]) => [`${side}_${key}`, item]),
  );
}
export function clearComparisonResult(): Partial<WorkbenchSelection> {
  return {
    comparison_id: undefined,
    comparison_manifest_sha256: undefined,
    event_pair_id: undefined,
    comparison_offset: undefined,
    comparison_event_offset: undefined,
    comparison_instrument_id: undefined,
    mechanism_index: undefined,
  };
}
export function clearComparisonSide(
  side: ComparisonSide,
): Partial<WorkbenchSelection> {
  return comparisonSidePatch(side, {
    source_kind: undefined,
    source_id: undefined,
    strategy_version_id: undefined,
    code_sha256: undefined,
    spec_revision: undefined,
    spec_sha256: undefined,
    evaluation_id: undefined,
    execution_attempt_id: undefined,
    result_manifest_sha256: undefined,
    trace_manifest_sha256: undefined,
    trace_status: undefined,
  });
}

/** Verification jobs and physical results never borrow replay/comparison identities. */
export function verificationSelectionProblem(
  value: WorkbenchSelection,
): string | null {
  if (
    (value.diagnostic_config_sha256 ||
      value.diagnostic_event_offset !== undefined ||
      value.diagnostic_report_sha256 ||
      value.diagnostic_evaluation_id) &&
    !value.diagnostic_run_id
  )
    return "Diagnostic evidence requires its own immutable run ID.";
  if (value.diagnostic_report_sha256 && !value.diagnostic_config_sha256)
    return "A diagnostic report requires the exact diagnostic configuration hash.";
  if (
    (value.controlled_request_sha256 ||
      value.controlled_protocol_sha256 ||
      value.controlled_arm !== undefined ||
      value.controlled_arm_run_id) &&
    !value.controlled_experiment_id
  )
    return "Controlled evidence requires its own immutable experiment ID.";
  if (
    value.controlled_arm !== undefined &&
    !(
      value.controlled_arm_run_id &&
      value.controlled_protocol_sha256 &&
      value.controlled_request_sha256
    )
  )
    return "A controlled arm requires its ordered run, request and protocol hashes. No other arm was substituted.";
  if (value.controlled_arm_run_id && value.controlled_arm === undefined)
    return "A controlled arm run requires its immutable ordinal.";
  for (const prefix of ["diagnostic", "controlled_arm"] as const) {
    const evaluation = value[`${prefix}_evaluation_id`];
    const attempt = value[`${prefix}_execution_attempt_id`];
    const result = value[`${prefix}_result_manifest_sha256`];
    const trace = value[`${prefix}_trace_manifest_sha256`];
    if (
      (evaluation || attempt || result || trace) &&
      !(evaluation && attempt && result)
    )
      return "Independent result evidence requires its exact Evaluation, physical attempt and result hash together.";
    if (
      prefix === "controlled_arm" &&
      evaluation &&
      value.controlled_arm === undefined
    )
      return "A controlled leaf requires its selected immutable arm.";
  }
  return null;
}
export function clearedDiagnosticSelection(): Partial<WorkbenchSelection> {
  return {
    diagnostic_run_id: undefined,
    diagnostic_config_sha256: undefined,
    diagnostic_report_sha256: undefined,
    diagnostic_event_offset: undefined,
    diagnostic_evaluation_id: undefined,
    diagnostic_execution_attempt_id: undefined,
    diagnostic_result_manifest_sha256: undefined,
    diagnostic_trace_manifest_sha256: undefined,
  };
}
export function clearedControlledArmSelection(): Partial<WorkbenchSelection> {
  return {
    controlled_arm: undefined,
    controlled_arm_run_id: undefined,
    controlled_arm_evaluation_id: undefined,
    controlled_arm_execution_attempt_id: undefined,
    controlled_arm_result_manifest_sha256: undefined,
    controlled_arm_trace_manifest_sha256: undefined,
  };
}
export function clearedControlledSelection(): Partial<WorkbenchSelection> {
  return {
    ...clearedControlledArmSelection(),
    controlled_experiment_id: undefined,
    controlled_request_sha256: undefined,
    controlled_protocol_sha256: undefined,
  };
}
