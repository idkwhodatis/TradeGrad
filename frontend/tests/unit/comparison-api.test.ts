import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { saveSession } from "../../src/lib/session";
import {
  comparisonBody,
  comparisonHash,
  comparisonId,
  page,
  result,
} from "./comparison-fixtures";
import type * as ComparisonApi from "../../src/lib/comparison-api";
let api: typeof ComparisonApi;
const bearer = crypto.randomUUID();
const fetchMock = vi.fn<typeof fetch>();
beforeEach(async () => {
  const values = new Map<string, string>();
  vi.stubGlobal(
    "window",
    Object.assign(new EventTarget(), {
      location: { origin: "https://comparison.example" },
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
  api = await import("../../src/lib/comparison-api");
  (await import("../../src/lib/api")).queryClient.clear();
});
afterEach(() => vi.unstubAllGlobals());
const sent = () => fetchMock.mock.calls.at(-1)![0] as Request;
function respond(body: unknown = {}, status = 200) {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}
describe("generated bounded authenticated comparison APIs", () => {
  it("creates the exact ordered read-only job with durable key and no strategy/model side effect", async () => {
    respond({}, 202);
    const key = crypto.randomUUID();
    await api.createComparison(comparisonBody, key);
    expect(new URL(sent().url).pathname).toBe("/api/v1/comparisons");
    expect(sent().method).toBe("POST");
    expect(sent().headers.get("Authorization")).toBe(`Bearer ${bearer}`);
    expect(sent().headers.get("Idempotency-Key")).toBe(key);
    expect(await sent().clone().json()).toEqual(comparisonBody);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("preserves explicit null historical absence in the request", async () => {
    respond({}, 202);
    await api.createComparison(
      {
        ...comparisonBody,
        left: { ...comparisonBody.left, trace_manifest_sha256: null },
      },
      crypto.randomUUID(),
    );
    expect((await sent().clone().json()).left).toHaveProperty(
      "trace_manifest_sha256",
      null,
    );
  });
  it("reads status separately from admitted immutable source/result", async () => {
    respond();
    const { queryClient } = await import("../../src/lib/api");
    await queryClient.fetchQuery(api.comparisonStatusOptions(comparisonId));
    expect(new URL(sent().url).pathname).toBe(
      `/api/v1/comparisons/${comparisonId}`,
    );
    respond();
    await queryClient.fetchQuery(
      api.comparisonResultOptions(comparisonId, comparisonHash),
    );
    expect(new URL(sent().url).pathname).toBe(
      `/api/v1/comparisons/${comparisonId}/result`,
    );
    expect(new URL(sent().url).searchParams.get("manifest_sha256")).toBe(
      comparisonHash,
    );
  });
  it("queries a bounded section with all exact filters and no date/symbol pairing", async () => {
    respond();
    const { queryClient } = await import("../../src/lib/api");
    await queryClient.fetchQuery(
      api.comparisonSectionOptions(comparisonId, comparisonHash, "predicates", {
        offset: 50,
        event_pair_id: "exact-pair",
        instrument_id: "B",
      }),
    );
    const url = new URL(sent().url);
    expect(url.pathname).toBe(
      `/api/v1/comparisons/${comparisonId}/sections/predicates`,
    );
    for (const [key, value] of Object.entries({
      manifest_sha256: comparisonHash,
      offset: "50",
      limit: "25",
      event_pair_id: "exact-pair",
      instrument_id: "B",
    }))
      expect(url.searchParams.get(key)).toBe(value);
    expect(url.searchParams.has("date")).toBe(false);
    expect(url.searchParams.has("token")).toBe(false);
  });
  it("gives selected event its own query identity independent of table paging/filter", () => {
    const table = api.comparisonSectionOptions(
      comparisonId,
      comparisonHash,
      "events",
      { offset: 50 },
    );
    const selected = api.comparisonSectionOptions(
      comparisonId,
      comparisonHash,
      "events",
      { event_pair_id: "same-day-pair-2" },
    );
    expect(table.queryKey).not.toEqual(selected.queryKey);
    expect(
      api.comparisonSectionOptions(comparisonId, "0".repeat(64), "events")
        .queryKey,
    ).not.toEqual(table.queryKey);
  });
  it.each(["cancel", "retry"] as const)(
    "uses exact keyed %s route without replacement inputs",
    async (action) => {
      respond({}, 202);
      const key = crypto.randomUUID();
      await api.changeComparison(comparisonId, action, key);
      expect(new URL(sent().url).pathname).toBe(
        `/api/v1/comparisons/${comparisonId}/${action}`,
      );
      expect(sent().headers.get("Idempotency-Key")).toBe(key);
      expect(await sent().clone().text()).toBe("");
    },
  );
  it.each([403, 404, 409, 422, 503])(
    "propagates %i error without zero-result fallback or new POST",
    async (status) => {
      respond(
        {
          code: "comparison_admission_failed",
          message: "Exact saved evidence is not admitted",
        },
        status,
      );
      await expect(
        api.createComparison(comparisonBody, crypto.randomUUID()),
      ).rejects.toMatchObject({ code: "comparison_admission_failed" });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );
  it("propagates network uncertainty without retrying with a fresh key", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(
      api.createComparison(comparisonBody, crypto.randomUUID()),
    ).rejects.toThrow("Failed to fetch");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("paginates owned source choices rather than silently omitting after the first hundred", async () => {
    respond();
    const { queryClient } = await import("../../src/lib/api");
    await queryClient.fetchQuery(api.comparisonStrategyOptions(125));
    expect(new URL(sent().url).searchParams.get("offset")).toBe("125");
    expect(new URL(sent().url).searchParams.get("limit")).toBe("25");
  });
});
describe("section pages must match immutable envelope and actual request", () => {
  it("accepts the exact bounded page", () =>
    expect(api.comparisonPageMatches(page(), result(), "events", 0)).toBe(
      true,
    ));
  it.each([
    "comparison_id",
    "manifest_sha256",
    "input_sha256",
    "section",
  ] as const)("rejects stale page %s", (key) =>
    expect(
      api.comparisonPageMatches(
        { ...page(), [key]: "other" },
        result(),
        "events",
        0,
      ),
    ).toBe(false),
  );
  it("rejects mismatched offset, bound, pair or instrument, not merely the same date", () => {
    expect(
      api.comparisonPageMatches(
        { ...page(), offset: 25 },
        result(),
        "events",
        0,
      ),
    ).toBe(false);
    expect(
      api.comparisonPageMatches(
        { ...page(), limit: 1000 },
        result(),
        "events",
        0,
      ),
    ).toBe(false);
    expect(
      api.comparisonPageMatches(
        page(),
        result(),
        "events",
        0,
        "another-same-date-event",
      ),
    ).toBe(false);
    expect(
      api.comparisonPageMatches(page(), result(), "events", 0, "pair-1", "B"),
    ).toBe(false);
  });
});

it("admits a multi-event economic row only through its exact saved association, not date proximity", () => {
  const association = {
    side: "left" as const,
    event_pair_id: "explicit-pair",
    decision_id: "decision-a",
    stage_id: "execution",
    logical_order_id: "logical-a",
    instrument_id: "B",
    link_ordinal: 0,
    source_event_paired: false,
    child_order_ids: ["order-a"],
    fill_ids: ["fill-a"],
  };
  const value = page("fills", [
    {
      ...page().items[0],
      section: "fills",
      event_pair_id: null,
      instrument_id: "B",
      associations: [
        association,
        {
          ...association,
          event_pair_id: "another-pair",
          decision_id: "decision-b",
        },
      ],
    },
  ]);
  expect(
    api.comparisonPageMatches(
      value,
      result(),
      "fills",
      0,
      "explicit-pair",
      "B",
    ),
  ).toBe(true);
  expect(
    api.comparisonPageMatches(
      value,
      result(),
      "fills",
      0,
      "same-date-but-absent-pair",
      "B",
    ),
  ).toBe(false);
  expect(
    api.comparisonPageMatches(
      value,
      result(),
      "fills",
      0,
      "explicit-pair",
      "C",
    ),
  ).toBe(false);
  const nonEconomic = {
    ...value,
    section: "candidates",
    items: value.items.map((row) => ({ ...row, section: "candidates" })),
  };
  expect(
    api.comparisonPageMatches(
      nonEconomic,
      result(),
      "candidates",
      0,
      "explicit-pair",
      "B",
    ),
  ).toBe(false);
});
