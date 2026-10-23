import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement, type ComponentType } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import type {
  ExposureSnapshot,
  FinalEvaluationDetail,
  FinalResultQuery,
  ResearchRunDetail,
  StrategyPackagePublic,
} from "../../src/client/types.gen";
import {
  actionFor,
  saveActionDraft,
} from "../../src/features/final-tests/durable-action";
import { finalTestSchema } from "../../src/features/final-tests/protocol";
const one = "a1111111-1111-4111-8111-111111111111";
const two = "b2222222-2222-4222-8222-222222222222";
const sha = "a".repeat(64);
const exposure: ExposureSnapshot = {
  schema_version: "tradegrad.exposure.v1",
  classification: "no_tracked_overlap",
  matching_exposure_ids: [],
  matching_purposes: [],
  matching_count: 0,
  identities: [],
  as_of: "2026-10-22T12:00:00Z",
  tracking_coverage: "historical tracking incomplete/external use unknown",
  independent_unseen_holdout_established: false,
  evidence_truncated: false,
};
beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  vi.stubGlobal("window", { location: { origin: "http://final.test" } });
});
afterEach(() => vi.unstubAllGlobals());
async function markup<P extends object>(
  Component: ComponentType<P>,
  props: P,
  cache: [unknown[], unknown][] = [],
  query = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  }),
) {
  cache.forEach(([key, data]) => query.setQueryData(key, data));
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
// SSR is a render contract with cached fixtures. It is not browser/worker acceptance.
describe("final UI render contracts with explicit local fixtures", () => {
  it("requires explicit confirmation and preflight, never auto submits on reading", async () => {
    const { FinalTestForm } =
      await import("../../src/features/final-tests/final-selection");
    const html = await markup(FinalTestForm, {
      owner: "owner-a",
      selectionId: one,
    });
    expect(html).toContain("Run final preflight");
    expect(html).toContain("Submit final holdout test");
    expect(html).toMatch(
      /<button[^>]*disabled=""[^>]*>Submit final holdout test/,
    );
    expect(html).not.toContain("Final preflight passed");
    expect(html).not.toContain('checked=""');
    expect(html).toContain("Historical/external use remains unknown");
  });
  it("restores pending submit with unchanged timeout, but never restores user confirmation", async () => {
    const { FinalTestForm } =
      await import("../../src/features/final-tests/final-selection");
    const body = { selection_id: one, max_runtime_seconds: 555 };
    saveActionDraft("owner-a", `test:${one}`, finalTestSchema, {
      version: 1,
      body,
      pending: actionFor(body, null),
    });
    const html = await markup(FinalTestForm, {
      owner: "owner-a",
      selectionId: one,
    });
    expect(html).toContain('value="555"');
    expect(html).toContain("Retry same final submission");
    expect(html).toContain("original immutable body and key");
    expect(html).not.toContain('checked=""');
    const other = await markup(FinalTestForm, {
      owner: "owner-b",
      selectionId: one,
    });
    expect(other).toContain('value="300"');
    expect(other).not.toContain("Retry same final submission");
  });
  it("cannot represent no tracked overlap as independent/unseen certification", async () => {
    const { ExposurePanel } =
      await import("../../src/features/final-tests/exposure");
    const html = await markup(ExposurePanel, { snapshot: exposure });
    expect(html).toContain(
      "Historical tracking incomplete/external use unknown",
    );
    expect(html).toContain(
      "does not establish an independent or previously unseen holdout",
    );
    expect(html).toContain("No tracked overlap");
    expect(html).toContain("lower bound");
  });
  it("historical unbound runs show recreate limitation and no freeze button", async () => {
    const { FreezeCandidatePanel } =
      await import("../../src/features/final-tests/freeze-selection");
    const run = {
      id: one,
      status: "succeeded",
      execution_mode: "optimization",
      experiment_id: null,
      split_version_id: null,
    } as ResearchRunDetail;
    const html = await markup(FreezeCandidatePanel, { run });
    expect(html).toContain("Recreate research");
    expect(html).toContain("Create a new bound experiment");
    expect(html).not.toContain("Freeze selected candidate");
  });
  it("bound selection waits for checkpoint and owner reads rather than showing an empty picker", async () => {
    const { FreezeCandidatePanel } =
      await import("../../src/features/final-tests/freeze-selection");
    const run = {
      id: one,
      status: "succeeded",
      execution_mode: "optimization",
      experiment_id: one,
      split_version_id: two,
      latest_checkpoint_id: one,
    } as ResearchRunDetail;
    const html = await markup(FreezeCandidatePanel, { run });
    expect(html).toContain(
      "Checking immutable checkpoint and existing selection",
    );
    expect(html).not.toContain("Freeze selected candidate");
  });
  it("bound selection has an honest empty checkpoint state", async () => {
    const { FreezeCandidatePanel } =
      await import("../../src/features/final-tests/freeze-selection");
    const run = {
      id: one,
      status: "succeeded",
      execution_mode: "optimization",
      experiment_id: one,
      split_version_id: two,
      latest_checkpoint_id: one,
    } as ResearchRunDetail;
    const html = await markup(FreezeCandidatePanel, { run }, [
      [["me"], { id: "owner-a" }],
      [["research-checkpoint", one], { id: one, summary: {} }],
      [["final-selections", two], { count: 0, data: [] }],
    ]);
    expect(html).toContain("No valid, complete candidate");
    expect(html).not.toContain("Freeze selected candidate");
  });
  it("already-frozen split links persisted selection instead of permitting replacement", async () => {
    const { FreezeCandidatePanel } =
      await import("../../src/features/final-tests/freeze-selection");
    const run = {
      id: one,
      status: "succeeded",
      execution_mode: "optimization",
      experiment_id: one,
      split_version_id: two,
      latest_checkpoint_id: one,
    } as ResearchRunDetail;
    const html = await markup(FreezeCandidatePanel, { run }, [
      [["me"], { id: "owner-a" }],
      [["research-checkpoint", one], { id: one, summary: {} }],
      [["final-selections", two], { count: 1, data: [{ id: two }] }],
    ]);
    expect(html).toContain("Open frozen selection");
    expect(html).toContain("cannot be overwritten");
    expect(html).not.toContain("Freeze selected candidate");
  });
  it("cancel-requested is visibly not stopped and cannot retry or pause", async () => {
    const { FinalTestContent } =
      await import("../../src/features/final-tests/final-test-detail");
    const job = {
      id: one,
      final_selection_id: two,
      split_version_id: two,
      generation: 1,
      status: "cancel_requested",
      next_event_seq: 2,
      attempts: [{ sandbox_stopped: false }],
      config: {
        holdout: { start: "2024-04-22", end: "2024-04-27" },
        selected_strategy: { sha256: sha },
        environment_digest: sha,
      },
      result: null,
    } as FinalEvaluationDetail;
    const html = await markup(FinalTestContent, { job }, [
      [["me"], { id: "owner-a" }],
      [["events", one], []],
      [["final-evidence", one, 0], { count: 0, data: [] }],
    ]);
    expect(html).toContain("This is not a stopped sandbox");
    expect(html).toContain(
      "No holdout evaluation evidence has been published yet",
    );
    expect(html).not.toContain("Retry complete final test");
    expect(html).not.toContain("Pause final test");
  });
  it("failed final work waits for stop, then exposes only explicit complete-holdout retry", async () => {
    const { FinalTestContent } =
      await import("../../src/features/final-tests/final-test-detail");
    const job = {
      id: one,
      final_selection_id: two,
      split_version_id: two,
      generation: 1,
      status: "failed",
      next_event_seq: 2,
      attempts: [{ sandbox_stopped: false }],
      config: {
        holdout: { start: "2024-04-22", end: "2024-04-27" },
        selected_strategy: { sha256: sha },
        environment_digest: sha,
      },
      result: null,
      error: "Native execution failed",
    } as FinalEvaluationDetail;
    const cache: [unknown[], unknown][] = [
      [["me"], { id: "owner-a" }],
      [["events", one], []],
      [["final-evidence", one, 0], { count: 0, data: [] }],
    ];
    const blocked = await markup(FinalTestContent, { job }, cache);
    expect(blocked).toContain("Native execution failed");
    expect(blocked).toContain("Waiting for the server to confirm");
    expect(blocked).toMatch(
      /<button[^>]*disabled=""[^>]*>Retry complete final test/,
    );
    const stopped = await markup(
      FinalTestContent,
      { job: { ...job, attempts: [] } },
      cache,
    );
    expect(stopped).toContain("reruns the complete holdout");
    expect(stopped).not.toContain("Waiting for the server to confirm");
    expect(stopped).toMatch(
      /<button[^>]*disabled=""[^>]*>Retry complete final test/,
    ); // Confirmation is still unchecked.
    expect(stopped).not.toContain("Pause final test");
  });
  it("empty holdout tables never synthesize returns or zero-valued records", async () => {
    const { HoldoutTable } =
      await import("../../src/features/final-tests/holdout-result");
    const table = {
      columns: [{ name: "equity", dtype: "float", unit: "CNY" }],
      rows: [],
      returned_rows: 0,
      filtered_rows: 0,
      total_rows: 0,
      downsampled: false,
      sampling: "none",
      filter_status: "not_requested",
      offset: 0,
      limit: 50,
    } as import("../../src/client/types.gen").ResultTable;
    const html = await markup(HoldoutTable, { table });
    expect(html).toContain("No persisted rows in this window");
    expect(html).not.toContain("<td");
  });
  it("keeps historical success metadata inert and removes cached metrics/export when current job changes", async () => {
    const { FinalTestContent } =
      await import("../../src/features/final-tests/final-test-detail");
    const evaluationId = "c3333333-3333-4333-8333-333333333333";
    const oldEvaluationId = "d4444444-4444-4444-8444-444444444444";
    const attempt = {
      id: "e5555555-5555-4555-8555-555555555555",
      generation: 2,
      status: "succeeded",
      sandbox_stopped: true,
      exit_code: 0,
      finished_at: "2026-10-22T12:00:01Z",
    };
    // Explicit local rendering fixtures, not backend admission or runtime evidence.
    const job = {
      id: one,
      final_selection_id: two,
      split_version_id: two,
      generation: 2,
      active_attempt_id: attempt.id,
      status: "succeeded",
      next_event_seq: 5,
      attempts: [attempt],
      config: {
        holdout: { start: "2024-04-22", end: "2024-04-27" },
        selected_strategy: { sha256: sha },
        environment_digest: sha,
        source_config: {
          dataset: { id: one, sha256: sha },
          market_profile: { initial_cash: 1000000 },
        },
      },
      result: {
        evaluation_id: evaluationId,
        selection_id: two,
        holdout_executed: true,
        exposure_snapshot: exposure,
      },
    } as FinalEvaluationDetail;
    const result: FinalResultQuery = {
      engine_timezone: "Asia/Shanghai",
      artifact_sha256: sha,
      candidate_key: `final:${two}`,
      code_sha256: sha,
      config_sha256: sha,
      price_definition: {},
      result_trust: "native_strategy_and_engine_share_process",
      exposure_snapshot: { ...exposure },
      capabilities: {
        benchmark: false,
        closed_trades: false,
        drawdown: false,
        equity: false,
        fee_component_breakdown: false,
        fees_aggregate: true,
        fills: false,
        internal_decisions: false,
        observable_order_trace: false,
        orders: false,
        positions: false,
        trusted_ledger: false,
      },
      evaluation_id: evaluationId,
      run_id: one,
      selection_id: two,
      split: "holdout",
      selection_feedback: false,
      config: job.config,
      window: { ...job.config.holdout, split: "holdout" },
      warnings: [],
      is_synthetic: true,
      currency: "CNY",
      tables: {},
      benchmark: { status: "not_configured", reason: "No benchmark fixture." },
      fees: {
        total_from_fills: 0,
        unit: "CNY",
        native_total_commission: 0,
        breakdown_status: "unsupported_by_execution_report",
        calculation_basis: "full_holdout",
        definition_version: "test.v1",
        slippage_cost_status: "not_separately_reported_by_native_engine",
      },
      metrics: [
        {
          name: "current_accepted_return",
          value: 12.34,
          unit: "%",
          definition_version: "test.v1",
          calculation_basis: "full_holdout",
          unavailable_reason: null,
        },
      ],
    };
    const oldResult = {
      ...result,
      evaluation_id: oldEvaluationId,
      metrics: [
        { ...result.metrics[0], name: "stale_previous_return", value: 98.76 },
      ],
    };
    const query = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    });
    const chartQuery = { sections: ["equity", "drawdown"], max_points: 600 };
    const cache: [unknown[], unknown][] = [
      [["me"], { id: "owner-a" }],
      [["events", one], []],
      [
        ["final-evidence", one, 0],
        {
          count: 2,
          data: [
            {
              id: oldEvaluationId,
              generation: 1,
              attempt_id: "old-attempt",
              status: "succeeded",
              sandbox_stopped: true,
            },
            {
              id: evaluationId,
              generation: 2,
              attempt_id: attempt.id,
              status: "succeeded",
              sandbox_stopped: true,
            },
          ],
        },
      ],
      [["final-results", evaluationId, chartQuery], result],
      [["final-results", oldEvaluationId, chartQuery], oldResult],
    ];
    const accepted = await markup(FinalTestContent, { job }, cache, query);
    expect(accepted).toContain('data-testid="holdout-result"');
    expect(accepted).toContain("Current accepted return");
    expect(accepted).toContain("Download original holdout JSON");
    expect(accepted).toContain("Create reference-only strategy package");
    expect(accepted).toMatch(
      /<span>Holdout evaluation (?:<!-- -->)?d4444444<\/span>/,
    );
    expect(accepted).not.toContain("Stale previous return");
    expect(accepted).not.toMatch(/<button[^>]*>Holdout evaluation/);

    // Reuse the identical query cache, including both old and accepted artifacts.
    // These are SSR snapshots of lifecycle changes, not interactive browser proof.
    for (const patch of [
      { status: "queued" as const },
      { status: "running" as const },
      { status: "cancel_requested" as const },
      { status: "cancelled" as const },
      { status: "failed" as const },
      { status: "interrupted" as const },
      { generation: 3 },
      { active_attempt_id: "replacement-attempt" },
      { result: null },
      { result: { ...job.result!, holdout_executed: false } },
    ]) {
      const html = await markup(
        FinalTestContent,
        { job: { ...job, ...patch } },
        [],
        query,
      );
      expect(html).toMatch(
        /<span>Holdout evaluation (?:<!-- -->)?d4444444<\/span>/,
      );
      expect(html).toContain(
        "Historical execution metadata is not a reference result",
      );
      expect(html).not.toContain('data-testid="holdout-result"');
      expect(html).not.toContain("Current accepted return");
      expect(html).not.toContain("Stale previous return");
      expect(html).not.toContain("Download original holdout JSON");
      expect(html).not.toContain("Create reference-only strategy package");
      expect(html).not.toMatch(/<button[^>]*>Holdout evaluation/);
    }
    expect(
      query.getQueryData(["final-results", evaluationId, chartQuery]),
    ).toEqual(result);
    expect(
      query.getQueryData(["final-results", oldEvaluationId, chartQuery]),
    ).toEqual(oldResult);
    const nextEvaluationId = "f6666666-6666-4666-8666-666666666666";
    query.setQueryData(["final-results", nextEvaluationId, chartQuery], {
      ...result,
      evaluation_id: nextEvaluationId,
      metrics: [
        { ...result.metrics[0], name: "new_generation_return", value: 0.25 },
      ],
    });
    const nextAttempt = { ...attempt, id: "next-attempt", generation: 3 };
    const next = await markup(
      FinalTestContent,
      {
        job: {
          ...job,
          generation: 3,
          active_attempt_id: nextAttempt.id,
          attempts: [...job.attempts, { ...job.attempts[0], ...nextAttempt }],
          result: { ...job.result!, evaluation_id: nextEvaluationId },
        },
      },
      [],
      query,
    );
    expect(next).toContain("New generation return");
    expect(next).toContain("Create reference-only strategy package");
    expect(next).not.toContain("Current accepted return");
    expect(next).not.toContain("Stale previous return");
  });
  it("renders private references-only ZIP metadata without inventing replay certification", async () => {
    const { StrategyPackageContent } =
      await import("../../src/features/final-tests/strategy-package");
    const item: StrategyPackagePublic = {
      id: one,
      final_selection_id: two,
      request_sha256: sha,
      content_sha256: sha,
      archive_sha256: sha,
      size_bytes: 4200,
      manifest: {},
      exposure_snapshot: {},
      exposure_as_of: "2026-10-22T12:00:00Z",
      created_at: "2026-10-22T12:00:00Z",
    };
    const html = await markup(StrategyPackageContent, { item });
    expect(html).toContain("Download authenticated strategy ZIP");
    expect(html).toContain(sha);
    expect(html).toContain("no raw market data");
    expect(html).toContain("not established by export");
    expect(html).not.toContain('href="/api/');
    expect(html).not.toContain("Replay certified");
  });
  it.each(["native_akquant", "portable"] as const)(
    "uses the %s package manifest for its return link and trust notice",
    async (mode) => {
      const { StrategyPackageContent } =
        await import("../../src/features/final-tests/strategy-package");
      const item: StrategyPackagePublic = {
        id: one,
        final_selection_id: two,
        request_sha256: sha,
        content_sha256: sha,
        archive_sha256: sha,
        size_bytes: 4200,
        manifest: { mode },
        exposure_snapshot: {},
        exposure_as_of: "2026-10-22T12:00:00Z",
        created_at: "2026-10-22T12:00:00Z",
      };
      const html = await markup(StrategyPackageContent, { item });
      expect(html).toContain("Download authenticated strategy ZIP");
      expect(html).toContain(
        "Creating this archive does not certify successful replay",
      );
      if (mode === "portable") {
        expect(html).toContain(`href="/portable/final-selections/${two}"`);
        expect(html).toContain(
          "Final evaluation is a separate frozen holdout job",
        );
        expect(html).not.toContain("Native · UNTRUSTED");
        expect(html).not.toContain(`href="/final-selections/${two}"`);
      } else {
        expect(html).toContain(`href="/final-selections/${two}"`);
        expect(html).toContain("Native · UNTRUSTED");
        expect(html).not.toContain(`href="/portable/final-selections/${two}"`);
      }
    },
  );
});
