import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearComparisonAction,
  clearComparisonDraft,
  clearUnreadableComparisonDraft,
  comparisonActionKey,
  comparisonActionResolved,
  comparisonCompletionCurrent,
  loadComparisonAction,
  loadComparisonDraft,
  prepareComparisonDraft,
  storeComparisonAction,
  storeComparisonDraft,
} from "../../src/lib/comparison-draft";
import {
  comparisonSelection,
  comparisonId,
  inputHash,
} from "./comparison-fixtures";
import { comparisonRequest } from "../../src/lib/comparison-evidence";
import {
  changeSelection,
  clearComparisonResult,
  clearComparisonSide,
} from "../../src/lib/workbench-selection";
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
describe("durable comparison create recovery", () => {
  it("restores the exact ordered body/key after a lost response or refresh", () => {
    const draft = prepareComparisonDraft(comparisonSelection);
    storeComparisonDraft("owner", draft);
    const restored = loadComparisonDraft("owner")!;
    expect(restored.key).toBe(draft.key);
    expect(restored.body).toEqual(draft.body);
    expect(restored.selection.left_code_sha256).toBe(
      comparisonSelection.left_code_sha256,
    );
    expect(restored.selection.comparison_id).toBeUndefined();
  });
  it("isolates owners and never persists auth or editable source text", () => {
    storeComparisonDraft("owner", prepareComparisonDraft(comparisonSelection));
    expect(loadComparisonDraft("other")).toBeNull();
    const text = [...values.values()][0];
    expect(text).not.toContain("accessToken");
    expect(text).not.toContain('code":');
  });
  it("refuses storage failure and unreadable pending submissions instead of allocating replacement keys", () => {
    values.set("tradegrad.comparison.pending.v1.owner", "malformed");
    expect(() => loadComparisonDraft("owner")).toThrow("not replaced");
    expect(values.get("tradegrad.comparison.pending.v1.owner")).toBe(
      "malformed",
    );
    clearUnreadableComparisonDraft("owner");
    expect(loadComparisonDraft("owner")).toBeNull();
    vi.stubGlobal("sessionStorage", {
      getItem: () => null,
      setItem: () => undefined,
    });
    expect(() =>
      storeComparisonDraft(
        "owner",
        prepareComparisonDraft(comparisonSelection),
      ),
    ).toThrow("No request was sent");
  });
  it("rejects tampered request/selection disagreement", () => {
    const draft = prepareComparisonDraft(comparisonSelection);
    storeComparisonDraft("owner", draft);
    const raw = JSON.parse(
      values.get("tradegrad.comparison.pending.v1.owner")!,
    );
    raw.body.left.code_sha256 = "0".repeat(64);
    values.set("tradegrad.comparison.pending.v1.owner", JSON.stringify(raw));
    expect(() => loadComparisonDraft("owner")).toThrow("unreadable");
  });
  it.each([
    "left_evaluation_id",
    "right_code_sha256",
    "right_spec_sha256",
    "left_trace_manifest_sha256",
  ] as const)(
    "ignores late accepted responses after newer %s selection",
    (key) => {
      const draft = prepareComparisonDraft(comparisonSelection);
      expect(
        comparisonCompletionCurrent(draft, {
          active: true,
          selection: { ...comparisonSelection, [key]: "newer" },
          key: draft.key,
        }),
      ).toBe(false);
    },
  );
  it("ignores late completion after unmount or a newer pending write", () => {
    const draft = prepareComparisonDraft(comparisonSelection);
    expect(
      comparisonCompletionCurrent(draft, {
        active: false,
        selection: comparisonSelection,
        key: draft.key,
      }),
    ).toBe(false);
    expect(
      comparisonCompletionCurrent(draft, {
        active: true,
        selection: comparisonSelection,
        key: crypto.randomUUID(),
      }),
    ).toBe(false);
    expect(
      comparisonCompletionCurrent(draft, {
        active: true,
        selection: comparisonSelection,
        key: draft.key,
      }),
    ).toBe(true);
  });
  it("does not erase a newer retained request on an old acceptance", () => {
    const old = prepareComparisonDraft(comparisonSelection),
      newer = prepareComparisonDraft(comparisonSelection);
    storeComparisonDraft("owner", newer);
    clearComparisonDraft("owner", old.key);
    expect(loadComparisonDraft("owner")?.key).toBe(newer.key);
    clearComparisonDraft("owner", newer.key);
    expect(loadComparisonDraft("owner")).toBeNull();
  });
  it("restores historical absence without retaining a newer registered trace", () => {
    const absent = {
      ...comparisonSelection,
      left_trace_status: "historical_absence" as const,
      left_trace_manifest_sha256: undefined,
    };
    storeComparisonDraft("owner", prepareComparisonDraft(absent));
    const restored = changeSelection(comparisonSelection, {
      ...clearComparisonSide("left"),
      ...clearComparisonSide("right"),
      ...clearComparisonResult(),
      ...loadComparisonDraft("owner")!.selection,
    });
    expect(restored.left_trace_manifest_sha256).toBeUndefined();
    expect(comparisonRequest(restored).left.trace_manifest_sha256).toBeNull();
  });
});
describe("cancel/retry uncertainty is durable across lifecycle generations", () => {
  it("reuses the exact owner/job/action/generation key and separates different intents", () => {
    const key = comparisonActionKey("owner", comparisonId, "retry", 1);
    expect(comparisonActionKey("owner", comparisonId, "retry", 1)).toBe(key);
    expect(comparisonActionKey("owner", comparisonId, "cancel", 1)).not.toBe(
      key,
    );
    expect(comparisonActionKey("owner", comparisonId, "retry", 2)).not.toBe(
      key,
    );
    expect(comparisonActionKey("other", comparisonId, "retry", 1)).not.toBe(
      key,
    );
  });
  it("restores uncertain action after refresh and never mistakes a newer retry for the old generation", () => {
    const pending = {
      action: "retry" as const,
      key: crypto.randomUUID(),
      generation: 1,
      input_sha256: inputHash,
    };
    storeComparisonAction("owner", comparisonId, pending);
    expect(loadComparisonAction("owner", comparisonId)).toEqual(pending);
    expect(
      comparisonActionResolved(pending, {
        generation: 1,
        status: "failed",
        input_sha256: inputHash,
      }),
    ).toBe(false);
    expect(
      comparisonActionResolved(pending, {
        generation: 2,
        status: "running",
        input_sha256: inputHash,
      }),
    ).toBe(true);
    expect(
      comparisonActionResolved(pending, {
        generation: 2,
        status: "failed",
        input_sha256: "changed",
      }),
    ).toBe(false);
    clearComparisonAction("owner", comparisonId, crypto.randomUUID());
    expect(loadComparisonAction("owner", comparisonId)).toEqual(pending);
    clearComparisonAction("owner", comparisonId, pending.key);
    expect(loadComparisonAction("owner", comparisonId)).toBeNull();
  });
  it.each([
    "cancel_requested",
    "cancelled",
    "succeeded",
    "failed",
    "interrupted",
  ])("resolves an uncertain cancel through authoritative %s status", (status) =>
    expect(
      comparisonActionResolved(
        {
          action: "cancel",
          key: crypto.randomUUID(),
          generation: 1,
          input_sha256: inputHash,
        },
        { generation: 1, status, input_sha256: inputHash },
      ),
    ).toBe(true),
  );
  it("fails closed on corrupted action storage", () => {
    values.set(
      `tradegrad.comparison.pending-action.v1.owner.${comparisonId}`,
      "bad",
    );
    expect(() => loadComparisonAction("owner", comparisonId)).toThrow(
      "No replacement",
    );
  });
});
