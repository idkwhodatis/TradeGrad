import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";
import {
  actionCompletionIsCurrent,
  actionFor,
  clearAcceptedActionDraft,
  loadActionDraft,
  saveActionDraft,
  saveEditedActionDraft,
} from "../../src/features/final-tests/durable-action";
import {
  finalTestSchema,
  freezeSchema,
  packageSchema,
  retrySchema,
} from "../../src/features/final-tests/protocol";
const selection = "a1111111-1111-4111-8111-111111111111";
const other = "b2222222-2222-4222-8222-222222222222";
const body = { selection_id: selection, max_runtime_seconds: 300 };
let storage: Map<string, string>;
beforeEach(() => {
  storage = new Map();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
});
afterEach(() => vi.unstubAllGlobals());
describe("owner-scoped durable final mutations", () => {
  it("restores the identical request body and key after lost response/reload", () => {
    const pending = actionFor(body, null);
    expect(
      saveActionDraft("owner-a", "test", finalTestSchema, {
        version: 1,
        body,
        pending,
      }),
    ).toBe(true);
    const draft = loadActionDraft("owner-a", "test", finalTestSchema)!;
    expect(actionFor(draft.body!, draft.pending)).toEqual(pending);
    expect(actionFor({ ...body }, pending).key).toBe(pending.key);
  });
  it("does not retain references callers could mutate after submit", () => {
    const mutable = { ...body };
    const pending = actionFor(mutable, null);
    mutable.max_runtime_seconds = 999;
    expect(pending.body.max_runtime_seconds).toBe(300);
  });
  it("separates owners, selections and action types", () => {
    const pending = actionFor(body, null);
    saveActionDraft("owner-a", "test:one", finalTestSchema, {
      version: 1,
      body,
      pending,
    });
    expect(loadActionDraft("owner-b", "test:one", finalTestSchema)).toBeNull();
    expect(loadActionDraft("owner-a", "test:two", finalTestSchema)).toBeNull();
    expect(
      loadActionDraft("owner-a", "freeze:one", finalTestSchema),
    ).toBeNull();
  });
  it("edits invalidate the old body/key without ambiguous key reuse", () => {
    const original = actionFor(body, null);
    expect(
      actionFor({ ...body, max_runtime_seconds: 301 }, original).key,
    ).not.toBe(original.key);
    expect(actionFor({ ...body, selection_id: other }, original).key).not.toBe(
      original.key,
    );
  });
  it("does not let an unmounted completion navigate or clear storage", () => {
    const pending = actionFor(body, null);
    expect(
      actionCompletionIsCurrent(pending, { active: false, body, pending }),
    ).toBe(false);
    expect(
      actionCompletionIsCurrent(pending, { active: true, body, pending }),
    ).toBe(true);
  });
  it("uses compare-and-remove so old accepted responses cannot erase a newer draft", () => {
    const old = actionFor(body, null);
    const newerBody = { ...body, max_runtime_seconds: 444 };
    const newer = actionFor(newerBody, old);
    saveActionDraft("owner-a", "test", finalTestSchema, {
      version: 1,
      body: newerBody,
      pending: newer,
    });
    expect(
      clearAcceptedActionDraft("owner-a", "test", finalTestSchema, old),
    ).toBe(false);
    expect(
      loadActionDraft("owner-a", "test", finalTestSchema)?.pending?.key,
    ).toBe(newer.key);
    expect(
      clearAcceptedActionDraft("owner-a", "test", finalTestSchema, newer),
    ).toBe(true);
    expect(loadActionDraft("owner-a", "test", finalTestSchema)).toBeNull();
  });
  it("does not clear a newer draft that retains an older pending envelope", () => {
    const pending = actionFor(body, null);
    saveActionDraft("owner-a", "test", finalTestSchema, {
      version: 1,
      body: { ...body, max_runtime_seconds: 500 },
      pending,
    });
    expect(
      clearAcceptedActionDraft("owner-a", "test", finalTestSchema, pending),
    ).toBe(false);
  });
  it("rejects credential-bearing and corrupted persisted requests", () => {
    const dirty = { ...body, api_key: "must-not-persist" };
    expect(
      saveActionDraft("owner-a", "test", finalTestSchema, {
        version: 1,
        body: dirty,
        pending: null,
      }),
    ).toBe(false);
    expect(storage.size).toBe(0);
    saveActionDraft("owner-a", "test", finalTestSchema, {
      version: 1,
      body,
      pending: null,
    });
    storage.set([...storage.keys()][0], "{corrupt");
    expect(loadActionDraft("owner-a", "test", finalTestSchema)).toBeNull();
  });
  it("requires storage before a request can be considered durable", () => {
    vi.stubGlobal("sessionStorage", {
      getItem: () => {
        throw Error("denied");
      },
      setItem: () => {
        throw Error("denied");
      },
    });
    expect(loadActionDraft("owner-a", "test", finalTestSchema)).toBeNull();
    expect(
      saveActionDraft("owner-a", "test", finalTestSchema, {
        version: 1,
        body,
        pending: null,
      }),
    ).toBe(false);
  });
  it("strictly pins freeze refs, retry target/generation and references-only export", () => {
    expect(
      freezeSchema.safeParse({
        split_version_id: selection,
        source_run_id: selection,
        source_candidate_id: other,
        source_checkpoint_id: selection,
      }).success,
    ).toBe(true);
    expect(freezeSchema.safeParse({ source_candidate_id: other }).success).toBe(
      false,
    );
    expect(
      retrySchema.safeParse({ final_evaluation_id: selection, generation: -1 })
        .success,
    ).toBe(false);
    expect(
      packageSchema.safeParse({
        final_selection_id: selection,
        reference_evaluation_id: other,
        profile: "raw_data",
      }).success,
    ).toBe(false);
    expect(
      finalTestSchema.safeParse({ ...body, start_date: "2024-01-01" }).success,
    ).toBe(false);
  });
  it.each([0, -1, 3601, 1.1, NaN, Infinity])(
    "blocks invalid runtime %s",
    (max_runtime_seconds) => {
      expect(
        finalTestSchema.safeParse({ ...body, max_runtime_seconds }).success,
      ).toBe(false);
    },
  );
  it("invalid runtime edits durably discard the old pending key before reload", () => {
    const pending = actionFor(body, null);
    saveActionDraft("owner-a", "test", finalTestSchema, {
      version: 1,
      body,
      pending,
    });
    expect(
      saveEditedActionDraft(
        "owner-a",
        "test",
        finalTestSchema,
        { ...body, max_runtime_seconds: 0 },
        null,
      ),
    ).toBe(true);
    expect(loadActionDraft("owner-a", "test", finalTestSchema)).toEqual({
      version: 1,
      body: null,
      pending: null,
    });
  });
  it("mismatched stored pending envelopes cannot bypass a new preflight", () => {
    const pending = actionFor(body, null);
    saveActionDraft("owner-a", "test", finalTestSchema, {
      version: 1,
      body: { ...body, max_runtime_seconds: 500 },
      pending,
    });
    expect(
      loadActionDraft("owner-a", "test", finalTestSchema)?.pending,
    ).toBeNull();
  });
  it("does not accept a pending key that is not a UUID", () => {
    const schema = z.strictObject({ id: z.string() });
    expect(
      saveActionDraft("owner-a", "test", schema, {
        version: 1,
        body: { id: "one" },
        pending: { key: "not-a-key", body: { id: "one" } },
      }),
    ).toBe(false);
  });
});
