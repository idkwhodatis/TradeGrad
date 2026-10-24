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
import {
  capabilityFixture,
  operatorCapabilityFixture,
  researchFixture,
} from "./research-fixtures";
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
async function markup(
  step: number,
  capabilities = capabilityFixture(),
  body = researchFixture().body,
  stored?: string,
  savedDefaults: object | null = {
    initial_cash: 250000,
    max_iterations: 3,
    max_model_calls: 20,
    max_tokens: 50000,
    max_runtime_seconds: 600,
    model_profile_id: "fixture",
  },
) {
  const { NewExperimentPage } =
    await import("../../src/features/new-experiment");
  const { catalog } = researchFixture();
  if (stored !== undefined)
    sessionStorage.setItem("tradegrad.research-draft.v1.owner-1", stored);
  else
    saveResearchDraft("owner-1", { version: 1, body, step, submission: null });
  const query = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  query.setQueryData(["research-input-catalog"], catalog);
  query.setQueryData(["research-capabilities"], capabilities);
  query.setQueryData(["me"], { id: "owner-1" });
  if (savedDefaults) query.setQueryData(["workspace-defaults"], savedDefaults);
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
  it("renders all implemented budget controls and only the available fixture profile", async () => {
    const html = await markup(2);
    for (const label of [
      "Maximum iterations",
      "Maximum model calls",
      "Maximum tokens",
      "Maximum runtime · seconds",
      "Maximum repair attempts",
      "Deployment environment digest",
      "Model profile",
      "Observation capture mode",
      "Observation stage IDs",
      "Observation instrument IDs",
      "Maximum saved decisions",
      "Maximum saved detail rows",
      "Maximum uncompressed capture bytes",
    ])
      expect(html).toContain(label);
    expect(html).toContain("0.0005 means 0.05%");
    expect(html).toContain("no browser credentials enter configuration");
    expect(html).not.toContain('name="model.credential_ref"');
    expect(html).toContain("fixture / synthetic");
    expect(html).not.toContain("operator / paid calls");
  });
  it("restores editable capture scope and shows it in immutable review", async () => {
    const { body } = researchFixture();
    body.research_capture = {
      mode: "research",
      observation_window: { start: "2024-04-01", end: "2024-05-01" },
      stage_ids: ["ranking"],
      instrument_ids: ["listing-a"],
      max_decisions: 120,
    };
    const html = await markup(2, capabilityFixture(), body);
    expect(html).toContain('id="research_capture.mode"');
    expect(html).toContain('value="research" selected=""');
    expect(html).toContain('id="research_capture.observation_window.end"');
    expect(html).toContain('value="2024-05-01"');
    expect(html).toContain('value="120"');
    expect(html).toContain("ranking");
    const review = await markup(3, capabilityFixture(), body);
    expect(review).toContain("Research capture");
    expect(review).toContain(
      "research · [2024-04-01, 2024-05-01) · 1 stages · 1 instruments",
    );
    expect(review).not.toContain("Preflight passed");
  });
  it("renders owner-enabled operator profiles with pinned parameters and paid disclosure", async () => {
    const { capabilities, profile } = operatorCapabilityFixture();
    const { body } = researchFixture();
    body.model = profile.config;
    const html = await markup(2, capabilities, body);
    expect(html).toContain(`value="${profile.id}" selected=""`);
    expect(html).toContain("operator / paid calls");
    expect(html).toContain("not a currency spending cap");
    expect(html).toContain(
      "Strategy code, task instructions and research feedback",
    );
    expect(html).toContain(profile.config.profile_sha256);
    expect(html).toContain("Maximum output tokens per call");
    expect(html).not.toContain('name="model.temperature"');
    expect(html).not.toContain('name="model.max_output_tokens"');
    expect(html).not.toContain('type="password"');
    expect(html).not.toContain("deterministic fixture-model optimization only");
  });
  it("labels an operator's synthetic endpoint separately from paid evidence", async () => {
    const { capabilities, profile } = operatorCapabilityFixture(true);
    const { body } = researchFixture();
    body.model = profile.config;
    const html = await markup(3, capabilities, body);
    expect(html).toContain("This operator profile is marked synthetic");
    expect(html).toContain("not paid-model quality or cost evidence");
    expect(html).not.toContain("This operator-managed profile can make paid");
    expect(html).toContain("No live model calls are enabled");
  });
  it("warns before replacing an incompatible saved draft instead of claiming it was restored", async () => {
    const html = await markup(
      0,
      capabilityFixture(),
      researchFixture().body,
      '{"version":0}',
    );
    expect(html).toContain('data-testid="research-draft-unrestored"');
    expect(html).toContain("It has not been translated or resubmitted");
    expect(html).not.toContain("Restored this signed-in owner");
  });
  it("does not silently substitute fixture for a revoked or changed restored profile", async () => {
    const { profile } = operatorCapabilityFixture();
    const { body } = researchFixture();
    body.model = profile.config;
    const html = await markup(2, capabilityFixture(), body);
    expect(html).toContain('data-testid="research-model-unavailable"');
    expect(html).toContain("No profile is substituted");
    expect(html).toContain('<option value="" selected="">');
    expect(html).not.toContain('value="fixture" selected=""');
  });
  it("restores exact draft without waiting for saved defaults", async () => {
    const html = await markup(
      3,
      capabilityFixture(),
      researchFixture().body,
      undefined,
      null,
    );
    expect(html).toContain("Review every immutable RunConfig field");
    expect(html).not.toContain("Loading published versions");
  });
  it("does not apply a stale default profile over a recovered draft", async () => {
    const html = await markup(
      2,
      capabilityFixture(),
      researchFixture().body,
      undefined,
      { model_profile_id: "revoked" },
    );
    expect(html).toContain("Maximum iterations");
    expect(html).not.toContain("default model profile is no longer available");
  });
  it("rejects a stale model default for fresh initialization instead of silently substituting", async () => {
    const html = await markup(
      0,
      capabilityFixture(),
      researchFixture().body,
      "{}",
      { model_profile_id: "revoked" },
    );
    expect(html).toContain("default model profile is no longer available");
    expect(html).not.toContain("Submit experiment");
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
