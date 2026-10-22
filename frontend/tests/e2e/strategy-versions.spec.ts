import { createHash } from "node:crypto";
import { test, expect } from "@playwright/test";
import type { StrategyDetail } from "../../src/client/types.gen";
import { account, api, authorization, signIn, token } from "./helpers";

// Acceptance uses the real authenticated API and real migrated PostgreSQL.
// A lost-response case below drops the response AFTER the real server write;
// it does not fabricate a successful backend response or replace the API.
test.beforeAll(async ({ request }) => {
  account();
  account(true);
  expect((await request.get(api("/health/ready"))).ok()).toBe(true);
});

test("upload preserves exact source, derive creates a child, parent stays unchanged", async ({
  page,
  request,
}) => {
  await signIn(page);
  const auth = await token(request);
  const name = `Uploaded native ${crypto.randomUUID()}`;
  const original = '# exact Unicode 源码\r\nvalue = "é"\r\n';
  await page.goto("/strategies");
  await page.getByRole("button", { name: "Upload or create strategy" }).click();
  await page.getByLabel("Version name", { exact: true }).fill(name);
  await page.getByLabel("Upload Python file", { exact: true }).setInputFiles({
    name: "native.py",
    mimeType: "text/x-python",
    buffer: Buffer.from(original),
  });
  // A textarea normalizes its DOM value's newlines, but uploading must preserve
  // the original string in the request, unless the user actually edits it.
  await expect(page.getByLabel("Python source", { exact: true })).toHaveValue(
    original.replaceAll("\r\n", "\n"),
  );
  const created = page.waitForResponse(
    (response) =>
      response.url().endsWith("/strategy-versions") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  const response = await created;
  expect(response.status()).toBe(201);
  const parent = (await response.json()) as StrategyDetail;
  expect(parent.code).toBe(original);
  expect(parent.content_sha256).toBe(
    createHash("sha256").update(original).digest("hex"),
  );
  await expect(
    page.getByText(`Saved ${name} as a new immutable version.`, {
      exact: false,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Edit as new version", exact: true })
    .click();
  await expect(
    page.getByLabel("Strategy task", { exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Version name", { exact: true }).fill(`${name} child`);
  const changed = "# derived native source\nvalue = 2\n";
  await page.getByLabel("Python source", { exact: true }).fill(changed);
  await page.getByRole("button", { name: "Compare draft with parent" }).click();
  await expect(
    page.getByRole("region", { name: "Draft and immutable parent comparison" }),
  ).toBeVisible();
  const derived = page.waitForResponse(
    (result) =>
      result.url().endsWith("/strategy-versions") &&
      result.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  const child = (await (await derived).json()) as StrategyDetail;
  expect(child.parent_id).toBe(parent.id);
  expect(child.id).not.toBe(parent.id);
  expect(child.code).toBe(changed);
  const stillOriginal = await request.get(
    api(`/strategy-versions/${parent.id}`),
    { headers: authorization(auth) },
  );
  expect(await stillOriginal.json()).toEqual(parent);
  await page
    .getByRole("button", { name: "Compare with parent", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Saved version and parent comparison" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Strategy library" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: `${name} child`, exact: true }),
  ).toBeVisible();
});

test("syntax rejection is actionable and close/reload preserves only the current account's draft", async ({
  page,
}) => {
  await signIn(page);
  const name = `Local draft ${crypto.randomUUID()}`;
  await page.goto("/strategies");
  await page.getByRole("button", { name: "Upload or create strategy" }).click();
  await page.getByLabel("Version name", { exact: true }).fill(name);
  await page.getByLabel("Python source", { exact: true }).fill("def broken(\n");
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  await expect(
    page.getByText("Python syntax could not be parsed.", { exact: false }),
  ).toBeVisible();
  await expect(page.getByLabel("Python source", { exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Close and keep draft" }).click();
  await expect(
    page.getByRole("form", { name: "Create strategy version" }),
  ).not.toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Version name", { exact: true })).toHaveValue(
    name,
  );
  await expect(page.getByLabel("Python source", { exact: true })).toHaveValue(
    "def broken(\n",
  );
  // Existing logout clears authentication and caches, not another owner's drafts.
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(page, true);
  await page.goto("/strategies");
  await expect(
    page.getByRole("button", { name: "Upload or create strategy" }),
  ).toBeVisible();
  await expect(
    page.getByRole("form", { name: "Create strategy version" }),
  ).not.toBeVisible();
});

test("lost save response survives reload and retries the same real mutation exactly once", async ({
  page,
  request,
}) => {
  await signIn(page);
  const auth = await token(request);
  const headers = authorization(auth);
  const before = (
    await (await request.get(api("/strategy-versions"), { headers })).json()
  ).count as number;
  const name = `Lost response ${crypto.randomUUID()}`;
  await page.goto("/strategies");
  await page.getByRole("button", { name: "Upload or create strategy" }).click();
  await page.getByLabel("Version name", { exact: true }).fill(name);
  await page
    .getByLabel("Python source", { exact: true })
    .fill("# real API submission, never executed\n");
  let firstKey: string | undefined;
  let dropped = false;
  await page.route("**/api/v1/strategy-versions", async (route) => {
    if (route.request().method() !== "POST" || dropped) {
      await route.continue();
      return;
    }
    dropped = true;
    firstKey = route.request().headers()["idempotency-key"];
    const committed = await route.fetch();
    expect(committed.status()).toBe(201);
    await route.abort("failed");
  });
  await page
    .getByRole("form", { name: "Create strategy version" })
    .evaluate((node) => {
      const form = node as HTMLFormElement;
      form.requestSubmit();
      form.requestSubmit();
    });
  await expect(
    page.getByRole("button", { name: "Retry same save" }),
  ).toBeEnabled();
  await expect(
    page.getByLabel("Python source", { exact: true }),
  ).toBeDisabled();
  await page.reload();
  await expect(page.getByLabel("Version name", { exact: true })).toHaveValue(
    name,
  );
  const replay = page.waitForResponse(
    (response) =>
      response.url().endsWith("/strategy-versions") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Retry same save" }).click();
  const result = await replay;
  expect(result.status()).toBe(200);
  expect(result.request().headers()["idempotency-key"]).toBe(firstKey);
  await expect(
    page.getByText(`Saved ${name} as a new immutable version.`, {
      exact: false,
    }),
  ).toBeVisible();
  const after = await (
    await request.get(api("/strategy-versions"), { headers })
  ).json();
  expect(after.count).toBe(before + 1);
});
