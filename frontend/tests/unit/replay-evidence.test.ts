import { describe, expect, it } from "vitest";
import type {
  CandidatePage,
  CandidateRow,
  DecisionIndex,
  ReplayMarketPage,
  ReplayExecutionPage,
} from "../../src/client/types.gen";
import {
  candidatePageMatches,
  chooseEvent,
  decisionMatches,
  eventPageMatches,
  executionPageMatches,
  inspectObservation,
  marketPageMatches,
  marketTime,
  nextSession,
  observationValue,
  replayContextMatches,
  replayReady,
  replayAdmitted,
  savedFeaturePoints,
  selectedRecord,
} from "../../src/lib/replay-evidence";
import {
  changeSelection,
  clearedReplaySelection,
  selectionProblem,
  workbenchSearch,
} from "../../src/lib/workbench-selection";
import { leafSelection } from "../../src/lib/workbench-evidence";
import { detail, hashes, ids, leaf, rule } from "./workbench-fixtures";
export const selection = {
  tab: "replay" as const,
  candidate_id: ids.candidate,
  code_sha256: hashes.code,
  evaluation_id: ids.evaluation,
  execution_attempt_id: ids.attempt,
  result_manifest_sha256: hashes.result,
  trace_manifest_sha256: hashes.trace,
  session_date: "2024-01-02",
  decision_id: "decision-1",
  instrument_id: "B",
  stage_id: "liquidity",
  spec_revision: 1,
  spec_sha256: hashes.spec,
};
export const candidate: CandidateRow = {
  ordinal: 0,
  stage_id: "liquidity",
  observation: {
    instrument_id: "B",
    eligibility: "ineligible",
    reason: "eligibility_filter",
    score: 0,
    score_validity: "valid",
    rank: 2,
    ranking_direction: "descending",
    tie_break: "instrument_id",
    selected: false,
    prior_holding: false,
    universe_member: true,
    source: "strategy_emitted",
    source_ref: rule.source_ref,
  },
};
export const candidatePage = (): CandidatePage => ({
  context: detail().context,
  decision_id: "decision-1",
  items: [candidate],
  total: 1,
  limit: 50,
  offset: 0,
});
export const event: DecisionIndex = {
  ...detail().decision!,
  session_date: "2024-01-02",
  stage_ids: ["liquidity"],
};
export function marketPage(): ReplayMarketPage {
  return {
    context: detail().context,
    decision_id: "decision-1",
    session_date: "2024-01-02",
    instrument_id: "B",
    status: "available",
    missing_reason: null,
    information_cutoff: event.information_cutoff,
    decision_time: event.decision_time,
    bars: [
      {
        listing_id: ids.strategy,
        session_date: "2024-01-02",
        open: 5,
        high: 7,
        low: 4,
        close: 6,
        volume: 0,
        volume_unit: "shares",
        currency: "CNY",
        flags: [],
      },
    ],
    total: 1,
    offset: 0,
    limit: 100,
    start: "2024-01-01",
    end: "2024-01-03",
    instrument: {
      listing_id: ids.strategy,
      instrument_id: "B",
      symbol: "B",
      configured_evaluation_member: true,
      configured_session_member: true,
    },
    market: {
      dataset_version_id: ids.strategy,
      dataset_sha256: "e".repeat(64),
      universe_version_id: null,
      universe_sha256: null,
      universe_state: "user_fixed",
      universe_as_of: null,
      historical_membership_status: "user_fixed_assumption",
      membership_status: "available",
      membership_reason: "Configured basket only",
      timezone: "Asia/Shanghai",
      window_start: "2024-01-01",
      window_end: "2024-01-03",
      result_trust: "native_strategy_and_engine_share_process",
      ledger_origin: "native_same_process",
      warnings: [],
    },
  };
}
describe("URL-recoverable exact replay selection", () => {
  it("preserves every observation, filter, page and source pin", () => {
    const s = {
      ...selection,
      eligibility: "ineligible" as const,
      reason: "rank_not_selected" as const,
      rank_min: 1,
      rank_max: 4,
      score_min: -0.5,
      score_max: 0,
      candidate_offset: 50,
      detail_offset: 100,
      event_offset: 25,
      instrument_offset: 50,
      execution_offset: 50,
      market_offset: 100,
      market_start: "2024-01-01",
      market_end: "2024-01-03",
      record_kind: "predicate" as const,
      record_id: "liquid",
      record_ordinal: 50,
    };
    expect(workbenchSearch(s)).toEqual(s);
    expect(selectionProblem(s)).toBeNull();
    expect(changeSelection(s, { tab: "description" })).toEqual({
      ...s,
      tab: "description",
    });
  });
  it.each([
    { session_date: "2024-02-30" },
    { session_date: "no" },
    { market_end: "2024-1-2" },
    { rank_min: 0 },
    { rank_max: 1.5 },
    { score_min: Infinity },
    { candidate_offset: -1 },
    { event_offset: 100001 },
    { record_ordinal: 0.1 },
    { eligibility: "true" },
    { reason: "no_fill_means_no_intent" },
    { record_kind: "guessed" },
  ])("rejects invalid selector %j without falling back", (value) =>
    expect(() => workbenchSearch(value)).toThrow(),
  );
  it.each([
    { rank_min: 4, rank_max: 2 },
    { score_min: 1, score_max: -1 },
    { market_start: "2024-01-03", market_end: "2024-01-02" },
    { record_id: "orphan" },
    {
      record_kind: "order" as const,
      record_id: "order-1",
      decision_id: undefined,
    },
  ])("blocks contradictory selector %j", (patch) =>
    expect(selectionProblem({ ...selection, ...patch })).not.toBeNull(),
  );
  it("choosing same-day events pins distinct identities and resets date-relative page", () => {
    const one = chooseEvent(event),
      two = chooseEvent({ ...event, decision_id: "decision-2", event_seq: 2 });
    expect(one.decision_id).not.toBe(two.decision_id);
    expect(one.session_date).toBe(two.session_date);
    expect(one.event_offset).toBeUndefined();
    expect(one.instrument_id).toBeUndefined();
    expect(nextSession("2024-02-29")).toBe("2024-03-01");
  });
  it("clears selected records and all scopes when clearing or changing the leaf", () => {
    const withRecord = {
      ...selection,
      record_kind: "predicate" as const,
      record_id: "liquid",
      record_ordinal: 50,
    };
    const cleared = {
      ...withRecord,
      ...clearedReplaySelection(),
      evaluation_id: undefined,
      execution_attempt_id: undefined,
      result_manifest_sha256: undefined,
      trace_manifest_sha256: undefined,
    };
    expect(selectionProblem(cleared)).toBeNull();
    expect(leafSelection(withRecord, leaf).record_ordinal).toBeUndefined();
    expect(
      changeSelection(withRecord, { record_kind: "order", record_id: "x" })
        .record_ordinal,
    ).toBeUndefined();
  });
  it("does not retain an old row when selecting another rule stage or instrument", () => {
    const old = {
      ...selection,
      record_kind: "predicate" as const,
      record_id: "liquid",
      record_ordinal: 7,
    };
    for (const patch of [
      { stage_id: "ranking" },
      { instrument_id: "C" },
      { decision_id: "decision-2" },
    ]) {
      const next = changeSelection(old, patch);
      expect(next.record_kind).toBeUndefined();
      expect(next.record_id).toBeUndefined();
      expect(next.record_ordinal).toBeUndefined();
    }
  });
  it("distinguishes absent manifests from registered capture-off manifests", () => {
    const off = { ...selection, trace_status: "not_collected" as const };
    expect(selectionProblem(off)).toBeNull();
    expect(workbenchSearch(off).trace_manifest_sha256).toBe(hashes.trace);
    expect(replayReady(off)).toBe(false);
    expect(
      replayAdmitted(
        selection,
        {
          context: { ...detail().context, status: "not_collected" },
          manifest: null,
        },
        true,
      ),
    ).toBe(false);
    expect(
      replayContextMatches(selection, {
        ...detail().context,
        status: "not_collected",
        missing_reason: "capture off",
      }),
    ).toBe(false);
    expect(replayReady({ ...off, trace_manifest_sha256: undefined })).toBe(
      false,
    );
  });
});
describe("strict evidence identity and scopes", () => {
  it("accepts the exact decision but rejects another event, date, scope or page", () => {
    expect(decisionMatches(selection, detail())).toBe(true);
    for (const patch of [
      { decision_id: "other" },
      { session_date: "2024-01-01" },
      { instrument_id: "C" },
      { stage_id: "rank" },
      { detail_offset: 50 },
      { code_sha256: "0".repeat(64) },
    ])
      expect(decisionMatches({ ...selection, ...patch }, detail())).toBe(false);
    const corrupt = detail();
    corrupt.features.items![0].observation.instrument_id = "C";
    expect(decisionMatches(selection, corrupt)).toBe(false);
  });
  it("never chooses an event by day alone or accepts a different date page", () => {
    const page = {
      context: detail().context,
      items: [event],
      total: 1,
      offset: 0,
      limit: 25,
    };
    expect(eventPageMatches(selection, page)).toBe(true);
    expect(
      eventPageMatches({ ...selection, session_date: "2024-01-01" }, page),
    ).toBe(false);
    expect(eventPageMatches({ ...selection, event_offset: 25 }, page)).toBe(
      false,
    );
  });
  it("candidate filtering retains non-trades, zero score and real null state", () => {
    expect(
      candidatePageMatches(
        {
          ...selection,
          eligibility: "ineligible",
          reason: "eligibility_filter",
          score_min: 0,
          score_max: 0,
          rank_min: 2,
          rank_max: 2,
        },
        candidatePage(),
      ),
    ).toBe(true);
    for (const patch of [
      { reason: "selected" as const },
      { eligibility: "eligible" as const },
      { score_min: 1 },
      { score_max: -1 },
      { rank_min: 3 },
      { rank_max: 1 },
      { stage_id: "other" },
    ])
      expect(
        candidatePageMatches({ ...selection, ...patch }, candidatePage()),
      ).toBe(false);
    const missing = candidatePage();
    missing.items = [
      { ...candidate, observation: { ...candidate.observation, score: null } },
    ];
    expect(candidatePageMatches({ ...selection, score_min: 0 }, missing)).toBe(
      false,
    );
    expect(
      candidatePageMatches(
        { ...selection, instrument_id: "C" },
        candidatePage(),
        true,
      ),
    ).toBe(false);
  });
  it("market rows require exact selected event, frozen dataset, listing and window", () => {
    expect(marketPageMatches(selection, marketPage(), detail())).toBe(true);
    for (const patch of [
      { decision_id: "other" },
      { instrument_id: "C" },
      { session_date: "2024-01-01" },
      { market_offset: 100 },
      { market_start: "2024-01-02" },
    ])
      expect(
        marketPageMatches({ ...selection, ...patch }, marketPage(), detail()),
      ).toBe(false);
    const bad = marketPage();
    bad.bars![0].listing_id = ids.candidate;
    expect(marketPageMatches(selection, bad, detail())).toBe(false);
    const wrong = marketPage();
    wrong.market.dataset_sha256 = "0".repeat(64);
    expect(marketPageMatches(selection, wrong, detail())).toBe(false);
    const future = marketPage();
    future.information_cutoff = "2024-01-03T07:00:00Z";
    expect(marketPageMatches(selection, future, detail())).toBe(false);
  });
  it("execution records are not attached through a date or instrument guess", () => {
    const page: ReplayExecutionPage = {
      context: detail().context,
      decision_id: "decision-1",
      instrument_id: "B",
      stage_id: "liquidity",
      logical_order_id: null,
      status: "no_linked_records",
      missing_reason: "No explicit links",
      items: [],
      total: 0,
      offset: 0,
      limit: 50,
      warnings: [],
    };
    expect(executionPageMatches(selection, page)).toBe(true);
    expect(
      executionPageMatches({ ...selection, decision_id: "other" }, page),
    ).toBe(false);
    expect(
      executionPageMatches(
        { ...selection, record_kind: "order", record_id: "x" },
        page,
      ),
    ).toBe(false);
  });
});
describe("saved feature and actual-record semantics", () => {
  it.each([
    [null, "Not recorded"],
    [undefined, "Not recorded"],
    [false, "false"],
    [0, "0"],
    [[], "Observed empty []"],
    [{}, "Observed empty {}"],
  ])("keeps %j distinct", (value, expected) =>
    expect(observationValue(value)).toBe(expected),
  );
  it("plots only numeric valid values at saved cutoff without interpolating or evaluating", () => {
    const rows = detail().features.items!;
    expect(
      savedFeaturePoints(rows, "B", event.information_cutoff)[0].value,
    ).toBe(0);
    for (const observation of [
      { value: false },
      { value: null },
      { validity: "warmup_insufficient" as const },
      { information_cutoff: "2024-01-03T07:00:00Z" },
      { instrument_id: "C" },
    ])
      expect(
        savedFeaturePoints(
          [
            {
              ...rows[0],
              observation: { ...rows[0].observation, ...observation },
            },
          ],
          "B",
          event.information_cutoff,
        ),
      ).toEqual([]);
  });
  it("shows market-zone local time and original UTC timestamp separately", () => {
    expect(marketTime("2024-01-02T07:00:00Z", "Asia/Shanghai")).toContain(
      "15:00:00",
    );
    expect(marketTime("2024-01-02T07:00:00Z", "Asia/Shanghai")).toContain(
      "2024-01-02T07:00:00Z",
    );
    expect(marketTime(null, "UTC")).toBe("Not recorded");
  });
  it("restores an exact saved ordinal after narrowing a later mixed detail page", () => {
    const original = {
      ...selection,
      instrument_id: undefined,
      stage_id: undefined,
      detail_offset: 50,
    };
    const row = detail().predicates.items![0];
    row.ordinal = 73;
    row.observation.source_ref = rule.source_ref;
    const chosen = changeSelection(
      original,
      inspectObservation(original, "predicate", row),
    );
    expect(chosen.detail_offset).toBeUndefined();
    expect(chosen.stage_id).toBe("liquidity");
    const restored = workbenchSearch(chosen);
    expect(restored.record_ordinal).toBe(73);
    const recorded = detail();
    recorded.predicates.items = [row];
    expect(selectedRecord(restored, recorded, undefined)?.source_ref).toEqual(
      rule.source_ref,
    );
  });
  it("selects duplicate IDs by persisted ordinal, never first-match values", () => {
    const original = detail().predicates.items![0];
    const different = {
      ...original,
      ordinal: 9,
      observation: {
        ...original.observation,
        operands: [99],
        source_ref: rule.source_ref,
      },
    };
    const recorded = detail();
    recorded.predicates.items = [original, different];
    recorded.predicates.total = 2;
    const ambiguous = {
      ...selection,
      record_kind: "predicate" as const,
      record_id: "liquid",
    };
    expect(
      selectedRecord(ambiguous, recorded, undefined)?.missing_reason,
    ).toContain("ambiguous");
    expect(
      selectedRecord({ ...ambiguous, record_ordinal: 9 }, recorded, undefined)
        ?.value,
    ).toEqual(different);
    expect(
      selectedRecord({ ...ambiguous, record_ordinal: 8 }, recorded, undefined)
        ?.value,
    ).toBeNull();
    const candidates = candidatePage();
    candidates.items = [
      candidate,
      {
        ...candidate,
        ordinal: 60,
        observation: { ...candidate.observation, score: 99 },
      },
    ];
    candidates.total = 2;
    const candidateSelection = {
      ...selection,
      record_kind: "candidate" as const,
      record_id: "B",
    };
    expect(
      selectedRecord(candidateSelection, detail(), candidates)?.missing_reason,
    ).toContain("ambiguous");
    expect(
      selectedRecord(
        { ...candidateSelection, record_ordinal: 60 },
        detail(),
        candidates,
      )?.value,
    ).toEqual(candidates.items[1]);
  });
  it("cannot certify uniqueness from a partial page for old unordinaled links", () => {
    const recorded = detail();
    recorded.predicates.total = 101;
    expect(
      selectedRecord(
        { ...selection, record_kind: "predicate", record_id: "liquid" },
        recorded,
        undefined,
      )?.missing_reason,
    ).toContain("ambiguous");
    const candidates = candidatePage();
    candidates.total = 101;
    expect(
      selectedRecord(
        { ...selection, record_kind: "candidate", record_id: "B" },
        detail(),
        candidates,
      )?.missing_reason,
    ).toContain("ambiguous");
  });
  it("never falls back from an unmapped actual record to nearby rule source", () => {
    const current = selectedRecord(
      { ...selection, record_kind: "predicate", record_id: "liquid" },
      detail(),
      undefined,
    );
    expect(current?.source_ref).toBeNull();
    expect(
      selectedRecord(
        { ...selection, record_kind: "predicate", record_id: "other" },
        detail(),
        undefined,
      )?.missing_reason,
    ).toContain("not present");
    expect(
      selectedRecord(
        { ...selection, record_kind: "candidate", record_id: "B" },
        detail(),
        candidatePage(),
      )?.source_ref,
    ).toEqual(rule.source_ref);
  });
});
