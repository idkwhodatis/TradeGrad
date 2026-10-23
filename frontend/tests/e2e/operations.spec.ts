import { expect, test } from "@playwright/test";
import { api, authorization, signIn, token } from "./helpers";

// Real authenticated API and PostgreSQL only. No route mocks or backend substitutes.
test("operational overview survives reload and owner cannot request deployment data", async ({
  page,
  request,
}) => {
  await signIn(page);
  const panel = page.locator("section").filter({
    has: page.getByRole("heading", {
      name: "Operational observations",
      exact: true,
    }),
  });
  await expect(
    panel.getByText("Queue wait (started attempts)", { exact: true }),
  ).toBeVisible();
  await expect(panel.getByLabel("Observation scope")).toHaveValue("owner");
  await panel.getByLabel("Observation window").selectOption("1");
  await expect(panel.getByText(/window starts/)).toBeVisible();
  await panel.getByRole("button", { name: "Refresh observations" }).click();
  await page.reload();
  await expect(
    panel.getByText("Import failure rate", { exact: true }),
  ).toBeVisible();
  const headers = authorization(await token(request));
  const response = await request.get(api("/operations"), { headers });
  expect(response.status()).toBe(200);
  const data = await response.json();
  expect(data.scope).toBe("owner");
  expect(data.disk.state).toBe("restricted");
  expect(
    (
      await request.get(api("/operations?scope=deployment"), { headers })
    ).status(),
  ).toBe(403);
  await page.goto("/settings");
  await expect(
    page.getByText("Backup status: restricted", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Operational observations",
      exact: true,
    }),
  ).toBeVisible();
});

test("operations unavailable is recoverable without displaying cached healthy metrics", async ({
  page,
  context,
}) => {
  await signIn(page);
  await page.goto("/settings");
  const panel = page.locator("section").filter({
    has: page.getByRole("heading", {
      name: "Operational observations",
      exact: true,
    }),
  });
  await expect(
    panel.getByText("Import failure rate", { exact: true }),
  ).toBeVisible();
  await context.setOffline(true);
  await panel.getByRole("button", { name: "Refresh observations" }).click();
  await expect(panel.getByText(/could not be reached/)).toBeVisible();
  await expect(
    panel.getByText("Import failure rate", { exact: true }),
  ).toHaveCount(0);
  await context.setOffline(false);
  await panel.getByRole("button", { name: "Refresh observations" }).click();
  await expect(
    panel.getByText("Import failure rate", { exact: true }),
  ).toBeVisible();
});
