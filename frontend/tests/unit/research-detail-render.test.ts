import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement, type ComponentType } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import type {
  ResearchRunDetail,
  ResearchUsagePublic,
} from "../../src/client/types.gen";
import {
  operatorCapabilityFixture,
  researchFixture,
} from "./research-fixtures";

const runId = "f1111111-1111-4111-8111-111111111111";
const at = "2026-10-23T00:00:00Z";
const network = vi.fn(() => {
  throw new Error("A pure SSR contract must not make network requests");
});
beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  vi.stubGlobal("window", { location: { origin: "https://render.test" } });
  network.mockClear();
  vi.stubGlobal("fetch", network);
});
afterEach(() => vi.unstubAllGlobals());

function runFixture(operator = true): ResearchRunDetail {
  const { body } = researchFixture();
  if (operator) body.model = operatorCapabilityFixture().profile.config;
  return {
    id: runId,
    config: body,
    status: "interrupted",
    generation: 1,
    dataset_version_id: body.dataset.id,
    strategy_version_id: body.seed_strategy.id,
    active_attempt_id: null,
    attempts: [],
    next_event_seq: 0,
    result: null,
    error: null,
    latest_checkpoint_id: null,
    experiment_id: null,
    split_version_id: null,
    created_at: at,
    updated_at: at,
  };
}
function usageFixture(): ResearchUsagePublic {
  return {
    run_id: runId,
    calls: 1,
    adopted_calls: 0,
    input_tokens: null,
    output_tokens: null,
    charged_tokens: 2048,
    known_usage_calls: 0,
    unknown_usage_calls: 1,
    usage_sources: ["conservative_reservation"],
    cost: null,
    cost_status: "unavailable",
  };
}
async function renderPage(
  Component: ComponentType,
  cache: [unknown[], unknown][],
  path = "/research-runs/$runId",
  entry = `/research-runs/${runId}`,
) {
  const query = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  cache.forEach(([key, data]) => query.setQueryData(key, data));
  const root = createRootRoute();
  const route = createRoute({
    getParentRoute: () => root,
    path,
    component: () => createElement(Component),
  });
  const router = createRouter({
    routeTree: root.addChildren([route]),
    history: createMemoryHistory({ initialEntries: [entry] }),
    isServer: true,
  });
  await router.load();
  const html = renderToString(
    createElement(
      QueryClientProvider,
      { client: query },
      createElement(RouterProvider, { router }),
    ),
  );
  expect(network).not.toHaveBeenCalled();
  query.clear();
  return html.replace(/\s+/g, " ");
}
async function researchMarkup(run = runFixture(), usage = usageFixture()) {
  const { ResearchDetailPage } =
    await import("../../src/features/research-detail");
  const empty = { data: [], count: 0 };
  return renderPage(ResearchDetailPage, [
    [["research-run", run.id], run],
    [["research-usage", run.id], usage],
    [["events", run.id], []],
    [
      ["dataset", run.dataset_version_id],
      researchFixture().catalog.datasets[0],
    ],
    ...[
      "research-candidates",
      "research-evaluations",
      "research-checkpoints",
      "research-model-calls",
      "artifacts",
    ].map((key): [unknown[], unknown] => [[key, run.id, 0], empty]),
  ]);
}

// Cached local SSR fixtures verify wording/controls, not provider or browser execution.
describe("model recovery and billing render contracts", () => {
  it("discloses that failed logical calls are not retried and a new experiment can incur a new charge", async () => {
    const html = await researchMarkup();
    expect(html).toContain(
      "Failed or uncertain model calls, including HTTP 429/5xx, are not automatically retried on resume.",
    );
    expect(html).toContain(
      "A deliberately new experiment is a separate potentially billed action.",
    );
    expect(html).toContain(
      "Usage may be actual tokens or a conservative reservation; no dollar cost is inferred.",
    );
  });
  it("does not display conservative reservations as actual usage or invent a price", async () => {
    const html = await researchMarkup();
    expect(html).toContain("<dt>Input tokens</dt><dd>Unknown</dd>");
    expect(html).toContain("<dt>Output tokens</dt><dd>Unknown</dd>");
    expect(html).toContain("<dt>Budget-charged tokens</dt><dd>2048</dd>");
    expect(html).toContain(
      "<dt>Currency cost</dt><dd>Unavailable: no verified price</dd>",
    );
    expect(html).toContain(
      "Budget-charged reservations are not measured provider tokens or money.",
    );
    expect(html).toContain("No token-to-currency estimate is invented.");
  });
  it("keeps fixture zero usage distinct from unknown values and paid-provider notices", async () => {
    const usage = {
      ...usageFixture(),
      input_tokens: 0,
      output_tokens: 0,
      charged_tokens: 0,
      known_usage_calls: 1,
      unknown_usage_calls: 0,
      usage_sources: ["synthetic_fixture"],
    };
    const html = await researchMarkup(runFixture(false), usage);
    expect(html).toContain(
      "Deterministic model fixture, not a paid or live model.",
    );
    expect(html).toContain("<dt>Input tokens</dt><dd>0</dd>");
    expect(html).not.toContain(
      "Failed or uncertain model calls, including HTTP 429/5xx",
    );
  });
  it("keeps missing model-stop evidence visibly blocked rather than implying a dead worker stopped billing", async () => {
    const run = runFixture();
    run.error = "Recovery blocked: missing model process termination receipt";
    run.latest_checkpoint_id = "f2222222-2222-4222-8222-222222222222";
    run.attempts = [
      {
        id: "f3333333-3333-4333-8333-333333333333",
        generation: 1,
        status: "interrupted",
        sandbox_stopped: false,
        exit_code: null,
        error: run.error,
        started_at: at,
        finished_at: null,
        heartbeat_at: at,
      },
    ];
    const html = await researchMarkup(run);
    expect(html).toContain(
      "Model-process stop evidence is missing. Recovery stays blocked and no request is automatically retried.",
    );
    expect(html).toContain(
      "An operator must investigate; a timeout or dead worker alone does not prove provider billing stopped.",
    );
    expect(html).not.toMatch(
      /<button[^>]*>[\s\S]*?Resume checkpoint<\/button>/,
    );
  });
  it("describes native operator access without expanding portable or legacy retry scope", async () => {
    const { OverviewPage } = await import("../../src/features/overview");
    const html = await renderPage(
      OverviewPage,
      [
        [["runs", 0, 100], { data: [], count: 0 }],
        [["datasets"], { data: [], count: 0 }],
        [["strategies"], { data: [], count: 0 }],
      ],
      "/",
      "/",
    );
    expect(html).toContain(
      "Fixture models are the default; explicitly enabled operator profiles may be available for native research.",
    );
    expect(html).toContain(
      "Legacy seed evaluations remain separate and restart the fixed seed on retry.",
    );
  });
});
