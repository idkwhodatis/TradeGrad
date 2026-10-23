import { createHash } from "node:crypto";
import { test, expect } from "@playwright/test";
import { api, authorization, signIn, token } from "./helpers";

// Hosted browser + actual API/PostgreSQL only. Source is inert during authoring.
// Collection does not start a browser/server. Runtime acceptance is a separate system gate.
const nativeDeclaration = {
  required_fields: ["open", "high", "low", "close", "volume", "amount"],
  lookback_bars: 30,
  warmup_bars: 30,
  requires_shorting: false,
  parameters: [
    { name: "window", kind: "integer", value: 30, minimum: 1, maximum: 252 },
  ],
};
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object")
    return `{${Object.entries(value)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, child]) => `${JSON.stringify(key)}:${canonical(child)}`)
      .join(",")}}`;
  return JSON.stringify(value);
}
const hash = (value: unknown) =>
  createHash("sha256").update(canonical(value)).digest("hex");
function customManifest() {
  const state = { name: "empty", version: 1, fields: [] };
  return {
    strategy_id: crypto.randomUUID(),
    strategy_kind: "event",
    market: "US",
    currency: "USD",
    calendar: "XNYS",
    timezone: "America/New_York",
    code_sha256: "0".repeat(64),
    parameters_sha256: "0".repeat(64),
    data: { lookback_bars: 2, warmup_bars: 2, max_listings: 1 },
    universe: {
      semantics: "user_fixed",
      snapshot_sha256: "1".repeat(64),
      survivorship_bias: "possible",
    },
    prices: { actions_snapshot_sha256: "2".repeat(64) },
    engine_profile: {
      profile_sha256: "3".repeat(64),
      environment_sha256: "4".repeat(64),
    },
    state_schema: state,
    authoring: {
      parameter_schema: {
        name: "parameters",
        version: 1,
        fields: [{ name: "threshold", kind: "number", required: true }],
      },
      initial_state: {
        codec_version: "state-json.v1",
        schema_name: "empty",
        schema_version: 1,
        schema_sha256: hash(state),
        payload_json: "{}",
        payload_sha256: hash({}),
      },
    },
  };
}

test("native declarations survive close/reload and parameters create immutable children", async ({
  page,
  request,
}) => {
  await signIn(page);
  const auth = await token(request);
  const name = `Declared native ${crypto.randomUUID()}`;
  await page.goto("/strategies");
  await page
    .getByRole("button", { name: "Upload or create strategy", exact: true })
    .click();
  await page.getByLabel("Version name", { exact: true }).fill(name);
  await page
    .getByLabel("Python source", { exact: true })
    .fill("raise AssertionError('SOURCE MUST NOT RUN DURING SAVE')\n");
  await page
    .getByLabel("Native declaration JSON")
    .fill(JSON.stringify(nativeDeclaration));
  await page
    .getByRole("button", { name: "Close and keep draft", exact: true })
    .click();
  await page.reload();
  await page
    .getByRole("button", { name: "Continue local draft", exact: true })
    .click();
  await expect(page.getByLabel("Native declaration JSON")).toHaveValue(
    JSON.stringify(nativeDeclaration),
  );
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/strategy-versions") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  const response = await saved;
  expect(response.status()).toBe(201);
  const parent = await response.json();
  expect(parent.parameters).toEqual({ window: 30 });
  await page
    .getByRole("button", { name: "Edit as new version", exact: true })
    .click();
  await page.getByLabel("Native declaration JSON").fill(
    JSON.stringify({
      ...nativeDeclaration,
      parameters: [{ ...nativeDeclaration.parameters[0], value: 40 }],
    }),
  );
  const childResponse = page.waitForResponse(
    (item) =>
      item.url().endsWith("/strategy-versions") &&
      item.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  const child = await (await childResponse).json();
  expect(child.parent_id).toBe(parent.id);
  expect(child.content_sha256).toBe(parent.content_sha256);
  expect(child.parameters).toEqual({ window: 40 });
  const unchanged = await request.get(api(`/strategy-versions/${parent.id}`), {
    headers: authorization(auth),
  });
  expect(await unchanged.json()).toEqual(parent);
});

test("portable arbitrary source lost-response replay and full-source derivation", async ({
  page,
  request,
}) => {
  await signIn(page);
  const auth = await token(request);
  const name = `Custom portable ${crypto.randomUUID()}`;
  const source =
    "raise AssertionError('HOST MUST NEVER EXECUTE')\nclass Policy: pass\n";
  await page.goto("/strategies");
  await page
    .getByRole("button", { name: "Upload or create portable policy" })
    .click();
  await page.getByLabel("Portable version name", { exact: true }).fill(name);
  await page
    .getByLabel("Portable task", { exact: true })
    .selectOption("us_time");
  await page
    .getByLabel("Upload portable Python file", { exact: true })
    .setInputFiles({
      name: "policy.py",
      mimeType: "text/x-python",
      buffer: Buffer.from(source),
    });
  await page
    .getByLabel("Portable manifest JSON")
    .fill(JSON.stringify(customManifest()));
  await page.getByLabel("Portable parameter JSON").fill('{"threshold":0.2}');
  let committed: { id: string; code: string } | undefined;
  await page.route(
    "**/api/v1/portable-strategy-versions",
    async (route) => {
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      committed = await response.json();
      await route.abort("failed");
    },
    { times: 1 },
  );
  await page
    .getByRole("button", { name: "Save portable version", exact: true })
    .click();
  await expect(
    page
      .getByText("A save may already have succeeded.", { exact: false })
      .last(),
  ).toBeVisible();
  await expect.poll(() => committed?.id).toBeTruthy();
  await page.reload();
  const replay = page.waitForResponse(
    (response) =>
      response.url().endsWith("/portable-strategy-versions") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Retry portable save", exact: true })
    .click();
  const replayResponse = await replay;
  expect(replayResponse.status()).toBe(200);
  const parent = await replayResponse.json();
  expect(parent.id).toBe(committed?.id);
  expect(parent.code).toBe(source);
  await page
    .getByRole("button", { name: "Edit portable as new version", exact: true })
    .click();
  await page
    .getByLabel("Portable Python source", { exact: true })
    .fill(source + "# new full source\n");
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/portable-strategy-versions") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save portable version", exact: true })
    .click();
  const childResponse = await saved;
  expect(childResponse.status()).toBe(201);
  const child = await childResponse.json();
  expect(child.parent_id).toBe(parent.id);
  const unchanged = await request.get(
    api(`/portable-strategies/${parent.id}`),
    { headers: authorization(auth) },
  );
  expect(await unchanged.json()).toEqual(parent);
});
