import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearUnreadableVerificationDraft,
  clearVerificationAction,
  clearVerificationDraft,
  controlledRequestSchema,
  diagnosticRequestSchema,
  loadVerificationAction,
  loadVerificationDraft,
  prepareVerificationDraft,
  storeVerificationAction,
  storeVerificationDraft,
  verificationActionResolved,
  verificationCompletionCurrent,
  verificationRequestFingerprint,
} from "../../src/lib/verification-draft";
import {
  controlledBody,
  diagnosticBody,
  independentSource,
  verificationHashes as h,
  verificationIds as id,
} from "./verification-fixtures";
import { ids } from "./workbench-fixtures";
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
describe("exact durable independent mutation intent", () => {
  it.each(["diagnostic", "controlled"] as const)(
    "retains %s exact body/key and source after lost responses/refresh",
    (kind) => {
      const draft =
        kind === "diagnostic"
          ? prepareVerificationDraft(kind, diagnosticBody, ids.strategy)
          : prepareVerificationDraft(kind, controlledBody, ids.strategy);
      storeVerificationDraft("owner", draft);
      expect(loadVerificationDraft("owner", kind)).toEqual(draft);
      expect(loadVerificationDraft("other", kind)).toBeNull();
      expect(
        loadVerificationDraft(
          "owner",
          kind === "diagnostic" ? "controlled" : "diagnostic",
        ),
      ).toBeNull();
      clearVerificationDraft("owner", kind, crypto.randomUUID());
      expect(loadVerificationDraft("owner", kind)).toEqual(draft);
      clearVerificationDraft("owner", kind, draft.key);
      expect(loadVerificationDraft("owner", kind)).toBeNull();
    },
  );
  it("refuses to replace an uncertain body, variant order or key with a new intent", () => {
    const old = prepareVerificationDraft(
      "controlled",
      controlledBody,
      ids.strategy,
    );
    storeVerificationDraft("owner", old);
    expect(() =>
      storeVerificationDraft(
        "owner",
        prepareVerificationDraft(
          "controlled",
          { ...controlledBody, arms: [...controlledBody.arms].reverse() },
          ids.strategy,
        ),
      ),
    ).toThrow("unresolved");
    expect(loadVerificationDraft("owner", "controlled")).toEqual(old);
  });
  it("fails closed before network when persistence silently fails", () => {
    vi.stubGlobal("sessionStorage", {
      getItem: () => null,
      setItem: () => undefined,
    });
    expect(() =>
      storeVerificationDraft(
        "owner",
        prepareVerificationDraft("diagnostic", diagnosticBody, ids.strategy),
      ),
    ).toThrow("No request was sent");
  });
  it("retains corruption and refuses new key allocation until explicit cleanup", () => {
    values.set("tradegrad.verification.pending.v1.owner.diagnostic", "broken");
    expect(() => loadVerificationDraft("owner", "diagnostic")).toThrow(
      "unreadable",
    );
    expect(values.size).toBe(1);
    clearUnreadableVerificationDraft("owner", "diagnostic");
    expect(loadVerificationDraft("owner", "diagnostic")).toBeNull();
  });
  it("rejects saved source/request disagreement", () => {
    const draft = prepareVerificationDraft(
      "diagnostic",
      diagnosticBody,
      ids.strategy,
    );
    storeVerificationDraft("owner", draft);
    const name = "tradegrad.verification.pending.v1.owner.diagnostic",
      value = JSON.parse(values.get(name)!);
    value.selection.code_sha256 = "0".repeat(64);
    values.set(name, JSON.stringify(value));
    expect(() => loadVerificationDraft("owner", "diagnostic")).toThrow(
      "unreadable",
    );
  });
  it("late completion never replaces a newer source, draft, unmounted view or changed control patch", () => {
    const draft = prepareVerificationDraft(
        "controlled",
        controlledBody,
        ids.strategy,
      ),
      current = {
        active: true,
        key: draft.key,
        source: independentSource,
        body: controlledBody,
      };
    expect(verificationCompletionCurrent(draft, current)).toBe(true);
    for (const patch of [
      { active: false },
      { key: crypto.randomUUID() },
      { source: { ...independentSource, code_sha256: h.report } },
      { body: { ...controlledBody, hypothesis: "newer hypothesis" } },
    ])
      expect(
        verificationCompletionCurrent(draft, { ...current, ...patch }),
      ).toBe(false);
  });
  it("normalizes only declared server defaults for response equality without changing retained wire bodies", () => {
    const before = JSON.stringify(controlledBody);
    expect(verificationRequestFingerprint(controlledBody)).toBe(
      verificationRequestFingerprint({
        ...controlledBody,
        research_capture: {
          ...controlledBody.research_capture,
          observation_window: null,
          stage_ids: [],
          instrument_ids: [],
          max_decisions: 10000,
          max_detail_rows: 2000000,
          max_uncompressed_bytes: 268435456,
        },
      }),
    );
    expect(JSON.stringify(controlledBody)).toBe(before);
    expect(verificationRequestFingerprint(controlledBody)).not.toBe(
      verificationRequestFingerprint({
        ...controlledBody,
        arms: [...controlledBody.arms].reverse(),
      }),
    );
  });
});
describe("manual control authoring bounds and semantic scope", () => {
  it.each([0, 1, 4])("rejects %i arms", (count) =>
    expect(
      controlledRequestSchema.safeParse({
        ...controlledBody,
        arms: Array.from({ length: count }, (_, index) => ({
          ...controlledBody.arms[0],
          name: `arm-${index}`,
        })),
      }).success,
    ).toBe(false),
  );
  it("rejects duplicate names and extra manifest/model/optimizer fields", () => {
    expect(
      controlledRequestSchema.safeParse({
        ...controlledBody,
        arms: [controlledBody.arms[0], controlledBody.arms[0]],
      }).success,
    ).toBe(false);
    for (const field of ["optimizer", "model", "dataset", "state_policy"])
      expect(
        diagnosticRequestSchema.safeParse({ ...diagnosticBody, [field]: {} })
          .success,
      ).toBe(false);
    expect(
      controlledRequestSchema.safeParse({
        ...controlledBody,
        arms: [
          { ...controlledBody.arms[0], manifest: {} },
          controlledBody.arms[1],
        ],
      }).success,
    ).toBe(false);
  });
  it("rejects invalid observation windows but accepts explicit valid no-observation mode", () => {
    expect(
      diagnosticRequestSchema.safeParse({
        ...diagnosticBody,
        research_capture: {
          mode: "research",
          observation_window: { start: "2024-01-02", end: "2024-01-01" },
        },
      }).success,
    ).toBe(false);
    expect(
      diagnosticRequestSchema.parse({
        ...diagnosticBody,
        research_capture: { mode: "off" },
      }).research_capture.mode,
    ).toBe("off");
  });
});
describe("generation-aware uncertain independent actions", () => {
  const pending = () => ({
    action: "retry" as const,
    key: crypto.randomUUID(),
    generation: 2,
    config_sha256: h.config,
  });
  it("retains exact retry and refuses an unrelated cancel/generation", () => {
    const action = pending();
    storeVerificationAction("owner", id.run, action);
    expect(loadVerificationAction("owner", id.run)).toEqual(action);
    expect(loadVerificationAction("other", id.run)).toBeNull();
    expect(() =>
      storeVerificationAction("owner", id.run, { ...action, action: "cancel" }),
    ).toThrow("Resolve");
    expect(
      verificationActionResolved(action, {
        status: "failed",
        generation: 2,
        config_sha256: h.config,
      }),
    ).toBe(false);
    expect(
      verificationActionResolved(action, {
        status: "running",
        generation: 3,
        config_sha256: h.config,
      }),
    ).toBe(true);
    expect(
      verificationActionResolved(action, {
        status: "running",
        generation: 3,
        config_sha256: h.report,
      }),
    ).toBe(false);
    clearVerificationAction("owner", id.run, crypto.randomUUID());
    expect(loadVerificationAction("owner", id.run)).not.toBeNull();
    clearVerificationAction("owner", id.run, action.key);
    expect(loadVerificationAction("owner", id.run)).toBeNull();
  });
  it.each([
    "cancel_requested",
    "cancelled",
    "failed",
    "interrupted",
    "succeeded",
  ])(
    "resolves cancel from same/newer durable %s status, not an old generation",
    (status) => {
      const action = { ...pending(), action: "cancel" as const };
      expect(
        verificationActionResolved(action, {
          status,
          generation: 2,
          config_sha256: h.config,
        }),
      ).toBe(true);
      expect(
        verificationActionResolved(action, {
          status,
          generation: 1,
          config_sha256: h.config,
        }),
      ).toBe(false);
    },
  );
});
