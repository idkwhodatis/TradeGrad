import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { createElement, type ComponentType } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import {
  portableFixture,
  portableResultFixture,
  portableRunFixture,
} from "./portable-fixtures";
import {
  loadPortableDraft,
  portableSubmissionFor,
  savePortableDraft,
} from "../../src/features/portable/durable";
const cacheKey = (resource: string, ...parts: unknown[]) => [
  "portable",
  "owner-a",
  resource,
  ...parts,
];
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => data.set(k, v),
    removeItem: (k: string) => data.delete(k),
  });
  vi.stubGlobal("window", { location: { origin: "https://portable.test" } });
});
afterEach(() => vi.unstubAllGlobals());
async function markup<P extends object>(
  Component: ComponentType<P>,
  props: P,
  cache: [unknown[], unknown][] = [],
  errors: [unknown[], Error][] = [],
) {
  const query = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  cache.forEach(([key, data]) => query.setQueryData(key, data));
  errors.forEach(([key, error]) => {
    query.setQueryData(key, {});
    query
      .getQueryCache()
      .find({ queryKey: key })!
      .setState({ status: "error", error });
  });
  const root = createRootRoute({
    component: () => createElement(Component, props),
  });
  const router = createRouter({
    routeTree: root,
    history: createMemoryHistory({ initialEntries: ["/"] }),
    isServer: true,
  });
  await router.load();
  return renderToString(
    createElement(
      QueryClientProvider,
      { client: query },
      createElement(RouterProvider, { router }),
    ),
  );
}
// Cached SSR renders and mock transport are not browser/worker/container acceptance.
describe("portable render contracts", () => {
  it("shows both explicit policy templates and never offers native conversion", async () => {
    const { PortableInputPicker } =
      await import("../../src/features/portable/experiment");
    const html = await markup(PortableInputPicker, {
      owner: "owner-a",
      catalog: portableFixture().catalog,
      onConfigured: () => {},
    });
    for (const phrase of [
      "PortfolioPolicy",
      "EventPolicy",
      "China",
      "United States",
      "Published dataset version",
      "Exact universe version",
      "Portable seed version",
      "No native strategy is converted automatically",
    ])
      expect(html).toContain(phrase);
    expect(html).toContain("Create reference template version");
    expect(html).not.toContain('type="password"');
  });
  it("empty catalogs explain synthetic input setup and do not fake an eligible dataset", async () => {
    const { PortableInputPicker } =
      await import("../../src/features/portable/experiment");
    const html = await markup(PortableInputPicker, {
      owner: "owner-a",
      catalog: { datasets: [], universes: [], strategies: [], templates: [] },
      onConfigured: () => {},
    });
    expect(html).toContain("No published dataset is available");
    expect(html).toContain(
      "explicitly synthetic portable raw/no-actions fixture",
    );
    expect(html).toMatch(
      /<button[^>]*disabled=""[^>]*>Create reference template version/,
    );
  });
  it("restored config requires fresh exact preflight and keeps all hashes and metadata-only holdout visible", async () => {
    const { PortableConfigForm } =
      await import("../../src/features/portable/experiment");
    const { body, catalog } = portableFixture();
    const html = await markup(PortableConfigForm, {
      owner: "owner-a",
      initial: body,
      catalog,
      onChangeInputs: () => {},
    });
    for (const phrase of [
      "Seed manifest SHA-256",
      "Seed parameters SHA-256",
      "Deployment environment SHA-256",
      "Metadata-only holdout",
      "Warm-up exchange sessions per listing",
      "Maximum model calls",
      "Maximum synthetic tokens",
      "Run strict portable preflight",
    ])
      expect(html).toContain(phrase);
    expect(html).toMatch(
      /<button[^>]*disabled=""[^>]*>Submit portable experiment/,
    );
    expect(html).not.toContain("Portable preflight passed");
  });
  it("lost-response recovery restores original values/key but never an old preflight", async () => {
    const { PortableConfigForm } =
      await import("../../src/features/portable/experiment");
    const { body, catalog } = portableFixture();
    body.budgets.max_runtime_seconds = 555;
    savePortableDraft("owner-a", {
      version: 1,
      body,
      pending: portableSubmissionFor(body, null),
    });
    const html = await markup(PortableConfigForm, {
      owner: "owner-a",
      initial: loadPortableDraft("owner-a")!.body,
      catalog,
      onChangeInputs: () => {},
    });
    expect(html).toContain('value="555"');
    expect(html).toContain("Retry same portable submission");
    expect(html).toContain("original immutable body and key");
    expect(html).not.toContain("Portable preflight passed");
    const other = await markup(PortableConfigForm, {
      owner: "owner-b",
      initial: portableFixture().body,
      catalog,
      onChangeInputs: () => {},
    });
    expect(other).not.toContain('value="555"');
    expect(other).not.toContain("Retry same portable submission");
  });
  it("cancel requested is not a stopped sandbox or a successful result", async () => {
    const { PortableResearchContent } =
      await import("../../src/features/portable/research");
    const run = portableRunFixture();
    run.status = "cancel_requested";
    const html = await markup(
      PortableResearchContent,
      { owner: "owner-a", run },
      [
        ...["candidates", "evaluations", "checkpoints", "model-calls"].map(
          (name) =>
            [cacheKey(name, run.id, 0), { data: [], count: 0 }] as [
              unknown[],
              unknown,
            ],
        ),
        [
          cacheKey("usage", run.id),
          {
            calls: 0,
            adopted_calls: 0,
            input_tokens: 0,
            output_tokens: 0,
            charged_tokens: 0,
            unknown_usage_calls: 0,
            usage_sources: [],
          },
        ],
      ],
    );
    expect(html).toContain("not proof of stopped execution");
    expect(html).toContain("No completed portable summary");
    expect(html).toContain("Runtime certification");
    expect(html).toContain("unverified until actual CI");
    expect(html).toContain(run.portable_policy_image_digest!);
    expect(html).not.toContain(">Resume from portable checkpoint");
    expect(html).not.toContain(">Open portable result");
  });
  it("renders actual precision, both image IDs and synthetic limits without claiming runtime certification", async () => {
    const { PortableResultContent } =
      await import("../../src/features/portable/result");
    const data = portableResultFixture();
    const html = await markup(
      PortableResultContent,
      { owner: "owner-a", evaluationId: data.evaluation_id },
      [
        [
          cacheKey("results", data.evaluation_id, "account_series", 0, 50),
          data,
        ],
        [
          cacheKey("results", data.evaluation_id, "decision_trace", 0, 50),
          {
            ...data,
            section: "decision_trace",
            rows: [
              {
                state_before_sha256: "before",
                state_after_sha256: "after",
                warmup: true,
                outcome: "warmup_state_only",
                decision: { kind: "target_portfolio", action: "no_decision" },
              },
            ],
            count: 1,
          },
        ],
      ],
    );
    expect(html).toContain("1000234.567");
    expect(html).toContain("1704697200000000000");
    expect(html).toContain(data.image_digest);
    expect(html).toContain(data.policy_image_digest);
    expect(html).toContain("Explicit synthetic raw/no-actions fixture");
    expect(html).toContain("warmup_state_only");
    expect(html).toContain("no_decision");
    expect(html).toContain("bounded page");
    expect(html).not.toContain("native_strategy_and_engine_share_process");
  });
  it("blocks a stale/mismatched result and exposes actionable API failure rather than stale rows", async () => {
    const { PortableResultContent } =
      await import("../../src/features/portable/result");
    const data = portableResultFixture();
    const key = cacheKey(
      "results",
      data.evaluation_id,
      "account_series",
      0,
      50,
    );
    const bad = await markup(
      PortableResultContent,
      { owner: "owner-a", evaluationId: data.evaluation_id },
      [[key, { ...data, evaluation_id: "wrong" }]],
    );
    expect(bad).toContain("does not match this portable evaluation");
    expect(bad).not.toContain("1000234.567");
    const failure = await markup(
      PortableResultContent,
      { owner: "owner-a", evaluationId: data.evaluation_id },
      [],
      [[key, new Error("Portable result integrity verification failed")]],
    );
    expect(failure).toContain("integrity verification failed");
    expect(failure).not.toContain("1000234.567");
  });
  it("empty persisted rows never synthesize decisions, returns or zeros", async () => {
    const { PortableTable } =
      await import("../../src/features/portable/result");
    const html = await markup(PortableTable, {
      rows: [],
      label: "Decision trace",
    });
    expect(html).toContain("No persisted rows");
    expect(html).not.toContain("<td");
  });
  it("maps only actual finite series values and preserves lossless string timestamps", async () => {
    const { portableChart, portableResultContextMatches } =
      await import("../../src/features/portable/result");
    const data = portableResultFixture();
    expect(
      portableChart(
        [
          {
            native_at: "2024-01-08T07:00:00Z",
            equity: null,
            cash: Infinity,
            drawdown: 0.001,
          },
        ],
        ["equity", "cash", "drawdown"],
        "CNY",
      ).rows,
    ).toEqual([["2024-01-08T07:00:00Z", null, null, 0.001]]);
    expect(
      portableResultContextMatches(
        data,
        data.evaluation_id,
        "account_series",
        0,
      ),
    ).toBe(true);
    expect(
      portableResultContextMatches(
        data,
        data.evaluation_id,
        "decision_trace",
        0,
      ),
    ).toBe(false);
    expect(
      portableResultContextMatches(
        data,
        data.evaluation_id,
        "account_series",
        50,
      ),
    ).toBe(false);
  });
});
