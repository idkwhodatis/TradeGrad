import { exerciseCandidateQueries } from "./named-list-queries";
import { exerciseModeExplorer } from "./exploration-modes";
import { createHash } from "node:crypto";
import { test, expect, type APIRequestContext } from "@playwright/test";
import type {
  CoveragePublic,
  DatasetPublic,
  ImportDetail,
  ImportPublic,
  PortableEvaluationPublic,
  PortablePreflightPublic,
  PortableResultQuery,
  PortableRunCreate,
  PortableRunDetail,
  PortableRunPublic,
  Token,
  UniversePublic,
} from "../../src/client/types.gen";
import { account, api, authorization, signIn, token } from "./helpers";
import {
  cleanupPortableFinalJobs,
  exercisePortableFinal,
} from "./portable-final-workflow";

// PREPARED, NOT LOCALLY EXECUTED: real PostgreSQL + migrations, data worker,
// research worker and BOTH actual portable Docker images are mandatory.
// No mock API, fake browser result, host policy execution, paid model or live provider.
// The only interception forwards POST to the REAL API then deliberately loses its
// response. --list collects without launching a browser or certifying acceptance.
const terminal = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "paused",
  "interrupted",
]);
async function read<T>(
  request: APIRequestContext,
  auth: Token,
  path: string,
): Promise<T> {
  const response = await request.get(api(path), {
    headers: authorization(auth),
  });
  expect(response.status(), `GET ${path}`).toBe(200);
  return response.json() as Promise<T>;
}
async function pages<T>(request: APIRequestContext, auth: Token, path: string) {
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
const detail = (request: APIRequestContext, auth: Token, id: string) =>
  read<PortableRunDetail>(request, auth, `/portable-runs/${id}`);
async function syntheticInputs(
  request: APIRequestContext,
  auth: Token,
  market: "CN" | "US",
  imports: Set<string>,
) {
  const response = await request.post(api("/data/imports"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    data: {
      name: `E2E explicit portable synthetic ${market} ${crypto.randomUUID()}`,
      provider: "portable_fixture",
      market,
      symbols: market === "CN" ? ["600000", "000001"] : ["AAPL"],
      start_date: "2024-01-02",
      end_date: "2024-05-01",
      price_mode: "raw",
    },
  });
  expect(response.status()).toBe(202);
  const job = (await response.json()) as ImportPublic;
  imports.add(job.id);
  await expect
    .poll(
      async () => {
        const result = await read<ImportDetail>(
          request,
          auth,
          `/data/imports/${job.id}`,
        );
        if (["failed", "partial", "cancelled"].includes(result.status))
          throw Error(
            `Real synthetic data worker failed: ${result.status}: ${result.error}`,
          );
        return result.status;
      },
      { timeout: 120000, intervals: [250, 500, 1000] },
    )
    .toBe("ready");
  const ready = await read<ImportDetail>(
    request,
    auth,
    `/data/imports/${job.id}`,
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
  const published = await request.post(api("/data/universes"), {
    headers: authorization(auth),
    data: {
      name: `E2E user-fixed portable basket ${crypto.randomUUID()}`,
      state: "user_fixed",
      as_of: new Date().toISOString(),
      members: coverage.universe.members,
      evidence: [],
    },
  });
  expect(published.status()).toBe(201);
  return { dataset, universe: (await published.json()) as UniversePublic };
}

test("real portable CN/US research, frozen final retry and package flows preserve identity, lost-response recovery and owner isolation", async ({
  page,
  request,
}) => {
  test.setTimeout(1200000);
  account();
  account(true);
  const ready = await request.get(api("/health/ready"));
  expect(
    ready.ok(),
    "Use the real migrated stack, never an in-memory substitute",
  ).toBe(true);
  const auth = await token(request),
    other = await token(request, true);
  const owned = new Set<string>();
  const finalJobs = new Set<string>();
  const imports = new Set<string>();
  try {
    await signIn(page);
    for (const [market, kind] of [
      ["CN", "portfolio"],
      ["US", "event"],
    ] as const) {
      const { dataset, universe } = await syntheticInputs(
        request,
        auth,
        market,
        imports,
      );
      const benchmarkInput =
        market === "US"
          ? await syntheticInputs(request, auth, market, imports)
          : null;
      await page.goto("/portable/new");
      const change = page.getByRole("button", { name: "Change pinned inputs" });
      if (await change.isVisible()) await change.click();
      await expect(page.getByTestId("portable-trust")).toContainText(
        "interface_only",
      );
      await page.getByLabel("Market", { exact: true }).selectOption(market);
      await page
        .getByLabel("Portable template", { exact: true })
        .selectOption(kind);
      await page
        .getByLabel("Published dataset version")
        .selectOption(dataset.id);
      await page.getByLabel("Exact universe version").selectOption(universe.id);
      const seedResponse = page.waitForResponse(
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
      expect((await seedResponse).status()).toBe(201);
      await page
        .getByRole("button", {
          name: "Load pinned portable configuration",
          exact: true,
        })
        .click();
      await page
        .getByLabel("train start · inclusive", { exact: true })
        .fill("2024-01-08");
      await page
        .getByLabel("train end · exclusive", { exact: true })
        .fill("2024-01-15");
      await page
        .getByLabel("validation start · inclusive", { exact: true })
        .fill("2024-01-15");
      await page
        .getByLabel("validation end · exclusive", { exact: true })
        .fill("2024-01-22");
      await page
        .getByText("Metadata-only holdout", { exact: true })
        .locator("input")
        .check();
      await page
        .getByLabel("holdout start · inclusive", { exact: true })
        .fill("2024-01-22");
      await page
        .getByLabel("holdout end · exclusive", { exact: true })
        .fill("2024-01-29");
      await page.getByLabel("Candidate sample size", { exact: true }).fill("1");
      await page.getByLabel("Maximum iterations", { exact: true }).fill("1");
      await page
        .getByLabel("Maximum runtime · seconds", { exact: true })
        .fill("180");
      if (benchmarkInput) {
        const listing = (
          benchmarkInput.dataset.manifest.listings as { listing_id: string }[]
        )[0];
        await page
          .getByLabel("Benchmark snapshot and listing (optional)")
          .selectOption(`${benchmarkInput.dataset.id}:${listing.listing_id}`);
      }
      const check = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname ===
          "/api/v1/portable-runs/preflight",
      );
      await page
        .getByRole("button", {
          name: "Run strict portable preflight",
          exact: true,
        })
        .click();
      const preflight = await check;
      expect(preflight.status()).toBe(200);
      const checked = (await preflight.json()) as PortablePreflightPublic;
      expect(checked.holdout_execution).toBe(false);
      expect(checked.runtime_certified).toBe(false);
      expect(
        checked.evaluation_windows.every((row) => row.split !== "holdout"),
      ).toBe(true);
      let first = true,
        acceptedId: string | undefined;
      const sent: { key: string | undefined; body: PortableRunCreate }[] = [];
      await page.route("**/api/v1/portable-runs", async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        sent.push({
          key: route.request().headers()["idempotency-key"],
          body: route.request().postDataJSON() as PortableRunCreate,
        });
        if (!first) return route.continue();
        first = false;
        const response = await route.fetch();
        expect(response.status()).toBe(202);
        const accepted = (await response.json()) as PortableRunPublic;
        acceptedId = accepted.id;
        owned.add(accepted.id);
        await route.abort("failed");
      });
      await page
        .getByRole("button", {
          name: "Submit portable experiment",
          exact: true,
        })
        .click();
      await expect(
        page.getByRole("button", {
          name: "Retry same portable submission",
          exact: true,
        }),
      ).toBeVisible();
      await page.reload();
      await expect(
        page.getByRole("button", {
          name: "Retry same portable submission",
          exact: true,
        }),
      ).toBeDisabled();
      await page
        .getByRole("button", {
          name: "Run strict portable preflight",
          exact: true,
        })
        .click();
      await expect(page.getByTestId("portable-preflight")).toContainText(
        "preflight passed",
      );
      await page
        .getByRole("button", {
          name: "Retry same portable submission",
          exact: true,
        })
        .click();
      await expect(page).toHaveURL(/\/portable\/runs\/[a-f0-9-]+$/);
      await page.unroute("**/api/v1/portable-runs");
      expect(sent).toHaveLength(2);
      expect(sent[1]).toEqual(sent[0]);
      expect(acceptedId).toBeTruthy();
      const id = page.url().split("/").pop()!;
      expect(id).toBe(acceptedId);
      await expect
        .poll(
          async () => {
            const run = await detail(request, auth, id);
            if (terminal.has(run.status) && run.status !== "succeeded")
              throw Error(
                `Real portable run ended ${run.status}: ${run.error}`,
              );
            return run.status;
          },
          { timeout: 240000, intervals: [250, 500, 1000] },
        )
        .toBe("succeeded");
      const run = await detail(request, auth, id);
      expect(run.attempts.every((attempt) => attempt.sandbox_stopped)).toBe(
        true,
      );
      expect(run.research_image_digest).toMatch(/^sha256:[a-f0-9]{64}$/);
      expect(run.portable_policy_image_digest).toMatch(/^sha256:[a-f0-9]{64}$/);
      expect(run.result?.runtime_certified).toBe(false);
      expect(run.result?.holdout_executed).toBe(false);
      await exerciseCandidateQueries(page, id, true);
      const evaluations = await pages<PortableEvaluationPublic>(
        request,
        auth,
        `/portable-runs/${id}/evaluations`,
      );
      expect(evaluations.length).toBeGreaterThan(0);
      expect(
        evaluations.every(
          (item) =>
            item.sandbox_stopped &&
            item.finished_at !== null &&
            item.status === "succeeded",
        ),
      ).toBe(true);
      expect(
        evaluations.every((item) =>
          ["train", "validation"].includes(item.split),
        ),
      ).toBe(true);
      const evaluation = evaluations[0];
      const result = await read<PortableResultQuery>(
        request,
        auth,
        `/portable-evaluations/${evaluation.id}/results?section=decision_trace&limit=50`,
      );
      expect(result.result_trust).toBe(
        "isolated_policy_trusted_akquant_engine",
      );
      expect(result.ledger_origin).toBe("trusted_engine");
      expect(result.is_synthetic).toBe(true);
      if (benchmarkInput) expect(result.benchmark?.status).toBe("available");
      expect(result.rows.length).toBeGreaterThan(0);
      expect(result.rows[0]).toHaveProperty("state_before_sha256");
      expect(result.rows[0]).toHaveProperty("state_after_sha256");
      expect(result.image_digest).toBe(run.research_image_digest);
      expect(result.policy_image_digest).toBe(run.portable_policy_image_digest);
      await page.goto(`/portable/evaluations/${evaluation.id}`);
      await exerciseModeExplorer(
        page,
        request,
        auth,
        other,
        "portable_research",
        {
          evaluation_id: result.evaluation_id,
          artifact_sha256: result.artifact_sha256,
          window: {
            start: String(result.window.start),
            end: String(result.window.end),
          },
        },
      );
      if (benchmarkInput) {
        await expect(page.getByTestId("benchmark-result")).toContainText(
          "not total return",
        );
        await expect(
          page
            .getByRole("img", {
              name: /Observed strategy return versus benchmark close-price return/,
            })
            .locator("svg"),
        ).toBeVisible();
        await page.reload();
        await expect(page.getByTestId("benchmark-result")).toContainText(
          "daily_close_observed_anchor.v1",
        );
      }
      await expect(
        page.getByText("Explicit synthetic raw/no-actions fixture.", {
          exact: false,
        }),
      ).toBeVisible();
      await expect(
        page.getByRole("region", {
          name: "Portable decision trace",
          exact: true,
        }),
      ).toContainText("warmup");
      await page
        .getByRole("button", {
          name: "Show persisted account charts",
          exact: true,
        })
        .click();
      await expect(
        page.getByRole("img", { name: /Portable equity and cash/ }),
      ).toBeVisible();
      const download = await request.get(
        api(`/portable-evaluations/${evaluation.id}/results/download`),
        { headers: authorization(auth) },
      );
      expect(download.status()).toBe(200);
      expect(
        createHash("sha256")
          .update(await download.body())
          .digest("hex"),
      ).toBe(result.artifact_sha256);
      for (const path of [
        `/portable-runs/${id}`,
        `/portable-runs/${id}/candidates`,
        `/portable-evaluations/${evaluation.id}/results`,
        `/portable-evaluations/${evaluation.id}/results/download`,
      ]) {
        const denied = await request.get(api(path), {
          headers: authorization(other),
        });
        expect(denied.status()).toBe(404);
      }
      await exercisePortableFinal({
        page,
        request,
        auth,
        other,
        run,
        owned: finalJobs,
      });
    }
  } finally {
    const failures = await cleanupPortableFinalJobs(request, auth, finalJobs);
    for (const id of owned) {
      try {
        const run = await detail(request, auth, id);
        if (!terminal.has(run.status) || run.status === "paused") {
          const cancel = await request.post(
            api(`/portable-runs/${id}/cancel`),
            { headers: authorization(auth) },
          );
          expect(cancel.status()).toBe(202);
        }
        await expect
          .poll(
            async () => {
              const run = await detail(request, auth, id);
              const evaluations = await pages<PortableEvaluationPublic>(
                request,
                auth,
                `/portable-runs/${id}/evaluations`,
              );
              return (
                terminal.has(run.status) &&
                run.attempts.every((item) => item.sandbox_stopped) &&
                evaluations.every(
                  (item) => item.sandbox_stopped && item.finished_at !== null,
                )
              );
            },
            { timeout: 45000, intervals: [250, 500, 1000] },
          )
          .toBe(true);
      } catch {
        failures.push(id);
      }
    }
    for (const id of imports) {
      try {
        const job = await read<ImportDetail>(
          request,
          auth,
          `/data/imports/${id}`,
        );
        if (!["ready", "partial", "failed", "cancelled"].includes(job.status)) {
          const cancel = await request.post(api(`/data/imports/${id}/cancel`), {
            headers: authorization(auth),
          });
          expect(cancel.status()).toBe(202);
          await expect
            .poll(
              async () =>
                (await read<ImportDetail>(request, auth, `/data/imports/${id}`))
                  .status,
              { timeout: 45000, intervals: [250, 500, 1000] },
            )
            .toMatch(/^(ready|partial|failed|cancelled)$/);
        }
      } catch {
        failures.push(`import:${id}`);
      }
    }
    expect(
      failures,
      "Only this scenario's accepted runs are cleaned up; both physical siblings must stop",
    ).toEqual([]);
  }
});

// PREPARED ONLY. The hosted launcher must provision its synthetic loopback model
// profile. This seed-only browser case never sends a model request; the real SDK
// request/replay/failure gates live in test_portable_operator_pipeline.py.
test("@portable-operator-profile real profile selection, reload, preflight and seed-only run", async ({
  page,
  request,
}) => {
  test.skip(
    process.env.E2E_MODEL_PROFILES !== "1" || !process.env.E2E_MODEL_PROFILE_ID,
    "Requires an explicitly provisioned hosted synthetic model profile",
  );
  test.setTimeout(300000);
  account();
  const auth = await token(request);
  const imports = new Set<string>();
  let runId: string | null = null;
  try {
    const capabilities = await read<
      import("../../src/client/types.gen").ResearchCapabilities
    >(request, auth, "/research-capabilities");
    const profile = capabilities.model_profiles?.find(
      (row) => row.id === process.env.E2E_MODEL_PROFILE_ID,
    );
    expect(
      profile?.synthetic,
      "Never use a real paid model in this browser gate",
    ).toBe(true);
    expect(profile?.strategy_modes).toContain("portable");
    const { dataset, universe } = await syntheticInputs(
      request,
      auth,
      "US",
      imports,
    );
    await signIn(page);
    await page.goto("/portable/new");
    const change = page.getByRole("button", { name: "Change pinned inputs" });
    if (await change.isVisible()) await change.click();
    await page.getByLabel("Market", { exact: true }).selectOption("US");
    await page
      .getByLabel("Portable template", { exact: true })
      .selectOption("event");
    await page.getByLabel("Published dataset version").selectOption(dataset.id);
    await page.getByLabel("Exact universe version").selectOption(universe.id);
    const seed = page.waitForResponse(
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
    expect((await seed).status()).toBe(201);
    await page
      .getByRole("button", {
        name: "Load pinned portable configuration",
        exact: true,
      })
      .click();
    await expect(page.getByLabel("Model profile", { exact: true })).toHaveValue(
      "fixture",
    );
    await page
      .getByLabel("Model profile", { exact: true })
      .selectOption(profile!.id);
    await expect(page.getByTestId("portable-model-notice")).toContainText(
      "Synthetic operator HTTP profile",
    );
    await page
      .getByLabel("Execution mode", { exact: true })
      .selectOption("seed_evaluation");
    await page
      .getByLabel("train start · inclusive", { exact: true })
      .fill("2024-01-08");
    await page
      .getByLabel("train end · exclusive", { exact: true })
      .fill("2024-01-15");
    await page
      .getByLabel("validation start · inclusive", { exact: true })
      .fill("2024-01-15");
    await page
      .getByLabel("validation end · exclusive", { exact: true })
      .fill("2024-01-22");
    await page.reload();
    await expect(page.getByLabel("Model profile", { exact: true })).toHaveValue(
      profile!.id,
    );
    await expect(
      page.getByRole("button", {
        name: "Submit portable experiment",
        exact: true,
      }),
    ).toBeDisabled();
    const preflight = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/v1/portable-runs/preflight",
    );
    await page
      .getByRole("button", {
        name: "Run strict portable preflight",
        exact: true,
      })
      .click();
    expect((await preflight).status()).toBe(200);
    const submitted = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/v1/portable-runs" &&
        response.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Submit portable experiment", exact: true })
      .click();
    const response = await submitted;
    expect(response.status()).toBe(202);
    const run = (await response.json()) as PortableRunPublic;
    runId = run.id;
    expect(run.config.model).toEqual(profile!.config);
    await expect
      .poll(async () => (await detail(request, auth, run.id)).status, {
        timeout: 150000,
      })
      .toBe("succeeded");
    await page.reload();
    await expect(
      page.getByText("Model calls and usage", { exact: true }),
    ).toBeVisible();
    const calls = await pages(
      request,
      auth,
      `/portable-runs/${run.id}/model-calls`,
    );
    expect(calls).toHaveLength(0);
    expect(
      (await detail(request, auth, run.id)).attempts.every(
        (row) => row.sandbox_stopped,
      ),
    ).toBe(true);
  } finally {
    if (runId) {
      const run = await detail(request, auth, runId);
      if (!terminal.has(run.status)) {
        expect(
          (
            await request.post(api(`/portable-runs/${runId}/cancel`), {
              headers: authorization(auth),
            })
          ).status(),
        ).toBe(202);
      }
      await expect
        .poll(
          async () => {
            const value = await detail(request, auth, runId!);
            return (
              terminal.has(value.status) &&
              value.attempts.every((row) => row.sandbox_stopped)
            );
          },
          { timeout: 45000 },
        )
        .toBe(true);
    }
    for (const id of imports) {
      const row = await read<ImportDetail>(
        request,
        auth,
        `/data/imports/${id}`,
      );
      if (!["ready", "partial", "failed", "cancelled"].includes(row.status)) {
        expect(
          (
            await request.post(api(`/data/imports/${id}/cancel`), {
              headers: authorization(auth),
            })
          ).status(),
        ).toBe(202);
        await expect
          .poll(
            async () =>
              (await read<ImportDetail>(request, auth, `/data/imports/${id}`))
                .status,
            { timeout: 45000 },
          )
          .toMatch(/^(ready|partial|failed|cancelled)$/);
      }
    }
  }
});
