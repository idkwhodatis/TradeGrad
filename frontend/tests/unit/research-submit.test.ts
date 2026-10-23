import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { client } from "../../src/client/client.gen";
import {
  collectResearchPages,
  clearAcceptedResearchDraft,
  researchCompletionIsCurrent,
  forgetResearchDraft,
  hasStoredResearchDraft,
  loadResearchDraft,
  matchingPreflight,
  preflightResearch,
  researchFingerprint,
  researchSubmissionFor,
  saveResearchDraft,
  singleFlight,
  submitResearch,
  type ResearchDraft,
} from "../../src/lib/research-submit";
import {
  operatorCapabilityFixture,
  researchFixture,
} from "./research-fixtures";

beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
const draft = (): ResearchDraft => ({
  version: 1,
  body: researchFixture().body,
  step: 3,
  submission: null,
});

describe("owner-scoped draft and retry lifecycle", () => {
  it("restores choices and step across reload without sharing another owner's draft", () => {
    const value = draft();
    expect(saveResearchDraft("owner-a", value)).toBe(true);
    expect(loadResearchDraft("owner-a")).toEqual(value);
    expect(loadResearchDraft("owner-b")).toBeNull();
    forgetResearchDraft("owner-a");
    expect(loadResearchDraft("owner-a")).toBeNull();
  });
  it("retains a stable key after ambiguous submission and reload", () => {
    const value = draft();
    value.submission = researchSubmissionFor(value.body);
    saveResearchDraft("owner-a", value);
    const restored = loadResearchDraft("owner-a")!;
    expect(researchSubmissionFor(restored.body, restored.submission).key).toBe(
      value.submission.key,
    );
  });
  it("preserves an exact operator profile across reload and changes the retry key on profile drift", () => {
    const value = draft();
    value.body.model = operatorCapabilityFixture().profile.config;
    value.submission = researchSubmissionFor(value.body);
    expect(saveResearchDraft("owner-a", value)).toBe(true);
    const restored = loadResearchDraft("owner-a")!;
    expect(restored.body.model).toEqual(value.body.model);
    expect(researchSubmissionFor(restored.body, restored.submission).key).toBe(
      value.submission.key,
    );
    if (restored.body.model.provider === "fixture")
      throw new Error("Expected the persisted operator model");
    restored.body.model.profile_sha256 = "c".repeat(64);
    expect(
      researchSubmissionFor(restored.body, restored.submission).key,
    ).not.toBe(value.submission.key);
  });
  it("preserves the exact original fixture wire shape and pending retry identity", () => {
    const value = draft();
    value.submission = researchSubmissionFor(value.body);
    const legacy = JSON.parse(JSON.stringify(value));
    for (const model of [legacy.body.model, legacy.submission.body.model]) {
      delete model.profile_id;
      delete model.profile_sha256;
      delete model.max_output_tokens;
    }
    const stored = JSON.stringify(legacy);
    sessionStorage.setItem("tradegrad.research-draft.v1.owner-a", stored);
    const restored = loadResearchDraft("owner-a")!;
    expect(restored).toEqual(legacy);
    expect(researchSubmissionFor(restored.body, restored.submission)).toEqual(
      value.submission,
    );
    expect(Object.keys(restored.body.model).sort()).toEqual([
      "credential_ref",
      "model",
      "provider",
      "temperature",
    ]);
    expect(hasStoredResearchDraft("owner-a")).toBe(true);
    expect(hasStoredResearchDraft("owner-b")).toBe(false);
    expect(sessionStorage.getItem("tradegrad.research-draft.v1.owner-a")).toBe(
      stored,
    );
  });
  it("invalidates every changed immutable input but ignores property order", () => {
    const { body } = researchFixture();
    const previous = researchSubmissionFor(body);
    const reordered = Object.fromEntries(
      Object.entries(body).reverse(),
    ) as typeof body;
    expect(researchSubmissionFor(reordered, previous)).toBe(previous);
    for (const edit of [
      (value: typeof body) => {
        value.optimizer.seed = 1;
      },
      (value: typeof body) => {
        value.budgets.max_tokens--;
      },
      (value: typeof body) => {
        value.market_profile.commission_rate = 0.001;
      },
      (value: typeof body) => {
        value.windows.validation.end = "2024-10-31";
      },
      (value: typeof body) => {
        value.environment_digest = "b".repeat(64);
      },
      (value: typeof body) => {
        value.dataset.sha256 = "c".repeat(64);
      },
    ]) {
      const changed = structuredClone(body);
      edit(changed);
      expect(researchSubmissionFor(changed, previous).key).not.toBe(
        previous.key,
      );
    }
  });
  it("does not mutate the persisted request when editable values change", () => {
    const { body } = researchFixture();
    const previous = researchSubmissionFor(body);
    body.optimizer.seed = 7;
    expect(previous.body.optimizer.seed).toBe(0);
  });
  it("retains incomplete dates/references for Back, Cancel and reload", () => {
    const value = draft();
    value.step = 1;
    value.body.windows.train.start = "";
    value.body.dataset = { id: "", sha256: "" };
    expect(saveResearchDraft("owner-a", value)).toBe(true);
    expect(loadResearchDraft("owner-a")?.body.windows.train.start).toBe("");
  });
  it("rejects corrupt, foreign and credential-bearing persisted objects", () => {
    sessionStorage.setItem("tradegrad.research-draft.v1.owner-a", "{broken");
    expect(loadResearchDraft("owner-a")).toBeNull();
    const value = draft();
    expect(
      saveResearchDraft("owner-a", {
        ...value,
        authorization: crypto.randomUUID(),
      } as ResearchDraft),
    ).toBe(false);
    expect(
      saveResearchDraft("owner-a", {
        ...value,
        body: {
          ...value.body,
          model: { ...value.body.model, credential_ref: crypto.randomUUID() },
        },
      } as unknown as ResearchDraft),
    ).toBe(false);
  });
  it("reports unavailable storage instead of claiming reload-safe persistence", () => {
    vi.stubGlobal("sessionStorage", {
      getItem() {
        throw new Error("denied");
      },
      setItem() {
        throw new Error("quota");
      },
      removeItem() {
        throw new Error("denied");
      },
    });
    expect(loadResearchDraft("owner-a")).toBeNull();
    expect(saveResearchDraft("owner-a", draft())).toBe(false);
    expect(() => forgetResearchDraft("owner-a")).not.toThrow();
  });
  it("requires preflight for exactly the current body and never restores a cached approval", () => {
    const { body } = researchFixture();
    const checked = {
      fingerprint: researchFingerprint(body),
      result: {
        valid: true as const,
        config_sha256: "c".repeat(64),
        evaluation_windows: [],
        is_synthetic: true,
        warnings: [],
        holdout_execution: false as const,
      },
    };
    expect(matchingPreflight(body, checked)).toBe(true);
    const changed = structuredClone(body);
    changed.budgets.max_tokens--;
    expect(matchingPreflight(changed, checked)).toBe(false);
    expect(matchingPreflight(body, null)).toBe(false);
    saveResearchDraft("owner-a", draft());
    expect(loadResearchDraft("owner-a")).not.toHaveProperty("preflight");
  });
  it("coalesces rapid double clicks synchronously and permits a later retry", async () => {
    const lock = singleFlight<string>();
    let finish!: (value: string) => void;
    const action = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          finish = resolve;
        }),
    );
    const first = lock(action);
    const second = lock(action);
    expect(second).toBe(first);
    await Promise.resolve();
    expect(action).toHaveBeenCalledOnce();
    finish("accepted");
    await expect(first).resolves.toBe("accepted");
    await expect(lock(async () => "retried")).resolves.toBe("retried");
  });
});

describe("stale research completion fencing", () => {
  it("clears only the exact persisted accepted body and submission key", () => {
    const value = draft();
    value.submission = researchSubmissionFor(value.body);
    saveResearchDraft("owner-a", value);
    expect(clearAcceptedResearchDraft("owner-b", value.submission)).toBe(false);
    expect(loadResearchDraft("owner-a")).toEqual(value);
    expect(clearAcceptedResearchDraft("owner-a", value.submission)).toBe(true);
    expect(loadResearchDraft("owner-a")).toBeNull();
  });
  it("preserves edited draft B even when it retains pending submission A", () => {
    const value = draft();
    const acceptedA = researchSubmissionFor(value.body);
    value.submission = acceptedA;
    saveResearchDraft("owner-a", value);
    const reopened = loadResearchDraft("owner-a")!;
    reopened.body.optimizer.seed = 8;
    saveResearchDraft("owner-a", reopened);
    expect(clearAcceptedResearchDraft("owner-a", acceptedA)).toBe(false);
    expect(loadResearchDraft("owner-a")).toEqual(reopened);
  });
  it("preserves a newly submitted B and its reload-safe key when A finishes", () => {
    const value = draft();
    const acceptedA = researchSubmissionFor(value.body);
    value.body.optimizer.seed = 9;
    value.submission = researchSubmissionFor(value.body, acceptedA);
    saveResearchDraft("owner-a", value);
    expect(clearAcceptedResearchDraft("owner-a", acceptedA)).toBe(false);
    const reopened = loadResearchDraft("owner-a")!;
    expect(researchSubmissionFor(reopened.body, reopened.submission).key).toBe(
      value.submission.key,
    );
  });
  it("does not clear a new unsent draft, reused key with different body, or unavailable storage", () => {
    const value = draft();
    const completed = researchSubmissionFor(value.body);
    saveResearchDraft("owner-a", value);
    expect(clearAcceptedResearchDraft("owner-a", completed)).toBe(false);
    value.submission = { ...completed, body: structuredClone(completed.body) };
    value.submission.body.optimizer.seed = 1;
    saveResearchDraft("owner-a", value);
    expect(clearAcceptedResearchDraft("owner-a", completed)).toBe(false);
    vi.stubGlobal("sessionStorage", {
      getItem() {
        throw new Error("unavailable");
      },
    });
    expect(clearAcceptedResearchDraft("owner-a", completed)).toBe(false);
  });
  it("rejects an unmounted or superseded form before completion effects", () => {
    const { body } = researchFixture();
    const completed = researchSubmissionFor(body);
    expect(
      researchCompletionIsCurrent(completed, {
        active: true,
        body,
        submission: completed,
      }),
    ).toBe(true);
    expect(
      researchCompletionIsCurrent(completed, {
        active: false,
        body,
        submission: completed,
      }),
    ).toBe(false);
    const changed = structuredClone(body);
    changed.optimizer.seed++;
    expect(
      researchCompletionIsCurrent(completed, {
        active: true,
        body: changed,
        submission: completed,
      }),
    ).toBe(false);
    expect(
      researchCompletionIsCurrent(completed, {
        active: true,
        body,
        submission: researchSubmissionFor(body),
      }),
    ).toBe(false);
  });
  it.each(["A-first", "B-first"] as const)(
    "keeps reopened B safe with %s response ordering",
    async (order) => {
      const original = draft();
      const pendingA = researchSubmissionFor(original.body);
      original.submission = pendingA;
      saveResearchDraft("owner-a", original);
      let resolveA!: () => void;
      const stateA = {
        active: true,
        body: original.body,
        submission: pendingA,
      };
      const responseA = new Promise<void>((resolve) => {
        resolveA = resolve;
      }).then(() => {
        if (researchCompletionIsCurrent(pendingA, stateA))
          clearAcceptedResearchDraft("owner-a", pendingA);
      });
      stateA.active = false;
      const reopened = loadResearchDraft("owner-a")!;
      reopened.body.optimizer.seed++;
      reopened.submission = researchSubmissionFor(
        reopened.body,
        reopened.submission,
      );
      saveResearchDraft("owner-a", reopened);
      const pendingB = reopened.submission;
      let resolveB!: () => void;
      const responseB = new Promise<void>((resolve) => {
        resolveB = resolve;
      }).then(() => clearAcceptedResearchDraft("owner-a", pendingB));
      if (order === "A-first") {
        resolveA();
        await responseA;
        expect(loadResearchDraft("owner-a")).toEqual(reopened);
        expect(
          researchSubmissionFor(
            reopened.body,
            loadResearchDraft("owner-a")!.submission,
          ).key,
        ).toBe(pendingB.key);
        resolveB();
        expect(await responseB).toBe(true);
        expect(loadResearchDraft("owner-a")).toBeNull();
      } else {
        resolveB();
        expect(await responseB).toBe(true);
        const nextDraft = draft();
        nextDraft.body.optimizer.seed = 3;
        saveResearchDraft("owner-a", nextDraft);
        resolveA();
        await responseA;
        expect(loadResearchDraft("owner-a")).toEqual(nextDraft);
      }
    },
  );
});

describe("generated API request contract", () => {
  it("sends preflight to the authenticated non-mutating endpoint with the whole RunConfig", async () => {
    const testBearer = crypto.randomUUID();
    const { body } = researchFixture();
    const seen: Request[] = [];
    client.setConfig({
      baseUrl: "http://research.test",
      auth: () => testBearer,
      fetch: async (request) => {
        seen.push(request as Request);
        return new Response(
          JSON.stringify({
            valid: true,
            config_sha256: "a".repeat(64),
            evaluation_windows: [],
            is_synthetic: true,
            warnings: [],
            holdout_execution: false,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      },
    });
    expect((await preflightResearch(body)).valid).toBe(true);
    expect(new URL(seen[0].url).pathname).toBe(
      "/api/v1/research-runs/preflight",
    );
    expect(seen[0].method).toBe("POST");
    expect(
      seen[0].headers.get("Authorization") === `Bearer ${testBearer}`,
    ).toBe(true);
    expect(seen[0].headers.has("Idempotency-Key")).toBe(false);
    const payload = await seen[0].json();
    expect(JSON.stringify(payload).includes(testBearer)).toBe(false);
    expect(payload).toEqual(body);
  });
  it("resends the exact key and body after an ambiguous network result", async () => {
    const testBearer = crypto.randomUUID();
    const value = draft();
    value.submission = researchSubmissionFor(value.body);
    saveResearchDraft("owner-a", value);
    const requests: { body: unknown; key: string | null }[] = [];
    client.setConfig({
      baseUrl: "http://research.test",
      auth: () => testBearer,
      fetch: async (request) => {
        const req = request as Request;
        requests.push({
          body: await req.json(),
          key: req.headers.get("Idempotency-Key"),
        });
        if (requests.length === 1)
          throw new TypeError("Network dropped after possible acceptance");
        return new Response(JSON.stringify({ id: "same-owned-run" }), {
          status: 202,
          headers: { "content-type": "application/json" },
        });
      },
    });
    await expect(
      submitResearch(value.body, value.submission.key),
    ).rejects.toThrow("Network dropped");
    const restored = loadResearchDraft("owner-a")!;
    const pending = researchSubmissionFor(restored.body, restored.submission);
    await expect(
      submitResearch(pending.body, pending.key),
    ).resolves.toMatchObject({ id: "same-owned-run" });
    expect(
      requests.some((request) => JSON.stringify(request).includes(testBearer)),
    ).toBe(false);
    expect(requests[1]).toEqual(requests[0]);
  });
  it("surfaces actionable server preflight failures instead of marking success", async () => {
    client.setConfig({
      baseUrl: "http://research.test",
      fetch: async () =>
        new Response(
          JSON.stringify({
            code: "research_preflight_failed",
            message: "listing-a needs 20 earlier sessions",
            correlation_id: "test-request",
          }),
          { status: 422, headers: { "content-type": "application/json" } },
        ),
    });
    await expect(
      preflightResearch(researchFixture().body),
    ).rejects.toMatchObject({
      code: "research_preflight_failed",
      message: "listing-a needs 20 earlier sessions",
    });
  });
  it("loads versions after the first page and detects a truncated catalog", async () => {
    const offsets: number[] = [];
    expect(
      await collectResearchPages(async (offset) => {
        offsets.push(offset);
        return offset === 0
          ? { data: ["a", "b"], count: 3 }
          : { data: ["c"], count: 3 };
      }),
    ).toEqual(["a", "b", "c"]);
    expect(offsets).toEqual([0, 2]);
    await expect(
      collectResearchPages(async () => ({ data: [], count: 1 })),
    ).rejects.toThrow("catalog changed");
  });
});
