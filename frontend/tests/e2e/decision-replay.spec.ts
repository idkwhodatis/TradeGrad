import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import {
  test as base,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type {
  CandidatePage,
  CandidatePublic,
  CandidateRow,
  CoveragePublic,
  DatasetPublic,
  DecisionDetail,
  DecisionIndex,
  DecisionPage,
  EvaluationPublic,
  ImportDetail,
  ImportPublic,
  ReplayExecutionPage,
  ReplayInstrumentPage,
  ReplayMarketPage,
  ResearchCapabilities,
  ResearchRunCreate,
  ResearchRunDetail,
  ResearchRunPublic,
  ResearchTemplate,
  StrategyDetail,
  Token,
  TraceManifestPublic,
  UniversePublic,
  WorkbenchEvaluation,
  WorkbenchSpec,
} from "../../src/client/types.gen";
import { account, api, authorization, signIn, token } from "./helpers";

// I3 REAL-STACK ACCEPTANCE, NOT LOCALLY EXECUTED. A migrated PostgreSQL API,
// data/research workers and the actual isolated native Docker evaluator are
// required. These tests do not intercept responses, invent successful traces,
// execute strategy Python on the browser host, or call models/live providers.
// --list/typecheck only collect/check this specification, not its acceptance.
// Keep the original research-workbench.spec.ts regression suite unchanged.
type Owned = { auth: Token; runs: Set<string> };
const test = base.extend<{ owned: Owned }>({
  owned: async ({ request }, use) => {
    await use({ auth: await token(request), runs: new Set() });
  },
});
const sha256 = (value: string | Buffer) =>
  createHash("sha256").update(value).digest("hex");
const terminal = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "paused",
]);
const windows = {
  train: { start: "2024-02-01", end: "2024-03-15" },
  validation: { start: "2024-03-15", end: "2024-04-20" },
  holdout: { start: "2024-04-22", end: "2024-04-27" },
};
async function read<T>(request: APIRequestContext, auth: Token, path: string) {
  const response = await request.get(api(path), {
    headers: authorization(auth),
  });
  expect(response.status(), `Real GET ${path}`).toBe(200);
  return (await response.json()) as T;
}
async function rows<T>(request: APIRequestContext, auth: Token, path: string) {
  const result: T[] = [];
  for (;;) {
    const next = await read<{ data: T[]; count: number }>(
      request,
      auth,
      `${path}?offset=${result.length}&limit=100`,
    );
    result.push(...next.data);
    if (result.length >= next.count) return result;
    expect(
      next.data.length,
      `Pagination must advance for ${path}`,
    ).toBeGreaterThan(0);
  }
}
const query = (values: Record<string, string | number>) =>
  new URLSearchParams(
    Object.entries(values).map(([key, value]) => [key, String(value)]),
  ).toString();
const tracePins = (leaf: WorkbenchEvaluation) => ({
  execution_attempt_id: leaf.execution_attempt_id,
  result_manifest_sha256: leaf.result_manifest_sha256!,
  trace_manifest_sha256: leaf.trace_manifest_sha256!,
});
async function expectSelection(
  page: Page,
  values: Record<string, string | number>,
) {
  await expect
    .poll(() => {
      const params = new URL(page.url()).searchParams;
      return Object.fromEntries(
        Object.keys(values).map((key) => [key, params.get(key)]),
      );
    })
    .toEqual(
      Object.fromEntries(
        Object.entries(values).map(([key, value]) => [key, String(value)]),
      ),
    );
}
async function createTemplate(
  page: Page,
  request: APIRequestContext,
  auth: Token,
  templateId: ResearchTemplate["template_id"],
) {
  const templates = await rows<ResearchTemplate>(
    request,
    auth,
    "/research-templates",
  );
  const template = templates.find((item) => item.template_id === templateId);
  expect(
    template,
    "Use exact backend-maintained source, not a browser imitation",
  ).toBeDefined();
  expect(sha256(template!.code)).toBe(template!.code_sha256);
  await page.goto("/strategies");
  await page
    .getByRole("button", {
      name:
        templateId === "daily_timing"
          ? "Use Daily timing template"
          : "Use Cross-sectional template",
      exact: true,
    })
    .click();
  await expect(page.getByLabel("Python source", { exact: true })).toHaveValue(
    template!.code,
  );
  await page
    .getByLabel("Version name", { exact: true })
    .fill(`I3 ${templateId} ${crypto.randomUUID()}`);
  const saved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/strategy-versions" &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  const response = await saved;
  expect(response.status()).toBe(201);
  const strategy = (await response.json()) as StrategyDetail;
  expect(strategy.code).toBe(template!.code);
  expect(strategy.content_sha256).toBe(template!.code_sha256);
  expect(strategy.manifest).toEqual(template!.manifest);
  return { strategy, template: template! };
}
async function snapshot(
  request: APIRequestContext,
  auth: Token,
  cross: boolean,
) {
  let imported: ImportPublic;
  if (cross) {
    // This is the ordinary fixture-only import provider and real data worker,
    // not an API substitute. Its generated bars are NOT observed market data.
    const response = await request.post(api("/data/imports"), {
      headers: {
        ...authorization(auth),
        "Idempotency-Key": crypto.randomUUID(),
      },
      data: {
        name: `I3 synthetic cross basket ${crypto.randomUUID()}`,
        provider: "portable_fixture",
        market: "CN",
        symbols: ["600000", "000001"],
        start_date: "2024-01-02",
        end_date: "2024-04-27",
        price_mode: "raw",
      },
    });
    expect(response.status()).toBe(202);
    imported = (await response.json()) as ImportPublic;
  } else {
    const [csv, metadataText] = await Promise.all([
      readFile(new URL("../fixtures/research-cn.csv", import.meta.url)),
      readFile(
        new URL("../fixtures/research-cn.json", import.meta.url),
        "utf8",
      ),
    ]);
    const metadata = JSON.parse(metadataText) as {
      synthetic: boolean;
      sha256: string;
      rows: number;
    };
    expect(metadata.synthetic).toBe(true);
    expect(sha256(csv)).toBe(metadata.sha256);
    expect(csv.toString("utf8").trim().split(/\r?\n/).length - 1).toBe(
      metadata.rows,
    );
    const response = await request.post(api("/data/uploads"), {
      headers: {
        ...authorization(auth),
        "Idempotency-Key": crypto.randomUUID(),
      },
      multipart: {
        mapping: JSON.stringify({
          name: `I3 frozen daily snapshot ${crypto.randomUUID()}`,
          symbol: "600000",
          market: "CN",
          mic: "XSHG",
          currency: "CNY",
          timezone: "Asia/Shanghai",
          calendar: "XSHG",
          start_date: "2024-01-02",
          end_date: "2024-04-27",
          columns: {
            session_date: "date",
            open: "open",
            high: "high",
            low: "low",
            close: "close",
            volume: "volume",
            amount: "amount",
          },
          volume_unit: "shares",
          price_semantics: {
            native_mode: "user_declared_synthetic",
            split_basis: "unadjusted",
            dividend_basis: "unadjusted",
            note: "Frozen synthetic browser fixture, not market prices or provider evidence",
          },
          unknown_flags: [],
        }),
        file: { name: "research-cn.csv", mimeType: "text/csv", buffer: csv },
      },
    });
    expect(response.status()).toBe(202);
    imported = (await response.json()) as ImportPublic;
  }
  await expect
    .poll(
      async () => {
        const job = await read<ImportDetail>(
          request,
          auth,
          `/data/imports/${imported.id}`,
        );
        if (["failed", "partial", "cancelled"].includes(job.status))
          throw new Error(
            `Real data worker ended ${job.status}: ${job.error}; do not substitute fabricated data`,
          );
        return job.status;
      },
      { timeout: 120_000, intervals: [250, 500, 1000] },
    )
    .toBe("ready");
  const ready = await read<ImportDetail>(
    request,
    auth,
    `/data/imports/${imported.id}`,
  );
  expect(ready.dataset_version_id).not.toBeNull();
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
  expect(coverage.dataset_version_id).toBe(dataset.id);
  expect((coverage.universe.members as unknown[]).length).toBe(cross ? 2 : 1);
  const response = await request.post(api("/data/universes"), {
    headers: authorization(auth),
    data: {
      name: `I3 fixed universe ${crypto.randomUUID()}`,
      state: "user_fixed",
      as_of: new Date().toISOString(),
      members: coverage.universe.members,
      evidence: [],
    },
  });
  expect(response.status()).toBe(201);
  return { dataset, universe: (await response.json()) as UniversePublic };
}
async function actualReplay(
  page: Page,
  request: APIRequestContext,
  owned: Owned,
  templateId: ResearchTemplate["template_id"] = "daily_timing",
) {
  await signIn(page);
  const { strategy, template } = await createTemplate(
    page,
    request,
    owned.auth,
    templateId,
  );
  const { dataset, universe } = await snapshot(
    request,
    owned.auth,
    templateId === "cross_sectional",
  );
  const capabilities = await read<ResearchCapabilities>(
    request,
    owned.auth,
    "/research-capabilities",
  );
  expect(capabilities.live_model_calls).toBe(false);
  expect(capabilities.model_providers).toEqual(["fixture"]);
  expect(capabilities.defaults?.optimizer).toBeDefined();
  const profile = capabilities.profile_defaults.find(
    (item) =>
      item.task === strategy.task &&
      item.profile === "corrected_explicit_config",
  );
  expect(profile).toBeDefined();
  const body: ResearchRunCreate = {
    schema_version: "tradegrad.run.v1",
    execution_mode: "optimization",
    task: strategy.task,
    profile: "corrected_explicit_config",
    dataset: { id: dataset.id, sha256: dataset.content_sha256 },
    universe: { id: universe.id, sha256: universe.content_sha256 },
    seed_strategy: { id: strategy.id, sha256: strategy.content_sha256 },
    strategy_contract: template.manifest,
    windows,
    warmup: { ...profile!.warmup, sessions: 20 },
    market_profile: structuredClone(profile!.market_profile),
    ...structuredClone(capabilities.defaults),
    optimizer: { ...capabilities.defaults!.optimizer, max_iterations: 0 },
    budgets: {
      max_model_calls: 0,
      max_tokens: 0,
      max_runtime_seconds: 180,
      max_repairs: 0,
    },
    prompt_version: "source_prompts.v1",
    environment_digest: capabilities.environment_digest,
    benchmark: null,
    research_capture: { mode: "research" },
  };
  const response = await request.post(api("/research-runs"), {
    headers: {
      ...authorization(owned.auth),
      "Idempotency-Key": crypto.randomUUID(),
    },
    data: body,
  });
  expect(response.status(), await response.text()).toBe(202);
  const submitted = (await response.json()) as ResearchRunPublic;
  owned.runs.add(submitted.id);
  await expect
    .poll(
      async () => {
        const run = await read<ResearchRunDetail>(
          request,
          owned.auth,
          `/research-runs/${submitted.id}`,
        );
        if (terminal.has(run.status) && run.status !== "succeeded")
          throw new Error(
            `Actual isolated research ended ${run.status}: ${run.error}; no fabricated success is permitted`,
          );
        return run.status;
      },
      { timeout: 210_000, intervals: [250, 500, 1000] },
    )
    .toBe("succeeded");
  expect(
    await rows(
      request,
      owned.auth,
      `/research-runs/${submitted.id}/model-calls`,
    ),
  ).toEqual([]);
  const candidates = await rows<CandidatePublic>(
    request,
    owned.auth,
    `/research-runs/${submitted.id}/candidates`,
  );
  expect(candidates).toHaveLength(1);
  const candidate = candidates[0];
  expect(candidate.kind).toBe("seed");
  expect(candidate.code_sha256).toBe(strategy.content_sha256);
  const leaves = await rows<WorkbenchEvaluation>(
    request,
    owned.auth,
    `/candidates/${candidate.id}/evaluations`,
  );
  expect(leaves.map((leaf) => leaf.split).sort()).toEqual([
    "train",
    "validation",
  ]);
  const leaf = leaves.find((item) => item.split === "train")!;
  expect(leaf.status).toBe("succeeded");
  expect(leaf.evidence_status).toBe("registered");
  expect(leaf.result_manifest_sha256).toMatch(/^[a-f0-9]{64}$/);
  expect(leaf.trace_manifest_sha256).toMatch(/^[a-f0-9]{64}$/);
  const spec = await read<WorkbenchSpec>(
    request,
    owned.auth,
    `/candidates/${candidate.id}/spec`,
  );
  expect(spec.code_sha256).toBe(strategy.content_sha256);
  const pins = {
    candidate_id: candidate.id,
    evaluation_id: leaf.evaluation_id,
    ...tracePins(leaf),
    code_sha256: strategy.content_sha256,
    spec_revision: spec.revision,
    spec_sha256: spec.spec_sha256,
  };
  const basePath = `/evaluations/${leaf.evaluation_id}`;
  const manifest = await read<TraceManifestPublic>(
    request,
    owned.auth,
    `${basePath}/trace-manifest?${query(tracePins(leaf))}`,
  );
  expect(manifest.context.status).toBe("available");
  expect(manifest.context.binding?.code_sha256).toBe(strategy.content_sha256);
  expect(manifest.manifest?.capture.mode).toBe("research");
  const events: DecisionIndex[] = [];
  for (;;) {
    const batch = await read<DecisionPage>(
      request,
      owned.auth,
      `${basePath}/decisions?${query({ ...tracePins(leaf), offset: events.length, limit: 100 })}`,
    );
    expect(batch.context).toEqual(manifest.context);
    expect(batch.items).not.toBeNull();
    events.push(...batch.items!);
    if (events.length >= batch.total!) break;
    expect(batch.items!.length).toBeGreaterThan(0);
  }
  expect(events.length).toBeGreaterThan(2);
  expect(new Set(events.map((event) => event.decision_id)).size).toBe(
    events.length,
  );
  const detail = (
    event: DecisionIndex,
    values: Record<string, string | number> = {},
  ) =>
    read<DecisionDetail>(
      request,
      owned.auth,
      `${basePath}/decisions/${event.decision_id}?${query({ ...tracePins(leaf), limit: 50, ...values })}`,
    );
  const candidateRows = (
    event: DecisionIndex,
    values: Record<string, string | number> = {},
  ) =>
    read<CandidatePage>(
      request,
      owned.auth,
      `${basePath}/decisions/${event.decision_id}/candidates?${query({ ...tracePins(leaf), limit: 50, ...values })}`,
    );
  const url = (values: Record<string, string | number> = {}) =>
    `/strategies/${strategy.id}/research?${query({ ...pins, tab: "replay", ...values })}`;
  return {
    strategy,
    dataset,
    universe,
    candidate,
    leaf,
    leaves,
    spec,
    pins,
    basePath,
    manifest,
    events,
    detail,
    candidateRows,
    url,
  };
}
async function chooseEvent(page: Page, event: DecisionIndex) {
  await page
    .getByLabel("Decision date", { exact: true })
    .fill(event.session_date);
  await page
    .getByLabel("Specific decision event", { exact: true })
    .selectOption(event.decision_id);
  await expectSelection(page, {
    session_date: event.session_date,
    decision_id: event.decision_id,
  });
  const identity = page.getByTestId("replay-event-identity");
  await expect(identity).toContainText(event.data_event_id);
  await expect(identity).toContainText(event.callback_phase);
  await expect(identity).toContainText(event.market_timezone);
}
async function applyFilters(page: Page, values: Record<string, string> = {}) {
  for (const label of ["Eligibility filter", "Reason filter"])
    await page
      .getByLabel(label, { exact: true })
      .selectOption(values[label] || "");
  for (const label of [
    "Minimum rank",
    "Maximum rank",
    "Minimum score",
    "Maximum score",
  ])
    await page.getByLabel(label, { exact: true }).fill(values[label] || "");
  await page
    .getByRole("button", { name: "Apply candidate filters", exact: true })
    .click();
}
async function expectCandidates(page: Page, rows: CandidateRow[]) {
  const table = page.getByTestId("replay-candidates-table");
  await expect(table.locator("tbody tr")).toHaveCount(rows.length);
  for (const { observation } of rows) {
    const row = table.getByTestId(
      `replay-candidate-${observation.instrument_id}`,
    );
    await expect(row).toContainText(observation.instrument_id);
    await expect(row).toContainText(observation.eligibility);
    await expect(row).toContainText(observation.reason);
  }
}

test.use({ timezoneId: "America/Los_Angeles" });

test.beforeAll(async ({ request }) => {
  account();
  account(true);
  expect(
    (await request.get(api("/health/ready"))).ok(),
    "Use a real migrated PostgreSQL stack",
  ).toBe(true);
});
test.afterEach(async ({ request, owned }) => {
  for (const id of owned.runs) {
    const run = await read<ResearchRunDetail>(
      request,
      owned.auth,
      `/research-runs/${id}`,
    );
    if (!terminal.has(run.status) || run.status === "paused") {
      const response = await request.post(api(`/research-runs/${id}/cancel`), {
        headers: authorization(owned.auth),
      });
      expect(response.status()).toBe(202);
    }
    await expect
      .poll(
        async () => {
          const current = await read<ResearchRunDetail>(
            request,
            owned.auth,
            `/research-runs/${id}`,
          );
          const children = await rows<EvaluationPublic>(
            request,
            owned.auth,
            `/research-runs/${id}/evaluations`,
          );
          return (
            terminal.has(current.status) &&
            current.attempts.every((attempt) => attempt.sandbox_stopped) &&
            children.every(
              (child) => child.sandbox_stopped && child.finished_at !== null,
            )
          );
        },
        { timeout: 45_000, intervals: [250, 500, 1000] },
      )
      .toBe(true);
  }
});

test("actual daily decisions retain non-trades, saved operands, pre-state and execution identity through history and owner checks", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(420_000);
  const replay = await actualReplay(page, request, owned);
  const strategyEvents = replay.events.filter((event) =>
    event.stage_ids.includes("signal"),
  );
  expect(strategyEvents.length).toBeGreaterThan(2);
  const observed: {
    event: DecisionIndex;
    detail: DecisionDetail;
    candidates: CandidatePage;
  }[] = [];
  for (const event of strategyEvents) {
    const detail = await replay.detail(event);
    const candidates = await replay.candidateRows(event, {
      stage_id: "signal",
    });
    expect(detail.context).toEqual(replay.manifest.context);
    expect(detail.decision?.decision_id).toBe(event.decision_id);
    expect(candidates.items).toHaveLength(1);
    observed.push({ event, detail, candidates });
  }
  const missed = observed.find(
    (row) =>
      row.candidates.items![0].observation.reason === "timing_not_triggered",
  );
  const intended = observed.find((row) =>
    row.detail.stages?.some((stage) =>
      stage.order_links.some((link) => link.child_order_ids.length > 0),
    ),
  );
  expect(
    missed,
    "Frozen prices must produce a real untriggered timing decision",
  ).toBeDefined();
  expect(
    intended,
    "Real template must submit an engine order, not just describe one",
  ).toBeDefined();
  const candidate = missed!.candidates.items![0].observation;
  expect(candidate.selected).toBe(false);
  expect(candidate.score).not.toBeNull();
  expect(missed!.detail.stages?.flatMap((stage) => stage.order_links)).toEqual(
    [],
  );
  const browsingMutations: string[] = [];
  page.on("request", (wire) => {
    if (
      wire.method() !== "GET" &&
      /\/api\/v1\/(data\/(imports|uploads)|research-runs|portable-runs)/.test(
        new URL(wire.url()).pathname,
      )
    )
      browsingMutations.push(
        `${wire.method()} ${new URL(wire.url()).pathname}`,
      );
  });
  await page.goto(replay.url());
  await chooseEvent(page, missed!.event);
  for (const name of [
    "Decision event selection",
    "Market retrospective and saved features",
    "Candidate observations",
    "Actual conditions and features",
    "Portfolio and execution",
  ])
    await expect(
      page.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
  await page
    .getByLabel("Observed stage", { exact: true })
    .selectOption("signal");
  await expectCandidates(page, missed!.candidates.items!);
  await applyFilters(page, {
    "Eligibility filter": candidate.eligibility,
    "Reason filter": "timing_not_triggered",
    "Minimum score": String(candidate.score! - 1),
    "Maximum score": String(candidate.score! + 1),
  });
  await expectSelection(page, {
    eligibility: candidate.eligibility,
    reason: "timing_not_triggered",
    score_min: candidate.score! - 1,
    score_max: candidate.score! + 1,
  });
  await expectCandidates(
    page,
    (
      await replay.candidateRows(missed!.event, {
        stage_id: "signal",
        eligibility: candidate.eligibility,
        reason: "timing_not_triggered",
        score_min: candidate.score! - 1,
        score_max: candidate.score! + 1,
      })
    ).items!,
  );
  await applyFilters(page, { "Reason filter": "selected" });
  await expect(
    page.getByTestId(`replay-candidate-${candidate.instrument_id}`),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Reset candidate filters", exact: true })
    .click();
  await expectCandidates(page, missed!.candidates.items!);
  await page
    .getByRole("button", {
      name: `Inspect candidate: ${candidate.instrument_id}`,
      exact: true,
    })
    .click();
  await expectSelection(page, {
    instrument_id: candidate.instrument_id,
    record_kind: "candidate",
    record_id: candidate.instrument_id,
  });
  await expect(
    page.getByLabel("Observed instrument", { exact: true }),
  ).toHaveValue(candidate.instrument_id);
  const instruments = await read<ReplayInstrumentPage>(
    request,
    owned.auth,
    `${replay.basePath}/replay-instruments?${query({ ...tracePins(replay.leaf), session_date: missed!.event.session_date })}`,
  );
  expect(instruments.context).toEqual(replay.manifest.context);
  expect(instruments.market.dataset_version_id).toBe(replay.dataset.id);
  expect(instruments.market.dataset_sha256).toBe(replay.dataset.content_sha256);
  expect(instruments.market.universe_version_id).toBe(replay.universe.id);
  expect(instruments.market.historical_membership_status).toBe(
    "user_fixed_assumption",
  );
  expect(instruments.items).toHaveLength(1);
  expect(instruments.items[0].instrument_id).toBe(candidate.instrument_id);
  const market = await read<ReplayMarketPage>(
    request,
    owned.auth,
    `${replay.basePath}/decisions/${missed!.event.decision_id}/market?${query({ ...tracePins(replay.leaf), instrument_id: candidate.instrument_id })}`,
  );
  expect(market.context).toEqual(replay.manifest.context);
  expect(market.status).toBe("available");
  expect(market.information_cutoff).toBe(missed!.event.information_cutoff);
  expect(market.market.dataset_sha256).toBe(replay.dataset.content_sha256);
  expect(market.bars!.length).toBeGreaterThan(0);
  const fixtureRows = (
    await readFile(
      new URL("../fixtures/research-cn.csv", import.meta.url),
      "utf8",
    )
  )
    .trim()
    .split(/\r?\n/)
    .slice(1)
    .map((line) => line.split(","));
  for (const bar of market.bars!) {
    const original = fixtureRows.find(([date]) => date === bar.session_date);
    expect(original).toBeDefined();
    expect([bar.open, bar.high, bar.low, bar.close, bar.volume]).toEqual(
      original!.slice(1, 6).map(Number),
    );
  }
  const chart = page.getByRole("img", {
    name: "Retrospective market prices, information cutoff, decision time and actually saved feature points",
    exact: true,
  });
  await expect(chart).toBeVisible();
  await expect(chart.locator("svg")).toBeVisible();
  await expect(chart).toHaveAttribute(
    "aria-description",
    /market-local midnight session labels/,
  );
  await expect(
    page.getByText("user_fixed_assumption", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Later prices are hindsight", { exact: false }),
  ).toBeVisible();
  const predicate = missed!.detail.predicates.items!.find(
    (row) => row.observation.condition_id === "entry_signal",
  )!.observation;
  expect(predicate.state).toBe("false");
  expect(predicate.operands).not.toBeNull();
  expect(predicate.source_ref?.code_sha256).toBe(
    replay.strategy.content_sha256,
  );
  const predicateRow = page.getByTestId("replay-predicate-entry_signal");
  for (const value of [
    predicate.operator,
    predicate.state,
    predicate.source,
    String(predicate.threshold),
    String(predicate.operands![0]),
  ])
    await expect(predicateRow).toContainText(value);
  await page
    .getByRole("button", {
      name: "Inspect condition: entry_signal",
      exact: true,
    })
    .click();
  await expectSelection(page, {
    record_kind: "predicate",
    record_id: "entry_signal",
    record_ordinal: missed!.detail.predicates.items!.find(
      (row) => row.observation.condition_id === "entry_signal",
    )!.ordinal,
    stage_id: "signal",
  });
  await expect(page.getByTestId("source-code-hash")).toContainText(
    replay.strategy.content_sha256,
  );
  const lines = replay.strategy.code
    .split(/\r\n|\n|\r/)
    .slice(
      predicate.source_ref!.line_start - 1,
      predicate.source_ref!.line_end,
    );
  await expect(
    page.getByRole("region", { name: "Selected source lines" }).locator("pre"),
  ).toContainText(lines[0]);
  await page
    .getByLabel("Observed stage", { exact: true })
    .selectOption("features");
  for (const { observation: feature } of missed!.detail.features.items!) {
    const row = page.getByTestId(`replay-feature-${feature.feature_id}`);
    for (const value of [
      feature.unit,
      feature.validity,
      feature.calculation_window,
      feature.information_cutoff,
      String(feature.value),
    ])
      await expect(row).toContainText(value);
    expect(feature.information_cutoff).toBe(missed!.event.information_cutoff);
    expect(feature.source_ref?.code_sha256).toBe(
      replay.strategy.content_sha256,
    );
  }
  expect(missed!.detail.decision?.pre_state?.cash).not.toBeNull();
  const preState = page.getByTestId("replay-pre-state");
  await expect(preState).toContainText(
    String(missed!.detail.decision!.pre_state!.cash),
  );
  await expect(preState).toContainText(
    missed!.detail.decision!.pre_state!.source,
  );
  // Real short-circuit state is not displayed as false, and a recorded
  // empty portfolio remains distinguishable from an unrecorded pre-state.
  const unexecuted = missed!.detail.predicates.items!.find(
    (row) => row.observation.condition_id === "retention_band",
  )!;
  expect(unexecuted.observation.state).toBe("not_evaluated");
  expect(unexecuted.observation.missing_reason).toBe("no_prior_holding");
  await page
    .getByLabel("Observed stage", { exact: true })
    .selectOption("allocation");
  await expect(
    page.getByTestId("replay-predicate-retention_band"),
  ).toContainText("not_evaluated");
  await expect(
    page.getByTestId("replay-predicate-retention_band"),
  ).toContainText("no_prior_holding");

  // An execution-result event is a distinct observation, not a later fill
  // silently injected into the earlier callback's information cutoff/pre-state.
  const sameDate = replay.events.filter(
    (event) => event.session_date === intended!.event.session_date,
  );
  let repeated = sameDate;
  if (repeated.length < 2)
    repeated = replay.events.filter(
      (event) =>
        event.session_date ===
        replay.events.find((candidateEvent) =>
          replay.events.some(
            (other) =>
              other.session_date === candidateEvent.session_date &&
              other.decision_id !== candidateEvent.decision_id,
          ),
        )?.session_date,
    );
  expect(
    repeated.length,
    "Actual native execution events must exercise date versus event identity",
  ).toBeGreaterThan(1);
  for (const event of repeated.slice(0, 2)) {
    await chooseEvent(page, event);
    await expectSelection(page, replay.pins);
  }
  const finalEvent = replay.events.find((event) =>
    event.callback_phase.startsWith("native.execution_result"),
  );
  expect(finalEvent).toBeDefined();
  const finalDetail = await replay.detail(finalEvent!);
  expect(finalDetail.decision?.pre_state).toBeNull();
  expect(finalDetail.decision?.pre_state_missing_reason).toBeTruthy();
  await chooseEvent(page, finalEvent!);
  await expect(page.getByTestId("replay-pre-state")).toContainText(
    "Not recorded",
  );
  await expect(page.getByTestId("replay-pre-state")).toContainText(
    finalDetail.decision!.pre_state_missing_reason!,
  );
  await expect(page.getByTestId("replay-pre-state")).not.toContainText(
    "Available cash0",
  );
  await chooseEvent(page, intended!.event);
  await page
    .getByLabel("Observed stage", { exact: true })
    .selectOption("execution");
  const link = intended!.detail
    .stages!.flatMap((stage) => stage.order_links)
    .find((item) => item.child_order_ids.length > 0)!;
  const orderRow = page.getByTestId(
    `replay-execution-${link.logical_order_id}`,
  );
  await expect(orderRow).toContainText("intended");
  for (const id of link.child_order_ids)
    await expect(orderRow).toContainText(id);
  await page
    .getByRole("button", {
      name: `Inspect order: ${link.logical_order_id}`,
      exact: true,
    })
    .click();
  await expectSelection(page, {
    record_kind: "order",
    record_id: link.logical_order_id,
  });
  const execution = await read<ReplayExecutionPage>(
    request,
    owned.auth,
    `${replay.basePath}/decisions/${intended!.event.decision_id}/execution?${query({ ...tracePins(replay.leaf), instrument_id: link.instrument_id, stage_id: "execution", logical_order_id: link.logical_order_id })}`,
  );
  expect(execution.context).toEqual(replay.manifest.context);
  expect(execution.status).toBe("available");
  const childOrders = execution.items!.filter(
    (row) => row.record_kind === "child_order",
  );
  expect(childOrders.map((row) => row.record_id).sort()).toEqual(
    [...link.child_order_ids].sort(),
  );
  for (const row of childOrders) {
    expect(row.association_basis).toBe("child_order_id");
    expect(row.record?.id).toBe(row.record_id);
    expect(row.link.status).toBe("intended");
    await expect(
      page.getByRole("button", {
        name: `Open linked order group: ${row.logical_order_id} from child_order ${row.record_id}`,
        exact: true,
      }),
    ).toBeVisible();
  }
  for (const row of execution.items!.filter(
    (item) => item.record_kind === "fill",
  )) {
    expect(["child_order_id", "fill_id"]).toContain(row.association_basis);
    expect(link.child_order_ids).toContain(row.record?.order_id);
    await expect(
      page.getByRole("button", {
        name: `Open linked order group: ${row.logical_order_id} from fill ${row.record_id}`,
        exact: true,
      }),
    ).toBeVisible();
  }
  const selection = {
    ...replay.pins,
    tab: "replay",
    session_date: intended!.event.session_date,
    decision_id: intended!.event.decision_id,
    stage_id: "execution",
    instrument_id: link.instrument_id,
    record_kind: "order",
    record_id: link.logical_order_id,
  };
  await expectSelection(page, selection);
  const exactUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(exactUrl);
  await expect(orderRow).toContainText(link.logical_order_id);
  await expectSelection(page, selection);
  await page.getByRole("link", { name: "Description", exact: true }).click();
  const descriptionUrl = page.url();
  await page.goBack();
  await expect(page).toHaveURL(exactUrl);
  await expectSelection(page, selection);
  await page.goForward();
  await expect(page).toHaveURL(descriptionUrl);
  await page.goBack();
  await expect(page).toHaveURL(exactUrl);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.goto(exactUrl);
  await expect(page).toHaveURL(/\/login\?/);
  expect(new URL(page.url()).searchParams.get("next")).toBe(
    new URL(exactUrl).pathname + new URL(exactUrl).search,
  );
  await page.getByLabel("Email address").fill(account().email);
  await page.getByLabel("Password", { exact: true }).fill(account().password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(exactUrl);
  await expectSelection(page, selection);
  await expect(orderRow).toContainText(link.logical_order_id);

  const wrongHash =
    "0".repeat(64) === replay.leaf.result_manifest_sha256
      ? "1".repeat(64)
      : "0".repeat(64);
  for (const [key, value] of Object.entries({
    code_sha256: wrongHash,
    spec_sha256: wrongHash,
    execution_attempt_id: crypto.randomUUID(),
    result_manifest_sha256: wrongHash,
    trace_manifest_sha256: wrongHash,
  })) {
    const invalid = new URL(exactUrl);
    invalid.searchParams.set(key, value);
    await page.goto(invalid.href);
    await expect(page.getByRole("alert").first()).toBeVisible();
    await expectSelection(page, { [key]: value });
    await expect(page.getByTestId("replay-predicates-table")).toHaveCount(0);
    await expect(
      page.getByTestId(`replay-execution-${link.logical_order_id}`),
    ).toHaveCount(0);
  }
  for (const key of [
    "execution_attempt_id",
    "result_manifest_sha256",
    "trace_manifest_sha256",
  ]) {
    const invalidPins = {
      ...tracePins(replay.leaf),
      [key]: key === "execution_attempt_id" ? crypto.randomUUID() : wrongHash,
    };
    const denied = await request.get(
      api(
        `${replay.basePath}/decisions/${intended!.event.decision_id}?${query(invalidPins)}`,
      ),
      { headers: authorization(owned.auth) },
    );
    expect(denied.status()).toBe(409);
  }
  // A different genuine owned leaf still cannot borrow this leaf's attempt,
  // result, trace or decision pins merely because its candidate code is equal.
  const otherLeaf = replay.leaves.find(
    (leaf) => leaf.evaluation_id !== replay.leaf.evaluation_id,
  )!;
  const swappedLeaf = new URL(exactUrl);
  swappedLeaf.searchParams.set("evaluation_id", otherLeaf.evaluation_id);
  await page.goto(swappedLeaf.href);
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expectSelection(page, {
    evaluation_id: otherLeaf.evaluation_id,
    ...tracePins(replay.leaf),
  });
  await expect(page.getByTestId("replay-features-table")).toHaveCount(0);
  const wrongDate = replay.events.find(
    (event) => event.session_date !== intended!.event.session_date,
  )!.session_date;
  const dateMismatch = new URL(exactUrl);
  dateMismatch.searchParams.set("session_date", wrongDate);
  await page.goto(dateMismatch.href);
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expectSelection(page, {
    session_date: wrongDate,
    decision_id: intended!.event.decision_id,
  });
  await expect(page.getByTestId("replay-features-table")).toHaveCount(0);
  const foreignEvent = crypto.randomUUID();
  const invalidEvent = new URL(exactUrl);
  invalidEvent.searchParams.set("decision_id", foreignEvent);
  await page.goto(invalidEvent.href);
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expectSelection(page, { decision_id: foreignEvent });
  const other = await token(request, true);
  for (const suffix of [
    "/trace-manifest",
    "/decisions",
    "/replay-instruments",
    `/decisions/${intended!.event.decision_id}`,
    `/decisions/${intended!.event.decision_id}/candidates`,
    `/decisions/${intended!.event.decision_id}/execution`,
    `/decisions/${intended!.event.decision_id}/market`,
  ]) {
    const denied = await request.get(
      api(
        `${replay.basePath}${suffix}?${query({ ...tracePins(replay.leaf), instrument_id: link.instrument_id })}`,
      ),
      { headers: authorization(other) },
    );
    expect(denied.status()).toBe(404);
    expect(await denied.text()).not.toContain(link.logical_order_id);
  }
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(page, true);
  await page.goto(exactUrl);
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expect(page.getByTestId("replay-candidates-table")).toHaveCount(0);
  await expect(page.getByTestId("replay-features-table")).toHaveCount(0);
  await expect(page.getByTestId("source-code-hash")).toHaveCount(0);
  expect(
    browsingMutations,
    "Replay browsing must never start imports, new evaluations or diagnostic work",
  ).toEqual([]);
});

test("actual cross-sectional candidate filters preserve rank losers and the full observed portfolio event", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(420_000);
  const replay = await actualReplay(page, request, owned, "cross_sectional");
  // The two-listing generator proves a real full-pool UI path. It is not the
  // separate hand-worked B/C low-liquidity acceptance fixture.
  let event: DecisionIndex | undefined;
  let candidates: CandidatePage | undefined;
  for (const actual of replay.events.filter((item) =>
    item.stage_ids.includes("ranking"),
  )) {
    const page = await replay.candidateRows(actual, { stage_id: "ranking" });
    if (
      page.items?.some(
        (row) => row.observation.reason === "rank_not_selected",
      ) &&
      page.items.some((row) => row.observation.reason === "selected")
    ) {
      event = actual;
      candidates = page;
      break;
    }
  }
  expect(event).toBeDefined();
  expect(candidates).toBeDefined();
  expect(candidates!.items).toHaveLength(2);
  const loser = candidates!.items!.find(
    (row) => row.observation.reason === "rank_not_selected",
  );
  const winner = candidates!.items!.find(
    (row) => row.observation.reason === "selected",
  );
  expect(loser, "Do not discard real non-selected candidates").toBeDefined();
  expect(winner).toBeDefined();
  expect(loser!.observation.rank).toBe(2);
  expect(winner!.observation.rank).toBe(1);
  expect(loser!.observation.selected).toBe(false);
  expect(event!.scope).toBe("portfolio");
  expect(event!.instrument_id).toBeNull();
  await page.goto(replay.url());
  await chooseEvent(page, event!);
  await page
    .getByLabel("Observed stage", { exact: true })
    .selectOption("ranking");
  await expectCandidates(page, candidates!.items!);
  const score = loser!.observation.score!;
  const filters = {
    stage_id: "ranking",
    eligibility: loser!.observation.eligibility,
    reason: "rank_not_selected",
    rank_min: 2,
    rank_max: 2,
    score_min: score - 1,
    score_max: score + 1,
  };
  await applyFilters(page, {
    "Eligibility filter": filters.eligibility,
    "Reason filter": filters.reason,
    "Minimum rank": "2",
    "Maximum rank": "2",
    "Minimum score": String(filters.score_min),
    "Maximum score": String(filters.score_max),
  });
  await expectSelection(page, filters);
  const filtered = await replay.candidateRows(event!, filters);
  expect(filtered.items).toEqual([loser!]);
  await expectCandidates(page, filtered.items!);
  const pinned = page.url();
  await page.reload();
  await expect(page).toHaveURL(pinned);
  await expectCandidates(page, filtered.items!);
  await page
    .getByRole("button", { name: "Reset candidate filters", exact: true })
    .click();
  await expectCandidates(page, candidates!.items!);
  await page
    .getByRole("button", {
      name: `Inspect candidate: ${loser!.observation.instrument_id}`,
      exact: true,
    })
    .click();
  await expectSelection(page, {
    ...replay.pins,
    decision_id: event!.decision_id,
    instrument_id: loser!.observation.instrument_id,
  });
  // Selecting one observed instrument narrows detail, never the true basket.
  await expectCandidates(page, candidates!.items!);
  const scoped = await replay.detail(event!, {
    instrument_id: loser!.observation.instrument_id,
    stage_id: "ranking",
  });
  expect(scoped.decision?.scope).toBe("portfolio");
  expect(scoped.decision?.decision_id).toBe(event!.decision_id);
  expect(scoped.decision?.instrument_id).toBeNull();
  expect(await replay.candidateRows(event!, { stage_id: "ranking" })).toEqual(
    candidates,
  );
});

const captureSelection = {
  mode: "research",
  observation_window: { start: "2024-02-05", end: "2024-02-08" },
  stage_ids: ["features", "ranking"],
  instrument_ids: ["sh.600000"],
  max_decisions: 200,
  max_detail_rows: 1000,
  max_uncompressed_bytes: 1048576,
};
async function authorCapture(page: Page) {
  const controls = page.getByTestId("research-capture-controls");
  await expect(controls).toContainText("full pinned dataset");
  await expect(
    controls.getByLabel("Observation capture mode", { exact: true }),
  ).toHaveValue("");
  await expect(
    controls.getByLabel("Observation stage IDs", { exact: true }),
  ).toBeDisabled();
  await controls
    .getByLabel("Observation capture mode", { exact: true })
    .selectOption("research");
  await controls
    .getByRole("checkbox", {
      name: "Limit saved observations to a date window",
      exact: true,
    })
    .check();
  await controls
    .getByLabel("Observation start · inclusive", { exact: true })
    .fill(captureSelection.observation_window.start);
  await controls
    .getByLabel("Observation end · exclusive", { exact: true })
    .fill(captureSelection.observation_window.start);
  await expect(controls.getByRole("alert")).toContainText(
    "nonempty and half-open",
  );
  await controls
    .getByLabel("Observation end · exclusive", { exact: true })
    .fill(captureSelection.observation_window.end);
  await controls
    .getByLabel("Observation stage IDs", { exact: true })
    .fill("features, ranking");
  await controls
    .getByLabel("Observation instrument IDs", { exact: true })
    .fill(captureSelection.instrument_ids[0]);
  await controls
    .getByLabel("Maximum saved decisions", { exact: true })
    .fill("0");
  await expect(controls.getByRole("alert")).toBeVisible();
  for (const [label, value] of [
    ["Maximum saved decisions", captureSelection.max_decisions],
    ["Maximum saved detail rows", captureSelection.max_detail_rows],
    [
      "Maximum uncompressed capture bytes",
      captureSelection.max_uncompressed_bytes,
    ],
  ] as const)
    await controls.getByLabel(label, { exact: true }).fill(String(value));
  await expect(controls.getByRole("alert")).toHaveCount(0);
}
async function configuration(page: Page, title: string) {
  const details = page
    .locator("details")
    .filter({ has: page.locator("summary").filter({ hasText: title }) });
  if (
    !(await details.evaluate((element) => (element as HTMLDetailsElement).open))
  )
    await details.locator("summary").click();
  return JSON.parse((await details.locator("pre").textContent())!) as Record<
    string,
    unknown
  >;
}

test("native and portable capture authoring preserve immutable inputs, explicit scope and omitted default through reload", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(180_000);
  await signIn(page);
  const { strategy } = await createTemplate(
    page,
    request,
    owned.auth,
    "cross_sectional",
  );
  const { dataset, universe } = await snapshot(request, owned.auth, true);
  const submissions: string[] = [];
  page.on("request", (wire) => {
    if (
      wire.method() === "POST" &&
      ["/api/v1/research-runs", "/api/v1/portable-runs"].includes(
        new URL(wire.url()).pathname,
      )
    )
      submissions.push(wire.url());
  });
  await page.goto("/experiments/new");
  await page
    .getByLabel("Market & task", { exact: true })
    .selectOption("cn_cross");
  await page
    .getByLabel("Execution profile", { exact: true })
    .selectOption("corrected_explicit_config");
  await page
    .getByLabel("Published dataset version", { exact: true })
    .selectOption(dataset.id);
  await page
    .getByLabel("Exact universe version", { exact: true })
    .selectOption(universe.id);
  await page
    .getByLabel("Seed strategy version", { exact: true })
    .selectOption(strategy.id);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("checkbox", {
      name: "Reserve a final holdout interval (metadata only)",
      exact: true,
    })
    .check();
  for (const [name, label] of [
    ["train", "Training"],
    ["validation", "Validation"],
    ["holdout", "Holdout"],
  ] as const) {
    await page
      .getByLabel(`${label} start · inclusive`, { exact: true })
      .fill(windows[name].start);
    await page
      .getByLabel(`${label} end · exclusive`, { exact: true })
      .fill(windows[name].end);
  }
  await page
    .getByLabel("Warm-up sessions per listing", { exact: true })
    .fill("20");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "3. Engine & optimization",
      exact: true,
    }),
  ).toBeVisible();
  await authorCapture(page);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  const nativeTitle = "Review every immutable RunConfig field";
  const native = await configuration(page, nativeTitle);
  expect(native.research_capture).toEqual(captureSelection);
  expect(native.dataset).toEqual({
    id: dataset.id,
    sha256: dataset.content_sha256,
  });
  expect(native.universe).toEqual({
    id: universe.id,
    sha256: universe.content_sha256,
  });
  expect(native.windows).toEqual(windows);
  expect((native.warmup as { sessions: number }).sessions).toBe(20);
  await page.reload();
  expect(await configuration(page, nativeTitle)).toEqual(native);
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(
    page.getByLabel("Observation capture mode", { exact: true }),
  ).toHaveValue("research");
  await page
    .getByLabel("Observation capture mode", { exact: true })
    .selectOption("off");
  await expect(
    page.getByLabel("Observation instrument IDs", { exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  expect((await configuration(page, nativeTitle)).research_capture).toEqual({
    mode: "off",
  });
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page
    .getByLabel("Observation capture mode", { exact: true })
    .selectOption("");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  const defaultNative = await configuration(page, nativeTitle);
  expect(defaultNative).not.toHaveProperty("research_capture");
  const expectedNative = { ...native };
  delete expectedNative.research_capture;
  expect(defaultNative).toEqual(expectedNative);

  await page.goto("/portable/new");
  await page.getByLabel("Market", { exact: true }).selectOption("CN");
  await page
    .getByLabel("Portable template", { exact: true })
    .selectOption("portfolio");
  await page
    .getByLabel("Published dataset version", { exact: true })
    .selectOption(dataset.id);
  await page
    .getByLabel("Exact universe version", { exact: true })
    .selectOption(universe.id);
  const saved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/portable-strategies" &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", {
      name: "Create reference template version",
      exact: true,
    })
    .click();
  expect((await saved).status()).toBe(201);
  await page
    .getByRole("button", {
      name: "Load pinned portable configuration",
      exact: true,
    })
    .click();
  const portableTitle = "Review every immutable portable configuration field";
  const initialPortable = await configuration(page, portableTitle);
  expect(initialPortable).not.toHaveProperty("research_capture");
  await authorCapture(page);
  await expect(page.getByTestId("research-capture-controls")).toContainText(
    "100-listing ABI cap is unchanged",
  );
  const scopedPortable = await configuration(page, portableTitle);
  expect(scopedPortable).toEqual({
    ...initialPortable,
    research_capture: captureSelection,
  });
  await page.reload();
  expect(await configuration(page, portableTitle)).toEqual(scopedPortable);
  await expect(
    page.getByLabel("Observation capture mode", { exact: true }),
  ).toHaveValue("research");
  await page
    .getByLabel("Observation capture mode", { exact: true })
    .selectOption("off");
  expect((await configuration(page, portableTitle)).research_capture).toEqual({
    mode: "off",
  });
  await page
    .getByLabel("Observation capture mode", { exact: true })
    .selectOption("");
  expect(await configuration(page, portableTitle)).toEqual(initialPortable);
  expect(
    submissions,
    "Capture draft editing alone must never enqueue an evaluation",
  ).toEqual([]);
});
