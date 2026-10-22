import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { capabilityFixture, researchFixture } from "./research-fixtures";
import { saveResearchDraft } from "../../src/lib/research-submit";

beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  vi.stubGlobal("window", { location: { origin: "http://research.test" } });
});
afterEach(() => vi.unstubAllGlobals());
async function markup(step: number) {
  const { NewExperimentPage } =
    await import("../../src/features/new-experiment");
  const { body, catalog } = researchFixture();
  saveResearchDraft("owner-1", { version: 1, body, step, submission: null });
  const query = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  query.setQueryData(["research-input-catalog"], catalog);
  query.setQueryData(["research-capabilities"], capabilityFixture());
  query.setQueryData(["me"], { id: "owner-1" });
  const root = createRootRoute({
    component: () => createElement(NewExperimentPage),
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
// Node SSR is a render contract, not a replacement for Playwright interaction.
describe("wizard server render contract", () => {
  it("renders labeled exact-version controls and fixture-only scope", async () => {
    const html = await markup(0);
    for (const label of [
      "Published dataset version",
      "Exact universe version",
      "Seed strategy version",
      "Market &amp; task",
      "Execution profile",
    ])
      expect(html).toContain(label);
    expect(html).toContain('id="dataset.id"');
    expect(html).toContain("deterministic fixture-model optimization only");
    expect(html).not.toContain('type="password"');
  });
  it("restores the window step with exclusive ends and metadata-only holdout", async () => {
    const html = await markup(1);
    expect(html).toContain("Training end · exclusive");
    expect(html).toContain("Validation start · inclusive");
    expect(html).toContain("Holdout end · exclusive");
    expect(html).toContain("Warm-up sessions per listing");
  });
  it("renders all implemented numeric budget controls, never a paid model selector", async () => {
    const html = await markup(2);
    for (const label of [
      "Maximum iterations",
      "Maximum model calls",
      "Maximum tokens",
      "Maximum runtime · seconds",
      "Maximum repair attempts",
      "Deployment environment digest",
    ])
      expect(html).toContain(label);
    expect(html).toContain("0.0005 means 0.05%");
    expect(html).toContain("no browser credentials enter configuration");
    expect(html).not.toContain('name="model.credential_ref"');
  });
  it("never enables submit just because a draft restored at review", async () => {
    const html = await markup(3);
    expect(html).toContain("Run server preflight");
    expect(html).toMatch(
      /<button[^>]*disabled=""[^>]*>[\s\S]*?Submit experiment/,
    );
    expect(html).toContain("Review every immutable RunConfig field");
    expect(html).not.toContain("Preflight passed");
  });
});
