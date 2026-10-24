import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { saveSession } from "../../src/lib/session";
import { hashes, ids } from "./workbench-fixtures";
import type * as ReplayApi from "../../src/lib/replay-api";
let api: typeof ReplayApi;
const bearer = crypto.randomUUID();
const fetchMock = vi.fn<typeof fetch>();
const selection = {
  code_sha256: hashes.code,
  evaluation_id: ids.evaluation,
  execution_attempt_id: ids.attempt,
  result_manifest_sha256: hashes.result,
  trace_manifest_sha256: hashes.trace,
  session_date: "2024-01-02",
  decision_id: "decision-1",
  instrument_id: "B",
  stage_id: "liquidity",
};
beforeEach(async () => {
  const values = new Map<string, string>();
  vi.stubGlobal(
    "window",
    Object.assign(new EventTarget(), {
      location: { origin: "https://replay.example" },
    }),
  );
  vi.stubGlobal("sessionStorage", {
    getItem: (k: string) => values.get(k) ?? null,
    setItem: (k: string, v: string) => values.set(k, v),
    removeItem: (k: string) => values.delete(k),
  });
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  saveSession(bearer, 3600);
  api = await import("../../src/lib/replay-api");
  const { queryClient } = await import("../../src/lib/api");
  queryClient.clear();
});
afterEach(() => vi.unstubAllGlobals());
function respond() {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify({}), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}
const sent = () => fetchMock.mock.calls.at(-1)![0] as Request;
describe("bounded authenticated generated replay APIs", () => {
  it("lists within-date exact events without splitting portfolio events by instrument/stage", async () => {
    respond();
    const { queryClient } = await import("../../src/lib/api");
    await queryClient.fetchQuery(
      api.replayEventsOptions({ ...selection, event_offset: 25 }),
    );
    const url = new URL(sent().url);
    expect(url.pathname).toBe(
      `/api/v1/evaluations/${ids.evaluation}/decisions`,
    );
    expect(url.searchParams.get("start")).toBe("2024-01-02");
    expect(url.searchParams.get("end")).toBe("2024-01-03");
    expect(url.searchParams.get("offset")).toBe("25");
    expect(url.searchParams.get("limit")).toBe("25");
    expect(url.searchParams.has("instrument_id")).toBe(false);
    expect(url.searchParams.has("stage_id")).toBe(false);
  });
  it("submits all eligibility/reason/finite-score/rank filters and retains the full portfolio candidate page", async () => {
    respond();
    const { queryClient } = await import("../../src/lib/api");
    await queryClient.fetchQuery(
      api.replayCandidatesOptions({
        ...selection,
        eligibility: "ineligible",
        reason: "rank_not_selected",
        rank_min: 2,
        rank_max: 8,
        score_min: -0.5,
        score_max: 0,
        candidate_offset: 50,
      }),
    );
    const url = new URL(sent().url);
    for (const [key, value] of Object.entries({
      stage_id: "liquidity",
      eligibility: "ineligible",
      reason: "rank_not_selected",
      rank_min: "2",
      rank_max: "8",
      score_min: "-0.5",
      score_max: "0",
      offset: "50",
      limit: "50",
    }))
      expect(url.searchParams.get(key)).toBe(value);
    expect(url.searchParams.has("instrument_id")).toBe(false);
  });
  it("resolves the selected candidate independently of candidate-table filters/page", async () => {
    respond();
    const { queryClient } = await import("../../src/lib/api");
    await queryClient.fetchQuery(
      api.replayCandidatesOptions(
        {
          ...selection,
          record_kind: "candidate",
          record_id: "B",
          reason: "selected",
          candidate_offset: 100,
        },
        true,
      ),
    );
    const url = new URL(sent().url);
    expect(url.searchParams.get("instrument_id")).toBe("B");
    expect(url.searchParams.get("offset")).toBe("0");
    expect(url.searchParams.has("reason")).toBe(false);
  });
  it.each(["instruments", "market", "execution"] as const)(
    "pins the %s request to exact immutable attempt/result/trace",
    async (kind) => {
      respond();
      const { queryClient } = await import("../../src/lib/api");
      if (kind === "instruments")
        await queryClient.fetchQuery(
          api.replayInstrumentsOptions({ ...selection, instrument_offset: 50 }),
        );
      if (kind === "market")
        await queryClient.fetchQuery(
          api.replayMarketOptions({
            ...selection,
            market_start: "2024-01-01",
            market_end: "2024-01-03",
            market_offset: 100,
          }),
        );
      if (kind === "execution")
        await queryClient.fetchQuery(
          api.replayExecutionOptions({
            ...selection,
            record_kind: "order",
            record_id: "logical-1",
            execution_offset: 50,
          }),
        );
      const request = sent(),
        url = new URL(request.url);
      expect(request.headers.get("Authorization")).toBe(`Bearer ${bearer}`);
      expect(request.url).not.toContain(bearer);
      for (const key of [
        "execution_attempt_id",
        "result_manifest_sha256",
        "trace_manifest_sha256",
      ] as const)
        expect(url.searchParams.get(key)).toBe(selection[key]);
      if (kind === "instruments") {
        expect(url.pathname).toMatch(/replay-instruments$/);
        expect(url.searchParams.get("session_date")).toBe(
          selection.session_date,
        );
      }
      if (kind === "market") {
        expect(url.pathname).toMatch(/decision-1\/market$/);
        expect(url.searchParams.get("start")).toBe("2024-01-01");
        expect(url.searchParams.get("end")).toBe("2024-01-03");
        expect(url.searchParams.get("offset")).toBe("100");
      }
      if (kind === "execution") {
        expect(url.pathname).toMatch(/decision-1\/execution$/);
        expect(url.searchParams.get("logical_order_id")).toBe("logical-1");
        expect(url.searchParams.get("offset")).toBe("50");
      }
    },
  );
  it("directs an exact candidate ordinal past the first page without scanning or substituting", async () => {
    respond();
    const { queryClient } = await import("../../src/lib/api");
    await queryClient.fetchQuery(
      api.replayCandidatesOptions(
        {
          ...selection,
          record_kind: "candidate",
          record_id: "B",
          record_ordinal: 123,
          candidate_offset: 100,
        },
        true,
      ),
    );
    const url = new URL(sent().url);
    expect(url.searchParams.get("record_ordinal")).toBe("123");
    expect(url.searchParams.get("stage_id")).toBe("liquidity");
    expect(url.searchParams.get("instrument_id")).toBe("B");
    expect(url.searchParams.get("offset")).toBe("0");
  });
  it("resolves selected feature/predicate ordinal without filtering the ordinary detail page", async () => {
    const { selectedDecisionOptions } =
      await import("../../src/lib/workbench-api");
    const { queryClient } = await import("../../src/lib/api");
    respond();
    await queryClient.fetchQuery(
      selectedDecisionOptions({ ...selection, detail_offset: 0 }, 73),
    );
    expect(new URL(sent().url).searchParams.get("record_ordinal")).toBe("73");
    expect(new URL(sent().url).searchParams.get("offset")).toBe("0");
    respond();
    await queryClient.fetchQuery(
      selectedDecisionOptions({
        ...selection,
        record_ordinal: 73,
        detail_offset: 50,
      }),
    );
    expect(new URL(sent().url).searchParams.has("record_ordinal")).toBe(false);
    expect(new URL(sent().url).searchParams.get("offset")).toBe("50");
  });
  it("does not enable unavailable or incomplete trace reads", () => {
    for (const value of [
      {},
      { ...selection, trace_status: "not_collected" as const },
      { ...selection, code_sha256: undefined },
      { ...selection, trace_manifest_sha256: undefined },
    ]) {
      expect(api.replayEventsOptions(value).enabled).toBe(false);
      expect(api.replayCandidatesOptions(value).enabled).toBe(false);
      expect(api.replayMarketOptions(value).enabled).toBe(false);
      expect(api.replayExecutionOptions(value).enabled).toBe(false);
    }
    expect(
      api.replayMarketOptions({ ...selection, instrument_id: undefined })
        .enabled,
    ).toBe(false);
    expect(
      api.replayCandidatesOptions({ ...selection, decision_id: undefined })
        .enabled,
    ).toBe(false);
  });
  it("never reuses another decision, filter or source pin's cache identity", () => {
    const original = api.replayCandidatesOptions(selection).queryKey;
    for (const patch of [
      { decision_id: "other" },
      { stage_id: "ranking" },
      { result_manifest_sha256: "0".repeat(64) },
      { trace_manifest_sha256: "0".repeat(64) },
      { reason: "selected" as const },
      { score_max: 0 },
      { candidate_offset: 50 },
    ])
      expect(
        api.replayCandidatesOptions({ ...selection, ...patch }).queryKey,
      ).not.toEqual(original);
  });
});
