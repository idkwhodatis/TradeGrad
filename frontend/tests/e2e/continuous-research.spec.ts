import { readFile } from "node:fs/promises";
import {
  expect,
  test as base,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type {
  CandidatePage,
  ComparisonDetail,
  ComparisonPublic,
  ComparisonResult,
  ComparisonSectionPage,
  ControlledExperimentPublic,
  CoveragePublic,
  DatasetPublic,
  DecisionDetail,
  DecisionHeader,
  DecisionIndex,
  DecisionPage,
  EvaluationPublic,
  ImportDetail,
  ImportPublic,
  IndependentEvaluationDetail,
  IndependentResultQuery,
  ReplayExecutionPage,
  ResearchCapabilities,
  ResearchNoteRevision,
  ResearchRunDetail,
  ResearchTemplate,
  Token,
  UniversePublic,
  WorkbenchSpec,
} from "../../src/client/types.gen";
import { account, api, authorization, signIn, token } from "./helpers";
import {
  query,
  read,
  rows,
  runActual,
  saveVersion,
  sha256,
  sourcePins,
  terminal,
  tracePins,
  type IndependentOwned,
} from "./independent-helpers";

// ONE continuous seven-step browser journey. Only immutable input/version/source
// setup is out of band. PostgreSQL, HTTP, queues, artifacts and isolated AKQuant
// are genuine production paths. No routes, traces, models or results are mocked.
// Explicit synthetic native integration, not live data or portable scale proof.
type Owned = IndependentOwned & {
  imports: Set<string>;
  independent: Set<string>;
  comparisons: Set<string>;
};
const test = base.extend<{ owned: Owned }>({
  owned: async ({ request }, use) =>
    use({
      auth: await token(request),
      runs: new Set(),
      imports: new Set(),
      independent: new Set(),
      comparisons: new Set(),
    }),
});
const windows = {
  train: { start: "2024-01-17", end: "2024-02-01" },
  validation: { start: "2024-02-01", end: "2024-02-22" },
  holdout: { start: "2024-02-22", end: "2024-03-01" },
};
async function pinned(page: Page, values: Record<string, string | number>) {
  await expect
    .poll(() =>
      Object.fromEntries(
        Object.keys(values).map((key) => [
          key,
          new URL(page.url()).searchParams.get(key),
        ]),
      ),
    )
    .toEqual(
      Object.fromEntries(
        Object.entries(values).map(([key, value]) => [key, String(value)]),
      ),
    );
}
async function submit<T>(
  page: Page,
  path: string,
  button: string,
  status = 202,
) {
  const pending = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api/v1${path}` &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: button, exact: true }).click();
  const response = await pending;
  expect(response.status(), await response.text()).toBe(status);
  return (await response.json()) as T;
}
async function snapshot(request: APIRequestContext, owned: Owned) {
  const auth = owned.auth;
  // Single-symbol CSV upload cannot form this four-symbol dataset. One ordinary
  // fixture-facade import owns the complete explicitly frozen cross-section.
  const response = await request.post(api("/data/imports"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    data: {
      name: `I7 frozen four-symbol profile ${crypto.randomUUID()}`,
      provider: "portable_fixture",
      market: "US",
      symbols: ["A", "B", "C", "D"],
      start_date: "2024-01-02",
      end_date: "2024-03-01",
      price_mode: "raw",
    },
  });
  expect(response.status(), await response.text()).toBe(202);
  const imported = (await response.json()) as ImportPublic;
  owned.imports.add(imported.id);
  await expect
    .poll(
      async () => {
        const job = await read<ImportDetail>(
          request,
          auth,
          `/data/imports/${imported.id}`,
        );
        if (["failed", "partial", "cancelled"].includes(job.status))
          throw new Error(`Four-symbol import ${job.status}: ${job.error}`);
        return job.status;
      },
      { timeout: 120000, intervals: [250, 500, 1000] },
    )
    .toBe("ready");
  const ready = await read<ImportDetail>(
    request,
    auth,
    `/data/imports/${imported.id}`,
  );
  const dataset = await read<DatasetPublic>(
    request,
    auth,
    `/datasets/${ready.dataset_version_id}`,
  );
  expect(dataset.is_synthetic).toBe(true);
  const coverage = await read<CoveragePublic>(
    request,
    auth,
    `/datasets/${dataset.id}/coverage`,
  );
  expect(coverage.universe.members as unknown[]).toHaveLength(4);
  const created = await request.post(api("/data/universes"), {
    headers: authorization(auth),
    data: {
      name: `I7 fixed A/B/C/D ${crypto.randomUUID()}`,
      state: "user_fixed",
      as_of: new Date().toISOString(),
      members: coverage.universe.members,
      evidence: [],
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  return { dataset, universe: (await created.json()) as UniversePublic };
}
async function pair(request: APIRequestContext, owned: Owned) {
  const template = (
    await rows<ResearchTemplate>(request, owned.auth, "/research-templates")
  ).find((item) => item.template_id === "cross_sectional")!;
  expect(template, "Maintained executable template required").toBeDefined();
  expect(sha256(template.code)).toBe(template.code_sha256);
  const capabilities = await read<ResearchCapabilities>(
    request,
    owned.auth,
    "/research-capabilities",
  );
  expect(capabilities.live_model_calls).toBe(false);
  expect(capabilities.model_providers).toEqual(["fixture"]);
  const inputs = await snapshot(request, owned);
  const parentManifest = structuredClone(template.manifest);
  const enabled = parentManifest.parameters!.find(
    (item) => item.name === "liquidity_enabled",
  )!;
  expect(enabled.value).toBe(true);
  enabled.value = false;
  const parent = await saveVersion(
    request,
    owned.auth,
    template,
    template.code,
    null,
    parentManifest,
    "us",
  );
  const child = await saveVersion(
    request,
    owned.auth,
    template,
    template.code,
    parent,
    template.manifest,
    "us",
  );
  const options = { windows, warmupSessions: 3, initialCash: 10000 };
  // Zero iterations only obtains genuine original-source leaves. The later
  // independent controls cannot call or imitate the source optimizer.
  const left = await runActual(
    request,
    owned,
    parent,
    inputs,
    capabilities,
    undefined,
    options,
  );
  const right = await runActual(
    request,
    owned,
    child,
    inputs,
    capabilities,
    undefined,
    options,
  );
  expect(right.strategy.parent_id).toBe(left.strategy.id);
  for (const key of [
    "market_profile",
    "dataset",
    "universe",
    "windows",
    "warmup",
  ] as const)
    expect(left.body[key]).toEqual(right.body[key]);
  return { left, right };
}
async function independent(
  request: APIRequestContext,
  auth: Token,
  id: string,
) {
  await expect
    .poll(
      async () => {
        const job = await read<IndependentEvaluationDetail>(
          request,
          auth,
          `/independent-evaluations/${id}`,
        );
        if (terminal.has(job.status) && job.status !== "succeeded")
          throw new Error(
            `Independent ${id} ${job.status}: ${job.error}; no fabricated zero-return fallback`,
          );
        return job.status;
      },
      { timeout: 210000, intervals: [250, 500, 1000] },
    )
    .toBe("succeeded");
  return read<IndependentEvaluationDetail>(
    request,
    auth,
    `/independent-evaluations/${id}`,
  );
}
async function comparison(request: APIRequestContext, auth: Token, id: string) {
  await expect
    .poll(
      async () => {
        const job = await read<ComparisonDetail>(
          request,
          auth,
          `/comparisons/${id}`,
        );
        if (terminal.has(job.status) && job.status !== "succeeded")
          throw new Error(`Comparison ${job.status}: ${job.error_code}`);
        return job.status;
      },
      { timeout: 210000, intervals: [250, 500, 1000] },
    )
    .toBe("succeeded");
  const job = await read<ComparisonDetail>(request, auth, `/comparisons/${id}`);
  return read<ComparisonResult>(
    request,
    auth,
    `/comparisons/${id}/result?${query({ manifest_sha256: job.manifest_sha256! })}`,
  );
}
async function section(
  request: APIRequestContext,
  auth: Token,
  result: ComparisonResult,
  name: string,
  extra: Record<string, string | number> = {},
) {
  return read<ComparisonSectionPage>(
    request,
    auth,
    `/comparisons/${result.comparison_id}/sections/${name}?${query({ manifest_sha256: result.manifest_sha256, offset: 0, limit: 25, ...extra })}`,
  );
}
// Bounded cleanup only for accepted IDs belonging to this test, even when an
// intermediate UI step fails. Queue terminal status alone is not sandbox stop.
test.afterEach(async ({ request, owned }) => {
  const cleanup = await Promise.allSettled([
    ...[...owned.imports].map(async (id) => {
      const path = `/data/imports/${id}`;
      const job = await read<ImportDetail>(request, owned.auth, path);
      if (!["ready", "failed", "partial", "cancelled"].includes(job.status)) {
        const cancelled = await request.post(api(`${path}/cancel`), {
          headers: authorization(owned.auth),
        });
        expect(cancelled.status()).toBe(202);
        expect((await cancelled.json()).status).toBe("cancelled");
      }
    }),
    ...[...owned.runs].map(async (id) => {
      const path = `/research-runs/${id}`;
      const job = await read<ResearchRunDetail>(request, owned.auth, path);
      if (!terminal.has(job.status) || job.status === "paused")
        expect(
          (
            await request.post(api(`${path}/cancel`), {
              headers: authorization(owned.auth),
            })
          ).status(),
        ).toBe(202);
      await expect
        .poll(
          async () => {
            const current = await read<ResearchRunDetail>(
              request,
              owned.auth,
              path,
            );
            const leaves = await rows<EvaluationPublic>(
              request,
              owned.auth,
              `${path}/evaluations`,
            );
            return (
              terminal.has(current.status) &&
              current.attempts.every((item) => item.sandbox_stopped) &&
              leaves.every(
                (item) => item.sandbox_stopped && item.finished_at !== null,
              )
            );
          },
          { timeout: 45000, intervals: [250, 500, 1000] },
        )
        .toBe(true);
    }),
    ...[...owned.independent].map(async (id) => {
      const path = `/independent-evaluations/${id}`;
      const job = await read<IndependentEvaluationDetail>(
        request,
        owned.auth,
        path,
      );
      if (!terminal.has(job.status))
        expect(
          (
            await request.post(api(`${path}/cancel`), {
              headers: {
                ...authorization(owned.auth),
                "Idempotency-Key": crypto.randomUUID(),
              },
            })
          ).status(),
        ).toBe(202);
      await expect
        .poll(
          async () => {
            const current = await read<IndependentEvaluationDetail>(
              request,
              owned.auth,
              path,
            );
            return (
              terminal.has(current.status) &&
              current.attempts.every(
                (item) => item.sandbox_stopped && item.finished_at !== null,
              )
            );
          },
          { timeout: 45000, intervals: [250, 500, 1000] },
        )
        .toBe(true);
    }),
    ...[...owned.comparisons].map(async (id) => {
      const path = `/comparisons/${id}`;
      const job = await read<ComparisonDetail>(request, owned.auth, path);
      if (!terminal.has(job.status))
        expect(
          (
            await request.post(api(`${path}/cancel`), {
              headers: authorization(owned.auth),
            })
          ).status(),
        ).toBe(202);
      await expect
        .poll(
          async () => {
            const current = await read<ComparisonDetail>(
              request,
              owned.auth,
              path,
            );
            return (
              terminal.has(current.status) &&
              current.attempts.every(
                (item) => item.sandbox_stopped && item.finished_at !== null,
              )
            );
          },
          { timeout: 45000, intervals: [250, 500, 1000] },
        )
        .toBe(true);
    }),
  ]);
  const failures = cleanup.filter(
    (item): item is PromiseRejectedResult => item.status === "rejected",
  );
  if (failures.length)
    throw new AggregateError(
      failures.map((item) => item.reason),
      "One or more owned jobs failed bounded cleanup",
    );
});

test("continuous frozen four-symbol research restores rule, decision, parent comparison and independent control from one evidence note", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(1000000);
  expect((await request.get(api("/health/ready"))).ok()).toBe(true);
  const { left, right } = await pair(request, owned);
  await signIn(page);
  const mutations: string[] = [];
  page.on("request", (wire) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(wire.method()))
      mutations.push(`${wire.method()} ${new URL(wire.url()).pathname}`);
  });
  const leafPins = tracePins(right.leaf),
    liquidity = right.spec.spec.rules.find(
      (rule) => rule.rule_id === "liquidity",
    )!;
  let chosen: DecisionIndex,
    savedComparison: ComparisonResult,
    control: ControlledExperimentPublic;
  let jobs: IndependentEvaluationDetail[],
    note: ResearchNoteRevision,
    noteUrl: string,
    exactSelection: Record<string, string>;

  await test.step("1. Select a mechanism-changing child from its version tree", async () => {
    await page.goto(`/strategies/${left.strategy.id}/research`);
    await page
      .getByRole("complementary", { name: "Version lineage" })
      .getByRole("button", {
        name: `Open version: ${right.strategy.name}`,
        exact: true,
      })
      .click();
    await expect(page).toHaveURL(
      new RegExp(`/strategies/${right.strategy.id}/research`),
    );
    await expect(
      page.getByRole("region", { name: "Code parent" }),
    ).toContainText(left.strategy.id);
    await page
      .getByLabel("Actual leaf Evaluation", { exact: true })
      .selectOption(right.leaf.evaluation_id);
    await pinned(page, {
      evaluation_id: right.leaf.evaluation_id,
      ...leafPins,
      code_sha256: right.strategy.content_sha256,
    });
  });
  await test.step("2. Read the controlled rule, readonly graph and anchored source", async () => {
    const card = page.getByTestId("rule-liquidity");
    for (const label of ["controlled", "static_inference", "liquidity_enabled"])
      await expect(card).toContainText(label);
    await page
      .getByRole("button", {
        name: `Inspect rule: ${liquidity.title}`,
        exact: true,
      })
      .click();
    const graph = page.getByRole("region", {
      name: "Expected template rule graph",
    });
    await expect(graph).toContainText("not an execution trace");
    const positions = () =>
      graph
        .locator(".react-flow__node")
        .evaluateAll((nodes) =>
          nodes.map((node) => (node as HTMLElement).style.transform),
        );
    const before = await positions(),
      node = graph.getByLabel(`Expected rule: ${liquidity.title}`, {
        exact: true,
      });
    await node.focus();
    await node.press("ArrowRight");
    await node.press("Delete");
    expect(await positions()).toEqual(before);
    await expect(graph.locator(".react-flow__edge")).toHaveCount(
      right.spec.spec.edges.length,
    );
    await expect(page.getByTestId("source-code-hash")).toContainText(
      right.strategy.content_sha256,
    );
    const ref = liquidity.source_ref;
    await expect(
      page
        .getByRole("region", { name: "Selected source lines" })
        .locator("pre"),
    ).toHaveText(
      right.strategy.code
        .split(/\r\n|\n|\r/)
        .slice(ref.line_start - 1, ref.line_end)
        .map((line, i) => `${ref.line_start + i}: ${line}`)
        .join("\n"),
    );
  });
  await test.step("3. Explain actual B selection and higher-score C liquidity filtering", async () => {
    const events = await read<DecisionPage>(
      request,
      owned.auth,
      `/evaluations/${right.leaf.evaluation_id}/decisions?${query({ ...leafPins, start: "2024-01-17", end: "2024-01-18", limit: 50 })}`,
    );
    chosen = events.items!.find(
      (item) =>
        item.callback_phase === "on_cross_section" &&
        item.stage_ids.includes("ranking"),
    )!;
    expect(chosen, "Actual frozen native event required").toBeDefined();
    expect(chosen.session_date).toBe("2024-01-17");
    expect(events.context.timestamp_semantics).toBe(
      "native_daily_session_labels",
    );
    expect(right.trace.context.timestamp_semantics).toBe(
      "native_daily_session_labels",
    );
    const candidates = await read<CandidatePage>(
      request,
      owned.auth,
      `/evaluations/${right.leaf.evaluation_id}/decisions/${chosen.decision_id}/candidates?${query({ ...leafPins, stage_id: "ranking", limit: 50 })}`,
    );
    expect(candidates.items).toHaveLength(4);
    const values = Object.fromEntries(
      candidates.items!.map((row) => [
        row.observation.instrument_id,
        row.observation,
      ]),
    );
    // Independently derivable literal input: B=12/10-1; C=15/10-1.
    // C mean dollar volume=(10+12+15)*10/3, below1000 despite higher return.
    expect(values.B.score).toBeCloseTo(0.2, 12);
    expect(values.C.score).toBeCloseTo(0.5, 12);
    expect(values.B).toMatchObject({
      selected: true,
      rank: 1,
      eligibility: "eligible",
      reason: "selected",
    });
    expect(values.C).toMatchObject({
      selected: false,
      rank: null,
      eligibility: "ineligible",
      elimination_stage: "liquidity",
      reason: "eligibility_filter",
      reason_detail: "liquidity_below_threshold",
    });
    await page
      .getByRole("link", { name: "Decision replay", exact: true })
      .click();
    await page
      .getByLabel("Decision date", { exact: true })
      .fill(chosen.session_date);
    await page
      .getByLabel("Specific decision event", { exact: true })
      .selectOption(chosen.decision_id);
    await page
      .getByLabel("Observed stage", { exact: true })
      .selectOption("ranking");
    const identity = page.getByTestId("replay-event-identity");
    await expect(identity).toContainText(
      "market-local midnight session labels",
    );
    await expect(identity).toContainText(
      "not observed exchange-close or data-availability instants",
    );
    const table = page.getByTestId("replay-candidates-table");
    await expect(table.locator("tbody tr")).toHaveCount(4);
    await expect(table.getByTestId("replay-candidate-B")).toContainText(
      "selected",
    );
    await expect(table.getByTestId("replay-candidate-C")).toContainText(
      "eligibility_filter",
    );
    // Selection is not a fill. Follow the explicit engine order association
    // before returning to C's explanatory predicate; never join by proximity.
    await page
      .getByRole("button", { name: "Inspect candidate: B", exact: true })
      .click();
    await page
      .getByLabel("Observed stage", { exact: true })
      .selectOption("execution");
    const chart = page.getByRole("img", {
      name: "Retrospective market prices, information cutoff, decision time and actually saved feature points",
      exact: true,
    });
    await expect(chart).toHaveAttribute(
      "aria-description",
      /market-local midnight session labels/,
    );
    await expect(page.locator("figure").filter({ has: chart })).toContainText(
      "not observed exchange-close or data-availability instants",
    );
    const executed = await read<DecisionDetail>(
      request,
      owned.auth,
      `/evaluations/${right.leaf.evaluation_id}/decisions/${chosen.decision_id}?${query({ ...leafPins, stage_id: "execution", instrument_id: "B" })}`,
    );
    expect(executed.decision?.decision_id).toBe(chosen.decision_id);
    expect(executed.context.timestamp_semantics).toBe(
      "native_daily_session_labels",
    );
    const allocation = await read<DecisionDetail>(
      request,
      owned.auth,
      `/evaluations/${right.leaf.evaluation_id}/decisions/${chosen.decision_id}?${query({ ...leafPins, stage_id: "allocation", instrument_id: "B" })}`,
    );
    expect(allocation.decision?.pre_state?.positions).toEqual([]);
    const buyTarget = allocation
      .stages!.find((stage) => stage.stage_id === "allocation")!
      .constrained_plan!.find((item) => item.instrument_id === "B")!;
    expect(buyTarget.target_quantity).toBeGreaterThan(0);
    const executionStage = executed.stages!.find(
      (stage) => stage.stage_id === "execution",
    )!;
    expect(executionStage.coverage).toBe("complete");
    expect(
      executionStage.order_links.filter((item) => item.instrument_id === "C"),
    ).toEqual([]);
    const link = executed
      .stages!.flatMap((stage) => stage.order_links)
      .find(
        (item) => item.instrument_id === "B" && item.child_order_ids.length,
      )!;
    expect(link, "B must have a genuine submitted engine order").toBeDefined();
    expect(link).toMatchObject({
      status: "intended",
      source: "strategy_emitted",
      fill_ids: [],
    });
    await expect(
      page.getByTestId(`replay-execution-${link.logical_order_id}`),
    ).toContainText("intended");
    await page
      .getByRole("button", {
        name: `Inspect order: ${link.logical_order_id}`,
        exact: true,
      })
      .click();
    const execution = await read<ReplayExecutionPage>(
      request,
      owned.auth,
      `/evaluations/${right.leaf.evaluation_id}/decisions/${chosen.decision_id}/execution?${query({ ...leafPins, stage_id: "execution", instrument_id: "B", logical_order_id: link.logical_order_id })}`,
    );
    expect(execution.status).toBe("available");
    expect(execution.decision_id).toBe(chosen.decision_id);
    expect(execution.logical_order_id).toBe(link.logical_order_id);
    const children = execution.items!.filter(
      (item) => item.record_kind === "child_order",
    );
    const fills = execution.items!.filter(
      (item) => item.record_kind === "fill",
    );
    expect(children.map((item) => item.record_id).sort()).toEqual(
      [...link.child_order_ids].sort(),
    );
    expect(fills.length).toBeGreaterThan(0);
    for (const child of children) {
      expect(child.association_basis).toBe("child_order_id");
      expect(child.status).toBe("available");
      expect(child.record?.id).toBe(child.record_id);
      expect(child.record?.symbol).toBe("B");
      expect(String(child.record?.side)).toMatch(/(?:^|\.)buy$/i);
      expect(String(child.record?.status)).toMatch(/(?:^|\.)filled$/i);
      expect(child.record?.quantity).toBeGreaterThan(0);
      expect(child.record?.filled_quantity).toBeGreaterThan(0);
      expect(link.child_order_ids).toContain(child.record_id);
      const childButton = page.getByRole("button", {
        name: `Open linked order group: ${child.logical_order_id} from child_order ${child.record_id}`,
        exact: true,
      });
      await expect(childButton).toBeVisible();
      await expect(childButton.locator("..").locator("pre")).toHaveText(
        JSON.stringify(child.record, null, 2),
      );
    }
    for (const fill of fills) {
      expect(fill.status).toBe("available");
      expect(fill.association_basis).toBe("child_order_id");
      expect(fill.logical_order_id).toBe(link.logical_order_id);
      expect(fill.record?.id).toBe(fill.record_id);
      expect(link.child_order_ids).toContain(fill.record?.order_id);
      expect(fill.record?.symbol).toBe("B");
      expect(String(fill.record?.side)).toMatch(/(?:^|\.)buy$/i);
      expect(fill.record?.quantity).toBeGreaterThan(0);
      const fillButton = page.getByRole("button", {
        name: `Open linked order group: ${fill.logical_order_id} from fill ${fill.record_id}`,
        exact: true,
      });
      await expect(fillButton).toBeVisible();
      await expect(fillButton.locator("..").locator("pre")).toHaveText(
        JSON.stringify(fill.record, null, 2),
      );
    }
    await page
      .getByLabel("Observed stage", { exact: true })
      .selectOption("ranking");
    await page
      .getByRole("button", { name: "Inspect candidate: C", exact: true })
      .click();
    await expect(table.locator("tbody tr")).toHaveCount(4);
    await page
      .getByLabel("Observed stage", { exact: true })
      .selectOption("execution");
    const noCExecution = await read<ReplayExecutionPage>(
      request,
      owned.auth,
      `/evaluations/${right.leaf.evaluation_id}/decisions/${chosen.decision_id}/execution?${query({ ...leafPins, stage_id: "execution", instrument_id: "C" })}`,
    );
    expect(noCExecution).toMatchObject({
      decision_id: chosen.decision_id,
      instrument_id: "C",
      stage_id: "execution",
      status: "no_linked_records",
      items: [],
      total: 0,
    });
    await expect(
      page.getByText(
        "No explicit links for this instrument in the saved stage.",
        { exact: false },
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: /^Inspect order:/ }),
    ).toHaveCount(0);
    await page
      .getByLabel("Observed stage", { exact: true })
      .selectOption("liquidity");
    const detail = await read<DecisionDetail>(
      request,
      owned.auth,
      `/evaluations/${right.leaf.evaluation_id}/decisions/${chosen.decision_id}?${query({ ...leafPins, stage_id: "liquidity", instrument_id: "C" })}`,
    );
    const predicate = detail.predicates.items!.find(
      (row) => row.observation.condition_id === "liquidity_minimum",
    )!;
    expect(predicate.observation.state).toBe("false");
    expect(predicate.observation.operands![0]).toBeCloseTo(370 / 3, 10);
    expect(predicate.observation.threshold).toBe(1000);
    const row = page.getByTestId("replay-predicate-liquidity_minimum");
    await expect(row).toContainText("false");
    await expect(row).toContainText("1000");
    await row
      .getByRole("button", {
        name: "Inspect condition: liquidity_minimum",
        exact: true,
      })
      .click();
    await pinned(page, {
      decision_id: chosen.decision_id,
      instrument_id: "C",
      stage_id: "liquidity",
      record_kind: "predicate",
      record_id: "liquidity_minimum",
      record_ordinal: predicate.ordinal,
    });
    await expect(page.getByTestId("replay-pre-state")).toContainText("10000");
    // Re-select the declared rule explicitly. Inspecting runtime observations
    // correctly clears a guessed rule association, so a note must not invent it.
    await page.getByRole("link", { name: "Description", exact: true }).click();
    await page
      .getByRole("button", {
        name: `Inspect rule: ${liquidity.title}`,
        exact: true,
      })
      .click();
    await pinned(page, {
      rule_id: "liquidity",
      decision_id: chosen.decision_id,
      record_kind: "predicate",
      record_ordinal: predicate.ordinal,
    });
  });
  await test.step("4. Compare parent mechanism and actual paired behavior with BOTH pre-states", async () => {
    await page
      .getByRole("button", { name: "Compare with code parent", exact: true })
      .click();
    expect(
      new URL(page.url()).searchParams.get("left_evaluation_id"),
    ).toBeNull();
    await page
      .getByLabel("Left actual leaf Evaluation", { exact: true })
      .selectOption(left.leaf.evaluation_id);
    const created = await submit<ComparisonPublic>(
      page,
      "/comparisons",
      "Create immutable comparison",
    );
    owned.comparisons.add(created.id);
    expect(created.request.left).toEqual(sourcePins(left));
    expect(created.request.right).toEqual(sourcePins(right));
    savedComparison = await comparison(request, owned.auth, created.id);
    expect(savedComparison.manifest.observed_behavior).toBe("changed");
    expect(savedComparison.manifest.causal_claim).toBe(false);
    expect(savedComparison.manifest.local_counterfactual).toBe(false);
    expect(savedComparison.manifest.mechanism_changes).toContainEqual(
      expect.objectContaining({
        rule_id: "liquidity",
        mapping: "controlled",
        status: "changed",
      }),
    );
    await expect(page.getByTestId("comparison-mechanisms")).toContainText(
      "liquidity",
    );
    await expect(page.getByTestId("comparison-outcome")).toContainText(
      /changed/i,
    );
    const events = await section(
      request,
      owned.auth,
      savedComparison,
      "events",
    );
    const current = events.items.find(
      (item) =>
        (item.right as DecisionHeader | null)?.decision_id ===
        chosen.decision_id,
    )!;
    expect(current.match_basis).toBe("source_event");
    await page
      .getByLabel("Explicit event pair", { exact: true })
      .selectOption(current.event_pair_id!);
    for (const side of ["left", "right"] as const) {
      expect(
        savedComparison[`${side}_timestamp_evidence`]?.timestamp_semantics,
      ).toBe("native_daily_session_labels");
      expect((current[side] as DecisionHeader).pre_state?.cash).toBe(10000);
      await expect(
        page.getByTestId(`comparison-prestate-${side}`),
      ).toContainText("10000");
      await expect(
        page.getByTestId(`comparison-prestate-${side}`),
      ).toContainText("strategy_emitted");
      await expect(
        page.getByTestId(`comparison-prestate-${side}`),
      ).toContainText("market-local midnight session labels");
    }
    // Subsequently derived states differ. No current-rule causal attribution or
    // same-intermediate-state injection is inferred from these full paths.
    const divergent = events.items.find((item) => {
      const a = item.left as DecisionHeader | null,
        b = item.right as DecisionHeader | null;
      return (
        a?.pre_state &&
        b?.pre_state &&
        (a.pre_state.cash !== b.pre_state.cash ||
          JSON.stringify(a.pre_state.positions) !==
            JSON.stringify(b.pre_state.positions))
      );
    })!;
    expect(
      divergent,
      "Actual histories must retain divergent portfolio state",
    ).toBeDefined();
    await page
      .getByLabel("Explicit event pair", { exact: true })
      .selectOption(divergent.event_pair_id!);
    for (const side of ["left", "right"] as const)
      await expect(
        page.getByTestId(`comparison-prestate-${side}`),
      ).toContainText(
        String((divergent[side] as DecisionHeader).pre_state!.cash),
      );
    await page
      .getByLabel("Explicit event pair", { exact: true })
      .selectOption(current.event_pair_id!);
    await page
      .getByLabel("Behavior section", { exact: true })
      .selectOption("candidates");
    const changes = await section(
      request,
      owned.auth,
      savedComparison,
      "candidates",
      { event_pair_id: current.event_pair_id! },
    );
    const c = changes.items.find((item) => item.instrument_id === "C")!;
    expect(c.status).toBe("changed");
    await expect(
      page.getByTestId(`comparison-record-${c.row_id}`),
    ).toContainText("changed");
    await expect(
      page
        .getByTestId("comparison-prestates")
        .getByText("Both decision pre-states must be read", { exact: false }),
    ).toBeVisible();
  });
  await test.step("5. Manually execute two independent fixed-condition arms through the UI", async () => {
    await page.getByRole("link", { name: "Verification", exact: true }).click();
    await page
      .getByLabel("Controlled experiment name", { exact: true })
      .fill(`I7 isolate liquidity ${crypto.randomUUID()}`);
    await page
      .getByLabel("Hypothesis", { exact: true })
      .fill(
        "Liquidity filtering excludes higher-return C under identical frozen conditions.",
      );
    await page
      .getByLabel("Planned change and isolated mechanism", { exact: true })
      .fill(
        "Disable only liquidity_enabled. Source, data, universe, initialization, costs and engine remain fixed.",
      );
    const parameters = page.getByLabel("Business parameter values (JSON)", {
      exact: true,
    });
    const original = JSON.parse(await parameters.inputValue()) as Record<
      string,
      unknown
    >;
    expect(original.liquidity_enabled).toBe(true);
    await parameters.fill(
      JSON.stringify({ ...original, liquidity_enabled: false }, null, 2),
    );
    await page
      .getByLabel("This arm's planned mechanism change", { exact: true })
      .fill("Only liquidity_enabled true to false, with no code mutation.");
    await expect(
      page.getByText("single parameter change not causal proof", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByTestId("fixed-protocol-fields").first(),
    ).toContainText(right.body.dataset.sha256);
    control = await submit<ControlledExperimentPublic>(
      page,
      "/controlled-experiments",
      "Create 2-arm controlled experiment",
    );
    for (const arm of control.arms) owned.independent.add(arm.run_id);
    expect(control.request.source).toEqual(sourcePins(right));
    expect(control.arms).toHaveLength(2);
    expect(control.request.arms[0].parameters).toEqual(original);
    expect(control.request.arms[1].parameters).toEqual({
      ...original,
      liquidity_enabled: false,
    });
    expect(
      control.request.arms.every((arm) => arm.code === right.strategy.code),
    ).toBe(true);
    jobs = await Promise.all(
      control.arms.map((arm) => independent(request, owned.auth, arm.run_id)),
    );
    expect(new Set(jobs.map((job) => job.config.protocol_sha256))).toEqual(
      new Set([control.protocol_sha256]),
    );
    expect(new Set(jobs.map((job) => job.result_pin!.evaluation_id)).size).toBe(
      2,
    );
    expect(
      jobs.every(
        (job) =>
          job.config.execution_mode === "independent_evaluation" &&
          job.result_ready,
      ),
    ).toBe(true);
    expect(control.parent_experiment_id).toBe(right.run.experiment_id);
    await expect(page.getByTestId("controlled-arms")).toContainText(
      "succeeded",
      { timeout: 60000 },
    );
    await page
      .getByRole("button", { name: "Inspect arm 2", exact: true })
      .click();
    await pinned(page, {
      controlled_experiment_id: control.id,
      controlled_arm: 1,
      controlled_arm_evaluation_id: jobs[1].result_pin!.evaluation_id,
      controlled_arm_result_manifest_sha256:
        jobs[1].result_pin!.result_manifest_sha256,
    });
    await expect(
      page.getByRole("heading", {
        name: "Actual independent economic observations",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByTestId("fixed-protocol-fields").last(),
    ).toContainText(right.body.universe.sha256);
    // Arm replay opens its own immutable source and leaf. Preserve the complete
    // original-source selection, including rule, predicate and comparison.
    const originalControlUrl = page.url();
    const originalControlSelection = Object.fromEntries(
      new URL(originalControlUrl).searchParams,
    );
    const armDecisionIds: string[] = [];
    for (const [ordinal, job] of jobs.entries()) {
      const pin = job.result_pin!;
      expect(pin.trace_manifest_sha256).toMatch(/^[a-f0-9]{64}$/);
      const armPins = {
        execution_attempt_id: pin.execution_attempt_id,
        result_manifest_sha256: pin.result_manifest_sha256,
        trace_manifest_sha256: pin.trace_manifest_sha256!,
      };
      const armSpec = await read<WorkbenchSpec>(
        request,
        owned.auth,
        `/strategy-versions/${pin.source_id}/spec`,
      );
      const armEvents = await read<DecisionPage>(
        request,
        owned.auth,
        `/evaluations/${pin.evaluation_id}/decisions?${query({ ...armPins, start: "2024-01-17", end: "2024-01-18", limit: 50 })}`,
      );
      // Date bounds the read; only the complete source-event tuple establishes
      // correspondence. Never select a nearby decision or final execution event.
      const matching = armEvents.items!.filter(
        (event) =>
          event.data_event_id === chosen.data_event_id &&
          event.callback_phase === chosen.callback_phase &&
          event.event_seq === chosen.event_seq &&
          event.scope === chosen.scope &&
          event.instrument_id === chosen.instrument_id,
      );
      expect(matching, `Arm ${ordinal + 1} exact source event`).toHaveLength(1);
      const armDecision = matching[0];
      expect(armDecision.session_date).toBe("2024-01-17");
      expect(armDecision.decision_id).not.toBe(chosen.decision_id);
      armDecisionIds.push(armDecision.decision_id);
      expect(armEvents.context.timestamp_semantics).toBe(
        "native_daily_session_labels",
      );
      const observed = await read<CandidatePage>(
        request,
        owned.auth,
        `/evaluations/${pin.evaluation_id}/decisions/${armDecision.decision_id}/candidates?${query({ ...armPins, stage_id: "ranking", limit: 50 })}`,
      );
      expect(observed.items).toHaveLength(4);
      const candidates = Object.fromEntries(
        observed.items!.map((row) => [
          row.observation.instrument_id,
          row.observation,
        ]),
      );
      expect(candidates.B.score).toBeCloseTo(0.2, 12);
      expect(candidates.C.score).toBeCloseTo(0.5, 12);
      expect(candidates.C.score!).toBeGreaterThan(candidates.B.score!);
      if (ordinal === 0) {
        expect(candidates.B).toMatchObject({
          selected: true,
          rank: 1,
          eligibility: "eligible",
          reason: "selected",
        });
        expect(candidates.C).toMatchObject({
          selected: false,
          rank: null,
          eligibility: "ineligible",
          elimination_stage: "liquidity",
          reason: "eligibility_filter",
          reason_detail: "liquidity_below_threshold",
        });
      } else {
        expect(candidates.C).toMatchObject({
          selected: true,
          rank: 1,
          eligibility: "eligible",
          elimination_stage: null,
          reason: "selected",
        });
        expect(candidates.B).toMatchObject({
          selected: false,
          rank: 2,
          eligibility: "eligible",
          reason: "rank_not_selected",
        });
      }
      const armDetail = await read<DecisionDetail>(
        request,
        owned.auth,
        `/evaluations/${pin.evaluation_id}/decisions/${armDecision.decision_id}?${query({ ...armPins, stage_id: "liquidity", instrument_id: "C" })}`,
      );
      expect(armDetail.decision?.pre_state?.cash).toBe(10000);
      expect(armDetail.context.timestamp_semantics).toBe(
        "native_daily_session_labels",
      );
      const predicates = armDetail.predicates.items!.filter(
        (row) => row.observation.condition_id === "liquidity_minimum",
      );
      expect(predicates).toHaveLength(1);
      const predicate = predicates[0];
      expect(predicate.observation.operands![0]).toBeCloseTo(370 / 3, 10);
      expect(predicate.observation.threshold).toBe(1000);
      expect(predicate.observation.state).toBe(
        ordinal === 0 ? "false" : "not_evaluated",
      );
      if (ordinal === 1)
        expect(predicate.observation.missing_reason).toBe(
          "liquidity_filter_disabled",
        );

      await page
        .getByRole("button", {
          name: `Inspect arm ${ordinal + 1}`,
          exact: true,
        })
        .click();
      await pinned(page, {
        controlled_experiment_id: control.id,
        controlled_arm: ordinal,
        controlled_arm_run_id: job.id,
        controlled_arm_evaluation_id: pin.evaluation_id,
        controlled_arm_execution_attempt_id: pin.execution_attempt_id,
        controlled_arm_result_manifest_sha256: pin.result_manifest_sha256,
        controlled_arm_trace_manifest_sha256: pin.trace_manifest_sha256!,
      });
      const economics = await read<IndependentResultQuery>(
        request,
        owned.auth,
        `/independent-evaluations/evaluations/${pin.evaluation_id}/result?${query({ execution_attempt_id: pin.execution_attempt_id, result_manifest_sha256: pin.result_manifest_sha256, section: "equity", offset: 0, limit: 25 })}`,
      );
      expect(economics).toMatchObject({
        evaluation_id: pin.evaluation_id,
        execution_attempt_id: pin.execution_attempt_id,
        run_id: job.id,
        artifact_sha256: pin.result_manifest_sha256,
        section: "equity",
        purpose: "controlled",
        reward_status: "not_applicable",
      });
      expect(economics.rows.length).toBeGreaterThan(0);
      const economicPanel = page.locator("section.panel").filter({
        has: page.getByRole("heading", {
          name: "Actual independent economic observations",
          exact: true,
        }),
      });
      await economicPanel
        .getByRole("button", { name: "equity", exact: true })
        .click();
      await expect(economicPanel).toContainText(pin.result_manifest_sha256);
      await expect(economicPanel.locator("table thead th")).toHaveText(
        economics.columns,
      );
      await expect(economicPanel.locator("table tbody tr")).toHaveCount(
        economics.rows.length,
      );
      const firstRow = economics.rows[0];
      const cellValues = economics.columns.map((column, index) => {
        const value = Array.isArray(firstRow)
          ? firstRow[index]
          : (firstRow as Record<string, unknown>)[column];
        return value === null || value === undefined || value === ""
          ? "Unknown"
          : typeof value === "object"
            ? JSON.stringify(value)
            : String(value);
      });
      await expect(
        economicPanel.locator("table tbody tr").first().locator("td"),
      ).toHaveText(cellValues);

      await page
        .getByRole("link", {
          name: `Open arm ${ordinal + 1} experiment result replay`,
          exact: true,
        })
        .click();
      await expect(page).toHaveURL(
        new RegExp(`/strategies/${pin.source_id}/research`),
      );
      await pinned(page, {
        evaluation_id: pin.evaluation_id,
        ...armPins,
        code_sha256: pin.code_sha256,
        spec_revision: armSpec.revision,
        spec_sha256: armSpec.spec_sha256,
      });
      await page
        .getByLabel("Decision date", { exact: true })
        .fill("2024-01-17");
      await page
        .getByLabel("Specific decision event", { exact: true })
        .selectOption(armDecision.decision_id);
      await page
        .getByLabel("Observed stage", { exact: true })
        .selectOption("ranking");
      const armTable = page.getByTestId("replay-candidates-table");
      await expect(armTable.locator("tbody tr")).toHaveCount(4);
      await expect(armTable.getByTestId("replay-candidate-B")).toContainText(
        `Selected: ${ordinal === 0}`,
      );
      await expect(armTable.getByTestId("replay-candidate-C")).toContainText(
        `Selected: ${ordinal === 1}`,
      );
      await expect(armTable.getByTestId("replay-candidate-C")).toContainText(
        ordinal === 0 ? "eligibility_filter" : "selected",
      );
      await page
        .getByRole("button", { name: "Inspect candidate: C", exact: true })
        .click();
      await page
        .getByLabel("Observed stage", { exact: true })
        .selectOption("liquidity");
      const condition = page.getByTestId("replay-predicate-liquidity_minimum");
      await expect(condition).toContainText(
        ordinal === 0 ? "false" : "not_evaluated",
      );
      await expect(condition).toContainText("1000");
      if (ordinal === 1)
        await expect(condition).toContainText("liquidity_filter_disabled");
      await condition
        .getByRole("button", {
          name: "Inspect condition: liquidity_minimum",
          exact: true,
        })
        .click();
      await pinned(page, {
        ...armPins,
        evaluation_id: pin.evaluation_id,
        decision_id: armDecision.decision_id,
        instrument_id: "C",
        stage_id: "liquidity",
        record_kind: "predicate",
        record_id: "liquidity_minimum",
        record_ordinal: predicate.ordinal,
      });
      await expect(page.getByTestId("replay-event-identity")).toContainText(
        "market-local midnight session labels",
      );
      await expect(page.getByTestId("replay-pre-state")).toContainText("10000");
      await page.goto(originalControlUrl);
      await pinned(page, originalControlSelection);
      await expect(page.getByTestId("controlled-arms")).toContainText(
        pin.evaluation_id,
      );
    }
    expect(new Set(armDecisionIds).size).toBe(2);
    await pinned(page, {
      ...leafPins,
      evaluation_id: right.leaf.evaluation_id,
      decision_id: chosen.decision_id,
      instrument_id: "C",
      stage_id: "liquidity",
      rule_id: "liquidity",
      comparison_id: savedComparison.comparison_id,
      controlled_experiment_id: control.id,
      controlled_arm: 1,
      controlled_arm_evaluation_id: jobs[1].result_pin!.evaluation_id,
    });
  });
  await test.step("6. Save one evidence note with counterexamples and explicit limitations", async () => {
    exactSelection = Object.fromEntries(new URL(page.url()).searchParams);
    expect(exactSelection.decision_id).toBe(chosen.decision_id);
    expect(exactSelection.comparison_id).toBe(savedComparison.comparison_id);
    expect(exactSelection.controlled_experiment_id).toBe(control.id);
    await page
      .getByRole("button", {
        name: "Write note from this evidence",
        exact: true,
      })
      .click();
    await page
      .getByRole("button", {
        name: "Create note from current evidence",
        exact: true,
      })
      .click();
    await page
      .getByLabel("Note title", { exact: true })
      .fill(`I7 B selected, C filtered ${crypto.randomUUID()}`);
    await page
      .getByLabel("Hypothesis", { exact: true })
      .fill("Liquidity eligibility can override a higher return score.");
    await page
      .getByLabel("Behavior evidence", { exact: true })
      .fill(
        "B=12/10-1=.2; C=15/10-1=.5, but C dollar volume=370/3<1000. Inspect the actual predicate and both pre-states.",
      );
    await page
      .getByLabel("Control results", { exact: true })
      .fill(
        "The new independent baseline selected B and filtered higher-score C; the liquidity-disabled variant selected C with its liquidity predicate not_evaluated. Both exact source-event decisions and their new economic observations were inspected under one fixed protocol. Profit does not automatically validate the hypothesis.",
      );
    await page
      .getByLabel("Counterexamples", { exact: true })
      .fill(
        "Liquid candidates can lose on rank; held candidates can be retained. The unfiltered parent selects C.",
      );
    await page
      .getByLabel("Applicability and limitations", { exact: true })
      .fill(
        "Explicit synthetic repeated eight-session prices and user-fixed four-symbol universe, not live prices or historical membership. Saved native daily timestamps are market-local midnight session labels, not proven availability instants or intraday/look-ahead validation. Full paths diverge; no intermediate-state counterfactual or independent no-lookahead proof.",
      );
    note = await submit<ResearchNoteRevision>(
      page,
      "/research-notes",
      "Save immutable note revision",
      201,
    );
    expect(note.content.status).toBe("pending");
    expect(note.content.evidence![0]).toMatchObject({
      kind: "source",
      rule_id: "liquidity",
      source_ref: liquidity.source_ref,
      code_sha256: right.strategy.content_sha256,
      spec_revision: right.spec.revision,
      spec_sha256: right.spec.spec_sha256,
    });
    expect(note.content.evidence!.map((item) => item.kind)).toEqual([
      "source",
      "decision",
      "comparison",
      "controlled",
    ]);
    expect(note.evidence.every((item) => item.state === "available")).toBe(
      true,
    );
    await expect(page.getByTestId("saved-research-note")).toContainText(
      "Pending validation",
    );
    await pinned(page, { note_revision_sha256: note.revision_sha256 });
    noteUrl = page.url();
    const downloaded = page.waitForEvent("download");
    await page
      .getByRole("button", {
        name: "Download Markdown with references",
        exact: true,
      })
      .click();
    const path = await (await downloaded).path(),
      markdown = await readFile(path!, "utf8");
    for (const hash of [
      right.strategy.content_sha256,
      right.spec.spec_sha256,
      right.leaf.result_manifest_sha256!,
      savedComparison.manifest_sha256,
      control.protocol_sha256,
    ])
      expect(markdown).toContain(hash);
  });
  await test.step("7. Reload and relogin, restoring the SAME complete immutable research selection", async () => {
    await page.reload();
    await expect(page).toHaveURL(noteUrl);
    await expect(page.getByTestId("saved-research-note")).toContainText(
      note.revision_sha256,
    );
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await page.goto(noteUrl);
    await expect(page).toHaveURL(/\/login\?next=/);
    expect(new URL(page.url()).searchParams.get("next")).toBe(
      new URL(noteUrl).pathname + new URL(noteUrl).search,
    );
    await page.getByLabel("Email address").fill(account().email);
    await page.getByLabel("Password", { exact: true }).fill(account().password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).toHaveURL(noteUrl);
    await page
      .getByRole("link", {
        name: "Restore full saved research selection",
        exact: true,
      })
      .click();
    await pinned(page, exactSelection);
    await expect(page.getByTestId("workbench-selection")).toContainText(
      right.spec.spec_sha256,
    );
    await expect(page.getByTestId("controlled-arms")).toContainText(
      jobs[1].result_pin!.evaluation_id,
    );
    await page.goto(noteUrl);
    await page
      .getByRole("link", { name: "Open exact source evidence", exact: true })
      .click();
    await pinned(page, {
      tab: "description",
      rule_id: "liquidity",
      code_sha256: right.strategy.content_sha256,
      spec_revision: right.spec.revision,
      spec_sha256: right.spec.spec_sha256,
    });
    await expect(page.getByTestId("rule-liquidity")).toHaveAttribute(
      "data-selected",
      "true",
    );
    await expect(page.getByTestId("source-code-hash")).toContainText(
      right.strategy.content_sha256,
    );
    await expect(
      page.getByRole("region", { name: "Selected source lines" }),
    ).toContainText("def _read_inputs");
    await page.goto(noteUrl);
    await page
      .getByRole("link", { name: "Open exact decision evidence", exact: true })
      .click();
    await pinned(page, {
      evaluation_id: right.leaf.evaluation_id,
      ...leafPins,
      decision_id: chosen.decision_id,
      instrument_id: "C",
      stage_id: "liquidity",
      spec_revision: right.spec.revision,
      spec_sha256: right.spec.spec_sha256,
      code_sha256: right.strategy.content_sha256,
    });
    await expect(
      page.getByTestId("replay-predicate-liquidity_minimum"),
    ).toContainText("false");
    await page.goto(noteUrl);
    await page
      .getByRole("link", {
        name: "Open exact comparison evidence",
        exact: true,
      })
      .click();
    await pinned(page, {
      comparison_id: savedComparison.comparison_id,
      comparison_manifest_sha256: savedComparison.manifest_sha256,
      left_evaluation_id: left.leaf.evaluation_id,
      right_evaluation_id: right.leaf.evaluation_id,
    });
    await expect(page.getByTestId("comparison-prestates")).toBeVisible();
    await page.goto(noteUrl);
    await page
      .getByRole("link", {
        name: "Inspect saved arm 2 from note revision 1",
        exact: true,
      })
      .click();
    await pinned(page, {
      note_id: note.note_id,
      note_revision: note.revision,
      note_revision_sha256: note.revision_sha256,
      note_evidence_index: 3,
      note_arm: 1,
    });
    await expect(page.getByTestId("historical-control-arm")).toContainText(
      jobs[1].active_attempt_id!,
    );
    await expect(page.getByTestId("historical-control-arm")).toContainText(
      jobs[1].result_pin!.result_manifest_sha256,
    );
  });
  expect(
    mutations.filter((path) =>
      /\/data\/(imports|uploads)|\/research-runs|\/portable-runs/.test(path),
    ),
    "Browsing and restoration must not import, optimize or silently rerun",
  ).toEqual([]);
  expect(
    mutations.filter((path) => path === "POST /api/v1/controlled-experiments"),
  ).toHaveLength(1);
  expect(
    mutations.filter((path) => path === "POST /api/v1/comparisons"),
  ).toHaveLength(1);
});
