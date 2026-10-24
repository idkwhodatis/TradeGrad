import { describe, expect, it } from "vitest";
import {
  workbenchSearch,
  selectionProblem,
  resourceSelection,
  changeSelection,
  tracePins,
} from "../../src/lib/workbench-selection";
import {
  evidenceMatches,
  leafBindingProblem,
  leafSelection,
  recordedValue,
  ruleObservation,
  workbenchRunTarget,
} from "../../src/lib/workbench-evidence";
import { detail, hashes, ids, leaf, rule } from "./workbench-fixtures";
const selection = {
  tab: "description" as const,
  candidate_id: ids.candidate,
  code_sha256: hashes.code,
  evaluation_id: ids.evaluation,
  execution_attempt_id: ids.attempt,
  result_manifest_sha256: hashes.result,
  trace_manifest_sha256: hashes.trace,
  decision_id: "decision-1",
  instrument_id: "B",
  stage_id: "liquidity",
  rule_id: "filter",
  spec_revision: 1,
  spec_sha256: hashes.spec,
};
describe("immutable workbench route selection", () => {
  it("restores every exact pin, including comparison identity", () => {
    const value = {
      ...selection,
      left_evaluation_id: ids.evaluation,
      right_evaluation_id: ids.strategy,
      comparison_id: ids.run,
      event_pair_id: "event-pair-2",
    };
    expect(workbenchSearch(value)).toEqual(value);
  });
  it("supports the explicit eval_id alias without replacing canonical selection", () => {
    expect(workbenchSearch({ eval_id: ids.evaluation }).evaluation_id).toBe(
      ids.evaluation,
    );
    expect(
      workbenchSearch({ eval_id: ids.strategy, evaluation_id: ids.evaluation })
        .evaluation_id,
    ).toBe(ids.evaluation);
  });
  it.each([
    { result_manifest_sha256: "bad" },
    { spec_revision: 0 },
    { spec_revision: "1.5" },
    { stage_id: "" },
    { evaluation_id: {} },
    { tab: "invented" },
  ])("rejects malformed pins rather than silently dropping them: %j", (value) =>
    expect(() => workbenchSearch(value)).toThrow(),
  );
  it("requires paired description pins and rejects incomplete successful result pins", () => {
    expect(selectionProblem({ spec_revision: 1 })).toContain("both");
    expect(selectionProblem({ spec_sha256: hashes.spec })).toContain("both");
    expect(
      selectionProblem({
        evaluation_id: ids.evaluation,
        result_manifest_sha256: hashes.result,
      }),
    ).toContain("Incomplete");
    expect(selectionProblem(selection)).toBeNull();
  });
  it("can pin a failed attempt without inventing a result", () =>
    expect(
      selectionProblem({
        evaluation_id: ids.evaluation,
        execution_attempt_id: ids.attempt,
      }),
    ).toBeNull());
  it("retains every pin on tab/rule changes and clears them on a deliberate source change", () => {
    expect(changeSelection(selection, { tab: "notes" })).toEqual({
      ...selection,
      tab: "notes",
    });
    expect(resourceSelection(selection, ids.strategy)).toEqual({
      tab: "description",
      candidate_id: ids.strategy,
    });
  });
  it("copies immutable artifact pins into API requests", () =>
    expect(tracePins(selection)).toEqual({
      execution_attempt_id: ids.attempt,
      result_manifest_sha256: hashes.result,
      trace_manifest_sha256: hashes.trace,
    }));
  it("changes the physical leaf without retaining an unrelated decision or description source", () => {
    const next = leafSelection(selection, { ...leaf, evaluation_id: ids.run });
    expect(next.evaluation_id).toBe(ids.run);
    expect(next.candidate_id).toBe(ids.candidate);
    expect(next.spec_sha256).toBe(hashes.spec);
    expect(next.decision_id).toBeUndefined();
    expect(next.stage_id).toBeUndefined();
    expect(next.trace_status).toBe("available");
  });
  it("freezes absent trace explicitly instead of opening later records silently", () => {
    const next = leafSelection(selection, {
      ...leaf,
      trace_manifest_sha256: null,
    });
    expect(next.trace_status).toBe("not_collected");
    expect(next.trace_manifest_sha256).toBeUndefined();
    expect(
      selectionProblem({ ...next, trace_manifest_sha256: hashes.trace }),
    ).toBeNull();
  });
  it("rejects cross-candidate or changed artifact bindings", () => {
    expect(leafBindingProblem(selection, ids.strategy, leaf)).toBeNull();
    expect(
      leafBindingProblem(selection, ids.strategy, {
        ...leaf,
        candidate_id: ids.run,
        source_association: {
          ...leaf.source_association,
          candidate_id: ids.run,
        },
      }),
    ).toContain("does not belong");
    expect(
      leafBindingProblem(selection, ids.strategy, {
        ...leaf,
        result_manifest_sha256: "e".repeat(64),
      }),
    ).toContain("not silently retargeted");
  });
  it("accepts the same actual candidate in a distinct final selected strategy, not another candidate", () =>
    expect(
      leafBindingProblem(selection, ids.strategy, {
        ...leaf,
        strategy_version_id: ids.run,
      }),
    ).toBeNull());
  it("rejects wrong trace/attempt/source even when a response is present", () => {
    expect(evidenceMatches(selection, detail().context)).toBe(true);
    for (const key of [
      "execution_attempt_id",
      "result_manifest_sha256",
      "trace_manifest_sha256",
    ] as const)
      expect(
        evidenceMatches(selection, { ...detail().context, [key]: "wrong" }),
      ).toBe(false);
    expect(
      evidenceMatches(
        { ...selection, code_sha256: "e".repeat(64) },
        detail().context,
      ),
    ).toBe(false);
  });
});
describe("observations never arise from the expected graph", () => {
  it("distinguishes no selection from an absent recorded stage", () => {
    expect(ruleObservation(rule).status).toBe("No selected recorded stage");
    expect(ruleObservation(rule, { ...detail(), stages: [] }).status).toContain(
      "branch outcome unknown",
    );
  });
  it("does not reuse another stage or decision as evidence", () => {
    expect(
      ruleObservation({ ...rule, stage_id: "other" }, detail()).stage,
    ).toBeNull();
    expect(
      ruleObservation(rule, { ...detail(), decision: null }).stage,
    ).toBeNull();
  });
  it("shows explicit partial coverage without promoting it to a predicate result", () => {
    expect(ruleObservation(rule, detail()).status).toContain("partial");
    expect(ruleObservation(rule, detail()).status).not.toContain("true");
  });
  it("keeps false, zero and missing values distinct", () => {
    expect(recordedValue(0)).toBe("0");
    expect(recordedValue(false)).toBe("false");
    expect(recordedValue(null)).toBe("Not recorded");
  });
});

describe("actual physical run status routes", () => {
  it.each([
    ["native_research", "/research-runs/$runId"],
    ["portable_research", "/portable/runs/$runId"],
    ["native_final", "/final-tests/$finalTestId"],
    ["portable_final", "/portable/final-tests/$finalTestId"],
    ["legacy", "/runs/$runId"],
  ] as const)(
    "routes %s by the leaf's immutable run schema",
    (run_kind, expected) => {
      expect(
        workbenchRunTarget({ ...leaf, run_kind, final_test_id: ids.run })?.to,
      ).toBe(expected);
    },
  );
  it("never replaces an absent final reference with a research-run link", () =>
    expect(
      workbenchRunTarget({
        ...leaf,
        run_kind: "native_final",
        final_test_id: null,
      }),
    ).toBeNull());
});

describe("server-verified source associations remain distinct from raw leaf fields", () => {
  it("retains a null raw FK while accepting an exact same-run key/hash association", () => {
    const interrupted = {
      ...leaf,
      candidate_id: null,
      status: "interrupted",
      source_association: {
        ...leaf.source_association,
        candidate_basis: "same_run_key_hash" as const,
      },
    };
    expect(leafBindingProblem(selection, ids.strategy, interrupted)).toBeNull();
    expect(interrupted.candidate_id).toBeNull();
  });
  it("does not accept generated identical-code candidates as the saved seed version", () => {
    const generated = {
      ...leaf,
      source_association: {
        ...leaf.source_association,
        strategy_version_id: null,
        strategy_basis: "unavailable" as const,
      },
    };
    expect(
      leafBindingProblem(
        { ...selection, candidate_id: undefined },
        ids.strategy,
        generated,
      ),
    ).toContain("does not belong");
  });
  it("allows a selected final StrategyVersion through its explicit final-selection association", () => {
    const selected = {
      ...leaf,
      strategy_version_id: ids.run,
      source_association: {
        ...leaf.source_association,
        strategy_version_id: ids.run,
        strategy_basis: "selected_final" as const,
      },
    };
    expect(
      leafBindingProblem(
        { ...selection, candidate_id: undefined },
        ids.run,
        selected,
      ),
    ).toBeNull();
  });
  it("does not infer an association from matching code when the association is unavailable", () => {
    const unavailable = {
      ...leaf,
      source_association: {
        ...leaf.source_association,
        candidate_basis: "unavailable" as const,
      },
    };
    expect(leafBindingProblem(selection, ids.strategy, unavailable)).toContain(
      "does not belong",
    );
  });
});

it("accepts explicitly missing legacy trace context without inventing a code-bound observation", () => {
  const context = {
    ...detail().context,
    status: "not_collected" as const,
    binding: null,
    trace_manifest_sha256: null,
    missing_reason: "Historical schema did not collect trace.",
  };
  expect(
    evidenceMatches(
      { ...selection, trace_manifest_sha256: undefined },
      context,
    ),
  ).toBe(true);
  expect(evidenceMatches(selection, context)).toBe(false);
  expect(
    evidenceMatches(
      { ...selection, trace_manifest_sha256: undefined },
      { ...context, status: "available" },
    ),
  ).toBe(false);
});
