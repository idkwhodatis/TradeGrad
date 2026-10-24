import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import {
  test as base,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type {
  CandidatePublic,
  ComparisonCreate,
  ComparisonDetail,
  ComparisonSectionPage,
  ComparisonPublic,
  ComparisonRecord,
  ComparisonResult,
  CoveragePublic,
  DatasetPublic,
  DecisionDetail,
  DecisionHeader,
  EvaluationPublic,
  ImportDetail,
  ImportPublic,
  NativeStrategyContract,
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
import comparisonFixture from "../fixtures/mechanism-comparison.json" with { type: "json" };

// I4 REAL-STACK ACCEPTANCE. These tests require a migrated PostgreSQL API,
// data/research/comparison workers and the real isolated native evaluator.
// They never intercept HTTP, fabricate a trace, execute Python on the browser
// host, call a model, or fetch a public provider. The uploaded OHLCV fixture is
// synthetic, explicitly labelled and checksum-verified. --list and typecheck
// collect/check this file; neither certifies that a browser test was executed.
// The original result-workbench and decision-replay suites stay unchanged.
type Owned = {
  auth: Token;
  runs: Set<string>;
  comparisons: Set<string>;
};
const test = base.extend<{ owned: Owned }>({
  owned: async ({ request }, use) => {
    await use({
      auth: await token(request),
      runs: new Set(),
      comparisons: new Set(),
    });
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
const windows = comparisonFixture.windows;
const sections = [
  "events",
  "candidates",
  "features",
  "predicates",
  "targets",
  "orders",
  "fills",
  "positions",
  "account",
  "turnover",
  "holding_duration",
] as const;
const query = (values: Record<string, string | number>) =>
  new URLSearchParams(
    Object.entries(values).map(([key, value]) => [key, String(value)]),
  ).toString();
const tracePins = (leaf: WorkbenchEvaluation) => ({
  execution_attempt_id: leaf.execution_attempt_id,
  result_manifest_sha256: leaf.result_manifest_sha256!,
  trace_manifest_sha256: leaf.trace_manifest_sha256!,
});
async function read<T>(request: APIRequestContext, auth: Token, path: string) {
  const response = await request.get(api(path), {
    headers: authorization(auth),
  });
  expect(response.status(), `Actual GET ${path}`).toBe(200);
  return (await response.json()) as T;
}
async function rows<T>(request: APIRequestContext, auth: Token, path: string) {
  const result: T[] = [];
  for (;;) {
    const page = await read<{ data: T[]; count: number }>(
      request,
      auth,
      `${path}?offset=${result.length}&limit=100`,
    );
    result.push(...page.data);
    if (result.length >= page.count) return result;
    expect(
      page.data.length,
      `Pagination must advance: ${path}`,
    ).toBeGreaterThan(0);
  }
}
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
async function frozenSnapshot(request: APIRequestContext, auth: Token) {
  const [csv, text] = await Promise.all([
    readFile(new URL("../fixtures/research-cn.csv", import.meta.url)),
    readFile(new URL("../fixtures/research-cn.json", import.meta.url), "utf8"),
  ]);
  const fixture = JSON.parse(text) as {
    synthetic: boolean;
    sha256: string;
    rows: number;
  };
  expect(fixture.synthetic).toBe(true);
  expect(sha256(csv)).toBe(fixture.sha256);
  expect(csv.toString("utf8").trim().split(/\r?\n/).length - 1).toBe(
    fixture.rows,
  );
  const uploaded = await request.post(api("/data/uploads"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    multipart: {
      mapping: JSON.stringify({
        name: `I4 frozen synthetic snapshot ${crypto.randomUUID()}`,
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
  expect(uploaded.status()).toBe(202);
  const imported = (await uploaded.json()) as ImportPublic;
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
            `Actual data worker ended ${job.status}: ${job.error}`,
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
  expect(coverage.universe.members as unknown[]).toHaveLength(1);
  const universeResponse = await request.post(api("/data/universes"), {
    headers: authorization(auth),
    data: {
      name: `I4 fixed synthetic universe ${crypto.randomUUID()}`,
      state: "user_fixed",
      as_of: new Date().toISOString(),
      members: coverage.universe.members,
      evidence: [],
    },
  });
  expect(universeResponse.status()).toBe(201);
  return {
    dataset,
    universe: (await universeResponse.json()) as UniversePublic,
  };
}
async function saveVersion(
  request: APIRequestContext,
  auth: Token,
  template: ResearchTemplate,
  code: string,
  parent: StrategyDetail | null,
  manifest = template.manifest,
) {
  const response = await request.post(api("/strategy-versions"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    data: {
      name: `I4 ${parent ? "derived" : "baseline"} ${crypto.randomUUID()}`,
      task: `cn_${template.task_suffix}`,
      code,
      manifest,
      parent_id: parent?.id ?? null,
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  const strategy = (await response.json()) as StrategyDetail;
  expect(strategy.code).toBe(code);
  expect(strategy.content_sha256).toBe(sha256(code));
  expect(strategy.parent_id).toBe(parent?.id ?? null);
  expect(strategy.manifest).toEqual(manifest);
  return strategy;
}
async function runActual(
  request: APIRequestContext,
  owned: Owned,
  strategy: StrategyDetail,
  snapshot: Awaited<ReturnType<typeof frozenSnapshot>>,
  capabilities: ResearchCapabilities,
  commissionRate?: number,
) {
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
    dataset: {
      id: snapshot.dataset.id,
      sha256: snapshot.dataset.content_sha256,
    },
    universe: {
      id: snapshot.universe.id,
      sha256: snapshot.universe.content_sha256,
    },
    seed_strategy: { id: strategy.id, sha256: strategy.content_sha256 },
    strategy_contract: strategy.manifest,
    windows,
    warmup: { ...profile!.warmup, sessions: comparisonFixture.warmup_sessions },
    market_profile: {
      ...structuredClone(profile!.market_profile),
      ...(commissionRate === undefined
        ? {}
        : { commission_rate: commissionRate }),
    },
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
  const run = (await response.json()) as ResearchRunPublic;
  owned.runs.add(run.id);
  await expect
    .poll(
      async () => {
        const detail = await read<ResearchRunDetail>(
          request,
          owned.auth,
          `/research-runs/${run.id}`,
        );
        if (terminal.has(detail.status) && detail.status !== "succeeded")
          throw new Error(
            `Actual isolated native run ended ${detail.status}: ${detail.error}; never replace with fabricated success`,
          );
        return detail.status;
      },
      { timeout: 210_000, intervals: [250, 500, 1000] },
    )
    .toBe("succeeded");
  expect(
    await rows(request, owned.auth, `/research-runs/${run.id}/model-calls`),
  ).toEqual([]);
  const candidates = await rows<CandidatePublic>(
    request,
    owned.auth,
    `/research-runs/${run.id}/candidates`,
  );
  expect(candidates).toHaveLength(1);
  expect(candidates[0].kind).toBe("seed");
  expect(candidates[0].code_sha256).toBe(strategy.content_sha256);
  const leaves = await rows<WorkbenchEvaluation>(
    request,
    owned.auth,
    `/strategy-versions/${strategy.id}/evaluations`,
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
    `/strategy-versions/${strategy.id}/spec`,
  );
  expect(spec.code_sha256).toBe(strategy.content_sha256);
  const trace = await read<TraceManifestPublic>(
    request,
    owned.auth,
    `/evaluations/${leaf.evaluation_id}/trace-manifest?${query(tracePins(leaf))}`,
  );
  expect(trace.context.status).toBe("available");
  expect(trace.context.binding?.code_sha256).toBe(strategy.content_sha256);
  expect(trace.manifest?.capture.mode).toBe("research");
  return { strategy, run, candidate: candidates[0], leaves, leaf, spec, body };
}
type ActualSide = Awaited<ReturnType<typeof runActual>>;
async function actualPair(
  request: APIRequestContext,
  owned: Owned,
  variant: "comment_only" | "changed_allocation_and_fees",
) {
  const templates = await rows<ResearchTemplate>(
    request,
    owned.auth,
    "/research-templates",
  );
  const template = templates.find(
    (item) => item.template_id === "daily_timing",
  );
  expect(template, "Use the backend-maintained native template").toBeDefined();
  expect(sha256(template!.code)).toBe(template!.code_sha256);
  const capabilities = await read<ResearchCapabilities>(
    request,
    owned.auth,
    "/research-capabilities",
  );
  expect(capabilities.live_model_calls).toBe(false);
  expect(capabilities.model_providers).toEqual(["fixture"]);
  expect(capabilities.defaults?.optimizer).toBeDefined();
  const snapshot = await frozenSnapshot(request, owned.auth);
  const baseline = await saveVersion(
    request,
    owned.auth,
    template!,
    template!.code,
    null,
  );
  const manifest: NativeStrategyContract = structuredClone(template!.manifest);
  if (variant === "changed_allocation_and_fees") {
    const maximum = manifest.parameters?.find(
      (item) => item.name === "max_weight",
    );
    expect(maximum).toBeDefined();
    // This exact frozen tape must produce closed trades on both splits under
    // the unchanged optimization reward gate. At 0.15, train has only an open
    // position and is correctly rejected; the native fixture regression pins
    // the admissible allocation change instead of weakening that gate.
    maximum!.value = comparisonFixture.allocation_and_fees.max_weight;
  }
  const source =
    variant === "comment_only"
      ? `${template!.code}\n# I4 comparison: documentation only, no executable change.\n`
      : template!.code;
  const derived = await saveVersion(
    request,
    owned.auth,
    template!,
    source,
    baseline,
    manifest,
  );
  // Both complete backtests share these same immutable dataset/universe IDs,
  // windows, initialization and engine. Only the declared variant differs.
  const left = await runActual(
    request,
    owned,
    baseline,
    snapshot,
    capabilities,
  );
  const right = await runActual(
    request,
    owned,
    derived,
    snapshot,
    capabilities,
    variant === "changed_allocation_and_fees"
      ? comparisonFixture.allocation_and_fees.commission_rate
      : undefined,
  );
  expect(left.leaf.evaluation_id).not.toBe(right.leaf.evaluation_id);
  expect(left.leaf.execution_attempt_id).not.toBe(
    right.leaf.execution_attempt_id,
  );
  expect(left.body.dataset).toEqual(right.body.dataset);
  expect(left.body.universe).toEqual(right.body.universe);
  expect(left.body.windows).toEqual(right.body.windows);
  expect(left.body.warmup).toEqual(right.body.warmup);
  return { left, right, snapshot };
}
function sourcePins(side: ActualSide) {
  return {
    source_kind: "strategy_version" as const,
    source_id: side.strategy.id,
    code_sha256: side.strategy.content_sha256,
    spec_revision: side.spec.revision,
    spec_sha256: side.spec.spec_sha256,
    evaluation_id: side.leaf.evaluation_id,
    ...tracePins(side.leaf),
  };
}
function sideSelection(side: ActualSide, name: "left" | "right") {
  return Object.fromEntries(
    Object.entries({
      ...sourcePins(side),
      strategy_version_id: side.strategy.id,
      trace_status: "registered",
    }).map(([key, value]) => [`${name}_${key}`, value]),
  );
}
function workbenchUrl(side: ActualSide) {
  return `/strategies/${side.strategy.id}/research?${query({
    tab: "comparison",
    evaluation_id: side.leaf.evaluation_id,
    code_sha256: side.strategy.content_sha256,
    spec_revision: side.spec.revision,
    spec_sha256: side.spec.spec_sha256,
    ...tracePins(side.leaf),
    trace_status: "available",
  })}`;
}
async function sectionPage(
  request: APIRequestContext,
  auth: Token,
  result: ComparisonResult,
  section: (typeof sections)[number],
  values: Record<string, string | number> = {},
) {
  const page = await read<ComparisonSectionPage>(
    request,
    auth,
    `/comparisons/${result.comparison_id}/sections/${section}?${query({
      manifest_sha256: result.manifest_sha256,
      offset: 0,
      limit: 25,
      ...values,
    })}`,
  );
  expect(page.comparison_id).toBe(result.comparison_id);
  expect(page.input_sha256).toBe(result.input_sha256);
  expect(page.manifest_sha256).toBe(result.manifest_sha256);
  expect(page.section).toBe(section);
  expect(page.availability).toEqual(result.manifest.sections[section]);
  return page;
}
async function allSectionRows(
  request: APIRequestContext,
  auth: Token,
  result: ComparisonResult,
  section: (typeof sections)[number],
) {
  const items: ComparisonRecord[] = [];
  for (;;) {
    const page = await sectionPage(request, auth, result, section, {
      offset: items.length,
      limit: 100,
    });
    items.push(...page.items);
    if (items.length >= page.total) return items;
    expect(
      page.items.length,
      "Immutable comparison pagination advances",
    ).toBeGreaterThan(0);
  }
}
async function createInBrowser(
  page: Page,
  request: APIRequestContext,
  owned: Owned,
  pair: Awaited<ReturnType<typeof actualPair>>,
) {
  await expectSelection(page, {
    ...sideSelection(pair.left, "left"),
    ...sideSelection(pair.right, "right"),
  });
  const responsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/comparisons" &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Create immutable comparison", exact: true })
    .click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(202);
  const body = response.request().postDataJSON() as ComparisonCreate;
  expect(body.left).toEqual(sourcePins(pair.left));
  expect(body.right).toEqual(sourcePins(pair.right));
  const key = response.request().headers()["idempotency-key"];
  expect(key).toBeTruthy();
  const submitted = (await response.json()) as ComparisonPublic;
  owned.comparisons.add(submitted.id);
  expect(submitted.execution_mode).toBe("comparison");
  expect(submitted.request).toEqual(body);
  await expectSelection(page, { comparison_id: submitted.id });
  await expect
    .poll(
      async () => {
        const status = await read<ComparisonDetail>(
          request,
          owned.auth,
          `/comparisons/${submitted.id}`,
        );
        if (terminal.has(status.status) && status.status !== "succeeded")
          throw new Error(
            `Actual comparison ended ${status.status}: ${status.error_code}`,
          );
        return status.status;
      },
      { timeout: 330_000, intervals: [250, 500, 1000] },
    )
    .toBe("succeeded");
  const completed = await read<ComparisonDetail>(
    request,
    owned.auth,
    `/comparisons/${submitted.id}`,
  );
  expect(completed.artifact_ready).toBe(true);
  expect(completed.manifest_sha256).toMatch(/^[a-f0-9]{64}$/);
  const result = await read<ComparisonResult>(
    request,
    owned.auth,
    `/comparisons/${submitted.id}/result?${query({ manifest_sha256: completed.manifest_sha256! })}`,
  );
  expect(result.comparison_id).toBe(submitted.id);
  expect(result.input_sha256).toBe(completed.input_sha256);
  expect(result.manifest_sha256).toBe(completed.manifest_sha256);
  expect(result.manifest.publication.comparison_id).toBe(submitted.id);
  expect(result.manifest.publication.run_id).toBe(submitted.run_id);
  for (const [name, side] of [
    ["left", pair.left],
    ["right", pair.right],
  ] as const) {
    expect(result.manifest[name]).toMatchObject({
      source_kind: "strategy_version",
      source_id: side.strategy.id,
      spec_revision: side.spec.revision,
      spec_sha256: side.spec.spec_sha256,
      trace_manifest_sha256: side.leaf.trace_manifest_sha256,
      trace_state: "registered",
      binding: {
        evaluation_id: side.leaf.evaluation_id,
        execution_attempt_id: side.leaf.execution_attempt_id,
        result_manifest_sha256: side.leaf.result_manifest_sha256,
        code_sha256: side.strategy.content_sha256,
      },
    });
    expect(result[`${name}_source`].code).toBe(side.strategy.code);
    expect(result[`${name}_source`].code_sha256).toBe(
      side.strategy.content_sha256,
    );
    expect(result[`${name}_source`].spec_sha256).toBe(side.spec.spec_sha256);
  }
  expect(result.manifest.namespace_status).toBe("common");
  expect(result.manifest.diagnostic_parity_certified).toBe(false);
  expect(result.manifest.local_counterfactual).toBe(false);
  expect(result.manifest.causal_claim).toBe(false);
  expect(Object.keys(result.manifest.sections).sort()).toEqual(
    [...sections].sort(),
  );
  await expectSelection(page, {
    comparison_id: submitted.id,
    comparison_manifest_sha256: result.manifest_sha256,
    ...sideSelection(pair.left, "left"),
    ...sideSelection(pair.right, "right"),
  });
  await expect(page.getByTestId("comparison-outcome")).toBeVisible();
  return { result, completed, body, key };
}
async function loginAtExactUrl(page: Page, exactUrl: string) {
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
}
function wrongHash(original: string) {
  return original === "0".repeat(64) ? "1".repeat(64) : "0".repeat(64);
}
async function assertDurableCache(
  request: APIRequestContext,
  owned: Owned,
  comparison: Awaited<ReturnType<typeof createInBrowser>>,
) {
  const { body, key, completed, result } = comparison;
  // Repetition and a separate alias exercise real server persistence; no
  // simulated lost response, localStorage replacement or intercepted POST.
  const alias = crypto.randomUUID();
  for (const repeatedKey of [key, alias, alias]) {
    const repeated = await request.post(api("/comparisons"), {
      headers: { ...authorization(owned.auth), "Idempotency-Key": repeatedKey },
      data: body,
    });
    expect(repeated.status()).toBe(202);
    const cached = (await repeated.json()) as ComparisonPublic;
    expect(cached.id).toBe(completed.id);
    expect(cached.run_id).toBe(completed.run_id);
    expect(cached.generation).toBe(completed.generation);
    expect(cached.manifest_sha256).toBe(result.manifest_sha256);
    expect(cached.manifest_artifact_id).toBe(result.manifest_artifact_id);
    expect(cached.status).toBe("succeeded");
  }
  const status = await read<ComparisonDetail>(
    request,
    owned.auth,
    `/comparisons/${completed.id}`,
  );
  expect(status.attempts.map((attempt) => attempt.id)).toEqual(
    completed.attempts.map((attempt) => attempt.id),
  );
  const conflict = await request.post(api("/comparisons"), {
    headers: { ...authorization(owned.auth), "Idempotency-Key": key },
    data: { ...body, left: body.right, right: body.left },
  });
  expect(conflict.status()).toBe(409);
}

test.use({ timezoneId: "America/Los_Angeles" });
test.beforeAll(async ({ request }) => {
  account();
  account(true);
  expect(
    (await request.get(api("/health/ready"))).ok(),
    "Use a real migrated PostgreSQL and worker stack, not API doubles",
  ).toBe(true);
});
test.afterEach(async ({ request, owned }) => {
  for (const id of owned.comparisons) {
    const job = await read<ComparisonDetail>(
      request,
      owned.auth,
      `/comparisons/${id}`,
    );
    if (!terminal.has(job.status)) {
      const cancelled = await request.post(api(`/comparisons/${id}/cancel`), {
        headers: authorization(owned.auth),
      });
      expect(cancelled.status()).toBe(202);
    }
    await expect
      .poll(
        async () => {
          const current = await read<ComparisonDetail>(
            request,
            owned.auth,
            `/comparisons/${id}`,
          );
          return (
            terminal.has(current.status) &&
            current.attempts.every(
              (attempt) =>
                attempt.sandbox_stopped && attempt.finished_at !== null,
            )
          );
        },
        { timeout: 45_000, intervals: [250, 500, 1000] },
      )
      .toBe(true);
  }
  for (const id of owned.runs) {
    const run = await read<ResearchRunDetail>(
      request,
      owned.auth,
      `/research-runs/${id}`,
    );
    if (!terminal.has(run.status) || run.status === "paused") {
      const cancelled = await request.post(api(`/research-runs/${id}/cancel`), {
        headers: authorization(owned.auth),
      });
      expect(cancelled.status()).toBe(202);
    }
    await expect
      .poll(
        async () => {
          const current = await read<ResearchRunDetail>(
            request,
            owned.auth,
            `/research-runs/${id}`,
          );
          const leaves = await rows<EvaluationPublic>(
            request,
            owned.auth,
            `/research-runs/${id}/evaluations`,
          );
          return (
            terminal.has(current.status) &&
            current.attempts.every((attempt) => attempt.sandbox_stopped) &&
            leaves.every(
              (leaf) => leaf.sandbox_stopped && leaf.finished_at !== null,
            )
          );
        },
        { timeout: 45_000, intervals: [250, 500, 1000] },
      )
      .toBe(true);
  }
});

function header(record: ComparisonRecord, side: "left" | "right") {
  const value = record[side] as DecisionHeader | null;
  expect(record.section).toBe("events");
  expect(value).not.toBeNull();
  expect(typeof value!.decision_id).toBe("string");
  return value!;
}
function assertReplayUrl(
  href: string | null,
  baseUrl: string,
  side: ActualSide,
  event: DecisionHeader,
  extra: Record<string, string | number> = {},
) {
  expect(href, "A verified explicit replay link must be present").toBeTruthy();
  const url = new URL(href!, baseUrl);
  expect(url.pathname).toBe(`/strategies/${side.strategy.id}/research`);
  const expected = {
    tab: "replay",
    evaluation_id: side.leaf.evaluation_id,
    ...tracePins(side.leaf),
    code_sha256: side.strategy.content_sha256,
    spec_revision: side.spec.revision,
    spec_sha256: side.spec.spec_sha256,
    decision_id: event.decision_id,
    session_date: event.session_date,
    ...extra,
  };
  for (const [name, value] of Object.entries(expected))
    expect(url.searchParams.get(name), `Exact replay ${name}`).toBe(
      String(value),
    );
  return url;
}
async function selectEventPair(
  page: Page,
  request: APIRequestContext,
  owned: Owned,
  result: ComparisonResult,
  pair: Awaited<ReturnType<typeof actualPair>>,
  events: ComparisonRecord[],
  event: ComparisonRecord,
) {
  await expectSelection(page, {
    comparison_id: result.comparison_id,
    comparison_manifest_sha256: result.manifest_sha256,
  });
  const position = events.findIndex((item) => item.row_id === event.row_id);
  expect(position).toBeGreaterThanOrEqual(0);
  // This deep link retains the real page ordinal rather than inventing a
  // date/instrument join or selecting the first row with matching display text.
  const selection = new URL(page.url());
  selection.searchParams.set("comparison_section", "events");
  selection.searchParams.set(
    "comparison_event_offset",
    String(Math.floor(position / 25) * 25),
  );
  selection.searchParams.set("comparison_offset", "0");
  selection.searchParams.delete("event_pair_id");
  await page.goto(selection.href);
  await page
    .getByRole("button", {
      name: `Select event pair ${event.event_pair_id}`,
      exact: true,
    })
    .click();
  await expectSelection(page, { event_pair_id: event.event_pair_id! });
  const lhs = header(event, "left");
  const rhs = header(event, "right");
  expect(lhs.decision_id).not.toBe(rhs.decision_id);
  expect(event.match_basis).toBe("source_event");
  for (const field of [
    "data_event_id",
    "callback_phase",
    "scope",
    "event_seq",
    "instrument_id",
  ] as const)
    expect(lhs[field], `Pair by ${field}, not the local decision UUID`).toEqual(
      rhs[field],
    );
  for (const [name, side, selected] of [
    ["left", pair.left, lhs],
    ["right", pair.right, rhs],
  ] as const) {
    const detail = await read<DecisionDetail>(
      request,
      owned.auth,
      `/evaluations/${side.leaf.evaluation_id}/decisions/${selected.decision_id}?${query(tracePins(side.leaf))}`,
    );
    expect(detail.decision?.pre_state).toEqual(selected.pre_state);
    expect(detail.decision?.pre_state_missing_reason).toEqual(
      selected.pre_state_missing_reason,
    );
    const preState = page.getByTestId(`comparison-prestate-${name}`);
    await expect(preState).toBeVisible();
    if (selected.pre_state) {
      await expect(preState).toContainText(selected.pre_state.source);
      await expect(preState).toContainText(String(selected.pre_state.cash));
    } else {
      expect(selected.pre_state_missing_reason).toBeTruthy();
      await expect(preState).toContainText(selected.pre_state_missing_reason!);
    }
    const label = name === "left" ? "Left" : "Right";
    assertReplayUrl(
      await page
        .getByRole("link", {
          name: `Open ${label} immutable replay`,
          exact: true,
        })
        .getAttribute("href"),
      page.url(),
      side,
      selected,
    );
  }
  return { lhs, rhs };
}
async function assertObservationRecordLinks(
  page: Page,
  request: APIRequestContext,
  owned: Owned,
  result: ComparisonResult,
  pair: Awaited<ReturnType<typeof actualPair>>,
  event: ComparisonRecord,
) {
  for (const section of ["candidates", "features", "predicates"] as const) {
    await page
      .getByLabel("Behavior section", { exact: true })
      .selectOption(section);
    await expectSelection(page, {
      comparison_section: section,
      event_pair_id: event.event_pair_id!,
      comparison_offset: 0,
    });
    const saved = await sectionPage(request, owned.auth, result, section, {
      event_pair_id: event.event_pair_id!,
    });
    expect(
      saved.items.length,
      `Real template saved ${section}`,
    ).toBeGreaterThan(0);
    const record = saved.items.find((item) => item.left && item.right)!;
    expect(record).toBeDefined();
    const visible = page.getByTestId(`comparison-record-${record.row_id}`);
    await expect(visible).toBeVisible();
    await expect(visible).toContainText(record.status);
    for (const [name, side] of [
      ["left", pair.left],
      ["right", pair.right],
    ] as const) {
      const group = record[name] as {
        items: { ordinal: number; record: Record<string, unknown> }[];
        count: number;
      };
      expect(group.items).toHaveLength(group.count);
      for (const item of group.items) {
        expect(Number.isSafeInteger(item.ordinal)).toBe(true);
        const id =
          section === "candidates"
            ? item.record.instrument_id
            : section === "features"
              ? item.record.feature_id
              : item.record.condition_id;
        const label = name === "left" ? "Left" : "Right";
        const link = visible.getByRole("link", {
          name: `Open ${label} ${section} record ${item.ordinal}`,
          exact: true,
        });
        const exact = assertReplayUrl(
          await link.getAttribute("href"),
          page.url(),
          side,
          header(event, name),
          {
            stage_id: record.stage_id!,
            instrument_id: record.instrument_id!,
            record_kind:
              section === "candidates"
                ? "candidate"
                : section === "features"
                  ? "feature"
                  : "predicate",
            record_id: String(id),
            record_ordinal: item.ordinal,
          },
        );
        // Open one actual ordinal-bound predicate on each side. Verifying only
        // the link string would not prove replay admission or saved-row selection.
        if (section === "predicates" && item === group.items[0]) {
          const comparisonUrl = page.url();
          await link.click();
          await expect(page).toHaveURL(
            (url) => url.pathname === exact.pathname,
          );
          await expectSelection(page, Object.fromEntries(exact.searchParams));
          await expect(
            page.getByTestId(`replay-predicate-${String(id)}`),
          ).toBeVisible();
          await expectSelection(page, {
            record_ordinal: item.ordinal,
            record_id: String(id),
          });
          await page.goBack();
          await expect(page).toHaveURL(comparisonUrl);
          await expect(visible).toBeVisible();
        }
      }
    }
  }
}

async function assertExplicitOrderAssociation(
  page: Page,
  request: APIRequestContext,
  owned: Owned,
  result: ComparisonResult,
  pair: Awaited<ReturnType<typeof actualPair>>,
) {
  const orders = await allSectionRows(request, owned.auth, result, "orders");
  expect(
    orders.length,
    "The actual timing fixture submits native orders",
  ).toBeGreaterThan(0);
  const position = orders.findIndex(
    (row) => (row.associations ?? []).length > 0,
  );
  // This maintained timing template emits explicit native order links. Losing
  // every association is a regression, not an optional browser-test branch.
  // Never replace missing graph evidence with a symbol/time join.
  expect(
    position,
    "The maintained timing fixture must retain at least one explicit order association for mandatory replay drilldown",
  ).toBeGreaterThanOrEqual(0);
  const row = orders[position];
  const association = row.associations![0];
  const side = pair[association.side];
  const original = page.url();
  const ordersUrl = new URL(original);
  ordersUrl.searchParams.set("comparison_section", "orders");
  ordersUrl.searchParams.set(
    "comparison_offset",
    String(Math.floor(position / 25) * 25),
  );
  ordersUrl.searchParams.delete("event_pair_id");
  await page.goto(ordersUrl.href);
  const record = page.getByTestId(`comparison-record-${row.row_id}`);
  await expect(record).toBeVisible();
  const name = association.side === "left" ? "Left" : "Right";
  // The first association for this side has its own explicit link and ordinal.
  const link = record.getByRole("link", {
    name: `Open ${name} orders association 1`,
    exact: true,
  });
  const href = await link.getAttribute("href");
  expect(href).toBeTruthy();
  const replay = new URL(href!, page.url());
  expect(replay.pathname).toBe(`/strategies/${side.strategy.id}/research`);
  const expected = {
    tab: "replay",
    code_sha256: side.strategy.content_sha256,
    spec_revision: side.spec.revision,
    spec_sha256: side.spec.spec_sha256,
    evaluation_id: side.leaf.evaluation_id,
    ...tracePins(side.leaf),
    decision_id: association.decision_id,
    stage_id: association.stage_id,
    instrument_id: association.instrument_id,
    record_kind: "order",
    record_id: association.logical_order_id,
    record_ordinal: association.link_ordinal,
  };
  for (const [key, value] of Object.entries(expected))
    expect(replay.searchParams.get(key), `Saved order association ${key}`).toBe(
      String(value),
    );
  const actual = await read<DecisionDetail>(
    request,
    owned.auth,
    `/evaluations/${side.leaf.evaluation_id}/decisions/${association.decision_id}?${query(tracePins(side.leaf))}`,
  );
  const saved = actual.stages?.find(
    (stage) => stage.stage_id === association.stage_id,
  )?.order_links[association.link_ordinal];
  expect(saved?.logical_order_id).toBe(association.logical_order_id);
  expect(saved?.instrument_id).toBe(association.instrument_id);
  expect(saved?.child_order_ids).toEqual(association.child_order_ids);
  expect(saved?.fill_ids).toEqual(association.fill_ids);
  const exactOrdersUrl = page.url();
  await link.click();
  await expect(page).toHaveURL((url) => url.pathname === replay.pathname);
  await expectSelection(page, expected);
  await expect(
    page.getByTestId(`replay-execution-${association.logical_order_id}`),
  ).toBeVisible();
  await expect(page.getByTestId("replay-selected-record-source")).toContainText(
    `saved ordinal ${association.link_ordinal}`,
  );
  await expectSelection(page, { session_date: actual.decision!.session_date });
  await page.goBack();
  await expect(page).toHaveURL(exactOrdersUrl);
  await page.goto(original);
}

test("comment-only native versions preserve scoped unchanged observations, explicit event identity, immutable history and durable owner-scoped cache", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(900_000);
  await signIn(page);
  const pair = await actualPair(request, owned, "comment_only");
  const browsingExecutions: string[] = [];
  page.on("request", (wire) => {
    if (
      wire.method() !== "GET" &&
      /\/api\/v1\/(data\/(imports|uploads)|research-runs|portable-runs)/.test(
        new URL(wire.url()).pathname,
      )
    )
      browsingExecutions.push(
        `${wire.method()} ${new URL(wire.url()).pathname}`,
      );
  });
  await page.goto(workbenchUrl(pair.left));
  await expect(
    page.getByRole("link", { name: "Open original run status", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Use current selection as Left", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Create immutable comparison",
      exact: true,
    }),
  ).toBeDisabled();
  // Changing the input kind must not remount the picker back to the saved
  // strategy default. This uses the actual seed Candidate from the same run,
  // not a fabricated UUID or a same-code proxy for source association.
  await expectSelection(page, sideSelection(pair.left, "left"));
  await page
    .getByLabel("Left source type", { exact: true })
    .selectOption("candidate");
  await expect(
    page.getByLabel("Left source type", { exact: true }),
  ).toHaveValue("candidate");
  const candidateInput = page.getByLabel("Left candidate ID", { exact: true });
  await expect(candidateInput).toBeVisible();
  await expect(candidateInput).toHaveValue("");
  expect(new URL(page.url()).searchParams.get("left_source_id")).toBeNull();
  expect(new URL(page.url()).searchParams.get("left_evaluation_id")).toBeNull();
  await candidateInput.fill(pair.left.candidate.id);
  await page
    .getByRole("button", { name: "Load Left source", exact: true })
    .click();
  await expect(
    page.getByLabel("Left actual leaf Evaluation", { exact: true }),
  ).toBeVisible();
  const candidateSpec = await read<WorkbenchSpec>(
    request,
    owned.auth,
    `/candidates/${pair.left.candidate.id}/spec`,
  );
  await page
    .getByLabel("Left actual leaf Evaluation", { exact: true })
    .selectOption(pair.left.leaf.evaluation_id);
  const candidatePins = {
    ...sideSelection(pair.left, "left"),
    left_source_kind: "candidate",
    left_source_id: pair.left.candidate.id,
    left_spec_revision: candidateSpec.revision,
    left_spec_sha256: candidateSpec.spec_sha256,
  };
  await expectSelection(page, candidatePins);
  await expect(
    page.getByLabel("Left source type", { exact: true }),
  ).toHaveValue("candidate");
  await expect(candidateInput).toHaveValue(pair.left.candidate.id);
  const candidateUrl = page.url();
  await page
    .getByRole("button", { name: "Use current selection as Left", exact: true })
    .click();
  await expectSelection(page, sideSelection(pair.left, "left"));
  await expect(
    page.getByLabel("Left source type", { exact: true }),
  ).toHaveValue("strategy_version");
  await expect(
    page.getByLabel("Left saved strategy", { exact: true }),
  ).toHaveValue(pair.left.strategy.id);
  const savedSourceUrl = page.url();
  await page.goBack();
  await expect(page).toHaveURL(candidateUrl);
  await expectSelection(page, candidatePins);
  await expect(
    page.getByLabel("Left source type", { exact: true }),
  ).toHaveValue("candidate");
  await expect(candidateInput).toHaveValue(pair.left.candidate.id);
  await expect(
    page.getByLabel("Left actual leaf Evaluation", { exact: true }),
  ).toHaveValue(pair.left.leaf.evaluation_id);
  await page.goForward();
  await expect(page).toHaveURL(savedSourceUrl);
  await expectSelection(page, sideSelection(pair.left, "left"));
  await expect(
    page.getByLabel("Left source type", { exact: true }),
  ).toHaveValue("strategy_version");
  await expect(candidateInput).toHaveCount(0);
  await expect(
    page.getByLabel("Left saved strategy", { exact: true }),
  ).toHaveValue(pair.left.strategy.id);
  await expect(
    page.getByLabel("Left actual leaf Evaluation", { exact: true }),
  ).toHaveValue(pair.left.leaf.evaluation_id);

  await page
    .getByLabel("Right source type", { exact: true })
    .selectOption("strategy_version");
  await page
    .getByLabel("Right saved strategy", { exact: true })
    .selectOption(pair.right.strategy.id);
  await page
    .getByLabel("Right actual leaf Evaluation", { exact: true })
    .selectOption(pair.right.leaf.evaluation_id);
  const comparison = await createInBrowser(page, request, owned, pair);
  const { result } = comparison;
  expect(result.manifest.code_changed).toBe(true);
  // Native execution-only snapshots deliberately remain unpaired. Global
  // insufficient_coverage is honest here; the specific observed equal sections
  // must still be explicit rather than erasing the unchanged-decision finding.
  expect(["unchanged_observed", "insufficient_coverage"]).toContain(
    result.manifest.observed_behavior,
  );
  expect(result.manifest.observed_equal_sections).toEqual(
    expect.arrayContaining(["candidates", "features", "predicates", "targets"]),
  );
  expect(
    result.manifest.configuration_differences.filter(
      (item) => item.status === "different",
    ),
  ).toEqual([]);
  expect(
    result.manifest.mechanism_changes.some(
      (item) => item.mapping === "inferred" || item.mapping === "unmapped",
    ),
  ).toBe(true);
  await expect(page.getByTestId("comparison-scoped-unchanged")).toContainText(
    /code changed/i,
  );
  await expect(page.getByTestId("comparison-scoped-unchanged")).toContainText(
    /unchanged/i,
  );
  for (const section of ["candidates", "targets"])
    await expect(page.getByTestId("comparison-scoped-unchanged")).toContainText(
      section,
    );
  await expect(page.getByTestId("comparison-coverage")).toContainText(
    /coverage|recorded|observed/i,
  );
  await expect(page.getByTestId("comparison-mechanisms")).toContainText(
    /inferred|unmapped/i,
  );
  for (const warning of result.manifest.warnings)
    await expect(page.getByText(warning, { exact: true })).toBeVisible();
  const diff = page.getByRole("region", {
    name: "Immutable left and right source diff",
    exact: true,
  });
  await expect(diff.locator(".monaco-diff-editor")).toBeVisible();
  expect(
    await page.getByTestId("comparison-basis").evaluate((element) => {
      const mechanism = document.querySelector(
        '[data-testid="comparison-mechanisms"]',
      );
      return (
        mechanism !== null &&
        Boolean(
          element.compareDocumentPosition(mechanism) &
          Node.DOCUMENT_POSITION_FOLLOWING,
        )
      );
    }),
  ).toBe(true);

  const events = await allSectionRows(request, owned.auth, result, "events");
  const matched = events.filter(
    (item) => item.left && item.right && item.match_basis === "source_event",
  );
  expect(matched.length).toBeGreaterThan(2);
  expect(matched.every((item) => item.status === "equal")).toBe(true);
  const selected = matched.find(
    (item) => header(item, "left").pre_state !== null,
  )!;
  expect(selected).toBeDefined();
  await selectEventPair(page, request, owned, result, pair, events, selected);
  await assertObservationRecordLinks(
    page,
    request,
    owned,
    result,
    pair,
    selected,
  );
  for (const section of sections) {
    await page
      .getByLabel("Behavior section", { exact: true })
      .selectOption(section);
    await expectSelection(page, {
      comparison_section: section,
      event_pair_id: selected.event_pair_id!,
      comparison_offset: 0,
    });
    const saved = await sectionPage(request, owned.auth, result, section, {
      event_pair_id: selected.event_pair_id!,
    });
    if (saved.items.length > 0)
      await expect(
        page.getByTestId(`comparison-record-${saved.items[0].row_id}`),
      ).toBeVisible();
    await expect(page.getByTestId("comparison-coverage")).toContainText(
      section,
    );
  }
  await assertExplicitOrderAssociation(page, request, owned, result, pair);

  // Same-day native execution snapshots are not paired by date or stock with
  // strategy callbacks. Keep authentic one-sided records and missing linkage.
  const unpaired = events.filter((item) => item.match_basis === "none");
  expect(unpaired.length).toBeGreaterThan(0);
  for (const item of unpaired) {
    expect(item.left === null || item.right === null).toBe(true);
    expect([item.left_state, item.right_state]).toContain("unpaired");
    expect(item.missing_reason).toBeTruthy();
  }
  expect(
    events.some((item) =>
      events.some(
        (other) =>
          item.session_date === other.session_date &&
          item.event_pair_id !== other.event_pair_id,
      ),
    ),
  ).toBe(true);

  await page
    .getByLabel("Behavior section", { exact: true })
    .selectOption("predicates");
  const pins = {
    tab: "comparison",
    ...sideSelection(pair.left, "left"),
    ...sideSelection(pair.right, "right"),
    comparison_id: result.comparison_id,
    comparison_manifest_sha256: result.manifest_sha256,
    event_pair_id: selected.event_pair_id!,
    comparison_section: "predicates",
    comparison_offset: 0,
    comparison_event_offset: Math.floor(events.indexOf(selected) / 25) * 25,
  };
  await expectSelection(page, pins);
  const exactUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(exactUrl);
  await expectSelection(page, pins);
  await expect(page.getByTestId("comparison-prestates")).toBeVisible();
  await page.getByRole("link", { name: "Description", exact: true }).click();
  const description = page.url();
  await page.goBack();
  await expect(page).toHaveURL(exactUrl);
  await page.goForward();
  await expect(page).toHaveURL(description);
  await page.goBack();
  await loginAtExactUrl(page, exactUrl);
  await expectSelection(page, pins);
  await expect(page.getByTestId("comparison-prestates")).toBeVisible();
  await assertDurableCache(request, owned, comparison);

  const absentPairId = sha256(`absent event pair ${crypto.randomUUID()}`);
  const absentPair = new URL(exactUrl);
  absentPair.searchParams.set("event_pair_id", absentPairId);
  await page.goto(absentPair.href);
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expectSelection(page, { event_pair_id: absentPairId });
  await expect(
    page.getByRole("link", { name: "Open Left immutable replay", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", {
      name: "Open Right immutable replay",
      exact: true,
    }),
  ).toHaveCount(0);
  const absentPage = await sectionPage(request, owned.auth, result, "events", {
    event_pair_id: absentPairId,
  });
  expect(absentPage.total).toBe(0);
  expect(absentPage.items).toEqual([]);

  // A stale comparison digest and each side's independent pins fail closed.
  // The invalid query remains visible, never silently retargeted to the latest.
  for (const [name, value] of Object.entries({
    comparison_manifest_sha256: wrongHash(result.manifest_sha256),
    left_code_sha256: wrongHash(pair.left.strategy.content_sha256),
    right_spec_sha256: wrongHash(pair.right.spec.spec_sha256),
    left_execution_attempt_id: crypto.randomUUID(),
    right_result_manifest_sha256: wrongHash(
      pair.right.leaf.result_manifest_sha256!,
    ),
    left_trace_manifest_sha256: wrongHash(
      pair.left.leaf.trace_manifest_sha256!,
    ),
    right_evaluation_id: pair.right.leaves.find(
      (leaf) => leaf.split === "validation",
    )!.evaluation_id,
  })) {
    const invalid = new URL(exactUrl);
    invalid.searchParams.set(name, value);
    await page.goto(invalid.href);
    await expect(page.getByRole("alert").first()).toBeVisible();
    await expectSelection(page, { [name]: value });
    await expect(page.getByTestId("comparison-prestates")).toHaveCount(0);
    await expect(page.getByTestId("comparison-outcome")).toHaveCount(0);
  }
  const staleResult = await request.get(
    api(
      `/comparisons/${result.comparison_id}/result?${query({ manifest_sha256: wrongHash(result.manifest_sha256) })}`,
    ),
    { headers: authorization(owned.auth) },
  );
  expect(staleResult.status()).toBe(409);
  const staleSection = await request.get(
    api(
      `/comparisons/${result.comparison_id}/sections/events?${query({ manifest_sha256: wrongHash(result.manifest_sha256) })}`,
    ),
    { headers: authorization(owned.auth) },
  );
  expect(staleSection.status()).toBe(409);
  for (const [key, value] of Object.entries({
    code_sha256: wrongHash(pair.left.strategy.content_sha256),
    spec_sha256: wrongHash(pair.left.spec.spec_sha256),
    execution_attempt_id: crypto.randomUUID(),
    result_manifest_sha256: wrongHash(pair.left.leaf.result_manifest_sha256!),
    trace_manifest_sha256: wrongHash(pair.left.leaf.trace_manifest_sha256!),
    evaluation_id: pair.left.leaves.find((leaf) => leaf.split === "validation")!
      .evaluation_id,
  })) {
    const denied = await request.post(api("/comparisons"), {
      headers: {
        ...authorization(owned.auth),
        "Idempotency-Key": crypto.randomUUID(),
      },
      data: {
        ...comparison.body,
        left: { ...comparison.body.left, [key]: value },
      },
    });
    expect(denied.status(), `Stale source pin ${key}`).toBe(409);
  }
  const other = await token(request, true);
  for (const suffix of [
    "",
    `/result?${query({ manifest_sha256: result.manifest_sha256 })}`,
    `/sections/events?${query({ manifest_sha256: result.manifest_sha256 })}`,
  ]) {
    const denied = await request.get(
      api(`/comparisons/${result.comparison_id}${suffix}`),
      { headers: authorization(other) },
    );
    expect(denied.status()).toBe(404);
  }
  const foreignSubmit = await request.post(api("/comparisons"), {
    headers: {
      ...authorization(other),
      "Idempotency-Key": crypto.randomUUID(),
    },
    data: comparison.body,
  });
  expect(foreignSubmit.status()).toBe(404);
  await signIn(page, true);
  await page.goto(exactUrl);
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expect(page.getByTestId("comparison-outcome")).toHaveCount(0);
  await expect(page.getByTestId("comparison-prestates")).toHaveCount(0);
  expect(browsingExecutions).toEqual([]);
});

test("code-parent comparison exposes real allocation and fee changes, both path-dependent pre-states and exact behavioral drilldown without causal certification", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(900_000);
  await signIn(page);
  const pair = await actualPair(request, owned, "changed_allocation_and_fees");
  expect(pair.left.strategy.content_sha256).toBe(
    pair.right.strategy.content_sha256,
  );
  expect(pair.left.strategy.manifest).not.toEqual(pair.right.strategy.manifest);
  await page.goto(workbenchUrl(pair.right));
  await expect(
    page.getByRole("link", { name: "Open original run status", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Compare with code parent", exact: true })
    .click();
  await expectSelection(page, {
    left_source_kind: "strategy_version",
    left_source_id: pair.left.strategy.id,
    right_source_kind: "strategy_version",
    right_source_id: pair.right.strategy.id,
    right_evaluation_id: pair.right.leaf.evaluation_id,
  });
  // Parent relation never silently chooses an arbitrary execution/window.
  expect(new URL(page.url()).searchParams.get("left_evaluation_id")).toBeNull();
  await expect(
    page.getByRole("button", {
      name: "Create immutable comparison",
      exact: true,
    }),
  ).toBeDisabled();
  await page
    .getByLabel("Left actual leaf Evaluation", { exact: true })
    .selectOption(pair.left.leaf.evaluation_id);
  const comparison = await createInBrowser(page, request, owned, pair);
  const { result } = comparison;
  expect(result.manifest.observed_behavior).toBe("changed");
  expect(result.manifest.code_changed).toBe(false);
  const fee = result.manifest.configuration_differences.find(
    (item) => item.field === "fees.commission_rate",
  );
  expect(fee).toMatchObject({
    status: "different",
    left_present: true,
    right_present: true,
    left: pair.left.body.market_profile.commission_rate,
    right: 0.001,
  });
  const basis = page.getByRole("table", {
    name: "Immutable configuration differences",
    exact: true,
  });
  await expect(basis).toContainText("fees.commission_rate");
  await expect(basis).toContainText("0.001");
  const allocation = result.manifest.mechanism_changes.find(
    (item) => item.rule_id === "allocation",
  );
  expect(allocation).toMatchObject({
    category: "portfolio",
    mapping: "controlled",
    status: "changed",
  });
  await expect(page.getByTestId("comparison-mechanisms")).toContainText(
    "allocation",
  );
  await expect(page.getByTestId("comparison-mechanisms")).toContainText(
    "controlled",
  );
  await expect(page.getByTestId("comparison-outcome")).toContainText(
    /changed/i,
  );
  for (const warning of result.manifest.warnings)
    await expect(page.getByText(warning, { exact: true })).toBeVisible();
  const events = await allSectionRows(request, owned.auth, result, "events");
  const divergent = events.find(
    (item) =>
      item.left &&
      item.right &&
      header(item, "left").pre_state !== null &&
      header(item, "right").pre_state !== null &&
      JSON.stringify(header(item, "left").pre_state) !==
        JSON.stringify(header(item, "right").pre_state),
  );
  expect(
    divergent,
    "Actual changed paths must retain their distinct subsequent cash/position state",
  ).toBeDefined();
  const selected = await selectEventPair(
    page,
    request,
    owned,
    result,
    pair,
    events,
    divergent!,
  );
  expect(selected.lhs.pre_state).not.toEqual(selected.rhs.pre_state);
  expect(selected.lhs.data_event_id).toBe(selected.rhs.data_event_id);
  const targets = await allSectionRows(request, owned.auth, result, "targets");
  expect(
    targets.some(
      (item) =>
        item.status === "changed" ||
        item.status === "left_only" ||
        item.status === "right_only",
    ),
  ).toBe(true);
  expect(result.manifest.sections.targets.observed_equality).toBe("different");
  expect(
    ["orders", "fills", "positions", "account"].some(
      (section) =>
        result.manifest.sections[section].observed_equality === "different",
    ),
  ).toBe(true);
  await page
    .getByLabel("Behavior section", { exact: true })
    .selectOption("targets");
  await expectSelection(page, {
    event_pair_id: divergent!.event_pair_id!,
    comparison_section: "targets",
    comparison_offset: 0,
  });
  const targetPage = await sectionPage(request, owned.auth, result, "targets", {
    event_pair_id: divergent!.event_pair_id!,
  });
  expect(targetPage.items.length).toBeGreaterThan(0);
  for (const item of targetPage.items)
    await expect(
      page.getByTestId(`comparison-record-${item.row_id}`),
    ).toContainText(item.status);
  const exactUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(exactUrl);
  await expect(page.getByTestId("comparison-prestates")).toBeVisible();
  for (const [name, side, event] of [
    ["Left", pair.left, selected.lhs],
    ["Right", pair.right, selected.rhs],
  ] as const) {
    const link = page.getByRole("link", {
      name: `Open ${name} immutable replay`,
      exact: true,
    });
    const expected = assertReplayUrl(
      await link.getAttribute("href"),
      page.url(),
      side,
      event,
    );
    await link.click();
    await expect(page).toHaveURL((url) => url.pathname === expected.pathname);
    await expectSelection(page, Object.fromEntries(expected.searchParams));
    await expect(page.getByTestId("replay-pre-state")).toContainText(
      String(event.pre_state!.cash),
    );
    await page.goBack();
    await expect(page).toHaveURL(exactUrl);
  }
  await assertDurableCache(request, owned, comparison);
});
