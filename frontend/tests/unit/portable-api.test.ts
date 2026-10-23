import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { client } from "../../src/client/client.gen";
import type * as PortableApi from "../../src/lib/portable-api";
import {
  loadPortableDraft,
  portableSubmissionFor,
  savePortableDraft,
} from "../../src/features/portable/durable";
import { portableFixture, portableRunFixture } from "./portable-fixtures";
let api: typeof PortableApi;
const requests: Request[] = [];
const bearer = crypto.randomUUID();
beforeEach(async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal(
    "window",
    Object.assign(new EventTarget(), {
      location: { origin: "https://portable.test" },
    }),
  );
  vi.stubGlobal("sessionStorage", {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => storage.set(k, v),
    removeItem: (k: string) => storage.delete(k),
  });
  api = await import("../../src/lib/portable-api");
  requests.length = 0;
  client.setConfig({
    baseUrl: "https://portable.test",
    auth: () => bearer,
    fetch: async (request) => {
      requests.push(request as Request);
      return new Response(JSON.stringify({ data: [], count: 0 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  });
  const { queryClient } = await import("../../src/lib/api");
  queryClient.clear();
});
afterEach(() => vi.unstubAllGlobals());
describe("generated portable transport contract, not integration acceptance", () => {
  it("uses distinct owner-scoped cache keys and abortable requests", async () => {
    const { queryClient } = await import("../../src/lib/api");
    const a = api.portableResultsOptions(
      "owner-a",
      "evaluation",
      "decision_trace",
      50,
      50,
    );
    const b = api.portableResultsOptions(
      "owner-b",
      "evaluation",
      "decision_trace",
      50,
      50,
    );
    expect(a.queryKey).not.toEqual(b.queryKey);
    await queryClient.fetchQuery(a);
    const url = new URL(requests[0].url);
    expect(url.pathname).toBe(
      "/api/v1/portable-evaluations/evaluation/results",
    );
    expect(url.searchParams.get("section")).toBe("decision_trace");
    expect(url.searchParams.get("offset")).toBe("50");
    expect(url.searchParams.get("limit")).toBe("50");
    expect(requests[0].headers.get("Authorization")).toBe(`Bearer ${bearer}`);
    expect(requests[0].url).not.toContain(bearer);
    expect(api.portableResultsOptions("", "evaluation").enabled).toBe(false);
  });
  it("keeps candidate, evaluation, checkpoint and call lists bounded and portable-only", async () => {
    const { queryClient } = await import("../../src/lib/api");
    for (const [resource, option] of [
      [
        "candidates",
        () =>
          queryClient.fetchQuery(
            api.portableCandidatesOptions("owner-a", "run", 25),
          ),
      ],
      [
        "evaluations",
        () =>
          queryClient.fetchQuery(
            api.portableEvaluationsOptions("owner-a", "run", 25),
          ),
      ],
      [
        "checkpoints",
        () =>
          queryClient.fetchQuery(
            api.portableCheckpointsOptions("owner-a", "run", 25),
          ),
      ],
      [
        "model-calls",
        () =>
          queryClient.fetchQuery(
            api.portableModelCallsOptions("owner-a", "run", 25),
          ),
      ],
    ] as const) {
      requests.length = 0;
      await option();
      const url = new URL(requests[0].url);
      expect(url.pathname).toBe(`/api/v1/portable-runs/run/${resource}`);
      expect(url.searchParams.get("offset")).toBe("25");
      expect(url.searchParams.get("limit")).toBe("25");
    }
  });
  it("posts full portable defaults and preflight without mutating run or adding a key", async () => {
    const { body } = portableFixture();
    await api.defaultPortableConfig({
      dataset: body.dataset,
      universe: body.universe,
      seed_strategy: body.seed_strategy,
      windows: body.windows,
      execution_mode: "optimization",
    });
    await api.preflightPortable(body);
    expect(new URL(requests[0].url).pathname).toBe(
      "/api/v1/portable-runs/default-config",
    );
    expect(new URL(requests[1].url).pathname).toBe(
      "/api/v1/portable-runs/preflight",
    );
    expect(requests[1].method).toBe("POST");
    expect(requests[1].headers.has("Idempotency-Key")).toBe(false);
    expect(await requests[1].json()).toEqual(body);
  });
  it("lost response retries exactly the same persisted key and complete body after reload", async () => {
    const { body } = portableFixture();
    const pending = portableSubmissionFor(body, null);
    savePortableDraft("owner-a", { version: 1, body, pending });
    const seen: { key: string | null; body: unknown }[] = [];
    client.setConfig({
      fetch: async (request) => {
        const req = request as Request;
        seen.push({
          key: req.headers.get("Idempotency-Key"),
          body: await req.json(),
        });
        if (seen.length === 1)
          throw TypeError("response lost after possible acceptance");
        return new Response(JSON.stringify({ id: "same-owned-run" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        });
      },
    });
    await expect(api.submitPortable(body, pending.key)).rejects.toThrow(
      "response lost",
    );
    const restored = loadPortableDraft("owner-a")!;
    const replay = portableSubmissionFor(restored.body, restored.pending);
    await expect(
      api.submitPortable(replay.body, replay.key),
    ).resolves.toMatchObject({ id: "same-owned-run" });
    expect(seen[0]).toEqual(seen[1]);
    expect(JSON.stringify(seen[0].body)).not.toContain(bearer);
  });
  it("surfaces unavailable raw/action/calendar evidence without accepting native data", async () => {
    client.setConfig({
      fetch: async () =>
        new Response(
          JSON.stringify({
            code: "portable_preflight_failed",
            message: "Raw prices and known action coverage are required",
            correlation_id: "fixture-request",
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        ),
    });
    await expect(
      api.preflightPortable(portableFixture().body),
    ).rejects.toMatchObject({
      code: "portable_preflight_failed",
      message: "Raw prices and known action coverage are required",
    });
  });
  it("commands only the requested portable run and preserves control endpoint semantics", async () => {
    for (const action of ["pause", "cancel", "resume"] as const)
      await api.portableCommand("run", action);
    expect(requests.map((req) => new URL(req.url).pathname)).toEqual([
      "/api/v1/portable-runs/run/pause",
      "/api/v1/portable-runs/run/cancel",
      "/api/v1/portable-runs/run/resume",
    ]);
    expect(requests.every((req) => req.method === "POST")).toBe(true);
  });
  it("keeps polling a terminal run until physical stop is confirmed", () => {
    const run = portableRunFixture();
    run.status = "succeeded";
    expect(api.portableNeedsRefresh(run)).toBe(false);
    run.attempts = [
      {
        id: "a",
        status: "running",
        generation: 1,
        started_at: "now",
        heartbeat_at: "now",
        finished_at: null,
        exit_code: null,
        error: null,
        sandbox_stopped: false,
      },
    ];
    expect(api.portableNeedsRefresh(run)).toBe(true);
    expect(api.portableNeedsRefresh()).toBe(true);
  });
  it("loads catalog beyond page one and rejects a zero-progress catalog", async () => {
    const seen: number[] = [];
    expect(
      await api.collectPortablePages(async (offset) => {
        seen.push(offset);
        return offset === 0
          ? { data: [1, 2], count: 3 }
          : { data: [3], count: 3 };
      }),
    ).toEqual([1, 2, 3]);
    expect(seen).toEqual([0, 2]);
    await expect(
      api.collectPortablePages(async () => ({ data: [], count: 1 })),
    ).rejects.toThrow("catalog changed");
  });
  it("does not read another owner's cached results after a switch", async () => {
    const { queryClient } = await import("../../src/lib/api");
    queryClient.setQueryData(
      api.portableKey("owner-a", "results", "eval", "decision_trace", 0, 50),
      { secret: "owner-a-only" },
    );
    await queryClient.fetchQuery(
      api.portableResultsOptions("owner-b", "eval", "decision_trace"),
    );
    expect(requests).toHaveLength(1);
    expect(
      queryClient.getQueryData(
        api.portableKey("owner-b", "results", "eval", "decision_trace", 0, 50),
      ),
    ).not.toHaveProperty("secret");
  });
  it("invalidates only the current owner's portable caches", async () => {
    const { queryClient } = await import("../../src/lib/api");
    queryClient.setQueryData(api.portableKey("a", "run", "run"), {});
    queryClient.setQueryData(api.portableKey("b", "run", "run"), {});
    queryClient.setQueryData(["research-run", "run"], {});
    await api.refreshPortable("a");
    expect(
      queryClient.getQueryState(api.portableKey("a", "run", "run"))
        ?.isInvalidated,
    ).toBe(true);
    expect(
      queryClient.getQueryState(api.portableKey("b", "run", "run"))
        ?.isInvalidated,
    ).toBe(false);
    expect(
      queryClient.getQueryState(["research-run", "run"])?.isInvalidated,
    ).toBe(false);
  });
});
