import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as FinalApi from "../../src/lib/final-api";
import { saveSession } from "../../src/lib/session";
import {
  actionFor,
  loadActionDraft,
  saveActionDraft,
} from "../../src/features/final-tests/durable-action";
import { finalTestSchema } from "../../src/features/final-tests/protocol";
const one = "a1111111-1111-4111-8111-111111111111";
const two = "b2222222-2222-4222-8222-222222222222";
const bearer = crypto.randomUUID();
const fetchMock = vi.fn<typeof fetch>();
let api: typeof FinalApi;
beforeEach(async () => {
  const values = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  vi.stubGlobal(
    "window",
    Object.assign(new EventTarget(), {
      location: { origin: "https://workbench.example" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  saveSession(bearer, 3600);
  api = await import("../../src/lib/final-api");
  (await import("../../src/lib/api")).queryClient.clear();
});
afterEach(() => vi.unstubAllGlobals());
const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
function sent(index = 0) {
  return fetchMock.mock.calls[index][0] as Request;
}

describe("final APIs use generated authenticated contracts (mocked transport)", () => {
  it("keeps final result context distinct from validation and requests bounded real artifact sections", async () => {
    fetchMock.mockResolvedValueOnce(
      response({ split: "holdout", selection_feedback: false }),
    );
    const { queryClient } = await import("../../src/lib/api");
    const value = await queryClient.fetchQuery(
      api.finalResultsOptions(one, {
        sections: ["equity", "drawdown"],
        max_points: 600,
        offset: 25,
        limit: 50,
        start: "2024-04-22",
        end: "2024-04-27",
      }),
    );
    expect(value.split).toBe("holdout");
    expect(value.selection_feedback).toBe(false);
    const url = new URL(sent().url);
    expect(url.pathname).toBe(
      `/api/v1/final-evaluations/evaluations/${one}/result`,
    );
    expect(url.searchParams.getAll("sections")).toEqual(["equity", "drawdown"]);
    expect(url.searchParams.get("max_points")).toBe("600");
    expect(url.searchParams.get("limit")).toBe("50");
    expect(sent().headers.get("Authorization")).toBe(`Bearer ${bearer}`);
    expect(sent().url).not.toContain(bearer);
  });
  it("reads immutable split and exposure without implicitly posting", async () => {
    const { queryClient } = await import("../../src/lib/api");
    for (const options of [
      api.splitVersionOptions(one),
      api.splitExposureOptions(one),
      api.finalSelectionsOptions(one),
    ]) {
      fetchMock.mockResolvedValueOnce(response({ data: [], count: 0 }));
      await queryClient.fetchQuery(
        options as ReturnType<typeof api.splitVersionOptions>,
      );
    }
    expect(
      fetchMock.mock.calls.map(([input]) => (input as Request).method),
    ).toEqual(["GET", "GET", "GET"]);
    expect(new URL(sent(0).url).pathname).toBe(`/api/v1/split-versions/${one}`);
    expect(new URL(sent(1).url).pathname).toBe(
      `/api/v1/split-versions/${one}/exposure`,
    );
  });
  it("replays accepted-then-lost submission with the same persisted key and body", async () => {
    const requests: { key: string | null; body: unknown }[] = [];
    let acceptedId: string | undefined;
    fetchMock.mockImplementation(async (input) => {
      const request = input as Request;
      requests.push({
        key: request.headers.get("Idempotency-Key"),
        body: await request.json(),
      });
      if (!acceptedId) {
        acceptedId = two;
        throw new TypeError("mock server accepted, response lost");
      }
      return response({ id: acceptedId, generation: 1 }, 202);
    });
    const body = { selection_id: one, max_runtime_seconds: 300 };
    const first = actionFor(body, null);
    saveActionDraft("owner-a", `test:${one}`, finalTestSchema, {
      version: 1,
      body,
      pending: first,
    });
    await expect(api.submitFinalTest(first.body, first.key)).rejects.toThrow();
    const restored = loadActionDraft(
      "owner-a",
      `test:${one}`,
      finalTestSchema,
    )!;
    const next = actionFor(restored.body!, restored.pending);
    const result = await api.submitFinalTest(next.body, next.key);
    expect(result.id).toBe(two);
    expect(result.generation).toBe(1);
    expect(requests).toHaveLength(2);
    expect(requests[1]).toEqual(requests[0]);
  });
  it("freezes exact UUIDs only and preserves deterministic body on replay", async () => {
    fetchMock.mockResolvedValue(response({ id: two }, 201));
    const body = {
      split_version_id: one,
      source_run_id: one,
      source_candidate_id: two,
      source_checkpoint_id: one,
    };
    const key = crypto.randomUUID();
    await api.freezeFinalSelection(body, key);
    expect(await sent().json()).toEqual(body);
    expect(new URL(sent().url).pathname).toBe("/api/v1/final-selections");
    expect(sent().headers.get("Idempotency-Key")).toBe(key);
  });
  it("keeps terminal jobs refreshing until their sandboxes are confirmed stopped", () => {
    const run = {
      status: "interrupted",
      attempts: [{ sandbox_stopped: false }],
    } as import("../../src/client/types.gen").FinalEvaluationDetail;
    expect(api.finalNeedsRefresh(run)).toBe(true);
    expect(api.finalNeedsRefresh({ ...run, attempts: [] })).toBe(false);
    expect(
      api.finalNeedsRefresh({ ...run, status: "queued", attempts: [] }),
    ).toBe(true);
  });
  it("uses a retry key and never exposes a pause API to UI", async () => {
    const key = crypto.randomUUID();
    fetchMock.mockResolvedValueOnce(response({ id: one }, 202));
    await api.retryFinalTest(one, key);
    expect(new URL(sent().url).pathname).toBe(
      `/api/v1/final-evaluations/${one}/retry`,
    );
    expect(sent().headers.get("Idempotency-Key")).toBe(key);
    expect(api).not.toHaveProperty("pauseFinalTest");
  });
  it.each([404, 409])(
    "surfaces owner or stop-gate refusal (%s) without automatic action retry",
    async (status) => {
      fetchMock.mockResolvedValueOnce(
        response(
          {
            code: status === 404 ? "not_found" : "sandbox_not_stopped",
            message: "Unavailable",
          },
          status,
        ),
      );
      await expect(
        api.retryFinalTest(one, crypto.randomUUID()),
      ).rejects.toBeDefined();
      expect(fetchMock).toHaveBeenCalledOnce();
    },
  );
  it("downloads only an authenticated raw ZIP blob, without a bearer URL", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(new Uint8Array([80, 75, 3, 4]), {
        headers: { "Content-Type": "application/zip" },
      }),
    );
    const blob = await api.downloadStrategyPackage(one);
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toBe("application/zip");
    expect(blob.size).toBe(4);
    expect(new URL(sent().url).pathname).toBe(
      `/api/v1/strategy-packages/${one}/download`,
    );
    expect(sent().headers.get("Authorization")).toBe(`Bearer ${bearer}`);
    expect(sent().url).not.toContain(bearer);
  });
  it("uses exact references-only package body, not raw data export flags", async () => {
    fetchMock.mockResolvedValueOnce(response({ id: two }, 201));
    await api.createStrategyPackage(one, {
      reference_evaluation_id: two,
      profile: "reference_only.v1",
    });
    expect(new URL(sent().url).pathname).toBe(
      `/api/v1/final-selections/${one}/packages`,
    );
    expect(await sent().json()).toEqual({
      reference_evaluation_id: two,
      profile: "reference_only.v1",
    });
  });
  it("rejects an unavailable private package instead of producing a fake download", async () => {
    fetchMock.mockResolvedValueOnce(
      response({ code: "not_found", message: "Package not found" }, 404),
    );
    await expect(api.downloadStrategyPackage(one)).rejects.toMatchObject({
      code: "not_found",
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });
  it("downloads original holdout JSON only through final-result route", async () => {
    fetchMock.mockResolvedValueOnce(response({ immutable: true }));
    const blob = await api.downloadFinalResult(one);
    expect(await blob.text()).toBe('{"immutable":true}');
    expect(new URL(sent().url).pathname).toBe(
      `/api/v1/final-evaluations/evaluations/${one}/result/download`,
    );
  });
});
