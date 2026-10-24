import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type {
  CandidateObservation,
  CandidatePage,
  TraceManifestPublic,
} from "../../src/client/types.gen";
import {
  CandidateObservations,
  ConditionsAndFeatures,
  OrderObservation,
  PortfolioAndExecution,
  RecordedCollection,
} from "../../src/features/research-workbench/replay-panels";
import { replayAdmitted } from "../../src/lib/replay-evidence";
import { detail, hashes, ids, leaf } from "./workbench-fixtures";
const selection = {
  code_sha256: hashes.code,
  evaluation_id: ids.evaluation,
  execution_attempt_id: ids.attempt,
  result_manifest_sha256: hashes.result,
  trace_manifest_sha256: hashes.trace,
  session_date: "2024-01-02",
  decision_id: "decision-1",
  instrument_id: "B",
  stage_id: "liquidity",
};
const update = () => undefined;
const render = (node: ReturnType<typeof createElement>) => renderToString(node);
const noNetwork = vi.fn(() => {
  throw new Error("SSR projection must not access network");
});
beforeEach(() => {
  vi.stubGlobal("window", { location: { origin: "https://replay.test" } });
  vi.stubGlobal("sessionStorage", {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
  vi.stubGlobal("fetch", noNetwork);
  noNetwork.mockClear();
});
afterEach(() => vi.unstubAllGlobals());
describe("replay renders saved evidence rather than inferred explanations", () => {
  it.each([
    "timing_not_triggered",
    "outside_universe",
    "feature_unavailable",
    "eligibility_filter",
    "rank_not_selected",
    "holding_retained",
    "position_slots",
    "portfolio_constraint",
    "cash_constraint",
    "order_rejected",
    "order_unfilled",
    "partial_fill",
    "not_evaluated",
    "not_collected",
    "not_sampled",
  ] as const)("shows distinct non-trade reason %s", (reason) => {
    const observation: CandidateObservation = {
      instrument_id: "B",
      eligibility: "ineligible",
      reason,
      reason_detail: "Saved reason, not computed by browser",
      score: 0,
      score_validity: "valid",
      selected: false,
      prior_holding: null,
      universe_member: null,
      source: "strategy_emitted",
    };
    const page: CandidatePage = {
      context: detail().context,
      decision_id: "decision-1",
      items: [{ ordinal: 0, stage_id: "liquidity", observation }],
      total: 101,
      offset: 50,
      limit: 50,
    };
    const html = render(
      createElement(CandidateObservations, { page, selection, update }),
    );
    expect(html).toContain(reason);
    expect(html).toContain("Saved reason");
    expect(html).toContain("Not recorded");
    expect(html).toContain("false");
    expect(html).toContain("101 matching saved records");
    expect(html).toContain("not proof of historical point-in-time membership");
    expect(html).toContain("Code anchor unknown");
  });
  it("shows actual predicate operands/operator/threshold/state and feature unit/window/cutoff/provenance", () => {
    const html = render(
      createElement(ConditionsAndFeatures, {
        detail: detail(),
        selection,
        update,
      }),
    );
    for (const text of [
      "liquid",
      "gte",
      "50",
      "false",
      "volume",
      "shares",
      "valid",
      "1 day",
      "2024-01-02T07:00:00Z",
      "Asia/Shanghai",
      "strategy_emitted",
      "Code anchor unknown",
    ])
      expect(html).toContain(text);
  });
  it.each([
    "true",
    "false",
    "not_evaluated",
    "unknown",
    "not_sampled",
  ] as const)("retains %s predicate state", (state) => {
    const value = detail();
    value.predicates.items![0].observation.state = state;
    expect(
      render(
        createElement(ConditionsAndFeatures, {
          detail: value,
          selection,
          update,
        }),
      ),
    ).toContain(state);
  });
  it.each([
    "warmup_insufficient",
    "input_missing",
    "calculation_error",
    "not_collected",
    "not_sampled",
  ] as const)("retains %s feature validity and missing value", (validity) => {
    const value = detail();
    value.features.items![0].observation.validity = validity;
    value.features.items![0].observation.value = null;
    const html = render(
      createElement(ConditionsAndFeatures, {
        detail: value,
        selection,
        update,
      }),
    );
    expect(html).toContain(validity);
    expect(html).toContain("Not recorded");
  });
  it("does not turn missing pre-state into an empty or zero account", () => {
    const html = render(
      createElement(PortfolioAndExecution, {
        detail: detail(),
        selection,
        update,
      }),
    );
    expect(html).toContain("Missing state is not zero cash or zero positions");
    expect(html).not.toContain("Available cash");
    expect(html).not.toContain("Observed empty []");
  });
  it("renders true observed-empty positions/openorders and zero cash without losing copied strategy state", () => {
    const value = detail();
    value.decision!.pre_state = {
      cash: 0,
      equity: 0,
      positions: [],
      open_orders: [],
      strategy_state: { remaining: false },
      source: "engine_observed",
      coverage: "complete",
    };
    value.stages![0].raw_targets = [];
    value.stages![0].constrained_plan = [];
    const html = render(
      createElement(PortfolioAndExecution, {
        detail: value,
        selection,
        update,
      }),
    );
    expect(html).toContain("Available cash");
    expect(html).toContain("Observed empty []");
    expect(html).toContain("remaining");
    expect(html).toContain("false");
    expect(html).toContain("Raw targets");
    expect(html).toContain("Constrained plan");
  });
  it.each(["rejected", "unfilled", "partially_filled"] as const)(
    "renders explicit %s order-to-child-to-fill evidence",
    (status) => {
      const html = render(
        createElement(OrderObservation, {
          order: {
            logical_order_id: "intent-1",
            instrument_id: "B",
            status,
            child_order_ids: ["child-1", "child-2"],
            fill_ids: ["fill-1", "fill-2"],
            fill_times: ["2024-01-03T01:31:00Z", "2024-01-03T01:32:00Z"],
            submitted_at: "2024-01-02T07:00:00Z",
            rejection_reason: status === "rejected" ? "cash_constraint" : null,
            source: "engine_observed",
          },
          stage: "allocation",
          timezone: "Asia/Shanghai",
          update,
        }),
      );
      for (const text of [
        status,
        "intent-1",
        "child-1",
        "child-2",
        "fill-1",
        "fill-2",
        "2024-01-02T07:00:00Z",
        "2024-01-03T01:31:00Z",
        "Asia/Shanghai",
      ])
        expect(html).toContain(text);
    },
  );
  it("keeps explicit null/empty raw collections distinct", () => {
    expect(
      render(
        createElement(RecordedCollection, { title: "Targets", value: null }),
      ),
    ).toContain("Not recorded");
    expect(
      render(
        createElement(RecordedCollection, { title: "Targets", value: [] }),
      ),
    ).toContain("Observed empty []");
  });
});
describe("unavailable trace gating", () => {
  it.each([true, false])(
    "does not query off/missing trace with registered manifest=%s",
    async (registered) => {
      const { DecisionReplay } =
        await import("../../src/features/research-workbench/decision-replay");
      const query = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      const trace: TraceManifestPublic = {
        context: {
          ...detail().context,
          status: "not_collected",
          trace_manifest_sha256: registered ? hashes.trace : null,
          missing_reason: "Capture disabled for this exact execution.",
        },
        manifest: registered
          ? {
              schema_version: "tradegrad.decision-trace.v2",
              binding: detail().context.binding!,
              capture: { mode: "off" },
              coverage: "not_collected",
              artifacts: [],
              decisions_saved: 0,
              decisions_seen: 0,
              detail_rows_saved: 0,
              detail_rows_seen: 0,
              missing_reasons: ["capture off"],
              source: "adapter_observed",
              stage_coverage: {},
              trust_boundary: "Same-process observation",
            }
          : null,
      };
      expect(replayAdmitted(selection, trace, true)).toBe(false);
      const html = render(
        createElement(
          QueryClientProvider,
          { client: query },
          createElement(DecisionReplay, {
            selection: {
              ...selection,
              trace_manifest_sha256: registered ? hashes.trace : undefined,
            },
            update,
            leaf,
            trace,
            verified: true,
            code: "pass",
            codeSha256: hashes.code,
          }),
        ),
      );
      expect(html).toContain("Capture disabled for this exact execution");
      expect(html).toContain("Inspect independent diagnostic capability");
      expect(html).not.toContain("Stale or mismatched");
      expect(html).not.toContain("replay-candidates-table");
      expect(noNetwork).not.toHaveBeenCalled();
      query.clear();
    },
  );
});
