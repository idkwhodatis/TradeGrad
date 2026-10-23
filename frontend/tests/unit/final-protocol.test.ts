import { describe, expect, it } from "vitest";
import type {
  AttemptPublic,
  FinalEvaluationDetail,
  ResearchRunDetail,
} from "../../src/client/types.gen";
import {
  acceptedFinalReference,
  eligibleCandidates,
  exposureNotice,
  finalControls,
  finalTerminal,
  finalTrustNotice,
  freezeUnavailable,
} from "../../src/features/final-tests/protocol";
const id = "a1111111-1111-4111-8111-111111111111";
const ready = {
  status: "succeeded",
  execution_mode: "optimization",
  experiment_id: id,
  split_version_id: id,
  latest_checkpoint_id: id,
} as const;
const stopped = { sandbox_stopped: true } as AttemptPublic;
const running = { sandbox_stopped: false } as AttemptPublic;

describe("accepted native final reference", () => {
  const attempt: AttemptPublic = {
    id: "b2222222-2222-4222-8222-222222222222",
    generation: 2,
    status: "succeeded",
    sandbox_stopped: true,
    exit_code: 0,
    error: null,
    heartbeat_at: "2026-10-22T12:00:00Z",
    started_at: "2026-10-22T12:00:00Z",
    finished_at: "2026-10-22T12:00:01Z",
  };
  const job = {
    status: "succeeded",
    final_selection_id: id,
    generation: 2,
    active_attempt_id: attempt.id,
    attempts: [attempt],
    result: {
      evaluation_id: "c3333333-3333-4333-8333-333333333333",
      selection_id: id,
      holdout_executed: true,
    } as NonNullable<FinalEvaluationDetail["result"]>,
  } satisfies Parameters<typeof acceptedFinalReference>[0];

  it("takes only the current committed logical reference, not a historical choice", () => {
    expect(acceptedFinalReference(job)).toBe(job.result.evaluation_id);
    const withStaleChoice = { ...job, chosen: "historical-evaluation" };
    expect(acceptedFinalReference(withStaleChoice)).toBe(
      job.result.evaluation_id,
    );
  });
  it.each([
    "queued",
    "preparing",
    "running",
    "finalizing",
    "cancel_requested",
    "cancelled",
    "failed",
    "interrupted",
    "paused",
    "pause_requested",
  ] as const)(
    "rejects cached committed results after status changes to %s",
    (status) => {
      expect(acceptedFinalReference({ ...job, status })).toBeNull();
    },
  );
  it("rejects stale generations, replaced attempts and uncommitted outcomes", () => {
    for (const patch of [
      { generation: 3 },
      { active_attempt_id: null },
      { active_attempt_id: "new-attempt" },
      { attempts: [] },
      { result: null },
      { result: { ...job.result, evaluation_id: null } },
      { result: { ...job.result, holdout_executed: false } },
      { result: { ...job.result, selection_id: "foreign-selection" } },
    ])
      expect(acceptedFinalReference({ ...job, ...patch })).toBeNull();
    for (const patch of [
      { generation: 1 },
      { status: "failed" },
      { sandbox_stopped: false },
      { finished_at: null },
      { exit_code: null },
      { exit_code: 1 },
    ])
      expect(
        acceptedFinalReference({
          ...job,
          attempts: [{ ...attempt, ...patch }],
        }),
      ).toBeNull();
  });
});
describe("frozen selection eligibility", () => {
  it("never retroactively binds or certifies a historical run", () => {
    expect(freezeUnavailable({ ...ready, split_version_id: null })).toContain(
      "Recreate research",
    );
    expect(freezeUnavailable({ ...ready, experiment_id: null })).toContain(
      "no immutable",
    );
    expect(freezeUnavailable(ready)).toBeNull();
  });
  it("blocks seed runs and missing checkpoints", () => {
    expect(
      freezeUnavailable({ ...ready, execution_mode: "seed_evaluation" }),
    ).toContain("optimization");
    expect(
      freezeUnavailable({ ...ready, latest_checkpoint_id: null }),
    ).toContain("checkpoint");
  });
  it.each([
    "queued",
    "running",
    "failed",
    "cancelled",
    "interrupted",
    "paused",
  ] as const)("cannot freeze %s research", (status) => {
    expect(
      freezeUnavailable({ ...ready, status } as ResearchRunDetail),
    ).not.toBeNull();
  });
  it("offers exact valid complete candidates without selecting the highest score", () => {
    const other = "b2222222-2222-4222-8222-222222222222";
    const rows = eligibleCandidates({
      candidates: {
        low: { id, valid: true, complete: true, combined_score: 0 },
        high: { id: other, valid: true, complete: true, combined_score: 95 },
        failed: { id, valid: false, complete: true },
        partial: { id, valid: true, complete: false },
        malformed: { id: "wrong", valid: true, complete: true },
      },
    });
    expect(rows).toEqual([
      { key: "low", id, score: 0 },
      { key: "high", id: other, score: 95 },
    ]);
    expect(eligibleCandidates({})).toEqual([]);
    expect(
      eligibleCandidates({
        candidates: {
          unknown: {
            id,
            valid: true,
            complete: true,
            combined_score: Infinity,
          },
        },
      })[0].score,
    ).toBeNull();
  });
});
describe("final testing has no optimizer pause/resume", () => {
  it.each(["queued", "preparing", "running", "finalizing"] as const)(
    "allows cancel but not retry while %s",
    (status) => {
      expect(finalControls({ status, attempts: [running] })).toMatchObject({
        cancel: true,
        retry: false,
      });
    },
  );
  it("a cancellation request is not a stopped sandbox", () => {
    expect(finalTerminal.has("cancel_requested")).toBe(false);
    expect(
      finalControls({ status: "cancel_requested", attempts: [running] }),
    ).toMatchObject({ cancel: false, retry: false });
  });
  it.each(["failed", "cancelled", "interrupted"] as const)(
    "requires all attempts stopped before retrying %s",
    (status) => {
      expect(
        finalControls({ status, attempts: [stopped, running] }).retry,
      ).toBe(false);
      expect(finalControls({ status, attempts: [stopped] }).retry).toBe(true);
      expect(
        finalControls({ status, attempts: [running] }).retryReason,
      ).toContain("server to confirm");
    },
  );
  it("cannot rerun a succeeded test with retry or offer pause", () => {
    expect(finalControls({ status: "succeeded", attempts: [stopped] })).toEqual(
      { cancel: false, retry: false, retryReason: null },
    );
    expect(
      finalControls({ status: "running", attempts: [running] }),
    ).not.toHaveProperty("pause");
    expect(finalTrustNotice).toContain("Strategy and engine share a process");
    expect(exposureNotice).toContain(
      "Historical tracking incomplete/external use unknown",
    );
    expect(exposureNotice).toContain("does not establish");
  });
});
