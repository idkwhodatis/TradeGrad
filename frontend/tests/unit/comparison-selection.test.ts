import { describe, expect, it } from "vitest";
import {
  comparisonRequest,
  comparisonStatusProblem,
  comparisonResultProblem,
  sideFromEvidence,
  sideSelectionProblem,
  comparisonLeafProblem,
  comparisonReplayTarget,
} from "../../src/lib/comparison-evidence";
import {
  comparisonSelection,
  comparisonBody,
  status,
  result,
  eventRow,
  rightId,
} from "./comparison-fixtures";
import { hashes, ids, leaf, spec } from "./workbench-fixtures";
import {
  workbenchSearch,
  comparisonSideSelection,
  comparisonSidePatch,
  clearComparisonSide,
  clearComparisonResult,
  changeSelection,
} from "../../src/lib/workbench-selection";
describe("ordered immutable comparison URL state", () => {
  it("restores both full sides, exact artifact, pair, section and independent pages", () => {
    const saved = {
      ...comparisonSelection,
      event_pair_id: "pair-99",
      comparison_section: "predicates" as const,
      comparison_offset: 50,
      comparison_event_offset: 25,
      comparison_instrument_id: "B",
      mechanism_index: 0,
    };
    expect(workbenchSearch(JSON.parse(JSON.stringify(saved)))).toEqual(saved);
    expect(changeSelection(saved, { tab: "notes" })).toEqual({
      ...saved,
      tab: "notes",
    });
  });
  it.each(["left", "right"] as const)(
    "rejects malformed %s hashes, revisions, types and trace markers",
    (side) => {
      for (const [key, value] of Object.entries({
        code_sha256: "bad",
        result_manifest_sha256: "latest",
        trace_manifest_sha256: "",
        spec_sha256: false,
        spec_revision: true,
        source_kind: "any",
        trace_status: "latest",
      }))
        expect(() => workbenchSearch({ [`${side}_${key}`]: value })).toThrow();
    },
  );
  it.each([
    { comparison_section: "all" },
    { comparison_offset: -1 },
    { comparison_event_offset: 100001 },
    { mechanism_index: 1.5 },
    { comparison_manifest_sha256: "latest" },
  ])("rejects malformed independent selection %j", (input) =>
    expect(() => workbenchSearch(input)).toThrow(),
  );
  it("does not infer either side from the open source", () =>
    expect(() =>
      comparisonRequest({
        ...comparisonSelection,
        right_evaluation_id: undefined,
      }),
    ).toThrow("Incomplete"));
  it("requires the source root and exact revision instead of latest description", () => {
    for (const key of [
      "left_strategy_version_id",
      "left_spec_sha256",
      "right_code_sha256",
      "right_trace_status",
    ] as const)
      expect(() =>
        comparisonRequest({ ...comparisonSelection, [key]: undefined }),
      ).toThrow();
  });
  it("pins verified historical absence as explicit JSON null and preserves off-mode hash", () => {
    const absent = sideFromEvidence(spec, {
      ...leaf,
      trace_manifest_sha256: null,
    });
    const selected = {
      ...comparisonSelection,
      ...clearComparisonSide("left"),
      ...comparisonSidePatch("left", absent),
    };
    expect(comparisonRequest(selected).left.trace_manifest_sha256).toBeNull();
    expect(comparisonBody.right.trace_manifest_sha256).toBeTruthy();
    expect(
      sideSelectionProblem({ ...absent, trace_manifest_sha256: hashes.trace }),
    ).toContain("Historical absence");
    expect(comparisonLeafProblem(absent, leaf)).toContain("registered trace");
  });
  it("preserves declared side ordering, never sorting by source or metric", () => {
    const swapped = {
      ...comparisonSelection,
      ...comparisonSidePatch(
        "left",
        comparisonSideSelection(comparisonSelection, "right"),
      ),
      ...comparisonSidePatch(
        "right",
        comparisonSideSelection(comparisonSelection, "left"),
      ),
    };
    expect(comparisonRequest(swapped).left).toEqual(comparisonBody.right);
    expect(comparisonStatusProblem(swapped, status())).toContain("ordered");
  });
  it("clears dependent pair/result but leaves the other source intact on deliberate source change", () => {
    const next = changeSelection(
      { ...comparisonSelection, event_pair_id: "pair-1" },
      { ...clearComparisonSide("left"), ...clearComparisonResult() },
    );
    expect(next.left_evaluation_id).toBeUndefined();
    expect(next.comparison_id).toBeUndefined();
    expect(next.event_pair_id).toBeUndefined();
    expect(next.right_evaluation_id).toBe(
      comparisonSelection.right_evaluation_id,
    );
  });
  it("rejects source association mismatch even when code hashes are identical", () => {
    expect(() =>
      sideFromEvidence(spec, {
        ...leaf,
        source_association: {
          ...leaf.source_association,
          strategy_basis: "unavailable",
          strategy_version_id: null,
        },
      }),
    ).toThrow("does not belong");
    expect(
      comparisonLeafProblem(
        comparisonSideSelection(comparisonSelection, "left"),
        { ...leaf, evaluation_id: rightId },
      ),
    ).toContain("returned leaf");
  });
});
describe("response admission never displays stale or substituted comparison", () => {
  it("binds the declared algorithm while retaining exact legacy results", () => {
    const legacyStatus = status();
    const legacyResult = result();
    legacyStatus.algorithm_version = "tradegrad.observed-comparison.v1";
    expect(
      comparisonResultProblem(comparisonSelection, legacyStatus, legacyResult),
    ).toBeTruthy();
    legacyResult.manifest.algorithm_version = legacyStatus.algorithm_version;
    expect(
      comparisonResultProblem(comparisonSelection, legacyStatus, legacyResult),
    ).toBeNull();
  });
  it("accepts exact ordered status and result", () => {
    expect(comparisonStatusProblem(comparisonSelection, status())).toBeNull();
    expect(
      comparisonResultProblem(comparisonSelection, status(), result()),
    ).toBeNull();
  });
  it.each(["id", "manifest_sha256"] as const)(
    "rejects changed status %s",
    (key) =>
      expect(
        comparisonStatusProblem(comparisonSelection, {
          ...status(),
          [key]: "wrong",
        }),
      ).toBeTruthy(),
  );
  it.each([
    "comparison_id",
    "input_sha256",
    "manifest_sha256",
    "manifest_artifact_id",
  ] as const)("rejects changed result %s", (key) =>
    expect(
      comparisonResultProblem(comparisonSelection, status(), {
        ...result(),
        [key]: "wrong",
      }),
    ).toBeTruthy(),
  );
  it.each(["comparison_id", "run_id", "execution_attempt_id"] as const)(
    "rejects changed comparison publication %s",
    (key) => {
      const altered = result();
      altered.manifest.publication[key] = ids.strategy;
      expect(
        comparisonResultProblem(comparisonSelection, status(), altered),
      ).toBeTruthy();
    },
  );
  it.each(["left", "right"] as const)(
    "rejects each mismatched %s source or leaf pin",
    (side) => {
      for (const key of [
        "evaluation_id",
        "execution_attempt_id",
        "result_manifest_sha256",
        "code_sha256",
      ] as const) {
        const altered = result();
        altered.manifest[side].binding[key] = "wrong";
        expect(
          comparisonResultProblem(comparisonSelection, status(), altered),
        ).toBeTruthy();
      }
      for (const key of [
        "source_id",
        "spec_sha256",
        "trace_manifest_sha256",
      ] as const) {
        const altered = result();
        altered.manifest[side][key] = "wrong";
        expect(
          comparisonResultProblem(comparisonSelection, status(), altered),
        ).toBeTruthy();
      }
    },
  );
  it("rejects a missing pinned spec, stale source root and wrong revision", () => {
    const missing = result();
    missing.left_source.spec = null;
    expect(
      comparisonResultProblem(comparisonSelection, status(), missing),
    ).toBeTruthy();
    const wrong = result();
    wrong.right_source.source_strategy_version_id = ids.strategy;
    expect(
      comparisonResultProblem(comparisonSelection, status(), wrong),
    ).toBeTruthy();
    const revision = result();
    revision.left_source.spec!.revision = 2;
    expect(
      comparisonResultProblem(comparisonSelection, status(), revision),
    ).toBeTruthy();
  });
  it("links each original decision identity with source root and all hashes, not date or shared ID", () => {
    const left = comparisonReplayTarget(result(), "left", eventRow())!;
    const right = comparisonReplayTarget(result(), "right", eventRow())!;
    expect(left.search.decision_id).toBe("decision-1");
    expect(right.search.decision_id).toBe("right-decision-7");
    expect(left.search.execution_attempt_id).toBe(ids.attempt);
    expect(left.search.trace_manifest_sha256).toBe(hashes.trace);
    expect(right.params.strategyId).toBe(rightId);
    expect(right.search.code_sha256).toBe(comparisonBody.right.code_sha256);
  });
  it("does not link unmatched side or non-event records by proximity", () => {
    expect(
      comparisonReplayTarget(result(), "right", { ...eventRow(), right: null }),
    ).toBeNull();
    expect(
      comparisonReplayTarget(result(), "left", {
        ...eventRow(),
        section: "orders",
      }),
    ).toBeNull();
  });
});
