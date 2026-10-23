import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { client } from "../../src/client/client.gen";
import type * as PortableFinalApi from "../../src/lib/portable-final-api";
import {
  actionFor,
  loadActionDraft,
  saveActionDraft,
  saveEditedActionDraft,
} from "../../src/features/final-tests/durable-action";
import {
  finalTestSchema,
  retrySchema,
} from "../../src/features/portable/final-protocol";
import { finalIds, portableFinalFixture } from "./portable-final-fixtures";
let api: typeof PortableFinalApi;
const requests: Request[] = [];
const bearer = crypto.randomUUID();
beforeEach(async () => {
  const values = new Map<string, string>();
  vi.stubGlobal(
    "window",
    Object.assign(new EventTarget(), {
      location: { origin: "https://portable-final.test" },
    }),
  );
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  api = await import("../../src/lib/portable-final-api");
  requests.length = 0;
  client.setConfig({
    baseUrl: "https://portable-final.test",
    auth: () => bearer,
    fetch: async (request) => {
      requests.push(request as Request);
      return new Response(JSON.stringify({ data: [], count: 0 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  });
  (await import("../../src/lib/api")).queryClient.clear();
});
afterEach(() => vi.unstubAllGlobals());
describe("portable final generated transport (not stack acceptance)", () => {
  it("keeps every owner and portable final resource distinct from native and research caches", async () => {
    const { queryClient } = await import("../../src/lib/api");
    const options = api.portableFinalResultsOptions(
      "owner-a",
      "evaluation",
      "decision_trace",
      50,
      50,
    );
    expect(options.queryKey).not.toEqual(
      api.portableFinalResultsOptions(
        "owner-b",
        "evaluation",
        "decision_trace",
        50,
        50,
      ).queryKey,
    );
    expect(api.portableFinalResultsOptions("", "evaluation").enabled).toBe(
      false,
    );
    await queryClient.fetchQuery(options);
    const url = new URL(requests[0].url);
    expect(url.pathname).toBe(
      "/api/v1/portable-final-evaluations/evaluations/evaluation/result",
    );
    expect(Object.fromEntries(url.searchParams)).toEqual({
      section: "decision_trace",
      offset: "50",
      limit: "50",
    });
    expect(requests[0].headers.get("Authorization")).toBe(`Bearer ${bearer}`);
    expect(url.href).not.toContain(bearer);
    for (const option of [
      api.portableFinalSelectionOptions("owner-a", "selection"),
      api.portableFrozenSourceOptions("owner-a", "strategy"),
      api.portableSplitOptions("owner-a", "split"),
      api.portableExposureOptions("owner-a", "split"),
      api.portableFinalSelectionsOptions("owner-a", "split"),
      api.portableFinalOptions("owner-a", "job"),
      api.portableFinalJobsOptions("owner-a", "selection", 25),
      api.portableFinalEvidenceOptions("owner-a", "job", 25),
    ])
      await queryClient.fetchQuery(option as never);
    expect(requests.map((request) => new URL(request.url).pathname)).toEqual([
      "/api/v1/portable-final-evaluations/evaluations/evaluation/result",
      "/api/v1/final-selections/selection",
      "/api/v1/portable-strategies/strategy",
      "/api/v1/split-versions/split",
      "/api/v1/split-versions/split/exposure",
      "/api/v1/final-selections",
      "/api/v1/portable-final-evaluations/job",
      "/api/v1/portable-final-evaluations",
      "/api/v1/portable-final-evaluations/job/evaluations",
    ]);
    expect(new URL(requests.at(-1)!.url).searchParams.get("limit")).toBe("25");
  });
  it("uses shared freeze only, portable preflight/create/retry/cancel routes and separate keys", async () => {
    const body = { selection_id: finalIds.selection, max_runtime_seconds: 300 };
    await api.freezePortableSelection(
      {
        split_version_id: finalIds.split,
        source_run_id: finalIds.job,
        source_candidate_id: finalIds.candidate,
        source_checkpoint_id: finalIds.checkpoint,
      },
      "freeze-key",
    );
    await api.preflightPortableFinal(body);
    await api.submitPortableFinal(body, "create-key");
    await api.retryPortableFinal(finalIds.job, "retry-key");
    await api.cancelPortableFinal(finalIds.job);
    expect(requests.map((r) => new URL(r.url).pathname)).toEqual([
      "/api/v1/final-selections",
      "/api/v1/portable-final-evaluations/preflight",
      "/api/v1/portable-final-evaluations",
      `/api/v1/portable-final-evaluations/${finalIds.job}/retry`,
      `/api/v1/portable-final-evaluations/${finalIds.job}/cancel`,
    ]);
    expect(requests.map((r) => r.headers.get("Idempotency-Key"))).toEqual([
      "freeze-key",
      null,
      "create-key",
      "retry-key",
      null,
    ]);
    expect(await requests[2].json()).toEqual(body);
    expect(requests.every((r) => r.method === "POST")).toBe(true);
  });
  it("replays exactly a durable submission after accepted response loss/reload, invalidates changed inputs", async () => {
    const body = { selection_id: finalIds.selection, max_runtime_seconds: 300 };
    const scope = `portable-test:${finalIds.selection}`,
      pending = actionFor(body, null);
    saveActionDraft("owner-a", scope, finalTestSchema, {
      version: 1,
      body,
      pending,
    });
    const seen: unknown[] = [];
    client.setConfig({
      fetch: async (request) => {
        const req = request as Request;
        seen.push({
          key: req.headers.get("Idempotency-Key"),
          body: await req.json(),
        });
        if (seen.length === 1) throw Error("accepted response lost");
        return new Response(JSON.stringify({ id: finalIds.job }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        });
      },
    });
    await expect(api.submitPortableFinal(body, pending.key)).rejects.toThrow(
      "response lost",
    );
    const loaded = loadActionDraft("owner-a", scope, finalTestSchema)!;
    expect(loadActionDraft("owner-b", scope, finalTestSchema)).toBeNull();
    const replay = actionFor(loaded.body!, loaded.pending);
    await api.submitPortableFinal(replay.body, replay.key);
    expect(seen[0]).toEqual(seen[1]);
    expect(
      actionFor({ ...body, max_runtime_seconds: 301 }, pending).key,
    ).not.toBe(pending.key);
    saveEditedActionDraft(
      "owner-a",
      scope,
      finalTestSchema,
      { ...body, max_runtime_seconds: 0 },
      null,
    );
    expect(
      loadActionDraft("owner-a", scope, finalTestSchema)?.pending,
    ).toBeNull();
    const retry = actionFor(
      { final_evaluation_id: finalIds.job, generation: 1 },
      null,
    );
    expect(retry.key).not.toBe(pending.key);
    saveActionDraft("owner-a", `portable-retry:${finalIds.job}`, retrySchema, {
      version: 1,
      body: retry.body,
      pending: retry,
    });
    expect(
      loadActionDraft("owner-a", `portable-retry:${finalIds.job}`, retrySchema)
        ?.pending,
    ).toEqual(retry);
  });
  it("bounds query error retries and keeps terminal-but-unstopped jobs active", () => {
    const job = portableFinalFixture();
    expect(api.portableFinalNeedsRefresh(job)).toBe(false);
    expect(
      api.portableFinalNeedsRefresh({
        ...job,
        attempts: [{ sandbox_stopped: false } as (typeof job.attempts)[number]],
      }),
    ).toBe(true);
    const interval = api.portableFinalOptions(
      "owner-a",
      job.id,
    ).refetchInterval;
    expect(typeof interval).toBe("function");
    if (typeof interval === "function")
      expect(
        interval({
          state: {
            error: Error("unavailable"),
            data: { ...job, status: "running" },
          },
        } as never),
      ).toBe(false);
  });
  it("recovers authenticated ordered events and rejects gaps, foreign jobs and malformed envelopes", async () => {
    const event = (seq: number) => ({
      schema_version: 1,
      attempt_id: null,
      run_id: finalIds.job,
      seq,
      type: "progress",
      occurred_at: "2026-10-22T12:00:00Z",
      payload: {},
    });
    expect(
      api
        .mergePortableFinalEvents(
          [event(1)],
          [event(2), event(2)],
          finalIds.job,
        )
        .map((e) => e.seq),
    ).toEqual([1, 2]);
    expect(() =>
      api.mergePortableFinalEvents([event(1)], [event(3)], finalIds.job),
    ).toThrow("gap");
    expect(() =>
      api.mergePortableFinalEvents(
        [],
        [{ ...event(1), run_id: "other" }],
        finalIds.job,
      ),
    ).toThrow("incompatible");
    const { queryClient } = await import("../../src/lib/api");
    queryClient.setQueryData(
      api.portableFinalKey("owner-a", "events", finalIds.job),
      [event(1)],
    );
    client.setConfig({
      fetch: async (request) => {
        requests.push(request as Request);
        return new Response(
          `id: 2\nevent: progress\ndata: ${JSON.stringify(event(2))}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        );
      },
    });
    expect(
      await queryClient.fetchQuery({
        ...api.portableFinalEventsOptions("owner-a", finalIds.job, false),
        staleTime: 0,
      }),
    ).toHaveLength(2);
    expect(new URL(requests[0].url).searchParams.get("after_seq")).toBe("1");
    expect(requests[0].headers.get("Authorization")).toBe(`Bearer ${bearer}`);
  });
  it("verifies exact downloaded bytes before saving and rejects checksum/size mismatch", async () => {
    const blob = new Blob(["portable final artifact"]);
    const expected = Array.from(
      new Uint8Array(
        await crypto.subtle.digest("SHA-256", await blob.arrayBuffer()),
      ),
      (byte) => byte.toString(16).padStart(2, "0"),
    ).join("");
    await expect(
      api.verifyPortableFinalDownload(blob, expected),
    ).resolves.toBeUndefined();
    await expect(
      api.verifyPortableFinalDownload(blob, "0".repeat(64)),
    ).rejects.toThrow("checksum differs");
    await expect(
      api.verifyPortableFinalDownload(new Blob([]), expected),
    ).rejects.toThrow("size is invalid");
    await expect(
      api.verifyPortableFinalDownload(blob, "missing"),
    ).rejects.toThrow("checksum or size is invalid");
  });
  it("fetches original final bytes through authenticated route and only invalidates this owner", async () => {
    client.setConfig({
      fetch: async (request) => {
        requests.push(request as Request);
        return new Response(
          '{"schema_version":"tradegrad.portable-result.v1"}',
          { headers: { "Content-Type": "application/json" } },
        );
      },
    });
    expect(await api.downloadPortableFinalResult("eval")).toBeInstanceOf(Blob);
    expect(new URL(requests[0].url).pathname).toBe(
      "/api/v1/portable-final-evaluations/evaluations/eval/result/download",
    );
    expect(requests[0].headers.get("Authorization")).toBe(`Bearer ${bearer}`);
    const { queryClient } = await import("../../src/lib/api");
    queryClient.setQueryData(api.portableFinalKey("a", "job", "job"), {});
    queryClient.setQueryData(api.portableFinalKey("b", "job", "job"), {});
    queryClient.setQueryData(["final-test", "job"], {});
    await api.refreshPortableFinal("a");
    expect(
      queryClient.getQueryState(api.portableFinalKey("a", "job", "job"))
        ?.isInvalidated,
    ).toBe(true);
    expect(
      queryClient.getQueryState(api.portableFinalKey("b", "job", "job"))
        ?.isInvalidated,
    ).toBe(false);
    expect(
      queryClient.getQueryState(["final-test", "job"])?.isInvalidated,
    ).toBe(false);
  });
});
