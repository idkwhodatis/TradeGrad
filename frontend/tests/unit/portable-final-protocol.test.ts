import { describe, expect, it } from "vitest";
import {
  acceptedPortableFinalReference,
  portableCheckpointMatches,
  portableFinalControls,
  portableFinalPreflightMatches,
  portableFinalResultMatches,
  portableFreezeUnavailable,
  portableSelectionMatches,
} from "../../src/features/portable/final-protocol";
import {
  rejectedPortableFinalReceipts,
  portableCheckpointFixture,
  portableFinalFixture,
  portableFinalPreflightFixture,
  portableFinalResultFixture,
  portableFrozenRunFixture,
  portableSelectionFixture,
} from "./portable-final-fixtures";
describe("portable-only final identities (local contracts, not runtime acceptance)", () => {
  it("requires successful portable optimization with frozen holdout and stopped execution", () => {
    const run = portableFrozenRunFixture();
    expect(portableFreezeUnavailable(run)).toBeNull();
    for (const invalid of [
      { ...run, status: "running" as const },
      { ...run, split_version_id: null },
      { ...run, latest_checkpoint_id: null },
      { ...run, execution_mode: "seed_evaluation" as const },
      {
        ...run,
        config: {
          ...run.config,
          windows: { ...run.config.windows, holdout: null },
        },
      },
    ])
      expect(portableFreezeUnavailable(invalid)).toBeTruthy();
    const checkpoint = portableCheckpointFixture();
    expect(portableCheckpointMatches(run, checkpoint)).toBe(true);
    expect(
      portableCheckpointMatches(
        { ...run, generation: run.generation + 1 },
        checkpoint,
      ),
    ).toBe(true);
    for (const patch of [
      { complete: false },
      { run_id: "another" },
      { id: "another" },
      { generation: 22 },
    ])
      expect(portableCheckpointMatches(run, { ...checkpoint, ...patch })).toBe(
        false,
      );
  });
  it("rejects native selection and changed frozen source, dates or deployment environment", () => {
    const selection = portableSelectionFixture(),
      preflight = portableFinalPreflightFixture();
    expect(portableSelectionMatches(selection)).toBe(true);
    expect(
      portableSelectionMatches({
        ...selection,
        selection_evidence: {
          source_config: { schema_version: "tradegrad.run.v1" },
        },
      }),
    ).toBe(false);
    expect(portableFinalPreflightMatches(preflight, selection)).toBe(true);
    for (const mutate of [
      (p: typeof preflight) => {
        p.config.selected_strategy.sha256 = "a".repeat(64);
      },
      (p: typeof preflight) => {
        p.config.holdout.start = "2024-01-01";
      },
      (p: typeof preflight) => {
        p.config.environment_digest = "a".repeat(64);
      },
      (p: typeof preflight) => {
        p.config.source_config.budgets!.max_model_calls = 999;
      },
      (p: typeof preflight) => {
        p.selection_id = "another";
      },
    ]) {
      const changed = structuredClone(preflight);
      mutate(changed);
      expect(portableFinalPreflightMatches(changed, selection)).toBe(false);
    }
  });
  it("requires exact final artifact schema, job, selection, both actual images, window and bounded section", () => {
    const job = portableFinalFixture(),
      result = portableFinalResultFixture();
    expect(
      portableFinalResultMatches(result, job, "account_series", 0, 50),
    ).toBe(true);
    for (const patch of [
      { schema_version: "tradegrad.portable-result.v1" },
      { result_schema_version: "native" },
      { runtime_certified: true },
      { selection_feedback: true },
      { selection_id: "another" },
      { run_id: "another" },
      { evaluation_id: "another" },
      { image_digest: "another" },
      { policy_image_digest: "another" },
      { offset: 50 },
      { limit: 500 },
      { count: -1 },
      { window: { ...job.config.holdout, split: "holdout" } },
      { section: "decision_trace" },
    ])
      expect(
        portableFinalResultMatches(
          { ...result, ...patch } as typeof result,
          job,
          "account_series",
          0,
          50,
        ),
      ).toBe(false);
    expect(
      portableFinalResultMatches(
        result,
        { ...job, status: "failed" },
        "account_series",
        0,
        50,
      ),
    ).toBe(false);
  });
  it.each(rejectedPortableFinalReceipts())(
    "rejects cached results with %s",
    (_label, job) => {
      expect(acceptedPortableFinalReference(job)).toBeNull();
      expect(
        portableFinalResultMatches(
          portableFinalResultFixture(),
          job,
          "account_series",
          0,
          50,
        ),
      ).toBe(false);
    },
  );
  it("selects only the successful stopped current-generation receipt", () => {
    const job = portableFinalFixture();
    expect(acceptedPortableFinalReference(job)).toBe(job.result!.evaluation_id);
  });
  it("only retries failed/cancelled/interrupted jobs after complete stop, never pauses", () => {
    const job = portableFinalFixture();
    expect(portableFinalControls(job)).toMatchObject({
      cancel: false,
      retry: false,
    });
    expect(
      portableFinalControls({ ...job, status: "cancel_requested" }),
    ).toMatchObject({ cancel: false, retry: false });
    expect(portableFinalControls({ ...job, status: "failed" })).toMatchObject({
      retry: true,
    });
    expect(
      portableFinalControls({
        ...job,
        status: "failed",
        attempts: [{ sandbox_stopped: false } as (typeof job.attempts)[number]],
      }),
    ).toMatchObject({ retry: false });
    expect(portableFinalControls(job)).not.toHaveProperty("pause");
  });
});
