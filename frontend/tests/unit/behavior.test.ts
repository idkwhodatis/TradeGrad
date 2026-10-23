import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EventPublic, RunCreate } from "../../src/client/types.gen";
import { mergeEvents, parseEvent } from "../../src/lib/events";
import {
  submissionFor,
  saveSubmission,
  loadSubmission,
  forgetSubmission,
} from "../../src/lib/idempotency";
import {
  clearSession,
  readSession,
  safeReturnTo,
  saveSession,
  watchSession,
} from "../../src/lib/session";
import { runFormSchema } from "../../src/lib/run-form";

const body: RunCreate = {
  task: "cn_time",
  dataset_version_id: "fa27479d-cd45-4fa7-899c-eac11d706184",
  strategy_version_id: "d6f4e598-ae5d-4b38-bfe6-1a3a72769c53",
  start_date: "2024-01-02",
  end_date: "2024-04-27",
  profile: "corrected_explicit_config",
};
const event = (seq: number): EventPublic => ({
  schema_version: 1,
  run_id: "run-1",
  attempt_id: null,
  seq,
  type: "stage_changed",
  occurred_at: "2024-01-02T12:00:00Z",
  payload: { status: "running" },
});
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => data.set(key, value),
    removeItem: (key: string) => data.delete(key),
  });
  vi.stubGlobal("window", new EventTarget());
});
describe("idempotent logical submissions", () => {
  it("reuses one key for repeated identical input and retries", () => {
    const first = submissionFor(body);
    expect(submissionFor({ ...body }, first)).toBe(first);
    expect(submissionFor({ ...body, profile: undefined }, first)).toBe(first);
  });
  it("treats property ordering as irrelevant but changed input as new", () => {
    const first = submissionFor(body);
    const reordered = Object.fromEntries(
      Object.entries(body).reverse(),
    ) as RunCreate;
    expect(submissionFor(reordered, first).key).toBe(first.key);
    expect(
      submissionFor({ ...body, end_date: "2024-04-26" }, first).key,
    ).not.toBe(first.key);
  });
  it("persists failures across reload without sharing another owner's draft", () => {
    const pending = submissionFor(body);
    saveSubmission("owner-1", pending);
    expect(loadSubmission("owner-1")).toEqual(pending);
    expect(loadSubmission("owner-2")).toBeNull();
    forgetSubmission("owner-1");
    expect(loadSubmission("owner-1")).toBeNull();
  });
});
describe("ordered SSE replay", () => {
  it("deduplicates replayed sequence IDs and orders reconnect events", () =>
    expect(
      mergeEvents([event(2), event(1)], [event(2), event(4), event(3)]).map(
        (item) => item.seq,
      ),
    ).toEqual([1, 2, 3, 4]));
  it("accepts a matching versioned event as JSON or object", () => {
    expect(parseEvent(event(1), "run-1")).toEqual(event(1));
    expect(parseEvent(JSON.stringify(event(1)), "run-1")).toEqual(event(1));
  });
  it.each([
    { ...event(1), schema_version: 2 },
    { ...event(1), run_id: "other-owner" },
    { ...event(1), seq: -1 },
    { ...event(1), seq: 1.5 },
    { ...event(1), payload: null },
  ])("rejects incompatible or cross-run events", (value) =>
    expect(() => parseEvent(value, "run-1")).toThrow(),
  );
});
describe("session and redirect safety", () => {
  it.each([
    "https://attacker.example/",
    "//attacker.example/",
    "/\\attacker.example",
    "/login?next=/runs",
    undefined,
  ])("rejects external or recursive return addresses", (value) =>
    expect(safeReturnTo(value)).toBe("/"),
  );
  it("preserves a direct run return path", () =>
    expect(safeReturnTo("/runs/abc")).toBe("/runs/abc"));
  it("persists an unexpired session then removes it on expiry", () => {
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now);
    const syntheticToken = crypto.randomUUID();
    saveSession(syntheticToken, 1);
    // Only booleans enter assertion diagnostics, never token material.
    expect(readSession()?.accessToken === syntheticToken).toBe(true);
    vi.mocked(Date.now).mockReturnValue(now + 1001);
    expect(readSession() === null).toBe(true);
    vi.restoreAllMocks();
  });
  it("notifies listeners when cleared and supports unsubscribe", () => {
    const listener = vi.fn();
    const off = watchSession(listener);
    clearSession(true);
    expect(listener).toHaveBeenCalledOnce();
    off();
    clearSession();
    expect(listener).toHaveBeenCalledOnce();
  });
  it("fails closed on corrupt storage", () => {
    sessionStorage.setItem("tradegrad.session.v1", "{bad");
    expect(readSession()).toBeNull();
  });
});
describe("evaluation form boundaries", () => {
  it("accepts matching generated RunCreate fields", () =>
    expect(runFormSchema.safeParse(body).success).toBe(true));
  it.each([
    { ...body, end_date: body.start_date },
    { ...body, end_date: "2024-01-01" },
    { ...body, start_date: "2024-02-30" },
    { ...body, task: "mixed" },
    { ...body, dataset_version_id: "" },
  ])("rejects invalid window or selection", (value) =>
    expect(runFormSchema.safeParse(value).success).toBe(false),
  );
});

it("transitional seed execution discloses cold starts without claiming M3 warm-up", async () => {
  const { nativeWindowWarning } =
    await import("../../src/lib/execution-notices");
  expect(nativeWindowWarning).toContain(
    "cold-start windows with no pre-start warm-up",
  );
  expect(nativeWindowWarning).toContain("original-source behavior");
  expect(nativeWindowWarning).toContain(
    "For corrected warm-up-aware optimization, use New Experiment",
  );
});
