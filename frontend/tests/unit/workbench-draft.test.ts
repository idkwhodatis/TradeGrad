import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  forgetDescriptionDraft,
  loadDescriptionDraft,
  newDescriptionDraft,
  prepareDescriptionSubmission,
  revisionFailureIsDefinitive,
  storeDescriptionDraft,
} from "../../src/lib/workbench-draft";
import { hashes, ids, spec } from "./workbench-fixtures";
const source = { kind: "strategy_version" as const, id: ids.strategy };
let values: Map<string, string>;
beforeEach(() => {
  values = new Map();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
});
afterEach(() => vi.unstubAllGlobals());
describe("durable owner-scoped prose revisions", () => {
  it("keeps code/parameters out of edit payloads", () => {
    const draft = newDescriptionDraft(spec);
    expect(draft.body.code_sha256).toBe(hashes.code);
    expect(draft.body.base_spec_sha256).toBe(hashes.spec);
    expect(Object.keys(draft.body.rules[0]).sort()).toEqual([
      "description",
      "rule_id",
      "title",
    ]);
  });
  it("submits only changed prose, preserving untouched rule provenance", () => {
    const draft = newDescriptionDraft(spec);
    draft.body.rules[0].description =
      "Human interpretation, not an execution record.";
    const pending = prepareDescriptionSubmission(draft, spec);
    expect(pending.key).toBeTruthy();
    expect(pending.body.rules).toHaveLength(1);
    expect(pending.body.rules[0].rule_id).toBe("filter");
  });
  it("rejects no-op saves and never silently rebases a local draft", () => {
    expect(() =>
      prepareDescriptionSubmission(newDescriptionDraft(spec), spec),
    ).toThrow("Change at least");
    const draft = newDescriptionDraft(spec);
    draft.body.rules[0].title = "Revised";
    expect(() =>
      prepareDescriptionSubmission(draft, { ...spec, revision: 2 }),
    ).toThrow("not rebased");
  });
  it("restores an exact uncertain submission after reload, without allocating a new key", () => {
    const draft = newDescriptionDraft(spec);
    draft.body.rules[0].title = "Revised";
    const pending = prepareDescriptionSubmission(draft, spec);
    storeDescriptionDraft("owner-1", source, pending);
    const restored = loadDescriptionDraft("owner-1", source)!;
    expect(
      prepareDescriptionSubmission(restored, { ...spec, revision: 2 }),
    ).toEqual(pending);
  });
  it("separates owners and exact source resources", () => {
    storeDescriptionDraft("owner-1", source, newDescriptionDraft(spec));
    expect(loadDescriptionDraft("owner-2", source)).toBeNull();
    expect(
      loadDescriptionDraft("owner-1", { kind: "candidate", id: source.id }),
    ).toBeNull();
    forgetDescriptionDraft("owner-1", source);
    expect(loadDescriptionDraft("owner-1", source)).toBeNull();
  });
  it("does not silently discard unreadable pending data", () => {
    values.set(
      `tradegrad.description.owner-1.strategy_version.${ids.strategy}`,
      "{not-json",
    );
    expect(() => loadDescriptionDraft("owner-1", source)).toThrow(
      "not replaced",
    );
  });
  it("refuses a write when its exact retry cannot be stored", () => {
    vi.stubGlobal("sessionStorage", {
      setItem: () => {
        throw new Error("Storage full");
      },
    });
    expect(() =>
      storeDescriptionDraft("owner-1", source, newDescriptionDraft(spec)),
    ).toThrow("Storage full");
  });
  it("distinguishes definitive conflicts from uncertain timeout/network outcomes", () => {
    expect(
      revisionFailureIsDefinitive({ code: "spec_revision_conflict" }),
    ).toBe(true);
    expect(revisionFailureIsDefinitive({ code: "invalid_spec_revision" })).toBe(
      true,
    );
    expect(revisionFailureIsDefinitive(new TypeError("Failed to fetch"))).toBe(
      false,
    );
    expect(revisionFailureIsDefinitive({ name: "TimeoutError" })).toBe(false);
  });
});
