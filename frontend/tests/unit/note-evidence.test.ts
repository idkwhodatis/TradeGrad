import { afterEach, describe, expect, it, vi } from "vitest";
import * as sdk from "../../src/client/sdk.gen";
import {
  captureNoteContext,
  noteRevisionMatches,
} from "../../src/lib/note-evidence";
import { leafSelection } from "../../src/lib/workbench-evidence";
import { detail, hashes, ids, leaf, spec } from "./workbench-fixtures";
import { comparisonSelection, result, status } from "./comparison-fixtures";
import {
  controlledExperiment,
  independentJob,
  verificationHashes,
  verificationIds,
} from "./verification-fixtures";
import { noteRevision, noteSelection } from "./note-fixtures";
const sourceSelection = {
  code_sha256: hashes.code,
  spec_revision: 1,
  spec_sha256: hashes.spec,
};
const selected = leafSelection(sourceSelection, leaf);
afterEach(() => vi.restoreAllMocks());
describe("pure typed note capture boundaries (SDK mocks, not real admission)", () => {
  it("starts pending and captures an exact rule/source without inventing an Evaluation", async () => {
    const content = await captureNoteContext(
      { ...sourceSelection, rule_id: "filter", stage_id: "liquidity" },
      spec,
    );
    expect(content.status).toBe("pending");
    expect(content.evidence).toHaveLength(1);
    expect(content.evidence?.[0]).toMatchObject({
      kind: "source",
      source_id: ids.strategy,
      code_sha256: hashes.code,
      spec_revision: 1,
      spec_sha256: hashes.spec,
      source_ref: spec.spec.rules[0].source_ref,
    });
    expect(content.restore?.selection.stage_id).toBe("liquidity");
  });
  it("captures physical leaf, exact decision/instrument/stage/ordinal and complete selected view", async () => {
    const selection = {
      ...selected,
      tab: "notes" as const,
      note_origin_tab: "replay" as const,
      decision_id: "decision-2",
      instrument_id: "B",
      stage_id: "liquidity",
      session_date: "2024-01-02",
      record_kind: "predicate" as const,
      record_id: "filter",
      record_ordinal: 2,
      detail_offset: 50,
      eligibility: "ineligible" as const,
    };
    const observed = detail();
    observed.decision!.decision_id = "decision-2";
    observed.predicates.items![0].ordinal = 2;
    observed.predicates.items![0].observation.condition_id = "filter";
    observed.predicates.items![0].observation.source_ref =
      spec.spec.rules[0].source_ref;
    vi.spyOn(sdk, "readTraceDecision").mockResolvedValue({
      data: observed,
    } as never);
    const content = await captureNoteContext(selection, spec, leaf, true);
    expect(content.evidence?.[1]).toMatchObject({
      kind: "decision",
      leaf: {
        evaluation_id: ids.evaluation,
        execution_attempt_id: ids.attempt,
        result_manifest_sha256: hashes.result,
        trace_manifest_sha256: hashes.trace,
      },
      decision_id: "decision-2",
      instrument_id: "B",
      stage_id: "liquidity",
      record_ordinal: 2,
      source_ref: spec.spec.rules[0].source_ref,
    });
    expect(content.restore?.selection).toMatchObject({
      tab: "replay",
      detail_offset: 50,
      eligibility: "ineligible",
      record_ordinal: 2,
    });
    expect(content.restore?.selection).not.toHaveProperty("note_origin_tab");
  });
  it.each([
    { code_sha256: "f".repeat(64) },
    { spec_revision: 2 },
    { spec_sha256: "f".repeat(64) },
  ])("rejects changed original source pins: %j", async (patch) => {
    await expect(
      captureNoteContext({ ...sourceSelection, ...patch }, spec),
    ).rejects.toThrow("No latest revision");
  });
  it("requires admitted successful physical evidence and never guesses missing rule", async () => {
    await expect(
      captureNoteContext(selected, spec, leaf, false),
    ).rejects.toThrow("admitted");
    await expect(
      captureNoteContext({ ...sourceSelection, rule_id: "missing" }, spec),
    ).rejects.toThrow("selected rule is missing");
  });
  it("pins both comparison sides and explicit event/section while verifying immutable response", async () => {
    vi.spyOn(sdk, "readComparison").mockResolvedValue({
      data: status(),
    } as never);
    vi.spyOn(sdk, "readComparisonResult").mockResolvedValue({
      data: result(),
    } as never);
    const content = await captureNoteContext(
      {
        ...selected,
        ...comparisonSelection,
        event_pair_id: "pair-1",
        comparison_section: "candidates",
        comparison_instrument_id: "B",
        mechanism_index: 0,
      },
      spec,
      leaf,
      true,
    );
    const comparison = content.evidence?.find(
      (ref) => ref.kind === "comparison",
    );
    expect(comparison).toMatchObject({
      left: status().request.left,
      right: status().request.right,
      input_sha256: status().input_sha256,
      event_pair_id: "pair-1",
      section: "candidates",
      instrument_id: "B",
      mechanism_index: 0,
    });
    vi.spyOn(sdk, "readComparison").mockResolvedValue({
      data: { ...status(), manifest_sha256: "f".repeat(64) },
    } as never);
    await expect(
      captureNoteContext(
        { ...selected, ...comparisonSelection },
        spec,
        leaf,
        true,
      ),
    ).rejects.toThrow("not retargeted");
  });
  it("retains failed arms with their stopped attempt and generation, never zero profit", async () => {
    const control = controlledExperiment();
    vi.spyOn(sdk, "readControlledExperiment").mockResolvedValue({
      data: control,
    } as never);
    const failedAttempt = crypto.randomUUID();
    vi.spyOn(sdk, "readIndependentEvaluation").mockImplementation(
      async (options) =>
        ({
          data:
            options.path.run_id === verificationIds.run
              ? {
                  ...independentJob(),
                  purpose: "controlled",
                  config: { ...independentJob().config, purpose: "controlled" },
                }
              : {
                  ...independentJob(),
                  id: verificationIds.armRun,
                  purpose: "controlled",
                  status: "failed",
                  active_attempt_id: failedAttempt,
                  evaluation_id: null,
                  result_pin: null,
                  result_ready: false,
                  config: { ...independentJob().config, purpose: "controlled" },
                },
        }) as never,
    );
    vi.spyOn(sdk, "readStrategySpec").mockResolvedValue({
      data: {
        ...spec,
        resource_id: verificationIds.strategy,
        strategy_version_id: verificationIds.strategy,
      },
    } as never);
    const content = await captureNoteContext(
      {
        ...selected,
        controlled_experiment_id: control.id,
        controlled_request_sha256: control.request_sha256,
        controlled_protocol_sha256: control.protocol_sha256,
        controlled_arm: 1,
        controlled_arm_run_id: verificationIds.armRun,
      },
      spec,
      leaf,
      true,
    );
    const ref = content.evidence?.find((ref) => ref.kind === "controlled");
    expect(ref).toMatchObject({
      kind: "controlled",
      selected_arm: 1,
      arms: [
        { status: "succeeded" },
        {
          status: "failed",
          generation: 1,
          execution_attempt_id: failedAttempt,
          result: null,
        },
      ],
    });
    expect(content.status).toBe("pending");
  });
  it("does not replace a saved diagnostic result by a newer physical attempt", async () => {
    vi.spyOn(sdk, "readIndependentEvaluation").mockResolvedValue({
      data: independentJob(),
    } as never);
    await expect(
      captureNoteContext(
        {
          ...selected,
          diagnostic_run_id: verificationIds.run,
          diagnostic_config_sha256: verificationHashes.config,
          diagnostic_report_sha256: verificationHashes.report,
          diagnostic_evaluation_id: verificationIds.evaluation,
          diagnostic_execution_attempt_id: crypto.randomUUID(),
          diagnostic_result_manifest_sha256: verificationHashes.result,
        },
        spec,
        leaf,
        true,
      ),
    ).rejects.toThrow("No newer attempt");
  });
  it("matches the note ID, revision and hash together", () => {
    expect(noteRevisionMatches(noteSelection, noteRevision())).toBe(true);
    expect(
      noteRevisionMatches(
        { ...noteSelection, note_revision: 2 },
        noteRevision(),
      ),
    ).toBe(false);
    expect(
      noteRevisionMatches(
        { ...noteSelection, note_id: ids.strategy },
        noteRevision(),
      ),
    ).toBe(false);
    expect(
      noteRevisionMatches(
        { ...noteSelection, note_revision_sha256: hashes.code },
        noteRevision(),
      ),
    ).toBe(false);
  });
});
