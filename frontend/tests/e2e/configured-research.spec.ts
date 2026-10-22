import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import {
  test as base,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type {
  ArtifactPublic,
  CandidateDetail,
  CandidatePublic,
  CheckpointPublic,
  CoveragePublic,
  DatasetPublic,
  EvaluationPublic,
  ImportDetail,
  ImportPublic,
  ModelCallPublic,
  ResearchCapabilities,
  ResearchPreflightPublic,
  ResearchRunCreate,
  ResearchRunDetail,
  ResearchRunPublic,
  ResultQueryPublic,
  StrategyDetail,
  StrategyPublic,
  Token,
  UniversePublic,
} from "../../src/client/types.gen";
import { account, api, authorization, signIn, token } from "./helpers";

// Acceptance requires the real migrated PostgreSQL API, data dispatcher/worker,
// research dispatcher/worker and Docker evaluator. The uploaded prices and model
// are explicitly synthetic; no paid model, live provider or host strategy runs.
// The one route interception below sends the POST to the REAL server, then loses
// its response. It never supplies fabricated API data or a replacement service.
// --list and TypeScript checking do not start browsers, workers or containers.
const sha256 = (bytes: string | Buffer) =>
  createHash("sha256").update(bytes).digest("hex");
const windows = {
  train: { start: "2024-02-01", end: "2024-03-15" },
  validation: { start: "2024-03-15", end: "2024-04-20" },
  holdout: { start: "2024-04-22", end: "2024-04-27" },
};
const terminal = new Set([
  "succeeded",
  "failed",
  "interrupted",
  "cancelled",
  "paused",
]);
type Owned = { auth: Token; runs: Set<string> };
const test = base.extend<{ owned: Owned }>({
  owned: async ({ request }, use) => {
    await use({ auth: await token(request), runs: new Set() });
  },
});

async function read<T>(request: APIRequestContext, auth: Token, path: string) {
  const response = await request.get(api(path), {
    headers: authorization(auth),
  });
  expect(response.status(), `GET ${path}`).toBe(200);
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
      `Pagination must advance for ${path}`,
    ).toBeGreaterThan(0);
  }
}
const detail = (request: APIRequestContext, auth: Token, id: string) =>
  read<ResearchRunDetail>(request, auth, `/research-runs/${id}`);
const evaluations = (request: APIRequestContext, auth: Token, id: string) =>
  rows<EvaluationPublic>(request, auth, `/research-runs/${id}/evaluations`);
const runStatus = (page: Page) =>
  page.locator(".page-header").getByTestId("run-status");

async function waitStatus(
  request: APIRequestContext,
  auth: Token,
  id: string,
  expected: ResearchRunDetail["status"],
) {
  await expect
    .poll(
      async () => {
        const run = await detail(request, auth, id);
        // Fail promptly with persisted evidence instead of hiding a failed run
        // behind a success timeout. Paused/cancelled are expected only explicitly.
        if (terminal.has(run.status) && run.status !== expected)
          throw new Error(
            `Research ${id} ended ${run.status}: ${run.error || "no error recorded"}`,
          );
        return run.status;
      },
      { timeout: 180_000, intervals: [250, 500, 1000] },
    )
    .toBe(expected);
  return detail(request, auth, id);
}
async function assertStopped(
  request: APIRequestContext,
  auth: Token,
  id: string,
) {
  await expect
    .poll(
      async () => {
        const [run, children] = await Promise.all([
          detail(request, auth, id),
          evaluations(request, auth, id),
        ]);
        return (
          terminal.has(run.status) &&
          run.attempts.every((attempt) => attempt.sandbox_stopped) &&
          children.every(
            (child) => child.sandbox_stopped && child.finished_at !== null,
          )
        );
      },
      { timeout: 45_000, intervals: [250, 500, 1000] },
    )
    .toBe(true);
}

test.beforeAll(async ({ request }) => {
  account();
  account(true);
  const response = await request.get(api("/health/ready"));
  expect(
    response.ok(),
    "Use the real migrated stack, never a mock service",
  ).toBe(true);
});
test.afterEach(async ({ request, owned }) => {
  // Never cancel another suite's work or sweep all owner runs. IDs are recorded
  // immediately after real acceptance, including the deliberately lost response.
  const failures: string[] = [];
  for (const id of owned.runs) {
    try {
      const run = await detail(request, owned.auth, id);
      if (!terminal.has(run.status) || run.status === "paused") {
        const response = await request.post(
          api(`/research-runs/${id}/cancel`),
          {
            headers: authorization(owned.auth),
          },
        );
        expect(response.status()).toBe(202);
      }
      await assertStopped(request, owned.auth, id);
    } catch {
      failures.push(id);
    }
  }
  expect(
    failures,
    "Owned run cleanup must confirm every physical child stopped",
  ).toEqual([]);
});

async function fixture(request: APIRequestContext, auth: Token, slow = false) {
  const [csv, metadataText, originalCode] = await Promise.all([
    readFile(new URL("../fixtures/research-cn.csv", import.meta.url)),
    readFile(new URL("../fixtures/research-cn.json", import.meta.url), "utf8"),
    readFile(
      new URL("../../../seed_strategies/cn_time.py", import.meta.url),
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
  expect(metadata.rows).toBe(76);
  const mapping = {
    name: `E2E configured synthetic fixture ${crypto.randomUUID()}`,
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
      note: "Synthetic engine fixture, not market prices or provider evidence",
    },
    unknown_flags: [],
  };
  const upload = await request.post(api("/data/uploads"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    multipart: {
      mapping: JSON.stringify(mapping),
      file: { name: "research-cn.csv", mimeType: "text/csv", buffer: csv },
    },
  });
  expect(upload.status()).toBe(202);
  const imported = (await upload.json()) as ImportPublic;
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
            `Actual data worker did not publish the fixture: ${job.status}`,
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
  // Dataset detail is /datasets/{id}, not a guessed /data/datasets endpoint.
  const dataset = await read<DatasetPublic>(
    request,
    auth,
    `/datasets/${ready.dataset_version_id}`,
  );
  expect(dataset.is_synthetic).toBe(true);
  expect(dataset.start_date).toBe(mapping.start_date);
  expect(dataset.end_date).toBe(mapping.end_date);
  const coverage = await read<CoveragePublic>(
    request,
    auth,
    `/datasets/${dataset.id}/coverage`,
  );
  expect(coverage.dataset_version_id).toBe(dataset.id);
  expect(coverage.coverage.length).toBeGreaterThan(0);
  expect(Array.isArray(coverage.universe.members)).toBe(true);
  const published = await request.post(api("/data/universes"), {
    headers: authorization(auth),
    data: {
      name: `E2E fixed research universe ${crypto.randomUUID()}`,
      state: "user_fixed",
      as_of: new Date().toISOString(),
      members: coverage.universe.members,
      evidence: [],
    },
  });
  expect(published.status()).toBe(201);
  const universe = (await published.json()) as UniversePublic;
  const catalog = await rows<StrategyPublic>(
    request,
    auth,
    "/strategy-versions",
  );
  const original = catalog.find(
    (strategy) =>
      strategy.name === "Original cn_time seed" &&
      strategy.task === "cn_time" &&
      strategy.parent_id === null &&
      strategy.content_sha256 === sha256(originalCode),
  );
  expect(
    original,
    "Bootstrap the exact unchanged repository cn_time seed",
  ).toBeDefined();
  let seed = await read<StrategyDetail>(
    request,
    auth,
    `/strategy-versions/${original!.id}`,
  );
  expect(seed.code).toBe(originalCode);
  expect(seed.content_sha256).toBe(sha256(originalCode));
  if (slow) {
    // This source is only stored/syntax-parsed here; the sleep and strategy are
    // executed exclusively by the production Docker evaluator, never by Node/host.
    const code = `import time as _e2e_time\n_e2e_time.sleep(5)\n${originalCode}`;
    const response = await request.post(api("/strategy-versions"), {
      headers: {
        ...authorization(auth),
        "Idempotency-Key": crypto.randomUUID(),
      },
      data: {
        name: `E2E bounded 5s research seed ${crypto.randomUUID()}`,
        task: "cn_time",
        parent_id: seed.id,
        code,
      },
    });
    expect(response.status()).toBe(201);
    seed = (await response.json()) as StrategyDetail;
    expect(seed.code).toBe(code);
    expect(seed.content_sha256).toBe(sha256(code));
  }
  const capabilities = await read<ResearchCapabilities>(
    request,
    auth,
    "/research-capabilities",
  );
  expect(capabilities.live_model_calls).toBe(false);
  expect(capabilities.model_providers).toEqual(["fixture"]);
  const profile = capabilities.profile_defaults.find(
    (item) =>
      item.task === "cn_time" && item.profile === "corrected_explicit_config",
  );
  expect(profile).toBeDefined();
  expect(capabilities.defaults?.model).toBeDefined();
  expect(capabilities.defaults?.optimizer).toBeDefined();
  const body: ResearchRunCreate = {
    schema_version: "tradegrad.run.v1",
    execution_mode: "optimization",
    task: "cn_time",
    profile: "corrected_explicit_config",
    dataset: { id: dataset.id, sha256: dataset.content_sha256 },
    universe: { id: universe.id, sha256: universe.content_sha256 },
    seed_strategy: { id: seed.id, sha256: seed.content_sha256 },
    windows: structuredClone(windows),
    market_profile: structuredClone(profile!.market_profile),
    warmup: { ...profile!.warmup, sessions: 20 },
    ...structuredClone(capabilities.defaults),
    optimizer: {
      ...capabilities.defaults!.optimizer,
      max_iterations: slow ? 0 : 1,
    },
    budgets: {
      max_model_calls: 10,
      max_tokens: 100000,
      max_runtime_seconds: 150,
      max_repairs: 0,
    },
    prompt_version: "source_prompts.v1",
    environment_digest: capabilities.environment_digest,
    benchmark: null,
  };
  return { body, dataset, universe, seed };
}

async function configure(
  page: Page,
  body: ResearchRunCreate,
  checkInvalid = false,
) {
  // Start through the real form, not by injecting sessionStorage/React state.
  await page.goto("/experiments/new");
  await expect(
    page.getByRole("heading", { name: "1. Inputs & profile", exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Market & task", { exact: true })
    .selectOption(body.task);
  await page
    .getByLabel("Execution profile", { exact: true })
    .selectOption(body.profile!);
  await page
    .getByLabel("Published dataset version", { exact: true })
    .selectOption(body.dataset.id);
  await page
    .getByLabel("Exact universe version", { exact: true })
    .selectOption(body.universe.id);
  await page
    .getByLabel("Seed strategy version", { exact: true })
    .selectOption(body.seed_strategy.id);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "2. Windows & history", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("checkbox", {
      name: "Reserve a final holdout interval (metadata only)",
      exact: true,
    })
    .check();
  for (const [key, label] of [
    ["train", "Training"],
    ["validation", "Validation"],
    ["holdout", "Holdout"],
  ] as const) {
    await page
      .getByLabel(`${label} start · inclusive`, { exact: true })
      .fill(body.windows[key]!.start);
    await page
      .getByLabel(`${label} end · exclusive`, { exact: true })
      .fill(body.windows[key]!.end);
  }
  await page
    .getByLabel("Warm-up sessions per listing", { exact: true })
    .fill("20");
  if (checkInvalid) {
    await page
      .getByLabel("Validation start · inclusive", { exact: true })
      .fill(body.windows.train.start);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText(
      "chronological and non-overlapping",
    );
    await expect(
      page.getByRole("heading", { name: "2. Windows & history", exact: true }),
    ).toBeVisible();
    await page
      .getByLabel("Validation start · inclusive", { exact: true })
      .fill(body.windows.validation!.start);
  }
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "3. Engine & optimization",
      exact: true,
    }),
  ).toBeVisible();
  for (const [label, value] of [
    ["Maximum iterations", body.optimizer!.max_iterations],
    ["Maximum model calls", body.budgets!.max_model_calls],
    ["Maximum tokens", body.budgets!.max_tokens],
    ["Maximum runtime · seconds", body.budgets!.max_runtime_seconds],
    ["Maximum repair attempts", body.budgets!.max_repairs],
  ] as const)
    await page.getByLabel(label, { exact: true }).fill(String(value));
  await expect(
    page.getByLabel("Deployment environment digest", { exact: true }),
  ).toHaveValue(body.environment_digest);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "4. Review & preflight", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Submit experiment", exact: true }),
  ).toBeDisabled();
}
function isPost(path: string) {
  return (response: import("@playwright/test").Response) =>
    new URL(response.url()).pathname === `/api/v1${path}` &&
    response.request().method() === "POST";
}
async function preflight(page: Page, body: ResearchRunCreate) {
  const pending = page.waitForResponse(isPost("/research-runs/preflight"));
  await page
    .getByRole("button", { name: "Run server preflight", exact: true })
    .click();
  const response = await pending;
  expect(response.status()).toBe(200);
  expect(response.request().postDataJSON()).toEqual(body);
  const result = (await response.json()) as ResearchPreflightPublic;
  expect(result.valid).toBe(true);
  expect(result.is_synthetic).toBe(true);
  expect(result.evaluation_windows).toEqual([
    { split: "train", ...body.windows.train },
    { split: "validation", ...body.windows.validation },
  ]);
  await expect(page.getByTestId("research-preflight-success")).toContainText(
    result.config_sha256,
  );
  await expect(
    page.getByRole("button", { name: "Submit experiment", exact: true }),
  ).toBeEnabled();
  return result;
}
async function submit(page: Page, owned: Owned, doubleClick = false) {
  const pending = page.waitForResponse(isPost("/research-runs"));
  const button = page.getByRole("button", {
    name: "Submit experiment",
    exact: true,
  });
  if (doubleClick) {
    // Same-turn DOM clicks exercise the synchronous guard before React rerenders.
    await button.evaluate((element: HTMLButtonElement) => {
      element.click();
      element.click();
    });
  } else await button.click();
  const response = await pending;
  expect(response.status()).toBe(202);
  const run = (await response.json()) as ResearchRunPublic;
  owned.runs.add(run.id);
  await expect(page).toHaveURL(new RegExp(`/research-runs/${run.id}$`));
  return run;
}
async function runsForDataset(
  request: APIRequestContext,
  auth: Token,
  dataset: string,
) {
  return (
    await rows<ResearchRunPublic>(request, auth, "/research-runs")
  ).filter((run) => run.dataset_version_id === dataset);
}
async function downloadedBytes(page: Page, button: string, filename: string) {
  const pending = page.waitForEvent("download");
  await page.getByRole("button", { name: button, exact: true }).click();
  const download = await pending;
  expect(download.suggestedFilename()).toBe(filename);
  expect(await download.failure()).toBeNull();
  const stream = await download.createReadStream();
  expect(stream).not.toBeNull();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks);
}

// The fixture-model returns the unchanged seed, but the optimizer, queue,
// checkpoints, native evaluations, artifacts, and browser interactions are real.
test("four-step configured optimizer survives reload and double submit, with native evidence and owner isolation", async ({
  page,
  request,
  owned,
  browser,
  baseURL,
}) => {
  test.setTimeout(360_000);
  const { body, seed } = await fixture(request, owned.auth);
  await signIn(page);
  await configure(page, body, true);
  const checked = await preflight(page, body);
  expect(await runsForDataset(request, owned.auth, body.dataset.id)).toEqual(
    [],
  );
  await page.reload();
  await expect(
    page.getByText("Restored this signed-in owner’s draft.", { exact: false }),
  ).toBeVisible();
  await expect(page.getByTestId("research-preflight-success")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Submit experiment", exact: true }),
  ).toBeDisabled();
  expect((await preflight(page, body)).config_sha256).toBe(
    checked.config_sha256,
  );
  let posts = 0;
  page.on("request", (request) => {
    if (
      new URL(request.url()).pathname === "/api/v1/research-runs" &&
      request.method() === "POST"
    )
      posts++;
  });
  const accepted = await submit(page, owned, true);
  expect(posts).toBe(1);
  expect(
    (await runsForDataset(request, owned.auth, body.dataset.id)).map(
      (run) => run.id,
    ),
  ).toEqual([accepted.id]);
  const run = await waitStatus(request, owned.auth, accepted.id, "succeeded");
  await assertStopped(request, owned.auth, run.id);
  await expect(runStatus(page)).toHaveText("Succeeded", { timeout: 20_000 });
  expect(run.execution_mode).toBe("optimization");
  expect(run.config).toEqual(body);
  expect(run.generation).toBe(1);
  expect(run.attempts).toHaveLength(1);
  expect(run.research_image_digest).toMatch(/^sha256:[0-9a-f]{64}$/);
  expect(run.result).toHaveProperty("best_candidate_id", expect.any(String));
  const [candidates, children, calls, checkpoints, artifacts] =
    await Promise.all([
      rows<CandidatePublic>(
        request,
        owned.auth,
        `/research-runs/${run.id}/candidates`,
      ),
      evaluations(request, owned.auth, run.id),
      rows<ModelCallPublic>(
        request,
        owned.auth,
        `/research-runs/${run.id}/model-calls`,
      ),
      rows<CheckpointPublic>(
        request,
        owned.auth,
        `/research-runs/${run.id}/checkpoints`,
      ),
      rows<ArtifactPublic>(request, owned.auth, `/runs/${run.id}/artifacts`),
    ]);
  expect(candidates.map((item) => item.kind).sort()).toEqual([
    "generated",
    "seed",
  ]);
  const seedCandidate = candidates.find((item) => item.kind === "seed")!;
  expect(seedCandidate.code_sha256).toBe(seed.content_sha256);
  for (const candidate of candidates) {
    const stored = await read<CandidateDetail>(
      request,
      owned.auth,
      `/candidates/${candidate.id}`,
    );
    expect(sha256(stored.code)).toBe(candidate.code_sha256);
    // Upstream parse_code strips outer whitespace on generated candidates. The
    // pinned seed remains byte-exact; generation must not falsely assume its
    // terminal newline survives the source parser.
    expect(stored.code).toBe(
      candidate.kind === "seed" ? seed.code : seed.code.trim(),
    );
  }
  expect(children).toHaveLength(4);
  expect(calls.length).toBeGreaterThan(0);
  expect(calls.every((call) => call.provider === "fixture")).toBe(true);
  expect(
    checkpoints.find((item) => item.id === run.latest_checkpoint_id),
  ).toMatchObject({
    complete: true,
    next_stage: "finished",
    config_sha256: checked.config_sha256,
  });
  for (const child of children) {
    expect(child.status).toBe("succeeded");
    expect(child.sandbox_stopped).toBe(true);
    expect(child.image_digest).toBe(run.research_image_digest);
    expect(checked.evaluation_windows).toContainEqual(child.window);
  }
  await page.reload();
  await expect(runStatus(page)).toHaveText("Succeeded");
  await expect(
    page.getByRole("heading", {
      name: "Complete checkpoint history",
      exact: true,
    }),
  ).toBeVisible();
  const train = children
    .filter((child) => child.split === "train")
    .sort(
      (left, right) =>
        Number(right.candidate_key === seedCandidate.candidate_key) -
        Number(left.candidate_key === seedCandidate.candidate_key),
    );
  const validation = children.find((child) => child.split === "validation")!;
  expect(train).toHaveLength(2);
  await page.goto(`/backtests/${train[0].id}`);
  await expect(
    page.getByRole("heading", {
      name: "Window metrics and definitions",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByTestId("result-evidence")).toContainText("Synthetic");
  const result = await read<ResultQueryPublic>(
    request,
    owned.auth,
    `/research-evaluations/${train[0].id}/results?sections=equity&sections=drawdown&max_points=600`,
  );
  expect(result.tables.equity?.rows.length).toBeGreaterThan(0);
  for (const name of ["Equity", "Drawdown"]) {
    const chart = page.getByRole("img", { name: new RegExp(`^${name} \\(`) });
    await expect(chart).toBeVisible();
    await expect(chart.locator("svg")).toBeVisible();
  }
  await page
    .getByText("Equity returned points · accessible original numeric values", {
      exact: true,
    })
    .click();
  await expect(
    page
      .getByRole("region", { name: "Equity returned points", exact: true })
      .getByRole("row"),
  ).toHaveCount(result.tables.equity!.rows.length + 1);
  const json = await downloadedBytes(
    page,
    "Original result JSON",
    `evaluation-${train[0].id}.json`,
  );
  expect(sha256(json)).toBe(result.artifact_sha256);
  const native = JSON.parse(json.toString("utf8"));
  expect(native.evaluation_mode).toBe("native_akquant");
  expect(native.engine.name).toBe("akquant");
  expect(native.result_trust).toBe("native_strategy_and_engine_share_process");
  expect(native.identity.run_config_sha256).toBe(checked.config_sha256);
  expect(native.identity.strategy_sha256).toBe(seed.content_sha256);
  const html = (
    await downloadedBytes(
      page,
      "Download safe report",
      `evaluation-${train[0].id}.html`,
    )
  ).toString("utf8");
  expect(html).toContain("TradeGrad persisted research report");
  expect(html).toContain(result.artifact_sha256);
  expect(html).not.toMatch(/<script[\s>]/i);
  expect(
    await page.evaluate(
      (text) =>
        new DOMParser()
          .parseFromString(text, "text/html")
          .querySelector('meta[http-equiv="Content-Security-Policy"]')
          ?.getAttribute("content"),
      html,
    ),
  ).toContain("default-src 'none'");
  await expect(page.locator("iframe")).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByRole("heading", {
      name: "Window metrics and definitions",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByRole("img", { name: /^Equity \(/ })).toBeVisible();
  await page.getByRole("link", { name: "Research run", exact: true }).click();
  for (const item of train)
    await page
      .getByRole("checkbox", {
        name: `Compare evaluation ${item.id}`,
        exact: true,
      })
      .check();
  await page
    .getByRole("link", { name: "Compare selected evaluations", exact: true })
    .click();
  await expect(page.getByTestId("comparison-basis")).toContainText(
    "share the server-checked comparison basis",
  );
  await expect(
    page.getByRole("heading", {
      name: "Matched metric comparison (not a ranking)",
      exact: true,
    }),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("comparison-basis")).toContainText(
    "never auto-ranked",
  );
  expect(new URL(page.url()).searchParams.get("evaluations")).toBe(
    train.map((item) => item.id).join(","),
  );
  await page.goto(`/compare?evaluations=${train[0].id},${validation.id}`);
  await expect(page.getByTestId("comparison-basis")).toContainText(
    "Ranking and comparison charts are blocked",
  );
  await expect(
    page.getByRole("heading", {
      name: "Matched metric comparison (not a ranking)",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(page.getByRole("img")).toHaveCount(0);

  const stranger = await token(request, true);
  const paths = [
    `/research-runs/${run.id}`,
    `/research-runs/${run.id}/candidates`,
    `/research-runs/${run.id}/evaluations`,
    `/research-runs/${run.id}/checkpoints`,
    `/research-runs/${run.id}/model-calls`,
    `/research-runs/${run.id}/usage`,
    `/runs/${run.id}/events?follow=false`,
    `/runs/${run.id}/artifacts`,
    `/datasets/${body.dataset.id}`,
    `/data/universes/${body.universe.id}`,
    `/candidates/${candidates[0].id}`,
    `/evaluations/${train[0].id}`,
    `/research-checkpoints/${run.latest_checkpoint_id}`,
    `/model-calls/${calls[0].id}`,
    `/research-evaluations/${train[0].id}/results`,
    `/research-evaluations/${train[0].id}/results/download`,
    `/research-evaluations/${train[0].id}/report`,
    ...artifacts.map((artifact) => `/artifacts/${artifact.id}/download`),
  ];
  for (const path of paths) {
    const response = await request.get(api(path), {
      headers: authorization(stranger),
    });
    expect(response.status(), path).toBe(404);
  }
  for (const action of ["pause", "cancel", "resume"]) {
    const response = await request.post(
      api(`/research-runs/${run.id}/${action}`),
      { headers: authorization(stranger) },
    );
    expect(response.status()).toBe(404);
  }
  const forbiddenCompare = await request.post(
    api("/research-evaluations/compare"),
    {
      headers: authorization(stranger),
      data: { evaluation_ids: train.map((item) => item.id) },
    },
  );
  expect(forbiddenCompare.status()).toBe(404);
  const otherContext = await browser.newContext({ baseURL });
  try {
    const otherPage = await otherContext.newPage();
    await signIn(otherPage, true);
    await otherPage.goto(`/research-runs/${run.id}`);
    await expect(otherPage.getByRole("alert")).toBeVisible();
    await expect(
      otherPage.getByRole("heading", {
        name: "Complete checkpoint history",
        exact: true,
      }),
    ).toHaveCount(0);
    await otherPage.goto(`/backtests/${train[0].id}`);
    await expect(
      otherPage.getByRole("heading", {
        name: "Backtest evidence unavailable",
        exact: true,
      }),
    ).toBeVisible();
    await expect(otherPage.getByRole("img")).toHaveCount(0);
  } finally {
    await otherContext.close();
  }
});

test("accepted configured POST with a lost response retains immutable retry identity after reload", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(300_000);
  const { body } = await fixture(request, owned.auth);
  body.optimizer = { ...body.optimizer, max_iterations: 0 };
  await signIn(page);
  await configure(page, body);
  await preflight(page, body);
  const wire: { key: string; body: ResearchRunCreate }[] = [];
  page.on("request", (request) => {
    if (
      new URL(request.url()).pathname === "/api/v1/research-runs" &&
      request.method() === "POST"
    )
      wire.push({
        key: request.headers()["idempotency-key"],
        body: request.postDataJSON() as ResearchRunCreate,
      });
  });
  let resolveAccepted!: (run: ResearchRunPublic) => void;
  let rejectAccepted!: (error: unknown) => void;
  const acceptedPromise = new Promise<ResearchRunPublic>((resolve, reject) => {
    resolveAccepted = resolve;
    rejectAccepted = reject;
  });
  await page.route("**/api/v1/research-runs", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    try {
      // A real committed acceptance precedes the transport failure. Unlike an
      // offline-before-submit test, this proves retry of an uncertain outcome.
      const response = await route.fetch({ maxRetries: 0 });
      expect(response.status()).toBe(202);
      const run = (await response.json()) as ResearchRunPublic;
      owned.runs.add(run.id);
      await route.abort("failed");
      resolveAccepted(run);
    } catch (error) {
      rejectAccepted(error);
      // Do not leave the browser request hanging if the real server rejects it.
      await route.abort("failed").catch(() => undefined);
    }
  });
  const [accepted] = await Promise.all([
    acceptedPromise,
    page
      .getByRole("button", { name: "Submit experiment", exact: true })
      .click(),
  ]);
  await expect(page.getByRole("alert")).toBeVisible();
  await page.unroute("**/api/v1/research-runs");
  expect(wire).toHaveLength(1);
  expect(wire[0].key).toMatch(/^[0-9a-f-]{36}$/);
  expect(wire[0].body).toEqual(body);
  expect(
    (await runsForDataset(request, owned.auth, body.dataset.id)).map(
      (run) => run.id,
    ),
  ).toEqual([accepted.id]);
  await page.reload();
  await expect(page.getByTestId("research-retry-notice")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Submit experiment", exact: true }),
  ).toBeDisabled();
  await expect(page.getByTestId("research-preflight-success")).toHaveCount(0);
  const retained = await page.evaluate(() => {
    const key = Object.keys(sessionStorage).find((item) =>
      item.startsWith("tradegrad.research-draft.v1."),
    );
    return key ? JSON.parse(sessionStorage.getItem(key)!).submission : null;
  });
  expect(retained).toEqual(wire[0]);
  await preflight(page, body);
  const replay = await submit(page, owned);
  expect(wire).toHaveLength(2);
  expect(wire[1]).toEqual(wire[0]);
  expect(replay.id).toBe(accepted.id);
  expect(replay.generation).toBe(accepted.generation);
  const finished = await waitStatus(
    request,
    owned.auth,
    accepted.id,
    "succeeded",
  );
  expect(finished.generation).toBe(1);
  expect(finished.attempts).toHaveLength(1);
  expect(await evaluations(request, owned.auth, accepted.id)).toHaveLength(2);
  expect(
    (await runsForDataset(request, owned.auth, body.dataset.id)).map(
      (run) => run.id,
    ),
  ).toEqual([accepted.id]);
  await page.reload();
  await expect(runStatus(page)).toHaveText("Succeeded");
});

async function waitActiveChild(
  request: APIRequestContext,
  auth: Token,
  id: string,
) {
  let child: EvaluationPublic | undefined;
  await expect
    .poll(
      async () => {
        const run = await detail(request, auth, id);
        if (terminal.has(run.status))
          throw new Error(
            `Run ${id} ended before its live evaluator boundary: ${run.status}`,
          );
        child = (await evaluations(request, auth, id)).find(
          (item) => !item.sandbox_stopped && item.finished_at === null,
        );
        // The real contract calls a launched/in-flight child "registered" until
        // completion, rather than fabricating a nonexistent child "running" status.
        return run.status === "running" && Boolean(child);
      },
      { timeout: 45_000, intervals: [100, 250] },
    )
    .toBe(true);
  return child!;
}

test("checkpoint pause resumes completed evaluation UUIDs and running cancellation stops every physical child", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(360_000);
  const { body } = await fixture(request, owned.auth, true);
  await signIn(page);
  await configure(page, body);
  await preflight(page, body);
  const accepted = await submit(page, owned);
  const active = await waitActiveChild(request, owned.auth, accepted.id);
  await expect(runStatus(page)).toHaveText("Running", { timeout: 15_000 });
  await page
    .getByRole("button", { name: "Pause at checkpoint", exact: true })
    .click();
  const paused = await waitStatus(request, owned.auth, accepted.id, "paused");
  await assertStopped(request, owned.auth, accepted.id);
  await expect(runStatus(page)).toHaveText("Paused");
  expect(paused.latest_checkpoint_id).not.toBeNull();
  const before = await evaluations(request, owned.auth, accepted.id);
  const completed = before.find((item) => item.id === active.id)!;
  expect(completed).toMatchObject({
    id: active.id,
    status: "succeeded",
    sandbox_stopped: true,
    generation: 1,
  });
  expect(completed.payload_sha256).toMatch(/^[0-9a-f]{64}$/);
  const checkpoint = await read<CheckpointPublic>(
    request,
    owned.auth,
    `/research-checkpoints/${paused.latest_checkpoint_id}`,
  );
  expect(checkpoint.complete).toBe(true);
  await page.reload();
  await expect(runStatus(page)).toHaveText("Paused");
  const resuming = page.waitForResponse(
    isPost(`/research-runs/${accepted.id}/resume`),
  );
  await page
    .getByRole("button", { name: "Resume checkpoint", exact: true })
    .click();
  expect((await resuming).status()).toBe(202);
  const resumed = await waitStatus(
    request,
    owned.auth,
    accepted.id,
    "succeeded",
  );
  await assertStopped(request, owned.auth, accepted.id);
  await expect(runStatus(page)).toHaveText("Succeeded");
  expect(resumed.generation).toBe(paused.generation + 1);
  expect(resumed.resume_checkpoint_id).toBe(paused.latest_checkpoint_id);
  expect(resumed.attempts).toHaveLength(2);
  const after = await evaluations(request, owned.auth, accepted.id);
  expect(after).toHaveLength(2);
  // Deep equality includes UUID, original attempt/generation, payload hash,
  // window and finish time: a recomputation with similar metrics cannot pass.
  for (const original of before.filter((item) => item.status === "succeeded"))
    expect(after.find((item) => item.id === original.id)).toEqual(original);
  expect(after.every((item) => item.status === "succeeded")).toBe(true);
  expect(
    await rows<ModelCallPublic>(
      request,
      owned.auth,
      `/research-runs/${accepted.id}/model-calls`,
    ),
  ).toEqual([]);
  await page.reload();
  await expect(runStatus(page)).toHaveText("Succeeded");

  await configure(page, body);
  await preflight(page, body);
  const cancelledRun = await submit(page, owned);
  const cancellingChild = await waitActiveChild(
    request,
    owned.auth,
    cancelledRun.id,
  );
  await expect(runStatus(page)).toHaveText("Running", { timeout: 15_000 });
  await page.getByRole("button", { name: "Cancel run", exact: true }).click();
  const cancelled = await waitStatus(
    request,
    owned.auth,
    cancelledRun.id,
    "cancelled",
  );
  await assertStopped(request, owned.auth, cancelledRun.id);
  expect(cancelled.attempts.length).toBeGreaterThan(0);
  const stoppedChildren = await evaluations(
    request,
    owned.auth,
    cancelledRun.id,
  );
  expect(stoppedChildren.length).toBeGreaterThan(0);
  expect(
    stoppedChildren.find((item) => item.id === cancellingChild.id)
      ?.sandbox_stopped,
  ).toBe(true);
  expect(
    stoppedChildren.every(
      (item) => item.sandbox_stopped && item.finished_at !== null,
    ),
  ).toBe(true);
  expect(cancelled.attempts.every((attempt) => attempt.sandbox_stopped)).toBe(
    true,
  );
  await page.reload();
  await expect(runStatus(page)).toHaveText("Cancelled");
  await expect(
    page.getByText("Sandbox confirmed stopped", { exact: false }).first(),
  ).toBeVisible();
  expect((await detail(request, owned.auth, accepted.id)).status).toBe(
    "succeeded",
  );
});
