import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type {
  CoveragePublic,
  DatasetPublic,
  FinalEvaluationDetail,
  FinalEvaluationPublic,
  FinalResultQuery,
  FinalSelectionPublic,
  ImportDetail,
  ImportPublic,
  ResearchCapabilities,
  ResearchRunCreate,
  ResearchRunDetail,
  ResearchRunPublic,
  StrategyPackagePublic,
  StrategyPublic,
  Token,
  UniversePublic,
} from "../../src/client/types.gen";
import { account, api, authorization, signIn, token } from "./helpers";

// Real-stack acceptance only: real PostgreSQL, data/research/final workers and
// native Docker evaluator. Synthetic uploaded prices and fixture model are NOT
// live-provider/model evidence. One interception loses an already committed real
// response; it never fabricates service results. --list does not run a browser.
const sha256 = (bytes: string | Buffer) =>
  createHash("sha256").update(bytes).digest("hex");
const terminal = new Set(["succeeded", "failed", "interrupted", "cancelled"]);
const windows = {
  train: { start: "2024-02-01", end: "2024-03-15" },
  validation: { start: "2024-03-15", end: "2024-04-20" },
  holdout: { start: "2024-04-22", end: "2024-04-27" },
};
async function read<T>(request: APIRequestContext, auth: Token, path: string) {
  const response = await request.get(api(path), {
    headers: authorization(auth),
  });
  expect(response.status(), `GET ${path}`).toBe(200);
  return (await response.json()) as T;
}
async function fixture(
  request: APIRequestContext,
  auth: Token,
): Promise<ResearchRunCreate> {
  const csv = await readFile(
    new URL("../fixtures/research-cn.csv", import.meta.url),
  );
  const metadata = JSON.parse(
    await readFile(
      new URL("../fixtures/research-cn.json", import.meta.url),
      "utf8",
    ),
  );
  expect(metadata.synthetic).toBe(true);
  expect(sha256(csv)).toBe(metadata.sha256);
  const mapping = {
    name: `E2E final synthetic fixture ${crypto.randomUUID()}`,
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
  const uploaded = await request.post(api("/data/uploads"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    multipart: {
      mapping: JSON.stringify(mapping),
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
          throw new Error(`Fixture import ${job.status}`);
        return job.status;
      },
      { timeout: 120_000 },
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
  const universeResponse = await request.post(api("/data/universes"), {
    headers: authorization(auth),
    data: {
      name: `E2E final fixed universe ${crypto.randomUUID()}`,
      state: "user_fixed",
      as_of: new Date().toISOString(),
      members: coverage.universe.members,
      evidence: [],
    },
  });
  expect(universeResponse.status()).toBe(201);
  const universe = (await universeResponse.json()) as UniversePublic;
  const original = await readFile(
    new URL("../../../seed_strategies/cn_time.py", import.meta.url),
    "utf8",
  );
  const strategies: StrategyPublic[] = [];
  for (;;) {
    const page = await read<{ data: StrategyPublic[]; count: number }>(
      request,
      auth,
      `/strategy-versions?offset=${strategies.length}&limit=100`,
    );
    strategies.push(...page.data);
    if (strategies.length >= page.count) break;
    expect(page.data.length).toBeGreaterThan(0);
  }
  const seed = strategies.find(
    (item) =>
      item.task === "cn_time" &&
      item.name === "Original cn_time seed" &&
      item.content_sha256 === sha256(original) &&
      item.parent_id === null,
  );
  expect(seed, "Bootstrap the exact repository cn_time seed").toBeDefined();
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
  )!;
  return {
    schema_version: "tradegrad.run.v1",
    execution_mode: "optimization",
    task: "cn_time",
    profile: "corrected_explicit_config",
    dataset: { id: dataset.id, sha256: dataset.content_sha256 },
    universe: { id: universe.id, sha256: universe.content_sha256 },
    seed_strategy: { id: seed!.id, sha256: seed!.content_sha256 },
    windows,
    market_profile: profile.market_profile,
    warmup: { ...profile.warmup, sessions: 20 },
    ...capabilities.defaults,
    optimizer: { ...capabilities.defaults!.optimizer, max_iterations: 1 },
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
}
async function configure(page: Page, body: ResearchRunCreate) {
  await page.goto("/experiments/new");
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
  await page
    .getByRole("checkbox", {
      name: "Reserve a final holdout interval (metadata only)",
      exact: true,
    })
    .check();
  for (const [key, name] of [
    ["train", "Training"],
    ["validation", "Validation"],
    ["holdout", "Holdout"],
  ] as const) {
    await page
      .getByLabel(`${name} start · inclusive`, { exact: true })
      .fill(body.windows[key]!.start);
    await page
      .getByLabel(`${name} end · exclusive`, { exact: true })
      .fill(body.windows[key]!.end);
  }
  await page
    .getByLabel("Warm-up sessions per listing", { exact: true })
    .fill("20");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  for (const [name, value] of [
    ["Maximum iterations", 1],
    ["Maximum model calls", 10],
    ["Maximum tokens", 100000],
    ["Maximum runtime · seconds", 150],
    ["Maximum repair attempts", 0],
  ] as const)
    await page.getByLabel(name, { exact: true }).fill(String(value));
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Submit experiment", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Run server preflight", exact: true })
    .click();
  await expect(page.getByTestId("research-preflight-success")).toBeVisible();
}
function post(path: string) {
  return (response: import("@playwright/test").Response) =>
    new URL(response.url()).pathname === `/api/v1${path}` &&
    response.request().method() === "POST";
}
async function waitSuccess<T extends ResearchRunDetail | FinalEvaluationDetail>(
  request: APIRequestContext,
  auth: Token,
  path: string,
) {
  await expect
    .poll(
      async () => {
        const job = await read<T>(request, auth, path);
        if (terminal.has(job.status) && job.status !== "succeeded")
          throw new Error(`${path} ended ${job.status}: ${job.error}`);
        return (
          job.status === "succeeded" &&
          job.attempts.every((attempt) => attempt.sandbox_stopped)
        );
      },
      { timeout: 180_000, intervals: [250, 500, 1000] },
    )
    .toBe(true);
  return read<T>(request, auth, path);
}
async function bytes(page: Page, button: string) {
  const next = page.waitForEvent("download");
  await page.getByRole("button", { name: button, exact: true }).click();
  const file = await next;
  expect(await file.failure()).toBeNull();
  const stream = await file.createReadStream();
  expect(stream).not.toBeNull();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  return { file, content: Buffer.concat(chunks) };
}

test.beforeAll(async ({ request }) => {
  account();
  account(true);
  expect(
    (await request.get(api("/health/ready"))).ok(),
    "Real migrated PostgreSQL API required, never substitute a mocked API",
  ).toBe(true);
});

test("bound native research → freeze → lost-response final submit → actual holdout → deterministic package, reload and owner isolation", async ({
  page,
  request,
  browser,
  baseURL,
}) => {
  test.setTimeout(480_000);
  const auth = await token(request);
  const owned: { kind: "research-runs" | "final-evaluations"; id: string }[] =
    [];
  try {
    const body = await fixture(request, auth);
    await signIn(page);
    await configure(page, body);
    const accepted = page.waitForResponse(post("/research-runs"));
    await page
      .getByRole("button", { name: "Submit experiment", exact: true })
      .click();
    const acceptedResponse = await accepted;
    expect(acceptedResponse.status()).toBe(202);
    const submitted = (await acceptedResponse.json()) as ResearchRunPublic;
    owned.push({ kind: "research-runs", id: submitted.id });
    const run = await waitSuccess<ResearchRunDetail>(
      request,
      auth,
      `/research-runs/${submitted.id}`,
    );
    expect(run.experiment_id).toBeTruthy();
    expect(run.split_version_id).toBeTruthy();
    await expect(
      page.getByLabel("Exact valid candidate", { exact: true }),
    ).toBeVisible();
    const candidates = await read<{
      data: { id: string; kind: string; code_sha256: string }[];
    }>(request, auth, `/research-runs/${run.id}/candidates?limit=100`);
    const candidate = candidates.data.find((item) => item.kind === "seed")!;
    expect(candidate).toBeDefined();
    await page
      .getByLabel("Exact valid candidate", { exact: true })
      .selectOption(candidate.id);
    await expect(
      page.getByText(candidate.code_sha256, { exact: true }).last(),
    ).toBeVisible();
    await page
      .getByRole("checkbox", { name: /I reviewed this exact candidate/ })
      .check();
    let freezes = 0;
    page.on("request", (request) => {
      if (
        new URL(request.url()).pathname === "/api/v1/final-selections" &&
        request.method() === "POST"
      )
        freezes++;
    });
    const frozen = page.waitForResponse(post("/final-selections"));
    await page
      .getByRole("button", { name: "Freeze selected candidate", exact: true })
      .evaluate((button: HTMLButtonElement) => {
        button.click();
        button.click();
      });
    const freezeResponse = await frozen;
    expect(freezeResponse.status()).toBe(201);
    const selection = (await freezeResponse.json()) as FinalSelectionPublic;
    expect(freezes).toBe(1);
    expect(selection.source_candidate_id).toBe(candidate.id);
    expect(selection.source_code_sha256).toBe(candidate.code_sha256);
    await expect(page).toHaveURL(
      new RegExp(`/final-selections/${selection.id}$`),
    );
    await expect(
      page.getByTestId("holdout-exposure-warning").first(),
    ).toContainText("Historical tracking incomplete/external use unknown");
    await page.reload();
    await expect(
      page.getByRole("heading", {
        name: "Exact frozen candidate",
        exact: true,
      }),
    ).toBeVisible();
    await page
      .getByRole("checkbox", { name: /I confirm running the frozen candidate/ })
      .check();
    await page
      .getByRole("button", { name: "Run final preflight", exact: true })
      .click();
    await expect(page.getByText(/Final preflight passed/)).toBeVisible();
    const wire: { key: string; body: unknown }[] = [];
    page.on("request", (request) => {
      if (
        new URL(request.url()).pathname === "/api/v1/final-evaluations" &&
        request.method() === "POST"
      )
        wire.push({
          key: request.headers()["idempotency-key"],
          body: request.postDataJSON(),
        });
    });
    let resolve!: (job: FinalEvaluationPublic) => void;
    let reject!: (error: unknown) => void;
    const lost = new Promise<FinalEvaluationPublic>((ok, no) => {
      resolve = ok;
      reject = no;
    });
    await page.route("**/api/v1/final-evaluations", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      try {
        const response = await route.fetch({ maxRetries: 0 });
        expect(response.status()).toBe(202);
        const job = (await response.json()) as FinalEvaluationPublic;
        owned.push({ kind: "final-evaluations", id: job.id });
        await route.abort("failed");
        resolve(job);
      } catch (error) {
        reject(error);
        await route.abort("failed").catch(() => undefined);
      }
    });
    const [final] = await Promise.all([
      lost,
      page
        .getByRole("button", { name: "Submit final holdout test", exact: true })
        .click(),
    ]);
    await expect(page.getByRole("alert")).toBeVisible();
    await page.unroute("**/api/v1/final-evaluations");
    await page.reload();
    await expect(page.getByTestId("final-pending")).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: "Retry same final submission",
        exact: true,
      }),
    ).toBeDisabled();
    await page
      .getByRole("checkbox", { name: /I confirm running the frozen candidate/ })
      .check();
    const replay = page.waitForResponse(post("/final-evaluations"));
    await page
      .getByRole("button", { name: "Retry same final submission", exact: true })
      .click();
    const replayResponse = await replay;
    expect(replayResponse.status()).toBe(202);
    expect((await replayResponse.json()).id).toBe(final.id);
    expect(wire).toHaveLength(2);
    expect(wire[1]).toEqual(wire[0]);
    await expect(page).toHaveURL(new RegExp(`/final-tests/${final.id}$`));
    const completed = await waitSuccess<FinalEvaluationDetail>(
      request,
      auth,
      `/final-evaluations/${final.id}`,
    );
    expect(completed.result?.holdout_executed).toBe(true);
    expect(completed.generation).toBe(1);
    expect(completed.attempts).toHaveLength(1);
    await expect(page.getByTestId("holdout-result")).toBeVisible();
    await page.reload();
    await expect(page.getByTestId("holdout-result")).toBeVisible();
    const evaluationId = completed.result!.evaluation_id!;
    const result = await read<FinalResultQuery>(
      request,
      auth,
      `/final-evaluations/evaluations/${evaluationId}/result?sections=equity&sections=drawdown&max_points=600`,
    );
    expect(result.split).toBe("holdout");
    expect(result.selection_feedback).toBe(false);
    expect(result.selection_id).toBe(selection.id);
    expect(result.tables.equity?.rows.length).toBeGreaterThan(0);
    expect(result.window).toMatchObject(windows.holdout);
    await page
      .getByRole("button", {
        name: "Show holdout equity and drawdown charts",
        exact: true,
      })
      .click();
    const equityChart = page.getByRole("img", {
      name: "Holdout Equity. Only the requested bounded series is shown. Original precision is available in the data table.",
      exact: true,
    });
    await expect(equityChart).toBeVisible();
    await expect(equityChart.locator("svg")).toBeVisible();
    const original = await bytes(page, "Download original holdout JSON");
    expect(sha256(original.content)).toBe(result.artifact_sha256);
    const exporting = page.waitForResponse(
      post(`/final-selections/${selection.id}/packages`),
    );
    await page
      .getByRole("button", {
        name: "Create reference-only strategy package",
        exact: true,
      })
      .click();
    const exportResponse = await exporting;
    expect([200, 201]).toContain(exportResponse.status());
    const item = (await exportResponse.json()) as StrategyPackagePublic;
    await expect(page).toHaveURL(new RegExp(`/strategy-packages/${item.id}$`));
    await expect(page.getByTestId("package-scope")).toContainText(
      "no raw market data",
    );
    const archive = await bytes(page, "Download authenticated strategy ZIP");
    expect(archive.file.suggestedFilename()).toBe(
      `strategy-package-${item.id}.zip`,
    );
    expect(sha256(archive.content)).toBe(item.archive_sha256);
    await page.reload();
    await expect(
      page.getByText(item.archive_sha256, { exact: true }),
    ).toBeVisible();
    const duplicate = await request.post(
      api(`/final-selections/${selection.id}/packages`),
      {
        headers: authorization(auth),
        data: {
          reference_evaluation_id: evaluationId,
          profile: "reference_only.v1",
        },
      },
    );
    expect(duplicate.status()).toBe(200);
    expect((await duplicate.json()).id).toBe(item.id);
    const stranger = await token(request, true);
    for (const path of [
      `/final-selections/${selection.id}`,
      `/final-evaluations/${final.id}`,
      `/final-evaluations/${final.id}/evaluations`,
      `/final-evaluations/evaluations/${evaluationId}/result`,
      `/strategy-packages/${item.id}`,
      `/strategy-packages/${item.id}/download`,
    ])
      expect(
        (
          await request.get(api(path), { headers: authorization(stranger) })
        ).status(),
        path,
      ).toBe(404);
    const context = await browser.newContext({ baseURL });
    try {
      const other = await context.newPage();
      await signIn(other, true);
      await other.goto(`/strategy-packages/${item.id}`);
      await expect(other.getByRole("alert")).toBeVisible();
      await expect(
        other.getByRole("button", {
          name: "Download authenticated strategy ZIP",
          exact: true,
        }),
      ).toHaveCount(0);
    } finally {
      await context.close();
    }
  } finally {
    // Only cancel jobs created by this test, and establish terminal physical stop.
    const failures: string[] = [];
    for (const item of owned.reverse()) {
      try {
        const path = `/${item.kind}/${item.id}`;
        const job = await read<ResearchRunDetail | FinalEvaluationDetail>(
          request,
          auth,
          path,
        );
        if (!terminal.has(job.status)) {
          const response = await request.post(api(`${path}/cancel`), {
            headers: authorization(auth),
          });
          expect(response.status()).toBe(202);
        }
        await expect
          .poll(
            async () => {
              const stopped = await read<
                ResearchRunDetail | FinalEvaluationDetail
              >(request, auth, path);
              return (
                terminal.has(stopped.status) &&
                stopped.attempts.every((attempt) => attempt.sandbox_stopped)
              );
            },
            { timeout: 45_000 },
          )
          .toBe(true);
      } catch {
        failures.push(item.id);
      }
    }
    expect(
      failures,
      "Every test-owned job must be terminal with confirmed stopped attempts",
    ).toEqual([]);
  }
});
