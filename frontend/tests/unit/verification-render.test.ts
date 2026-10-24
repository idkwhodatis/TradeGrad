import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement, type ReactNode } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
vi.hoisted(() => {
  vi.stubGlobal("window", {
    location: { origin: "https://verification.test" },
  });
});
import {
  FrozenVerificationInputs,
  FixedConditionFields,
  IndependentJob,
} from "../../src/features/research-workbench/verification-panels";
import { DiagnosticParitySummary } from "../../src/features/research-workbench/diagnostic-parity";
import { ControlledVerification } from "../../src/features/research-workbench/controlled-verification";
import { IndependentResults } from "../../src/features/research-workbench/independent-results";
import {
  capability,
  controlledExperiment,
  independentJob,
  independentPin,
  independentSource,
  parityReport,
  verificationSelection,
  verificationIds as id,
} from "./verification-fixtures";
import { ids } from "./workbench-fixtures";
const network = vi.fn(() => {
  throw new Error("Pure SSR tests never run an evaluator or network request");
});
beforeEach(() => {
  vi.stubGlobal("sessionStorage", {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
  vi.stubGlobal("fetch", network);
  network.mockClear();
});
afterEach(() => vi.unstubAllGlobals());
const render = (element: ReactNode) =>
  renderToString(element).replaceAll("<!-- -->", "");
async function routed(element: ReactNode, cache: [unknown[], unknown][] = []) {
  const query = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
    },
  });
  for (const [key, value] of cache) query.setQueryData(key, value);
  const root = createRootRoute({ component: () => element });
  const route = createRoute({
    getParentRoute: () => root,
    path: "/strategies/$strategyId/research",
  });
  const router = createRouter({
    routeTree: root.addChildren([route]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
    isServer: true,
  });
  await router.load();
  const html = render(
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
describe("honest frozen capability and replay reporting", () => {
  it("shows original start plus warmup separately from observation window and inherited exposure", () => {
    const html = render(
      createElement(FrozenVerificationInputs, { capability: capability() }),
    );
    for (const text of [
      "Original physical attempt",
      "Original result SHA-256",
      "original Evaluation start",
      "original warmup",
      "observation window",
      "Inherited family and owner-wide",
      "warmup.sessions",
      "Exact frozen original configuration",
      "RNG",
      "not a complete engine-state checkpoint",
    ])
      expect(html).toContain(text);
  });
  it("displays explicit missing environment/RNG and never says a replay succeeded", () => {
    const value = capability();
    value.can_execute = false;
    value.unavailable_reasons = [
      "original_rng_not_recorded",
      "original_environment_unavailable",
    ];
    const html = render(
      createElement(FrozenVerificationInputs, { capability: value }),
    );
    expect(html).toContain("original_rng_not_recorded");
    expect(html).toContain("original_environment_unavailable");
    expect(html).not.toContain("Verified diagnostic observation");
  });
  it("retains false, zero and unknown fixed fields", () => {
    const html = render(
      createElement(FixedConditionFields, {
        conditions: { fee: 0, short: false, rng: null },
      }),
    );
    expect(html).toContain(">0<");
    expect(html).toContain("false");
    expect(html).toContain("Unknown");
  });
  it.each(["matched", "mismatch", "not_verifiable"] as const)(
    "labels %s report with named semantic checks, coverage, tolerances and limitations",
    (outcome) => {
      const report = parityReport();
      report.outcome = outcome;
      if (outcome !== "matched") {
        report.checks[0].status = outcome;
        report.checks[0].reason = "original evidence insufficient";
        report.mapping_counts = { unverified_observed_pairing: 1 };
      }
      const html = render(createElement(DiagnosticParitySummary, { report }));
      expect(html).toContain(outcome);
      for (const check of report.checks) expect(html).toContain(check.key);
      expect(html).toContain("Equal returns alone never pass");
      expect(html).toContain("Exact observation scope");
      expect(html).toContain("Predetermined field-specific");
      expect(html).toContain("not a causal proof");
      expect(html).toContain(
        outcome === "matched"
          ? "Verified diagnostic observation within"
          : "Unverified new observation",
      );
    },
  );
});
describe("durable independent controls and failure visibility", () => {
  it("renders cancel without optimizer pause/resume and keeps incomplete sandbox stops nonretryable", async () => {
    const job = independentJob();
    job.status = "failed";
    job.error = "runtime_failed";
    job.result_pin = null;
    job.result_ready = false;
    job.attempts[0].sandbox_stopped = false;
    const html = await routed(
      createElement(IndependentJob, { owner: "owner", job }),
    );
    expect(html).toContain("runtime_failed");
    expect(html).toContain("not a zero-return successful result");
    expect(html).toMatch(
      /disabled=""[^>]*>Retry independent run from original start/,
    );
    expect(html).not.toContain("Pause run");
    expect(html).not.toContain("Resume optimizer");
  });
  it("renders two/three-arm manual authoring, immutable baseline and real patch without model/provider form", async () => {
    const html = await routed(
      createElement(ControlledVerification, {
        owner: "owner",
        source: independentSource,
        strategyVersionId: ids.strategy,
        capability: capability(),
        split: "train",
        selection: verificationSelection,
        update: () => undefined,
      }),
    );
    for (const text of [
      "Hypothesis",
      "Planned change and isolated mechanism",
      "Copied baseline",
      "Add third arm",
      "Business parameter values",
      "Controlled baseline and variant source diff",
      "not individually validated",
      "Create 2-arm controlled experiment",
    ])
      expect(html).toContain(text);
    expect(html).not.toContain('id="model');
    expect(html).not.toContain('id="optimizer');
    expect(html).not.toContain("provider credential");
  });
  it("disallows holdout controls and missing RNG independently of diagnostic executability", async () => {
    const cap = capability();
    cap.can_control = false;
    cap.control_unavailable_reasons = ["original_rng_not_recorded"];
    const html = await routed(
      createElement(ControlledVerification, {
        owner: "owner",
        source: independentSource,
        strategyVersionId: ids.strategy,
        capability: cap,
        split: "holdout",
        selection: verificationSelection,
        update: () => undefined,
      }),
    );
    expect(html).toContain("Controls cannot execute an interactive holdout");
    expect(html).toContain("original_rng_not_recorded");
    expect(html).toMatch(/disabled=""[^>]*>Create 2-arm controlled experiment/);
  });
  it("keeps failed arms and inherited parent/root/split information visible", async () => {
    const value = controlledExperiment();
    const html = await routed(
      createElement(ControlledVerification, {
        owner: "owner",
        source: independentSource,
        strategyVersionId: ids.strategy,
        capability: capability(),
        split: "train",
        selection: {
          ...verificationSelection,
          controlled_experiment_id: id.control,
        },
        update: () => undefined,
      }),
      [[["controlled-experiment", "owner", id.control], value]],
    );
    expect(html).toContain(
      "No successful return is assigned to this failed arm",
    );
    expect(html).toContain("execution_failed");
    expect(html).toContain("Root research family");
    expect(html).toContain("Child-owned split version");
    expect(html).toContain("owner-wide exposure after controls");
  });
  it("shows valid no-trade execution with missing metrics as unavailable, not zero", async () => {
    const job = independentJob();
    const key = ["independent-result", "owner", independentPin, "equity", 0];
    const result = {
      evaluation_id: independentPin.evaluation_id,
      execution_attempt_id: independentPin.execution_attempt_id,
      run_id: job.id,
      purpose: job.purpose,
      artifact_sha256: independentPin.result_manifest_sha256,
      evaluation_mode: "native_akquant",
      metrics: { annualized_return: null, closed_trade_count: 0 },
      metric_details: [
        {
          name: "annualized_return",
          value: null,
          unit: "percentage_points",
          definition_version: "tradegrad.metrics.v1",
          calculation_basis: "Native annualized return multiplied by 100",
          unavailable_reason: "native_nonfinite_or_missing",
        },
      ],
      reward_status: "not_applicable",
      result_trust: "native_untrusted",
      section: "equity",
      rows: [],
      columns: [],
      total: 0,
      offset: 0,
      limit: 25,
      warnings: ["No closed trades observed"],
    };
    const html = await routed(
      createElement(IndependentResults, {
        owner: "owner",
        job,
        pin: independentPin,
      }),
      [[key, result]],
    );
    expect(html).toContain("Unavailable, not zero");
    expect(html).toContain("No observed rows");
    expect(html).toContain("not_applicable");
    expect(html).toContain("No closed trades observed");
    expect(html).toContain("annualized_return [percentage_points]");
    expect(html).toContain("native_nonfinite_or_missing");
    expect(html).toContain("Native annualized return multiplied by 100");
  });
});
