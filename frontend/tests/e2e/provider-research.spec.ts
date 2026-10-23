import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type {
  CandidatePublic,
  CheckpointPublic,
  DatasetPublic,
  EvaluationPublic,
  ImportDetail,
  ListingPublic,
  ModelCallPublic,
  ProviderPublic,
  ResearchCapabilities,
  ResearchRunCreate,
  ResearchRunDetail,
  ResultQueryPublic,
  StrategyPublic,
  Token,
  UniversePublic,
} from "../../src/client/types.gen";
import { api, authorization, signIn, token } from "./helpers";

// Actual API/PG/data queue/research queue/Docker/TradeTGD, deterministic SDK and
// model response fixtures only. Never route mocks, market uploads or live probes.
const scenarios = [
  {
    provider: "akshare",
    symbols: ["600000", "000001"],
    mics: ["XSHG", "XSHE"],
    task: "cn_cross",
    mode: "raw",
    split: "unadjusted",
    dividend: "unadjusted",
  },
  {
    provider: "yfinance",
    symbols: ["AAPL"],
    mics: ["XNAS"],
    task: "us_time",
    mode: "provider_native",
    split: "adjusted",
    dividend: "unadjusted",
  },
  {
    provider: "openbb",
    symbols: ["MSFT"],
    mics: ["XNAS"],
    task: "us_time",
    mode: "provider_native",
    split: "unknown",
    dividend: "unknown",
  },
] as const;
const terminal = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
async function read<T>(
  request: APIRequestContext,
  auth: Token,
  path: string,
): Promise<T> {
  const response = await request.get(api(path), {
    headers: authorization(auth),
  });
  expect(response.status(), `GET ${path}`).toBe(200);
  return response.json();
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
    expect(page.data.length).toBeGreaterThan(0);
  }
}
async function readyImport(
  request: APIRequestContext,
  auth: Token,
  id: string,
) {
  await expect
    .poll(
      async () => {
        const value = await read<ImportDetail>(
          request,
          auth,
          `/data/imports/${id}`,
        );
        if (["failed", "partial", "cancelled"].includes(value.status))
          throw new Error(`Provider job ended ${value.status}`);
        return value.status;
      },
      { timeout: 120_000, intervals: [500, 1000] },
    )
    .toBe("ready");
  return read<ImportDetail>(request, auth, `/data/imports/${id}`);
}
async function configure(page: Page, body: ResearchRunCreate) {
  await page.goto("/experiments/new");
  await page
    .getByLabel("Market & task", { exact: true })
    .selectOption(body.task);
  await page
    .getByLabel("Execution profile", { exact: true })
    .selectOption("corrected_explicit_config");
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
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Model profile", { exact: true })
    .selectOption("fixture");
  for (const [label, value] of [
    ["Maximum iterations", 1],
    ["Maximum model calls", 10],
    ["Maximum tokens", 100000],
    ["Maximum runtime · seconds", 180],
    ["Maximum repair attempts", 0],
  ] as const)
    await page.getByLabel(label, { exact: true }).fill(String(value));
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  const checked = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/research-runs/preflight" &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Run server preflight", exact: true })
    .click();
  const response = await checked;
  expect(response.status()).toBe(200);
  expect((await response.json()).valid).toBe(true);
  expect(response.request().postDataJSON()).toEqual(body);
}

for (const scenario of scenarios)
  test(`${scenario.provider}: same immutable adapter snapshot to corrected TradeTGD optimization and reloaded charts @provider-research`, async ({
    page,
    request,
  }) => {
    test.setTimeout(420_000);
    test.skip(
      process.env.E2E_LIVE_PROVIDERS === "1",
      "Live market probes are a separate gate; no fallback or mixed evidence",
    );
    await signIn(page);
    const auth = await token(request);
    const catalogs = await read<ProviderPublic[]>(
      request,
      auth,
      "/data/providers",
    );
    expect(catalogs.find((p) => p.id === scenario.provider)?.runtime_mode).toBe(
      "fixture",
    );
    const ownedRuns: string[] = [];
    try {
      await page.goto("/data-sources");
      await page
        .getByLabel("Provider", { exact: true })
        .selectOption(scenario.provider);
      await page
        .getByLabel("Vendor symbols", { exact: true })
        .fill(scenario.symbols.join(","));
      await page
        .getByRole("button", { name: "Resolve securities", exact: true })
        .click();
      for (const [index, symbol] of scenario.symbols.entries())
        await page
          .getByRole("checkbox", {
            name: `Select ${symbol} on ${scenario.mics[index]}`,
            exact: true,
          })
          .check({ timeout: 60_000 });
      await page
        .getByLabel("Snapshot name", { exact: true })
        .fill(
          `Provider research synthetic ${scenario.provider} ${crypto.randomUUID()}`,
        );
      await page
        .getByLabel("Import start · inclusive", { exact: true })
        .fill("2023-07-03");
      await page
        .getByLabel("Import end · exclusive", { exact: true })
        .fill("2024-01-02");
      await page
        .getByLabel("Price mode", { exact: true })
        .selectOption(scenario.mode);
      await page
        .getByRole("button", { name: "Import selected listings", exact: true })
        .click();
      await expect(page).toHaveURL(/\/imports\/[0-9a-f-]{36}$/);
      const job = await readyImport(
        request,
        auth,
        new URL(page.url()).pathname.split("/").at(-1)!,
      );
      const snapshot = await read<DatasetPublic>(
        request,
        auth,
        `/datasets/${job.dataset_version_id}`,
      );
      const originalManifest = structuredClone(snapshot.manifest);
      expect(snapshot.source).toBe(scenario.provider);
      expect(snapshot.is_synthetic).toBe(true);
      expect(snapshot.manifest.price_semantics).toMatchObject({
        split_basis: scenario.split,
        dividend_basis: scenario.dividend,
      });
      expect(
        (snapshot.manifest.listings as ListingPublic[])
          .map((l) => l.symbol)
          .sort(),
      ).toEqual([...scenario.symbols].sort());
      for (const source of snapshot.manifest.provenance as Record<
        string,
        unknown
      >[])
        expect(source).toMatchObject({
          evidence: "fixture",
          terms_url: expect.stringMatching(/^https:\/\//),
        });
      const universeResponse = await request.post(api("/data/universes"), {
        headers: authorization(auth),
        data: {
          name: `Provider research basket ${crypto.randomUUID()}`,
          state: "user_fixed",
          as_of: new Date().toISOString(),
          members: (snapshot.manifest.universe as { members: unknown[] })
            .members,
          evidence: [],
        },
      });
      expect(universeResponse.status()).toBe(201);
      const universe = (await universeResponse.json()) as UniversePublic;
      const seed = (
        await rows<StrategyPublic>(request, auth, "/strategy-versions")
      ).find(
        (item) =>
          item.name === `Original ${scenario.task} seed` &&
          item.parent_id === null,
      )!;
      expect(seed).toBeDefined();
      const capabilities = await read<ResearchCapabilities>(
        request,
        auth,
        "/research-capabilities",
      );
      const profile = capabilities.profile_defaults.find(
        (p) =>
          p.task === scenario.task && p.profile === "corrected_explicit_config",
      )!;
      const body: ResearchRunCreate = {
        schema_version: "tradegrad.run.v1",
        execution_mode: "optimization",
        task: scenario.task,
        profile: "corrected_explicit_config",
        dataset: { id: snapshot.id, sha256: snapshot.content_sha256 },
        universe: { id: universe.id, sha256: universe.content_sha256 },
        seed_strategy: { id: seed.id, sha256: seed.content_sha256 },
        windows: {
          train: { start: "2023-09-01", end: "2023-10-16" },
          validation: { start: "2023-10-16", end: "2023-12-01" },
          holdout: { start: "2023-12-01", end: "2023-12-29" },
        },
        market_profile: structuredClone(profile.market_profile),
        warmup: { ...profile.warmup, sessions: 20 },
        ...structuredClone(capabilities.defaults),
        optimizer: { ...capabilities.defaults!.optimizer, max_iterations: 1 },
        budgets: {
          max_model_calls: 10,
          max_tokens: 100000,
          max_runtime_seconds: 180,
          max_repairs: 0,
        },
        prompt_version: "source_prompts.v1",
        environment_digest: capabilities.environment_digest,
        benchmark: null,
      };
      // No preview of reserved holdout data before or during optimization.
      await configure(page, body);
      const accepted = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === "/api/v1/research-runs" &&
          response.request().method() === "POST",
      );
      await page
        .getByRole("button", { name: "Submit experiment", exact: true })
        .click();
      const response = await accepted;
      expect(response.status()).toBe(202);
      const submitted = (await response.json()) as ResearchRunDetail;
      ownedRuns.push(submitted.id);
      await expect
        .poll(
          async () => {
            const run = await read<ResearchRunDetail>(
              request,
              auth,
              `/research-runs/${submitted.id}`,
            );
            if (terminal.has(run.status) && run.status !== "succeeded")
              throw new Error(`Research ended ${run.status}`);
            return run.status;
          },
          { timeout: 220_000, intervals: [500, 1000] },
        )
        .toBe("succeeded");
      const run = await read<ResearchRunDetail>(
        request,
        auth,
        `/research-runs/${submitted.id}`,
      );
      expect(run.config).toEqual(body);
      expect(run.dataset_version_id).toBe(snapshot.id);
      expect(run.attempts.every((a) => a.sandbox_stopped)).toBe(true);
      expect(run.research_image_digest).toMatch(/^sha256:[0-9a-f]{64}$/);
      const candidates = await rows<CandidatePublic>(
        request,
        auth,
        `/research-runs/${run.id}/candidates`,
      );
      expect(candidates.map((c) => c.kind).sort()).toEqual([
        "generated",
        "seed",
      ]);
      const calls = await rows<ModelCallPublic>(
        request,
        auth,
        `/research-runs/${run.id}/model-calls`,
      );
      expect(calls.length).toBeGreaterThan(0);
      expect(
        calls.every(
          (c) => c.provider === "fixture" && c.status === "completed",
        ),
      ).toBe(true);
      const checkpoints = await rows<CheckpointPublic>(
        request,
        auth,
        `/research-runs/${run.id}/checkpoints`,
      );
      expect(
        checkpoints.some(
          (c) =>
            c.id === run.latest_checkpoint_id &&
            c.complete &&
            c.next_stage === "finished",
        ),
      ).toBe(true);
      const children = await rows<EvaluationPublic>(
        request,
        auth,
        `/research-runs/${run.id}/evaluations`,
      );
      expect(children).toHaveLength(4);
      expect(
        children.every(
          (e) =>
            e.sandbox_stopped &&
            e.status === "succeeded" &&
            ["train", "validation"].includes(e.split),
        ),
      ).toBe(true);
      const exposure = await read<{
        matching_count: number;
        classification: string;
      }>(request, auth, `/split-versions/${run.split_version_id}/exposure`);
      expect(exposure.matching_count).toBe(0);
      expect(exposure.classification).toBe("no_tracked_overlap");
      const evaluated = children.find((e) => e.split === "validation")!;
      const resultPath = `/research-evaluations/${evaluated.id}/results?sections=equity&sections=drawdown&max_points=600`;
      const result = await read<ResultQueryPublic>(request, auth, resultPath);
      expect(result.tables.equity?.rows.length).toBeGreaterThan(0);
      await page.goto(`/backtests/${evaluated.id}`);
      await expect(page.getByTestId("result-evidence")).toContainText(
        "Synthetic",
      );
      for (const name of ["Equity", "Drawdown"])
        await expect(
          page
            .getByRole("img", { name: new RegExp(`^${name} \\(`) })
            .locator("svg"),
        ).toBeVisible();
      await page.reload();
      for (const name of ["Equity", "Drawdown"])
        await expect(
          page
            .getByRole("img", { name: new RegExp(`^${name} \\(`) })
            .locator("svg"),
        ).toBeVisible();
      expect(await read(request, auth, resultPath)).toEqual(result);
      expect(
        (await read<DatasetPublic>(request, auth, `/datasets/${snapshot.id}`))
          .manifest,
      ).toEqual(originalManifest);
      // Test management uses the real data queue, but fixture success stays scoped.
      await page.goto("/data-sources");
      const card = page.getByRole("region", {
        name: `${scenario.provider} connection test`,
        exact: true,
      });
      await card
        .getByRole("button", { name: "Test connection", exact: true })
        .click();
      await expect(page).toHaveURL(/\/imports\/[0-9a-f-]{36}$/);
      const tested = await readyImport(
        request,
        auth,
        new URL(page.url()).pathname.split("/").at(-1)!,
      );
      expect(tested.kind).toBe("probe");
      expect(tested.dataset_version_id).toBeNull();
      expect(tested.progress.probe_receipt).toMatchObject({
        outcome: "fixture_passed",
        capability_status: "documented",
        evidence: "fixture",
      });
      await page.goto("/data-sources");
      await page.reload();
      await expect(
        page.getByTestId(`provider-test-${scenario.provider}`),
      ).toContainText("fixture_passed");
      if (scenario.provider === "yfinance") {
        await page.goto(`/datasets/${snapshot.id}`);
        const pending = page.waitForResponse(
          (r) =>
            new URL(r.url()).pathname ===
              `/api/v1/datasets/${snapshot.id}/yahoo-repair` &&
            r.request().method() === "POST",
        );
        await page
          .getByRole("button", {
            name: "Create Yahoo repair-derived snapshot",
            exact: true,
          })
          .click();
        const acceptedRepair = await pending;
        expect(acceptedRepair.status()).toBe(202);
        const repaired = await readyImport(
          request,
          auth,
          (await acceptedRepair.json()).id,
        );
        const derived = await read<DatasetPublic>(
          request,
          auth,
          `/datasets/${repaired.dataset_version_id}`,
        );
        expect(derived.id).not.toBe(snapshot.id);
        expect(derived.manifest.parent_version_id).toBe(snapshot.id);
        expect(derived.manifest.derivation).toMatchObject({
          dataset_version_id: snapshot.id,
          manifest_sha256: snapshot.content_sha256,
        });
        expect(
          (await read<DatasetPublic>(request, auth, `/datasets/${snapshot.id}`))
            .manifest,
        ).toEqual(originalManifest);
        await page.goto(`/datasets/${derived.id}`);
        await expect(
          page.getByRole("link", {
            name: "Open preserved parent version",
            exact: true,
          }),
        ).toHaveAttribute("href", `/datasets/${snapshot.id}`);
        await page.reload();
        await expect(
          page.getByRole("button", {
            name: "Create Yahoo repair-derived snapshot",
            exact: true,
          }),
        ).toHaveCount(0);
      }
    } finally {
      for (const id of ownedRuns) {
        const run = await read<ResearchRunDetail>(
          request,
          auth,
          `/research-runs/${id}`,
        );
        if (!terminal.has(run.status))
          expect(
            (
              await request.post(api(`/research-runs/${id}/cancel`), {
                headers: authorization(auth),
              })
            ).status(),
          ).toBe(202);
        await expect
          .poll(
            async () => {
              const current = await read<ResearchRunDetail>(
                request,
                auth,
                `/research-runs/${id}`,
              );
              const children = await rows<EvaluationPublic>(
                request,
                auth,
                `/research-runs/${id}/evaluations`,
              );
              return (
                terminal.has(current.status) &&
                current.attempts.every((a) => a.sandbox_stopped) &&
                children.every((e) => e.sandbox_stopped)
              );
            },
            { timeout: 45_000 },
          )
          .toBe(true);
      }
    }
  });
