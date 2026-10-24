import type {
  CandidatePage,
  DecisionDetail,
  DecisionIndex,
  DecisionPage,
  FeatureRow,
  ReplayExecutionPage,
  ReplayInstrumentPage,
  ReplayMarketPage,
  SourceRef,
  TraceContext,
  TraceManifestPublic,
} from "@/client/types.gen";
import { evidenceMatches, recordedValue } from "./workbench-evidence";
import type { WorkbenchSelection } from "./workbench-selection";

export const REPLAY_PAGE_SIZE = 50;
export const EVENT_PAGE_SIZE = 25;
export function nextSession(date: string) {
  return new Date(Date.parse(`${date}T00:00:00Z`) + 86400000)
    .toISOString()
    .slice(0, 10);
}
export function replayReady(selection: WorkbenchSelection) {
  return Boolean(
    selection.evaluation_id &&
    selection.execution_attempt_id &&
    selection.result_manifest_sha256 &&
    selection.trace_manifest_sha256 &&
    selection.code_sha256 &&
    selection.trace_status !== "not_collected",
  );
}
export function replayContextMatches(
  selection: WorkbenchSelection,
  context: TraceContext,
) {
  return (
    replayReady(selection) &&
    context.status === "available" &&
    evidenceMatches(selection, context)
  );
}
export function decisionMatches(
  selection: WorkbenchSelection,
  detail: DecisionDetail,
) {
  const scoped = (row: {
    stage_id: string;
    observation: { instrument_id?: string | null };
  }) =>
    (!selection.stage_id || row.stage_id === selection.stage_id) &&
    (!selection.instrument_id ||
      row.observation.instrument_id === selection.instrument_id);
  return (
    replayContextMatches(selection, detail.context) &&
    detail.decision?.decision_id === selection.decision_id &&
    (!selection.session_date ||
      detail.decision?.session_date === selection.session_date) &&
    (detail.instrument_id ?? undefined) === selection.instrument_id &&
    (detail.stage_id ?? undefined) === selection.stage_id &&
    detail.features.offset === (selection.detail_offset ?? 0) &&
    detail.predicates.offset === (selection.detail_offset ?? 0) &&
    (detail.features.items ?? []).every(scoped) &&
    (detail.predicates.items ?? []).every(scoped)
  );
}
export function candidatePageMatches(
  selection: WorkbenchSelection,
  page: CandidatePage,
  selected = false,
) {
  return (
    replayContextMatches(selection, page.context) &&
    page.decision_id === selection.decision_id &&
    page.offset === (selected ? 0 : (selection.candidate_offset ?? 0)) &&
    (page.items ?? []).every(({ stage_id, observation: row }) => {
      if (selection.stage_id && stage_id !== selection.stage_id) return false;
      if (selected) return row.instrument_id === selection.instrument_id;
      return (
        (!selection.eligibility || row.eligibility === selection.eligibility) &&
        (!selection.reason || row.reason === selection.reason) &&
        (selection.rank_min === undefined ||
          (row.rank != null && row.rank >= selection.rank_min)) &&
        (selection.rank_max === undefined ||
          (row.rank != null && row.rank <= selection.rank_max)) &&
        (selection.score_min === undefined ||
          (row.score != null && row.score >= selection.score_min)) &&
        (selection.score_max === undefined ||
          (row.score != null && row.score <= selection.score_max))
      );
    })
  );
}
export function eventPageMatches(
  selection: WorkbenchSelection,
  page: DecisionPage,
) {
  return (
    replayContextMatches(selection, page.context) &&
    page.offset === (selection.event_offset ?? 0) &&
    (!selection.session_date ||
      (page.items ?? []).every(
        (item) => item.session_date === selection.session_date,
      ))
  );
}
export function instrumentPageMatches(
  selection: WorkbenchSelection,
  page: ReplayInstrumentPage,
) {
  return (
    replayContextMatches(selection, page.context) &&
    page.offset === (selection.instrument_offset ?? 0) &&
    (!selection.session_date || page.session_date === selection.session_date) &&
    page.market.dataset_version_id ===
      page.context.binding?.dataset_version_id &&
    page.market.dataset_sha256 === page.context.binding?.dataset_sha256
  );
}
export function marketPageMatches(
  selection: WorkbenchSelection,
  page: ReplayMarketPage,
  detail: DecisionDetail,
) {
  return (
    decisionMatches(selection, detail) &&
    replayContextMatches(selection, page.context) &&
    page.decision_id === selection.decision_id &&
    page.instrument_id === selection.instrument_id &&
    page.offset === (selection.market_offset ?? 0) &&
    page.session_date === detail.decision?.session_date &&
    (!selection.market_start || page.start === selection.market_start) &&
    (!selection.market_end || page.end === selection.market_end) &&
    page.information_cutoff === detail.decision?.information_cutoff &&
    page.decision_time === detail.decision?.decision_time &&
    page.market.dataset_version_id ===
      page.context.binding?.dataset_version_id &&
    page.market.dataset_sha256 === page.context.binding?.dataset_sha256 &&
    page.market.universe_version_id ===
      page.context.binding?.universe_version_id &&
    page.market.universe_sha256 === page.context.binding?.universe_sha256 &&
    (!page.instrument ||
      page.instrument.instrument_id === selection.instrument_id) &&
    (page.bars ?? []).every(
      (bar) =>
        bar.listing_id === page.instrument?.listing_id &&
        bar.session_date >= page.start &&
        bar.session_date < page.end,
    )
  );
}
export function executionPageMatches(
  selection: WorkbenchSelection,
  page: ReplayExecutionPage,
) {
  const order =
    selection.record_kind === "order" ? selection.record_id : undefined;
  return (
    replayContextMatches(selection, page.context) &&
    page.decision_id === selection.decision_id &&
    (page.instrument_id ?? undefined) === selection.instrument_id &&
    (page.stage_id ?? undefined) === selection.stage_id &&
    (page.logical_order_id ?? undefined) === order &&
    page.offset === (selection.execution_offset ?? 0) &&
    (page.items ?? []).every(
      (row) =>
        (!selection.instrument_id ||
          row.instrument_id === selection.instrument_id) &&
        (!selection.stage_id || row.stage_id === selection.stage_id) &&
        (!order || row.logical_order_id === order),
    )
  );
}
export function chooseEvent(event: DecisionIndex): Partial<WorkbenchSelection> {
  return {
    event_offset: undefined,
    instrument_offset: undefined,
    session_date: event.session_date,
    decision_id: event.decision_id,
    instrument_id:
      event.scope === "instrument"
        ? (event.instrument_id ?? undefined)
        : undefined,
    stage_id: undefined,
    detail_offset: undefined,
    candidate_offset: undefined,
    execution_offset: undefined,
    market_offset: undefined,
    record_kind: undefined,
    record_id: undefined,
    record_ordinal: undefined,
    rule_id: undefined,
  };
}
export function observationValue(value: unknown) {
  if (Array.isArray(value) && !value.length) return "Observed empty []";
  if (value && typeof value === "object" && !Object.keys(value).length)
    return "Observed empty {}";
  return recordedValue(value);
}
export function marketTime(value: string | null | undefined, timezone: string) {
  if (!value) return "Not recorded";
  try {
    const local = new Intl.DateTimeFormat("en-GB", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).format(new Date(value));
    return `${local} (${timezone}) · ${value}`;
  } catch {
    return `${value} · timezone ${timezone} unavailable`;
  }
}
export function savedFeaturePoints(
  rows: FeatureRow[] | null,
  instrument: string | undefined,
  decisionCutoff: string,
) {
  // Only plot the values actually persisted at this decision. No price-derived
  // indicators, recalculation, forward fill or joining another event by date.
  return (rows ?? [])
    .filter(
      ({ observation: f }) =>
        f.instrument_id === instrument &&
        f.validity === "valid" &&
        typeof f.value === "number" &&
        Number.isFinite(f.value) &&
        Date.parse(f.information_cutoff) <= Date.parse(decisionCutoff),
    )
    .map(({ stage_id, observation }) => ({
      stage_id,
      feature_id: observation.feature_id,
      unit: observation.unit,
      time: observation.information_cutoff,
      value: observation.value as number,
    }));
}
export type SelectedRecord = {
  label: string;
  value: unknown;
  source_ref: SourceRef | null;
  missing_reason: string | null;
};
export function selectedRecord(
  selection: WorkbenchSelection,
  detail: DecisionDetail | undefined,
  candidates: CandidatePage | undefined,
): SelectedRecord | null {
  if (!selection.record_kind || !selection.record_id || !detail) return null;
  const unavailable = (reason: string): SelectedRecord => ({
    label: `${selection.record_kind} ${selection.record_id}`,
    value: null,
    source_ref: null,
    missing_reason: reason,
  });
  const ambiguous = () =>
    unavailable(
      "The exact saved row is ambiguous or cannot be uniquely identified from this ID and available page; no first-match value or source anchor was substituted.",
    );
  const matches = (stage: string, instrument?: string | null) =>
    stage === selection.stage_id &&
    (instrument ?? undefined) === selection.instrument_id;
  if (selection.record_kind === "candidate") {
    const rows =
      candidates?.items?.filter(
        (row) =>
          matches(row.stage_id, row.observation.instrument_id) &&
          row.observation.instrument_id === selection.record_id &&
          (selection.record_ordinal === undefined ||
            row.ordinal === selection.record_ordinal),
      ) ?? [];
    if (
      rows.length > 1 ||
      (selection.record_ordinal === undefined &&
        candidates &&
        (candidates.offset !== 0 ||
          candidates.total !== candidates.items?.length))
    )
      return ambiguous();
    if (rows.length === 1)
      return {
        label: `Candidate ${rows[0].observation.instrument_id}`,
        value: rows[0],
        source_ref: rows[0].observation.source_ref ?? null,
        missing_reason: null,
      };
  }
  for (const kind of ["feature", "predicate"] as const) {
    if (selection.record_kind !== kind) continue;
    const page =
      kind === "feature" ? detail.features.items : detail.predicates.items;
    const rows = (page ?? []).filter(
      ({ stage_id, observation, ordinal }) =>
        (selection.record_ordinal === undefined ||
          ordinal === selection.record_ordinal) &&
        matches(stage_id, observation.instrument_id) &&
        ("feature_id" in observation
          ? observation.feature_id
          : observation.condition_id) === selection.record_id,
    );
    const collection = kind === "feature" ? detail.features : detail.predicates;
    if (
      rows.length > 1 ||
      (selection.record_ordinal === undefined &&
        (collection.offset !== 0 ||
          collection.total !== collection.items?.length))
    )
      return ambiguous();
    if (rows.length === 1)
      return {
        label: `${kind} ${selection.record_id}${selection.record_ordinal !== undefined ? ` · saved ordinal ${selection.record_ordinal}` : ""}`,
        value: rows[0],
        source_ref: rows[0].observation.source_ref ?? null,
        missing_reason: null,
      };
  }
  if (selection.record_kind === "order") {
    const rows = (detail.stages ?? [])
      .filter((stage) => stage.stage_id === selection.stage_id)
      .flatMap((stage) => stage.order_links)
      .filter(
        (row) =>
          row.logical_order_id === selection.record_id &&
          row.instrument_id === selection.instrument_id,
      );
    if (rows.length > 1) return ambiguous();
    if (rows.length === 1)
      return {
        label: `Logical order ${rows[0].logical_order_id}`,
        value: rows[0],
        source_ref: null,
        missing_reason:
          "This saved order association has no source anchor. No nearby rule was guessed.",
      };
  }
  return unavailable(
    "The exact selected record is not present on this evidence page. It may be uncollected or on another detail page; no replacement was selected.",
  );
}

/** Exact saved ordinal survives changes to filtering and page size. */
export function inspectObservation(
  selection: WorkbenchSelection,
  kind: "feature" | "predicate",
  row: {
    ordinal: number;
    stage_id: string;
    observation: {
      instrument_id?: string | null;
      feature_id?: string;
      condition_id?: string;
    };
  },
): Partial<WorkbenchSelection> {
  return {
    stage_id: row.stage_id,
    instrument_id: row.observation.instrument_id ?? undefined,
    record_kind: kind,
    record_id:
      kind === "feature"
        ? row.observation.feature_id
        : row.observation.condition_id,
    record_ordinal: row.ordinal,
    detail_offset:
      selection.stage_id === row.stage_id &&
      selection.instrument_id === (row.observation.instrument_id ?? undefined)
        ? selection.detail_offset
        : undefined,
    execution_offset: undefined,
    rule_id: undefined,
  };
}
export function replayAdmitted(
  selection: WorkbenchSelection,
  trace: TraceManifestPublic | undefined,
  verified: boolean,
) {
  return (
    verified &&
    replayReady(selection) &&
    Boolean(trace?.manifest && replayContextMatches(selection, trace.context))
  );
}
