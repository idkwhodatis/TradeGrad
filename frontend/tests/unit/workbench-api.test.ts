import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as WorkbenchApi from "../../src/lib/workbench-api";
import { saveSession } from "../../src/lib/session";
import { hashes, ids, spec } from "./workbench-fixtures";
let api: typeof WorkbenchApi;
const fetchMock = vi.fn<typeof fetch>();
const bearer = crypto.randomUUID();
const source = { kind: "candidate" as const, id: ids.candidate };
const selection = {
  candidate_id: ids.candidate,
  code_sha256: hashes.code,
  evaluation_id: ids.evaluation,
  execution_attempt_id: ids.attempt,
  result_manifest_sha256: hashes.result,
  trace_manifest_sha256: hashes.trace,
  decision_id: "decision-1",
  instrument_id: "B",
  stage_id: "filter",
};
beforeEach(async () => {
  const values = new Map<string, string>();
  vi.stubGlobal(
    "window",
    Object.assign(new EventTarget(), {
      location: { origin: "https://workbench.example" },
    }),
  );
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  saveSession(bearer, 3600);
  api = await import("../../src/lib/workbench-api");
  const { queryClient } = await import("../../src/lib/api");
  queryClient.clear();
});
afterEach(() => vi.unstubAllGlobals());
const response = (body: unknown) =>
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
const request = () => fetchMock.mock.calls.at(-1)![0] as Request;
describe("workbench uses generated authenticated APIs with exact pins", () => {
  it.each(["candidate", "strategy_version"] as const)(
    "pins the %s description revision and SHA in query identity",
    async (kind) => {
      const target = {
        kind,
        id: kind === "candidate" ? ids.candidate : ids.strategy,
      };
      response(spec);
      const { queryClient } = await import("../../src/lib/api");
      const options = api.workbenchSpecOptions(target, {
        spec_revision: 1,
        spec_sha256: hashes.spec,
      });
      await queryClient.fetchQuery(options);
      const sent = request();
      const url = new URL(sent.url);
      expect(url.pathname).toBe(
        `/api/v1/${kind === "candidate" ? "candidates" : "strategy-versions"}/${target.id}/spec`,
      );
      expect(url.searchParams.get("revision")).toBe("1");
      expect(url.searchParams.get("spec_sha256")).toBe(hashes.spec);
      expect(sent.headers.get("Authorization")).toBe(`Bearer ${bearer}`);
      expect(sent.url).not.toContain(bearer);
      expect(options.staleTime).toBe(Infinity);
      expect(options.queryKey).toContain(hashes.spec);
    },
  );
  it("uses bounded paginated lineage, history and physical leaf reads", async () => {
    const { queryClient } = await import("../../src/lib/api");
    for (const [suffix, options] of [
      ["lineage", api.workbenchLineageOptions(source, 25)],
      ["spec/revisions", api.specRevisionsOptions(source, 25)],
      ["evaluations", api.workbenchEvaluationsOptions(source, 25)],
    ] as const) {
      response({ data: [], count: 0 });
      await queryClient.fetchQuery(
        options as ReturnType<typeof api.workbenchEvaluationsOptions>,
      );
      const url = new URL(request().url);
      expect(url.pathname).toBe(`/api/v1/candidates/${source.id}/${suffix}`);
      expect(url.searchParams.get("limit")).toBe("25");
      expect(url.searchParams.get("offset")).toBe("25");
    }
  });
  it("loads the exact leaf directly, independently of list pagination", async () => {
    response({ evaluation_id: ids.evaluation });
    const { queryClient } = await import("../../src/lib/api");
    await queryClient.fetchQuery(
      api.workbenchEvaluationOptions(ids.evaluation),
    );
    expect(new URL(request().url).pathname).toBe(
      `/api/v1/evaluations/${ids.evaluation}/workbench`,
    );
  });
  it("sends all physical and hash pins plus bounded stage/instrument detail filters", async () => {
    response({});
    const { queryClient } = await import("../../src/lib/api");
    await queryClient.fetchQuery(api.selectedDecisionOptions(selection));
    const url = new URL(request().url);
    expect(url.pathname).toBe(
      `/api/v1/evaluations/${ids.evaluation}/decisions/decision-1`,
    );
    for (const key of [
      "execution_attempt_id",
      "result_manifest_sha256",
      "trace_manifest_sha256",
      "stage_id",
      "instrument_id",
    ] as const)
      expect(url.searchParams.get(key)).toBe(selection[key]);
    expect(url.searchParams.get("limit")).toBe("50");
    expect(url.searchParams.get("offset")).toBe("0");
  });
  it("does not enable trace/detail fetching for absent or incompletely pinned evidence", () => {
    expect(api.workbenchTraceOptions({}).enabled).toBe(false);
    expect(
      api.workbenchTraceOptions({ ...selection, trace_status: "not_collected" })
        .enabled,
    ).toBe(false);
    expect(
      api.selectedDecisionOptions({
        ...selection,
        trace_manifest_sha256: undefined,
      }).enabled,
    ).toBe(false);
    expect(
      api.selectedDecisionOptions({ ...selection, decision_id: undefined })
        .enabled,
    ).toBe(false);
  });
  it("separates cache entries by result, trace and selected stage", () => {
    const original = api.selectedDecisionOptions(selection).queryKey;
    expect(
      api.selectedDecisionOptions({
        ...selection,
        result_manifest_sha256: "0".repeat(64),
      }).queryKey,
    ).not.toEqual(original);
    expect(
      api.selectedDecisionOptions({
        ...selection,
        trace_manifest_sha256: "0".repeat(64),
      }).queryKey,
    ).not.toEqual(original);
    expect(
      api.selectedDecisionOptions({ ...selection, stage_id: "allocation" })
        .queryKey,
    ).not.toEqual(original);
  });
  it("submits prose with its exact idempotency key and never code/parameter mutations", async () => {
    response(spec);
    const body = {
      code_sha256: hashes.code,
      base_revision: 1,
      base_spec_sha256: hashes.spec,
      rules: [
        { rule_id: "filter", title: "Human title", description: "Human prose" },
      ],
    };
    await api.saveSpecRevision(source, body, "same-exact-key");
    const sent = request();
    expect(sent.method).toBe("POST");
    expect(sent.headers.get("Idempotency-Key")).toBe("same-exact-key");
    expect(await sent.json()).toEqual(body);
  });
  it("persists a desired bookmark state with the CORS-supported POST operation", async () => {
    response({
      candidate_id: ids.candidate,
      code_sha256: hashes.code,
      bookmarked: true,
    });
    await api.setCandidateBookmark(ids.candidate, true);
    expect(request().method).toBe("POST");
    expect(await request().json()).toEqual({ bookmarked: true });
  });
});
