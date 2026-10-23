import { exerciseModeExplorer } from "./exploration-modes";
import { createHash } from "node:crypto";
import { expect, type APIRequestContext, type Page } from "@playwright/test";
import type {
  CheckpointDetail,
  FinalSelectionPublic,
  PortableFinalEvaluationDetail,
  PortableFinalEvaluationPublic,
  PortableFinalResultQuery,
  PortableRunDetail,
  StrategyPackagePublic,
  Token,
} from "../../src/client/types.gen";
import { api, authorization } from "./helpers";

// PREPARED ONLY. Invoked by the real CN/US portable research scenario. Every POST
// reaches the actual API/DB/worker; interceptions only lose committed responses.
// Requires both pinned Docker images. Collection is not browser acceptance.
const terminal = new Set(["succeeded", "failed", "interrupted", "cancelled"]);
async function read<T>(
  request: APIRequestContext,
  auth: Token,
  path: string,
): Promise<T> {
  const response = await request.get(api(path), {
    headers: authorization(auth),
  });
  expect(response.status(), path).toBe(200);
  return response.json() as Promise<T>;
}
const finalPath = (id: string) => `/portable-final-evaluations/${id}`;
const post =
  (path: string) => (response: import("@playwright/test").Response) =>
    new URL(response.url()).pathname === `/api/v1${path}` &&
    response.request().method() === "POST";
const sha = (bytes: Buffer) => createHash("sha256").update(bytes).digest("hex");
async function downloaded(page: Page, label: string) {
  const next = page.waitForEvent("download");
  await page.getByRole("button", { name: label, exact: true }).click();
  const download = await next;
  expect(await download.failure()).toBeNull();
  const stream = await download.createReadStream();
  expect(stream).not.toBeNull();
  const parts: Buffer[] = [];
  for await (const chunk of stream!) parts.push(Buffer.from(chunk));
  return Buffer.concat(parts);
}
export async function exercisePortableFinal({
  page,
  request,
  auth,
  other,
  run,
  owned,
}: {
  page: Page;
  request: APIRequestContext;
  auth: Token;
  other: Token;
  run: PortableRunDetail;
  owned: Set<string>;
}) {
  expect(run.status).toBe("succeeded");
  expect(run.config.windows.holdout).not.toBeNull();
  const checkpoint = await read<CheckpointDetail>(
    request,
    auth,
    `/portable-checkpoints/${run.latest_checkpoint_id}`,
  );
  const candidates = Object.values(
    checkpoint.summary.candidates as Record<
      string,
      { id: string; valid: boolean; complete: boolean }
    >,
  ).filter((row) => row.valid && row.complete);
  expect(candidates.length).toBeGreaterThan(0);
  // Explicitly choose a recorded candidate, preferably not automatic-best. The UI
  // must start with no selection and cannot silently pick the highest score.
  const chosen =
    candidates.find((row) => row.id !== run.result?.best_candidate_id) ||
    candidates[0];
  const callsBefore = await read<{ count: number }>(
    request,
    auth,
    `/portable-runs/${run.id}/model-calls`,
  );
  await page.goto(`/portable/runs/${run.id}`);
  const picker = page.getByLabel("Exact valid portable candidate", {
    exact: true,
  });
  await expect(picker).toHaveValue("");
  await expect(
    page.getByRole("button", {
      name: "Freeze selected portable candidate",
      exact: true,
    }),
  ).toBeDisabled();
  await picker.selectOption(chosen.id);
  await page
    .getByRole("checkbox", { name: /I reviewed this exact candidate/ })
    .check();
  let frozen: FinalSelectionPublic | undefined;
  let freezes = 0;
  await page.route("**/api/v1/final-selections", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    freezes += 1;
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    frozen = (await response.json()) as FinalSelectionPublic;
    await route.abort("failed");
  });
  await page
    .getByRole("button", {
      name: "Freeze selected portable candidate",
      exact: true,
    })
    .evaluate((button) => {
      (button as HTMLButtonElement).click();
      (button as HTMLButtonElement).click();
    });
  await expect(
    page.getByRole("button", {
      name: "Retry same freeze request",
      exact: true,
    }),
  ).toBeVisible();
  expect(freezes).toBe(1);
  expect(frozen?.source_candidate_id).toBe(chosen.id);
  expect(frozen?.source_checkpoint_id).toBe(checkpoint.id);
  await page.unroute("**/api/v1/final-selections");
  await page.reload();
  await page
    .getByRole("link", { name: "Open frozen selection", exact: true })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`/portable/final-selections/${frozen!.id}$`),
  );
  await expect(page.getByTestId("portable-final-trust")).toContainText(
    "no optimizer, model calls",
  );
  await expect(
    page
      .getByText("Historical tracking incomplete/external use unknown", {
        exact: false,
      })
      .first(),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "Submit portable final holdout test",
      exact: true,
    }),
  ).toBeDisabled();
  await page
    .getByRole("checkbox", { name: /I confirm running the frozen candidate/ })
    .check();
  const preflightResponse = page.waitForResponse(
    post("/portable-final-evaluations/preflight"),
  );
  await page
    .getByRole("button", { name: "Run portable final preflight", exact: true })
    .click();
  expect((await preflightResponse).status()).toBe(200);
  await expect(
    page.getByText("Portable final preflight passed", { exact: false }),
  ).toBeVisible();
  let firstCreate = true,
    accepted: PortableFinalEvaluationPublic | undefined;
  const submissions: { key: string | undefined; body: unknown }[] = [];
  await page.route("**/api/v1/portable-final-evaluations", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    submissions.push({
      key: route.request().headers()["idempotency-key"],
      body: route.request().postDataJSON(),
    });
    if (!firstCreate) return route.continue();
    firstCreate = false;
    const response = await route.fetch();
    expect(response.status()).toBe(202);
    accepted = (await response.json()) as PortableFinalEvaluationPublic;
    owned.add(accepted.id);
    // Exercise real cancellation before publishing the lost admission response.
    // No request/result is mocked and no process runs outside the dual-image worker.
    const cancelled = await request.post(
      api(`${finalPath(accepted.id)}/cancel`),
      { headers: authorization(auth) },
    );
    expect(cancelled.status()).toBe(202);
    await route.abort("failed");
  });
  await page
    .getByRole("button", {
      name: "Submit portable final holdout test",
      exact: true,
    })
    .evaluate((button) => {
      (button as HTMLButtonElement).click();
      (button as HTMLButtonElement).click();
    });
  await expect(
    page.getByRole("button", {
      name: "Retry same portable final submission",
      exact: true,
    }),
  ).toBeVisible();
  expect(submissions).toHaveLength(1);
  await page.reload();
  await expect(
    page.getByRole("button", {
      name: "Retry same portable final submission",
      exact: true,
    }),
  ).toBeDisabled();
  await expect(
    page.getByRole("checkbox", {
      name: /I confirm running the frozen candidate/,
    }),
  ).not.toBeChecked();
  await page
    .getByRole("checkbox", { name: /I confirm running the frozen candidate/ })
    .check();
  await page
    .getByRole("button", {
      name: "Retry same portable final submission",
      exact: true,
    })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`/portable/final-tests/${accepted!.id}$`),
  );
  expect(submissions).toHaveLength(2);
  expect(submissions[1]).toEqual(submissions[0]);
  await page.unroute("**/api/v1/portable-final-evaluations");
  const id = accepted!.id;
  await expect
    .poll(
      async () => {
        const job = await read<PortableFinalEvaluationDetail>(
          request,
          auth,
          finalPath(id),
        );
        return (
          terminal.has(job.status) &&
          job.attempts.every((item) => item.sandbox_stopped)
        );
      },
      { timeout: 60000, intervals: [250, 500, 1000] },
    )
    .toBe(true);
  const cancelled = await read<PortableFinalEvaluationDetail>(
    request,
    auth,
    finalPath(id),
  );
  expect(["failed", "cancelled", "interrupted"]).toContain(cancelled.status);
  await page.reload();
  await expect(
    page.getByRole("button", {
      name: "Retry complete final test",
      exact: true,
    }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Pause final test", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("checkbox", {
      name: /Confirm retry of the unchanged frozen holdout/,
    })
    .check();
  const retryRequests: string[] = [];
  await page.route(
    `**/api/v1/portable-final-evaluations/${id}/retry`,
    async (route) => {
      retryRequests.push(route.request().headers()["idempotency-key"]);
      if (retryRequests.length > 1) return route.continue();
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      await route.abort("failed");
    },
  );
  await page
    .getByRole("button", { name: "Retry complete final test", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Recover same retry request",
      exact: true,
    }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("button", {
      name: "Recover same retry request",
      exact: true,
    }),
  ).toBeDisabled();
  await page
    .getByRole("checkbox", {
      name: /Confirm retry of the unchanged frozen holdout/,
    })
    .check();
  await page
    .getByRole("button", { name: "Recover same retry request", exact: true })
    .click();
  await expect.poll(() => retryRequests.length).toBe(2);
  expect(retryRequests[0]).toBe(retryRequests[1]);
  expect(retryRequests[0]).not.toBe(submissions[0].key);
  await page.unroute(`**/api/v1/portable-final-evaluations/${id}/retry`);
  await expect
    .poll(
      async () => {
        const job = await read<PortableFinalEvaluationDetail>(
          request,
          auth,
          finalPath(id),
        );
        if (terminal.has(job.status) && job.status !== "succeeded")
          throw Error(`Portable final retry ${job.status}: ${job.error}`);
        return (
          job.status === "succeeded" &&
          job.attempts.every((item) => item.sandbox_stopped)
        );
      },
      { timeout: 180000, intervals: [250, 500, 1000] },
    )
    .toBe(true);
  const final = await read<PortableFinalEvaluationDetail>(
    request,
    auth,
    finalPath(id),
  );
  expect(final.generation).toBe(cancelled.generation + 1);
  expect(final.config.source_config).toEqual(run.config);
  expect(final.config.holdout).toEqual(run.config.windows.holdout);
  expect(final.config.selected_strategy.sha256).toBe(
    frozen!.source_code_sha256,
  );
  expect(final.research_image_digest).toBe(run.research_image_digest);
  expect(final.portable_policy_image_digest).toBe(
    run.portable_policy_image_digest,
  );
  const evaluationId = final.result!.evaluation_id!;
  const result = await read<PortableFinalResultQuery>(
    request,
    auth,
    `/portable-final-evaluations/evaluations/${evaluationId}/result?section=decision_trace&limit=50`,
  );
  expect(result.schema_version).toBe(
    "tradegrad.portable.final-result-query.v1",
  );
  expect(result.selection_feedback).toBe(false);
  if (run.config.benchmark) {
    expect(result.benchmark?.status).toBe("available");
    await expect(page.getByTestId("benchmark-result")).toContainText(
      "not total return",
    );
  }
  await exerciseModeExplorer(page, request, auth, other, "portable_final", {
    evaluation_id: result.evaluation_id,
    artifact_sha256: result.artifact_sha256,
    selection_id: result.selection_id,
    split: "holdout",
    window: {
      start: String(result.window.start),
      end: String(result.window.end),
    },
  });
  expect(result.runtime_certified).toBe(false);
  expect(result.result_trust).toBe("isolated_policy_trusted_akquant_engine");
  expect(result.ledger_origin).toBe("trusted_engine");
  expect(result.image_digest).toBe(run.research_image_digest);
  expect(result.policy_image_digest).toBe(run.portable_policy_image_digest);
  expect(result.rows.length).toBeGreaterThan(0);
  await page.reload();
  await expect(page.getByTestId("portable-final-data-evidence")).toContainText(
    "Explicit synthetic raw/no-actions fixture",
  );
  await expect(
    page.getByRole("region", { name: "Portable decision trace", exact: true }),
  ).toContainText("state before sha256");
  const original = await downloaded(
    page,
    "Download checksum-verified portable final JSON",
  );
  expect(sha(original)).toBe(result.artifact_sha256);
  await page
    .getByRole("button", { name: "Show persisted account charts", exact: true })
    .click();
  await expect(
    page.getByRole("img", { name: /Portable equity and cash/ }),
  ).toBeVisible();
  const packageResponse = page.waitForResponse(
    post(`/final-selections/${frozen!.id}/packages`),
  );
  await page
    .getByRole("button", {
      name: "Create reference-only strategy package",
      exact: true,
    })
    .click();
  const exported = await packageResponse;
  expect(exported.status()).toBe(201);
  const pkg = (await exported.json()) as StrategyPackagePublic;
  await expect(page).toHaveURL(new RegExp(`/strategy-packages/${pkg.id}$`));
  expect(
    sha(await downloaded(page, "Download authenticated strategy ZIP")),
  ).toBe(pkg.archive_sha256);
  for (const path of [
    finalPath(id),
    `${finalPath(id)}/evaluations`,
    `/runs/${id}/events?follow=false`,
    `/portable-final-evaluations/evaluations/${evaluationId}/result`,
    `/portable-final-evaluations/evaluations/${evaluationId}/result/download`,
    `/final-selections/${frozen!.id}`,
    `/strategy-packages/${pkg.id}`,
    `/strategy-packages/${pkg.id}/download`,
  ]) {
    expect(
      (
        await request.get(api(path), { headers: authorization(other) })
      ).status(),
      path,
    ).toBe(404);
  }
  const sourceAfter = await read<PortableRunDetail>(
    request,
    auth,
    `/portable-runs/${run.id}`,
  );
  expect(sourceAfter.latest_checkpoint_id).toBe(run.latest_checkpoint_id);
  expect(sourceAfter.result).toEqual(run.result);
  expect(
    (
      await read<{ count: number }>(
        request,
        auth,
        `/portable-runs/${run.id}/model-calls`,
      )
    ).count,
  ).toBe(callsBefore.count);
  expect(
    await read<FinalSelectionPublic>(
      request,
      auth,
      `/final-selections/${frozen!.id}`,
    ),
  ).toEqual(frozen);
}
export async function cleanupPortableFinalJobs(
  request: APIRequestContext,
  auth: Token,
  owned: Set<string>,
) {
  const failures: string[] = [];
  for (const id of owned) {
    try {
      const job = await read<PortableFinalEvaluationDetail>(
        request,
        auth,
        finalPath(id),
      );
      if (!terminal.has(job.status))
        expect(
          (
            await request.post(api(`${finalPath(id)}/cancel`), {
              headers: authorization(auth),
            })
          ).status(),
        ).toBe(202);
      await expect
        .poll(
          async () => {
            const item = await read<PortableFinalEvaluationDetail>(
              request,
              auth,
              finalPath(id),
            );
            return (
              terminal.has(item.status) &&
              item.attempts.every((attempt) => attempt.sandbox_stopped)
            );
          },
          { timeout: 60000, intervals: [250, 500, 1000] },
        )
        .toBe(true);
    } catch {
      failures.push(id);
    }
  }
  return failures;
}
