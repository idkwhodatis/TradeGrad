import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ResearchRunDetail } from "../../src/client/types.gen";
import type * as ResearchApi from "../../src/lib/research-api";
import { saveSession } from "../../src/lib/session";
let api: typeof ResearchApi;
const runId = "a1111111-1111-4111-8111-111111111111";
const evaluationId = "b2222222-2222-4222-8222-222222222222";
const fetchMock = vi.fn<typeof fetch>();
const testBearer = crypto.randomUUID();
beforeEach(async () => {
  const storage = new Map<string, string>();
  const window = Object.assign(new EventTarget(), {
    location: { origin: "https://workbench.example" },
  });
  vi.stubGlobal("window", window);
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  saveSession(testBearer, 3600);
  api = await import("../../src/lib/research-api");
  const { queryClient } = await import("../../src/lib/api");
  queryClient.clear();
});
afterEach(() => vi.unstubAllGlobals());
function success(body: unknown) {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}
function request() {
  return fetchMock.mock.calls[0][0] as Request;
}

describe("research queries use generated authenticated transport", () => {
  it("uses bounded durable result sections and propagates filters without credentials in URL", async () => {
    success({ evaluation_id: evaluationId });
    const { queryClient } = await import("../../src/lib/api");
    const result = await queryClient.fetchQuery(
      api.resultsOptions(evaluationId, {
        sections: ["equity", "drawdown"],
        start: "2024-01-01",
        end: "2024-02-01",
        max_points: 600,
        limit: 50,
        offset: 0,
      }),
    );
    expect(result.evaluation_id).toBe(evaluationId);
    const sent = request();
    const url = new URL(sent.url);
    expect(url.pathname).toBe(
      `/api/v1/research-evaluations/${evaluationId}/results`,
    );
    expect(url.searchParams.getAll("sections")).toEqual(["equity", "drawdown"]);
    expect(url.searchParams.get("max_points")).toBe("600");
    expect(url.searchParams.get("end")).toBe("2024-02-01");
    expect(sent.headers.get("Authorization")).toBe(`Bearer ${testBearer}`);
    expect(sent.url).not.toContain(testBearer);
  });
  it("pages candidate/evaluation/checkpoint/model calls on the server", async () => {
    const { queryClient } = await import("../../src/lib/api");
    for (const [name, load] of [
      [
        "candidates",
        () => queryClient.fetchQuery(api.candidatesOptions(runId, 25)),
      ],
      [
        "evaluations",
        () => queryClient.fetchQuery(api.evaluationsOptions(runId, 25)),
      ],
      [
        "checkpoints",
        () => queryClient.fetchQuery(api.checkpointsOptions(runId, 25)),
      ],
      [
        "model-calls",
        () => queryClient.fetchQuery(api.modelCallsOptions(runId, 25)),
      ],
    ] as const) {
      fetchMock.mockClear();
      success({ count: 0, data: [] });
      await load();
      const url = new URL(request().url);
      expect(url.pathname).toBe(`/api/v1/research-runs/${runId}/${name}`);
      expect(url.searchParams.get("limit")).toBe("25");
      expect(url.searchParams.get("offset")).toBe("25");
    }
  });
  it("can reach artifacts beyond the first 50 rather than hiding later logs", async () => {
    success({
      count: 76,
      data: [{ id: "last-file", name: "final-result.json" }],
    });
    const { queryClient } = await import("../../src/lib/api");
    const page = await queryClient.fetchQuery(
      api.researchArtifactsOptions(runId, 75),
    );
    expect(page.count).toBe(76);
    expect(page.data[0].name).toBe("final-result.json");
    const url = new URL(request().url);
    expect(url.searchParams.get("offset")).toBe("75");
    expect(url.searchParams.get("limit")).toBe("25");
  });
  it("invalidates selected live model-call details when status/adoption changes", async () => {
    const { queryClient } = await import("../../src/lib/api");
    const options = api.modelCallOptions(evaluationId, runId, true);
    success({
      id: evaluationId,
      status: "started",
      adopted: false,
      usage: { input_tokens: null },
    });
    const before = await queryClient.fetchQuery(options);
    expect(before.status).toBe("started");
    expect(options.refetchInterval).toBe(2000);
    await api.refreshResearch(runId);
    success({
      id: evaluationId,
      status: "completed",
      adopted: true,
      usage: { input_tokens: 0 },
      response_sha256: "response",
    });
    const after = await queryClient.fetchQuery(options);
    expect(after.status).toBe("completed");
    expect(after.adopted).toBe(true);
    expect(after.usage.input_tokens).toBe(0);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
  it("loads whole-run persisted usage rather than adding only a visible call page", async () => {
    success({
      run_id: runId,
      calls: 0,
      input_tokens: 0,
      output_tokens: 0,
      cost: null,
    });
    const { queryClient } = await import("../../src/lib/api");
    const usage = await queryClient.fetchQuery(api.researchUsageOptions(runId));
    expect(new URL(request().url).pathname).toBe(
      `/api/v1/research-runs/${runId}/usage`,
    );
    expect(usage.input_tokens).toBe(0);
    expect(usage.cost).toBeNull();
  });
  it("keeps polling terminal runs whose sandboxes are not yet confirmed stopped", () => {
    const run = {
      status: "interrupted",
      attempts: [{ sandbox_stopped: false }],
    } as ResearchRunDetail;
    expect(api.researchNeedsRefresh(run)).toBe(true);
    expect(api.researchNeedsRefresh({ ...run, attempts: [] })).toBe(false);
  });
  it("compares only selected IDs through the typed endpoint, without client ranking", async () => {
    success({
      comparable: false,
      differences: [],
      evaluations: [],
      ranking: null,
      warnings: [],
    });
    const { queryClient } = await import("../../src/lib/api");
    const compared = await queryClient.fetchQuery(
      api.comparisonOptions([evaluationId, runId]),
    );
    expect(request().method).toBe("POST");
    expect(await request().json()).toEqual({
      evaluation_ids: [evaluationId, runId],
    });
    expect(compared.ranking).toBeNull();
    expect(compared.comparable).toBe(false);
  });
  it.each(["pause", "cancel", "resume"] as const)(
    "sends %s once and does not automatically replay a network failure",
    async (action) => {
      fetchMock.mockRejectedValueOnce(new TypeError("offline"));
      await expect(api.researchCommand(runId, action)).rejects.toThrow();
      expect(fetchMock).toHaveBeenCalledOnce();
      expect(new URL(request().url).pathname).toBe(
        `/api/v1/research-runs/${runId}/${action}`,
      );
    },
  );
  it("fetches HTML as an authenticated Blob instead of embedding report URLs", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("<html>escaped report</html>", {
        headers: { "Content-Type": "text/html" },
      }),
    );
    const report = await api.downloadResearchResult(evaluationId, "html");
    expect(report).toBeInstanceOf(Blob);
    expect(await report.text()).toContain("escaped report");
    expect(new URL(request().url).pathname).toBe(
      `/api/v1/research-evaluations/${evaluationId}/report`,
    );
    expect(request().headers.get("Authorization")).toBe(`Bearer ${testBearer}`);
  });
});
