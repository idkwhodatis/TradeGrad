import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
  Outlet,
} from "@tanstack/react-router";
import { RuleCards } from "../../src/features/research-workbench/rule-cards";
import { workbenchSearch } from "../../src/lib/workbench-selection";
import { detail, hashes, ids, spec } from "./workbench-fixtures";
const network = vi.fn(() => {
  throw new Error("Pure SSR tests must not make network requests.");
});
beforeEach(() => {
  vi.stubGlobal("window", { location: { origin: "https://workbench.test" } });
  vi.stubGlobal("sessionStorage", {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
  vi.stubGlobal("fetch", network);
  network.mockClear();
});
afterEach(() => vi.unstubAllGlobals());
const renderCards = (recorded?: ReturnType<typeof detail>) =>
  renderToString(
    createElement(RuleCards, {
      spec: spec.spec,
      selectedRule: "filter",
      detail: recorded,
      onSelect: () => undefined,
    }),
  );
describe("honest rule-card evidence rendering", () => {
  it("renders expected parameters without inventing actual values", () => {
    const html = renderCards();
    expect(html).toContain("No selected recorded stage");
    expect(html).toContain("minimum");
    expect(html).not.toContain("Actual selected stage observations");
  });
  it("renders recorded zero/false and explicit partial coverage", () => {
    const html = renderCards(detail());
    expect(html).toContain("Actual selected stage observations");
    expect(html).toContain("false");
    expect(html).toContain("partial");
    expect(html).toContain("strategy_emitted");
    expect(html).toContain("0");
  });
  it.each([
    "not_evaluated",
    "unknown",
    "not_sampled",
    "true",
    "false",
  ] as const)("keeps %s condition states visible", (state) => {
    const value = detail();
    value.predicates.items![0].observation.state = state;
    expect(renderCards(value)).toContain(state);
  });
  it.each([
    "warmup_insufficient",
    "input_missing",
    "calculation_error",
    "not_collected",
    "not_sampled",
  ] as const)("keeps %s feature validity visible", (validity) => {
    const value = detail();
    value.features.items![0].observation.validity = validity;
    value.features.items![0].observation.value = null;
    const html = renderCards(value);
    expect(html).toContain(validity);
    expect(html).toContain("Not recorded");
  });
  it("does not promote an absent stage to false", () => {
    const html = renderCards({ ...detail(), stages: [] });
    expect(html).toContain("branch outcome unknown");
    expect(html).not.toContain("Actual selected stage observations");
  });
});
async function renderWorkbench(tab = "description") {
  const { ResearchWorkbenchPage } =
    await import("../../src/features/research-workbench/page");
  const query = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  const cache: [unknown[], unknown][] = [
    [["me"], { id: "owner-1" }],
    [
      ["strategy", ids.strategy],
      {
        id: ids.strategy,
        name: "Controlled fixture",
        code: "def filter():\n    return False\n",
        content_sha256: hashes.code,
        mode: "native_akquant",
      },
    ],
    [
      ["workbench-spec", "strategy_version", ids.strategy, 1, hashes.spec],
      spec,
    ],
    [
      ["workbench-evaluations", "strategy_version", ids.strategy, 0],
      { data: [], count: 0 },
    ],
    [
      ["workbench-revisions", "strategy_version", ids.strategy, 0],
      { data: [spec], count: 1 },
    ],
    [
      ["workbench-lineage", "strategy_version", ids.strategy, 0],
      {
        focus: {
          resource_kind: "strategy_version",
          id: ids.strategy,
          strategy_version_id: ids.strategy,
          code_sha256: hashes.code,
          name: "Controlled fixture",
          kind: "strategy",
          mode: "native_akquant",
          created_at: "2026-10-23T00:00:00Z",
          run_id: null,
          candidate_key: null,
          parent_id: null,
          bookmarked: false,
          evaluation_count: 0,
          evaluations: [],
          evaluations_truncated: false,
          child_count: 0,
        },
        parent: null,
        seed_strategy: null,
        children: [],
        child_count: 0,
        offset: 0,
        limit: 25,
        reference_candidates: [],
        context: {
          status: "not_collected",
          missing_reasons: ["No iteration context"],
        },
        missing_reasons: [],
      },
    ],
  ];
  for (const [key, value] of cache) query.setQueryData(key, value);
  const root = createRootRoute({ component: Outlet });
  const workspace = createRoute({
    getParentRoute: () => root,
    id: "workspace",
    component: Outlet,
  });
  const route = createRoute({
    getParentRoute: () => workspace,
    path: "/strategies/$strategyId/research",
    validateSearch: workbenchSearch,
    component: ResearchWorkbenchPage,
  });
  const router = createRouter({
    routeTree: root.addChildren([workspace.addChildren([route])]),
    history: createMemoryHistory({
      initialEntries: [
        `/strategies/${ids.strategy}/research?tab=${tab}&spec_revision=1&spec_sha256=${hashes.spec}&code_sha256=${hashes.code}`,
      ],
    }),
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
  query.clear();
  expect(network).not.toHaveBeenCalled();
  return html;
}
describe("five-tab workbench shell", () => {
  it("renders exact source/spec identity, all tabs and explicit missing leaf", async () => {
    const html = await renderWorkbench();
    for (const title of [
      "Strategy research workbench",
      "Description",
      "Decision replay",
      "Version comparison",
      "Verification",
      "Notes",
      "No actual leaf Evaluation selected",
      hashes.code,
      hashes.spec,
    ])
      expect(html).toContain(title);
    expect(html).toContain("Expected template flow, not an execution trace");
    expect(html).toContain("Algorithm references (not code parents)");
  });
  it.each([
    ["comparison", "I4"],
    ["verification", "I5"],
    ["notes", "I6"],
  ])("keeps %s honestly pending", async (tab, phase) => {
    const html = await renderWorkbench(tab);
    expect(html).toContain(phase);
    expect(html).toContain("pending: not implemented in this phase");
    expect(html).toContain(`spec_sha256=${hashes.spec}`);
    expect(html).not.toContain("Actual selected stage observations");
  });
});

it("opens real replay controls only after an actual leaf is selected", async () => {
  const html = await renderWorkbench("replay");
  expect(html).toContain("Decision event selection");
  expect(html).toContain("Choose an actual leaf Evaluation");
  expect(html).toContain("Diagnostic replay unavailable");
  expect(html).not.toContain("I3 decision replay is pending");
});
