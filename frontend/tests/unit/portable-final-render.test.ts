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
import { portableFixture } from "./portable-fixtures";
import {
  actionFor,
  saveActionDraft,
} from "../../src/features/final-tests/durable-action";
const cacheKey = (resource: string, ...parts: unknown[]) => [
  "portable-final",
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
import { finalTestSchema } from "../../src/features/portable/final-protocol";
import {
  finalIds,
  portableCheckpointFixture,
  rejectedPortableFinalReceipts,
  portableFinalFixture,
  portableFinalResultFixture,
  portableFrozenRunFixture,
  portableSelectionFixture,
} from "./portable-final-fixtures";
// Cached SSR fixtures verify UI contracts only. No browser, worker or runtime certification.
describe("portable final render contracts", () => {
  it("requires explicit candidate choice and consent, never picks automatic-best", async () => {
    const { PortableFreezeCandidatePanel } =
      await import("../../src/features/portable/freeze-candidate");
    const run = portableFrozenRunFixture();
    const html = await markup(
      PortableFreezeCandidatePanel,
      { owner: "owner-a", run },
      [
        [
          ["portable", "owner-a", "checkpoint", finalIds.checkpoint],
          portableCheckpointFixture(),
        ],
        [cacheKey("selections", finalIds.split), { data: [], count: 0 }],
      ],
    );
    expect(html).toContain("Choose a candidate explicitly");
    expect(html).toContain("not necessarily the highest-scoring one");
    expect(html).toContain("not-auto-best");
    expect(html).not.toContain('checked=""');
    expect(html).toMatch(
      /<button[^>]*disabled=""[^>]*>Freeze selected portable candidate/,
    );
  });
  it("blocks missing or incomplete checkpoints and never offers native conversion", async () => {
    const { PortableFreezeCandidatePanel } =
      await import("../../src/features/portable/freeze-candidate");
    const run = portableFrozenRunFixture();
    const unbound = await markup(PortableFreezeCandidatePanel, {
      owner: "owner-a",
      run: { ...run, split_version_id: null },
    });
    expect(unbound).toContain(
      "historical runs cannot be retroactively certified",
    );
    const html = await markup(
      PortableFreezeCandidatePanel,
      { owner: "owner-a", run },
      [
        [
          ["portable", "owner-a", "checkpoint", finalIds.checkpoint],
          { ...portableCheckpointFixture(), complete: false },
        ],
        [cacheKey("selections", finalIds.split), { data: [], count: 0 }],
      ],
    );
    expect(html).toContain("No candidate can be frozen");
    expect(html).not.toContain("Freeze selected portable candidate");
  });
  it("links an already-frozen selection without permitting overwrite", async () => {
    const { PortableFreezeCandidatePanel } =
      await import("../../src/features/portable/freeze-candidate");
    const html = await markup(
      PortableFreezeCandidatePanel,
      { owner: "owner-a", run: portableFrozenRunFixture() },
      [
        [
          ["portable", "owner-a", "checkpoint", finalIds.checkpoint],
          portableCheckpointFixture(),
        ],
        [
          cacheKey("selections", finalIds.split),
          { data: [portableSelectionFixture()], count: 1 },
        ],
      ],
    );
    expect(html).toContain("cannot be overwritten");
    expect(html).toContain(`/portable/final-selections/${finalIds.selection}`);
    expect(html).not.toContain("Freeze selected portable candidate");
  });
  it("preserves source and checkpoint hashes, owner-wide exposure caveats and both source images", async () => {
    const { PortableSelectionContent } =
      await import("../../src/features/portable/final-selection");
    const selection = portableSelectionFixture();
    const html = await markup(
      PortableSelectionContent,
      { owner: "owner-a", selection },
      [
        [
          ["portable", "owner-a", "run", selection.source_run_id],
          portableFrozenRunFixture(),
        ],
        [
          cacheKey("source", selection.strategy_version_id),
          {
            ...portableFixture().catalog.strategies[0],
            id: selection.strategy_version_id,
            content_sha256: selection.source_code_sha256,
            code: "# exact frozen policy",
          },
        ],
        [cacheKey("jobs", selection.id, 0), { data: [], count: 0 }],
      ],
    );
    for (const phrase of [
      "Code SHA-256",
      "Checkpoint SHA-256",
      "Source config SHA-256",
      "Source trusted engine image ID",
      "Source isolated policy image ID",
      "Historical tracking incomplete/external use unknown",
      "no optimizer, model calls",
      "cash-daily raw-price profile",
      "# exact frozen policy",
    ])
      expect(html).toContain(phrase);
    expect(html).not.toContain("Native · UNTRUSTED");
    expect(html).not.toContain("Final preflight passed");
  });
  it("restores exact pending submission but never prior confirmation/preflight or another owner's key", async () => {
    const { PortableFinalTestForm } =
      await import("../../src/features/portable/final-selection");
    const selection = portableSelectionFixture(),
      body = { selection_id: selection.id, max_runtime_seconds: 555 };
    saveActionDraft(
      "owner-a",
      `portable-test:${selection.id}`,
      finalTestSchema,
      { version: 1, body, pending: actionFor(body, null) },
    );
    const html = await markup(PortableFinalTestForm, {
      owner: "owner-a",
      selection,
    });
    expect(html).toContain('value="555"');
    expect(html).toContain("Retry same portable final submission");
    expect(html).not.toContain('checked=""');
    expect(html).not.toContain("Portable final preflight passed");
    expect(html).toMatch(
      /<button[^>]*disabled=""[^>]*>Retry same portable final submission/,
    );
    const other = await markup(PortableFinalTestForm, {
      owner: "owner-b",
      selection,
    });
    expect(other).toContain('value="300"');
    expect(other).not.toContain("Retry same portable final submission");
  });
  it("cancel requested is not stopped; no pause, optimizer or result is offered", async () => {
    const { PortableFinalContent } =
      await import("../../src/features/portable/final-detail");
    const job = {
      ...portableFinalFixture(),
      status: "cancel_requested" as const,
      result: null,
    };
    const html = await markup(PortableFinalContent, { owner: "owner-a", job }, [
      [cacheKey("events", job.id), []],
      [cacheKey("evidence", job.id, 0), { data: [], count: 0 }],
    ]);
    expect(html).toContain("This is not a stopped sandbox");
    expect(html).toContain(
      "No holdout evaluation evidence has been published yet",
    );
    expect(html).not.toContain("Retry complete final test");
    expect(html).not.toContain("Pause final test");
    expect(html).not.toContain("Full-window final equity");
  });
  it("failed final job waits for physical stop and explicit full-holdout retry consent", async () => {
    const { PortableFinalContent } =
      await import("../../src/features/portable/final-detail");
    const job = {
      ...portableFinalFixture(),
      status: "failed" as const,
      result: null,
      attempts: [
        { sandbox_stopped: false } as ReturnType<
          typeof portableFinalFixture
        >["attempts"][number],
      ],
    };
    const html = await markup(PortableFinalContent, { owner: "owner-a", job }, [
      [cacheKey("events", job.id), []],
      [cacheKey("evidence", job.id, 0), { data: [], count: 0 }],
    ]);
    expect(html).toContain("both previous execution environments have stopped");
    expect(html).toMatch(
      /<button[^>]*disabled=""[^>]*>Retry complete final test/,
    );
    expect(html).not.toContain("Native · UNTRUSTED");
  });
  it("renders portable-only account tables and decision trace, actual images and honest synthetic limits", async () => {
    const { PortableFinalResult } =
      await import("../../src/features/portable/final-result");
    const job = portableFinalFixture(),
      result = portableFinalResultFixture();
    const html = await markup(PortableFinalResult, { owner: "owner-a", job }, [
      [
        cacheKey("result", result.evaluation_id, "account_series", 0, 50),
        result,
      ],
      [
        cacheKey("result", result.evaluation_id, "decision_trace", 0, 50),
        {
          ...result,
          section: "decision_trace",
          rows: [
            {
              state_before_sha256: "before",
              state_after_sha256: "after",
              warmup: false,
              decision: { kind: "hold" },
            },
          ],
        },
      ],
      [["me"], { id: "owner-a" }],
    ]);
    for (const phrase of [
      "Explicit synthetic raw/no-actions fixture",
      "isolated_policy_trusted_akquant_engine",
      "Trusted engine image ID",
      "Isolated policy image ID",
      "tradegrad.portable.final-result-query.v1",
      "state before sha256",
      "Download checksum-verified portable final JSON",
      "Create reference-only strategy package",
      "Only the returned",
      "No optimizer or model calls",
    ])
      expect(html).toContain(phrase);
    expect(html).not.toContain("Native · UNTRUSTED");
    expect(html).not.toContain("Sharpe");
    expect(html).not.toContain('href="/api/');
  });
  it("fails closed on swapped research schema or foreign image identity and bounds evidence failures", async () => {
    const { PortableFinalResult } =
      await import("../../src/features/portable/final-result");
    const job = portableFinalFixture(),
      result = portableFinalResultFixture();
    for (const patch of [
      { schema_version: "tradegrad.portable-result.v1" },
      { policy_image_digest: "foreign" },
    ]) {
      const html = await markup(
        PortableFinalResult,
        { owner: "owner-a", job },
        [
          [
            cacheKey("result", result.evaluation_id, "account_series", 0, 50),
            { ...result, ...patch },
          ],
        ],
      );
      expect(html).toContain("No result is displayed");
      expect(html).not.toContain("Full-window final equity");
      expect(html).not.toContain("Create reference-only strategy package");
    }
    const failed = await markup(
      PortableFinalResult,
      { owner: "owner-a", job },
      [],
      [
        [
          cacheKey("result", result.evaluation_id, "account_series", 0, 50),
          Error("owned artifact unavailable"),
        ],
      ],
    );
    expect(failed).toContain("owned artifact unavailable");
    expect(failed).toContain("not successful results");
  });
  it.each(rejectedPortableFinalReceipts())(
    "hides cached metrics, export and download for %s",
    async (_label, job) => {
      const { PortableFinalContent } =
        await import("../../src/features/portable/final-detail");
      const { PortableFinalResult } =
        await import("../../src/features/portable/final-result");
      const result = portableFinalResultFixture();
      const cache: [unknown[], unknown][] = [
        [cacheKey("events", job.id), []],
        [cacheKey("evidence", job.id, 0), { data: [], count: 0 }],
        [
          cacheKey("result", result.evaluation_id, "account_series", 0, 50),
          result,
        ],
        [
          cacheKey("result", result.evaluation_id, "decision_trace", 0, 50),
          { ...result, section: "decision_trace" },
        ],
      ];
      for (const Component of [PortableFinalContent, PortableFinalResult]) {
        const html = await markup(Component, { owner: "owner-a", job }, cache);
        expect(html).not.toContain("Persisted account charts");
        expect(html).not.toContain(
          "Download checksum-verified portable final JSON",
        );
        expect(html).not.toContain("Create reference-only strategy package");
        expect(html).not.toContain("no_tracked_overlap");
      }
    },
  );
});
