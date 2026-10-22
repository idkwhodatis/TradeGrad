import { test, expect } from "@playwright/test";
import {
  account,
  api,
  authorization,
  create,
  inputs,
  signIn,
  token,
  waitForSuccess,
} from "./helpers";
import type { ArtifactPublic, RunPublic } from "../../src/client/types.gen";

test.beforeAll(async ({ request }) => {
  account();
  account(true);
  const ready = await request.get(api("/health/ready"));
  expect(
    ready.ok(),
    "A real migrated PostgreSQL API is required; do not replace it with a mock",
  ).toBe(true);
});

test("login rejects incorrect credentials, then succeeds and survives reload", async ({
  page,
}) => {
  await page.goto("/login");
  await page.getByLabel("Email address").fill(account().email);
  let rejectedPassword = crypto.randomUUID();
  while (rejectedPassword === account().password) {
    rejectedPassword = crypto.randomUUID();
  }
  await page.getByLabel("Password", { exact: true }).fill(rejectedPassword);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel("Password", { exact: true }).fill(account().password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Research overview" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Research overview" }),
  ).toBeVisible();
  await expect(page.getByText("Seed-only · no model calls")).toBeVisible();
});

test("repeated submit creates one run; durable events, result and artifact survive reload", async ({
  page,
  request,
}) => {
  await signIn(page);
  const auth = await token(request);
  const before = await request.get(api("/runs"), {
    headers: authorization(auth),
  });
  const beforeCount = (await before.json()).count as number;
  await page.getByRole("link", { name: "New experiment", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Start with a fixed seed" }),
  ).toBeVisible();
  let submissions = 0;
  page.on("request", (req) => {
    if (
      new URL(req.url()).pathname === "/api/v1/runs" &&
      req.method() === "POST"
    )
      submissions++;
  });
  // Two real DOM clicks in the same turn exercise the synchronous in-flight guard.
  await page
    .getByRole("button", { name: "Start evaluation", exact: true })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
      button.click();
    });
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  const id = new URL(page.url()).pathname.split("/").at(-1)!;
  expect(submissions).toBe(1);
  const after = await request.get(api("/runs"), {
    headers: authorization(auth),
  });
  expect((await after.json()).count).toBe(beforeCount + 1);
  await expect(
    page.getByRole("heading", { name: "Execution timeline" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Execution timeline" }),
  ).toBeVisible();
  await waitForSuccess(request, auth, id);
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded", {
    timeout: 20_000,
  });
  await expect(
    page.getByRole("heading", { name: "Evaluation results" }),
  ).toBeVisible();
  await expect(page.locator("[data-event-seq]").first()).toBeVisible();
  const sequences = await page
    .locator("[data-event-seq]")
    .evaluateAll((nodes) =>
      nodes.map((node) => Number(node.getAttribute("data-event-seq"))),
    );
  expect(sequences).toEqual([...new Set(sequences)].sort((a, b) => a - b));
  await page.getByText("Inspect stored result bundle").click();
  const result = await page.getByTestId("result-bundle").textContent();
  const artifactResponse = await request.get(api(`/runs/${id}/artifacts`), {
    headers: authorization(auth),
  });
  const artifacts = (await artifactResponse.json()).data as ArtifactPublic[];
  expect(artifacts.length).toBeGreaterThan(0);
  await expect(
    page.getByRole("button", {
      name: `Download ${artifacts[0].name}`,
      exact: true,
    }),
  ).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await page
    .getByRole("button", { name: `Download ${artifacts[0].name}`, exact: true })
    .click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(artifacts[0].name);
  expect(await download.failure()).toBeNull();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Evaluation results" }),
  ).toBeVisible();
  await page.getByText("Inspect stored result bundle").click();
  expect(await page.getByTestId("result-bundle").textContent()).toBe(result);
});

test("concurrent HTTP replay with one idempotency key returns one logical run", async ({
  request,
}) => {
  const auth = await token(request);
  const body = await inputs(request, auth);
  const key = crypto.randomUUID();
  const [first, second] = await Promise.all([
    create(request, auth, body, key),
    create(request, auth, body, key),
  ]);
  expect(first.id).toBe(second.id);
  const replay = await create(request, auth, body, key);
  expect(replay.id).toBe(first.id);
  const changed = await request.post(api("/runs"), {
    headers: { ...authorization(auth), "Idempotency-Key": key },
    data: { ...body, start_date: "2024-01-03" },
  });
  expect(changed.status()).toBe(409);
  await waitForSuccess(request, auth, first.id);
});

test("running cancel stops the sandbox; resume creates a new seed evaluation attempt", async ({
  page,
  request,
}) => {
  await signIn(page);
  const auth = await token(request);
  await page.goto("/runs/new");
  await page
    .getByLabel("Seed strategy")
    .selectOption({ label: "E2E slow cn_time seed (20s import delay)" });
  await page
    .getByRole("button", { name: "Start evaluation", exact: true })
    .click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  const id = new URL(page.url()).pathname.split("/").at(-1)!;
  await expect(page.getByTestId("run-status")).toHaveText("Running", {
    timeout: 30_000,
  });
  await page.getByRole("button", { name: "Cancel run", exact: true }).click();
  await expect(page.getByTestId("run-status")).toHaveText("Cancelled", {
    timeout: 30_000,
  });
  await expect(
    page.getByText("Sandbox confirmed stopped", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("Resume creates a new attempt", { exact: false }),
  ).toBeVisible();
  await page.reload();
  const before = await request.get(api(`/runs/${id}`), {
    headers: authorization(auth),
  });
  const original = (await before.json()) as RunPublic;
  await page.getByRole("button", { name: "Resume evaluation" }).click();
  await waitForSuccess(request, auth, id);
  await expect(page.getByTestId("run-status")).toHaveText("Succeeded", {
    timeout: 20_000,
  });
  const response = await request.get(api(`/runs/${id}`), {
    headers: authorization(auth),
  });
  const resumed = (await response.json()) as RunPublic;
  expect(resumed.generation).toBe(original.generation + 1);
});

test("failed execution stays failed after refresh and retry retains its evidence", async ({
  page,
  request,
}) => {
  await signIn(page);
  const auth = await token(request);
  await page.goto("/runs/new");
  await page
    .getByLabel("Seed strategy")
    .selectOption({ label: "E2E syntax-error cn_time seed" });
  await page
    .getByRole("button", { name: "Start evaluation", exact: true })
    .click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  const id = new URL(page.url()).pathname.split("/").at(-1)!;
  await expect(page.getByTestId("run-status")).toHaveText("Failed", {
    timeout: 30_000,
  });
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Evaluation results" }),
  ).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId("run-status")).toHaveText("Failed");
  const before = await request.get(api(`/runs/${id}`), {
    headers: authorization(auth),
  });
  const original = (await before.json()) as RunPublic;
  await page.getByRole("button", { name: "Resume evaluation" }).click();
  await expect
    .poll(
      async () =>
        (
          await (
            await request.get(api(`/runs/${id}`), {
              headers: authorization(auth),
            })
          ).json()
        ).generation,
    )
    .toBe(original.generation + 1);
  await expect(page.getByTestId("run-status")).toHaveText("Failed", {
    timeout: 30_000,
  });
  await expect(
    page.getByRole("heading", { name: `Generation ${original.generation}` }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: `Generation ${original.generation + 1}`,
    }),
  ).toBeVisible();
});

test("expired session returns to login without losing the direct run address", async ({
  page,
  request,
}) => {
  await page.clock.install();
  await signIn(page);
  const auth = await token(request);
  const run = await create(request, auth);
  await page.goto(`/runs/${run.id}`);
  await expect(
    page.getByRole("heading", { name: "Execution timeline" }),
  ).toBeVisible();
  const remaining = await page.evaluate(() => {
    const session = JSON.parse(
      sessionStorage.getItem("tradegrad.session.v1")!,
    ) as { expiresAt: number };
    return Math.max(0, session.expiresAt - Date.now());
  });
  await page.clock.fastForward(remaining + 100);
  await expect(
    page.getByText("Your session expired.", { exact: false }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel("Email address").fill(account().email);
  await page.getByLabel("Password", { exact: true }).fill(account().password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/runs/${run.id}$`));
});

test("other users cannot read run, data, strategy, events or artifacts", async ({
  page,
  request,
}) => {
  const owner = await token(request);
  const stranger = await token(request, true);
  const body = await inputs(request, owner);
  const run = await create(request, owner, body);
  await waitForSuccess(request, owner, run.id);
  const artifactsResponse = await request.get(
    api(`/runs/${run.id}/artifacts`),
    { headers: authorization(owner) },
  );
  const artifacts = (await artifactsResponse.json()).data as ArtifactPublic[];
  const paths = [
    `/runs/${run.id}`,
    `/runs/${run.id}/events?follow=false`,
    `/runs/${run.id}/artifacts`,
    `/datasets/${body.dataset_version_id}`,
    `/strategy-versions/${body.strategy_version_id}`,
    ...artifacts.map((item) => `/artifacts/${item.id}/download`),
  ];
  for (const path of paths) {
    const forbidden = await request.get(api(path), {
      headers: authorization(stranger),
    });
    expect(forbidden.status(), path).toBe(404);
  }
  for (const action of ["cancel", "resume"]) {
    const forbidden = await request.post(api(`/runs/${run.id}/${action}`), {
      headers: authorization(stranger),
    });
    expect(forbidden.status()).toBe(404);
  }
  await signIn(page, true);
  await page.goto(`/runs/${run.id}`);
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Evaluation results" }),
  ).toHaveCount(0);
});

test("new-run cancel/back navigation is non-mutating and narrow screens remain usable", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  const auth = await token(request);
  const before = (
    await (
      await request.get(api("/runs"), { headers: authorization(auth) })
    ).json()
  ).count;
  await page.goto("/runs/new");
  await page.getByLabel("Market & task").selectOption("us_cross");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page).toHaveURL(/\/runs$/);
  await page.goBack();
  await expect(
    page.getByRole("heading", { name: "Start with a fixed seed" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Start evaluation" }),
  ).toBeVisible();
  const noOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth,
  );
  expect(noOverflow).toBe(true);
  const after = (
    await (
      await request.get(api("/runs"), { headers: authorization(auth) })
    ).json()
  ).count;
  expect(after).toBe(before);
});

test("real network interruption exposes an error and retry recovers", async ({
  page,
  context,
}) => {
  await signIn(page);
  await page
    .getByRole("link", { name: "Strategy library", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "View source", exact: true }).first(),
  ).toBeVisible();
  await context.setOffline(true);
  await page
    .getByRole("button", { name: "View source", exact: true })
    .first()
    .click();
  await expect(page.getByRole("alert")).toBeVisible({ timeout: 30_000 });
  await context.setOffline(false);
  await page.getByRole("button", { name: "Close source" }).click();
  await page
    .getByRole("button", { name: "View source", exact: true })
    .first()
    .click();
  await expect(page.getByText("Read-only strategy source")).toBeVisible();
  await expect(page.locator("pre.preview")).toContainText("class ");
});
