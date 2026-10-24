import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  loadResearchDraft,
  matchingPreflight,
  researchFingerprint,
  researchSubmissionFor,
  saveResearchDraft,
} from "../../src/lib/research-submit";
import {
  loadPortableDraft,
  portablePreflightMatches,
  portableSubmissionFor,
  savePortableDraft,
} from "../../src/features/portable/durable";
import { portableFingerprint } from "../../src/features/portable/protocol";
import {
  withResearchCapture,
  type ResearchCapture,
} from "../../src/lib/research-capture";
import { researchFixture } from "./research-fixtures";
import { portableFixture } from "./portable-fixtures";

const capture: ResearchCapture = {
  mode: "research",
  observation_window: { start: "2024-04-01", end: "2024-05-01" },
  stage_ids: ["rank"],
  instrument_ids: ["listing-a"],
  max_decisions: 100,
  max_detail_rows: 1000,
  max_uncompressed_bytes: 1048576,
};
const result = {
  valid: true as const,
  config_sha256: "a".repeat(64),
  evaluation_windows: [],
  is_synthetic: true,
  warnings: [],
  holdout_execution: false as const,
};
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => data.set(key, value),
    removeItem: (key: string) => data.delete(key),
  });
});
afterEach(() => vi.unstubAllGlobals());

describe("capture draft recovery and immutable submissions", () => {
  it("restores native capture and exact pending key but never preflight approval", () => {
    const body = withResearchCapture(researchFixture().body, capture);
    const submission = researchSubmissionFor(body);
    expect(
      saveResearchDraft("owner-a", { version: 1, body, step: 2, submission }),
    ).toBe(true);
    const restored = loadResearchDraft("owner-a")!;
    expect(restored.body).toEqual(body);
    expect(researchSubmissionFor(restored.body, restored.submission)).toEqual(
      submission,
    );
    expect(restored).not.toHaveProperty("preflight");
    expect(loadResearchDraft("owner-b")).toBeNull();
  });
  it("restores portable capture and exact pending key but never preflight approval", () => {
    const body = withResearchCapture(portableFixture().body, capture);
    const pending = portableSubmissionFor(body, null);
    expect(savePortableDraft("owner-a", { version: 1, body, pending })).toBe(
      true,
    );
    const restored = loadPortableDraft("owner-a")!;
    expect(restored.body).toEqual(body);
    expect(portableSubmissionFor(restored.body, restored.pending)).toEqual(
      pending,
    );
    expect(restored).not.toHaveProperty("preflight");
    expect(loadPortableDraft("owner-b")).toBeNull();
  });
  it.each([
    { mode: "off" as const },
    { mode: "summary" as const },
    {
      ...capture,
      observation_window: { start: "2024-04-02", end: "2024-05-01" },
    },
    { ...capture, stage_ids: ["allocation"] },
    { ...capture, instrument_ids: ["listing-b"] },
    { ...capture, max_decisions: 99 },
    { ...capture, max_detail_rows: 999 },
    { ...capture, max_uncompressed_bytes: 2048 },
    undefined,
  ])(
    "capture edit %# invalidates preflight and changes both retry identities",
    (next) => {
      const native = withResearchCapture(researchFixture().body, capture);
      const portable = withResearchCapture(portableFixture().body, capture);
      const nativeRequest = researchSubmissionFor(native);
      const portableRequest = portableSubmissionFor(portable, null);
      const changedNative = withResearchCapture(native, next);
      const changedPortable = withResearchCapture(portable, next);
      expect(
        matchingPreflight(changedNative, {
          fingerprint: researchFingerprint(native),
          result,
        }),
      ).toBe(false);
      expect(
        portablePreflightMatches(changedPortable, {
          fingerprint: portableFingerprint(portable),
          result,
        }),
      ).toBe(false);
      expect(researchSubmissionFor(changedNative, nativeRequest).key).not.toBe(
        nativeRequest.key,
      );
      expect(
        portableSubmissionFor(changedPortable, portableRequest).key,
      ).not.toBe(portableRequest.key);
      expect(nativeRequest.body.research_capture).toEqual(capture);
      expect(portableRequest.body.research_capture).toEqual(capture);
    },
  );
  it("resetting to default restores historical wire fingerprints without empty objects", () => {
    const native = researchFixture().body;
    const portable = portableFixture().body;
    expect(
      researchFingerprint(
        withResearchCapture(withResearchCapture(native, capture), undefined),
      ),
    ).toBe(researchFingerprint(native));
    expect(
      portableFingerprint(
        withResearchCapture(withResearchCapture(portable, capture), undefined),
      ),
    ).toBe(portableFingerprint(portable));
    // react-hook-form may retain an undefined property; JSON transport and its
    // canonical fingerprint still omit it rather than sending null/defaults.
    expect(
      researchFingerprint({ ...native, research_capture: undefined }),
    ).toBe(researchFingerprint(native));
  });
  it("persists half-entered observation windows for Cancel/reload but cannot submit them", () => {
    const partial = {
      ...capture,
      observation_window: { start: "2024-04-01", end: "" },
    };
    const native = withResearchCapture(researchFixture().body, partial);
    const portable = withResearchCapture(portableFixture().body, partial);
    expect(
      saveResearchDraft("owner-a", {
        version: 1,
        body: native,
        step: 2,
        submission: null,
      }),
    ).toBe(true);
    expect(
      savePortableDraft("owner-a", {
        version: 1,
        body: portable,
        pending: null,
      }),
    ).toBe(true);
    expect(loadResearchDraft("owner-a")?.body.research_capture).toEqual(
      partial,
    );
    expect(loadPortableDraft("owner-a")?.body.research_capture).toEqual(
      partial,
    );
    expect(() => researchSubmissionFor(native)).toThrow();
    expect(() => portableSubmissionFor(portable, null)).toThrow();
  });
});
