import { expect, test, type Page } from "@playwright/test";
import { api, authorization, signIn, token } from "./helpers";

// Only a disposable, explicitly opted-in stack may mutate management fixtures.
// No route mocks, browser-only authorization substitute, or live provider calls.
const required = process.env.TRADEGRAD_REQUIRE_SETTINGS_E2E === "1";
const adminEmail = process.env.E2E_ADMIN_EMAIL;
const adminPassword = process.env.E2E_ADMIN_PASSWORD;
if (required && (!adminEmail || !adminPassword))
  throw new Error(
    "Settings acceptance requires a separate disposable E2E_ADMIN_EMAIL and E2E_ADMIN_PASSWORD, provisioned as an administrator.",
  );
test.skip(
  !required,
  "Settings management needs an explicitly opted-in disposable stack.",
);

async function adminSignIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email address").fill(adminEmail!);
  await page.getByLabel("Password", { exact: true }).fill(adminPassword!);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Research overview" }),
  ).toBeVisible();
  await page.goto("/settings");
  await expect(
    page.getByRole("heading", { name: "User management", exact: true }),
  ).toBeVisible();
}
function panel(page: Page, name: string) {
  return page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
}

test("administrator creates, edits and reloads a disposable user and quota", async ({
  page,
  request,
}) => {
  await adminSignIn(page);
  const email = `settings-${crypto.randomUUID()}@example.com`;
  const password = crypto.randomUUID() + crypto.randomUUID();
  await page.getByRole("button", { name: "New account", exact: true }).click();
  await page.getByLabel("New account email").fill(email);
  await page
    .getByLabel("New account display name")
    .fill("Disposable settings user");
  await page.getByLabel("Initial account password").fill(password);
  await page
    .getByRole("button", { name: "Create account", exact: true })
    .click();
  await expect(page.getByLabel("New account email")).toHaveCount(0);
  const option = page
    .getByLabel("Manage account", { exact: true })
    .locator("option")
    .filter({ hasText: email });
  await expect(option).toHaveCount(1);
  const createdId = (await option.getAttribute("value"))!;
  await page
    .getByLabel("Manage account", { exact: true })
    .selectOption(createdId);
  await page
    .getByLabel("Account display name", { exact: true })
    .fill("Updated settings user");
  await page.getByRole("button", { name: "Save account", exact: true }).click();
  await expect(
    page.getByText("Account updated.", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Active run quota", { exact: true }).fill("0");
  await page.getByLabel("Active import quota", { exact: true }).fill("0");
  await page.getByRole("button", { name: "Save quotas", exact: true }).click();
  await expect(page.getByText(/Quotas saved/)).toBeVisible();
  await page.reload();
  await page
    .getByLabel("Manage account", { exact: true })
    .selectOption(createdId);
  await expect(
    page.getByLabel("Account display name", { exact: true }),
  ).toHaveValue("Updated settings user");
  await expect(
    page.getByLabel("Active run quota", { exact: true }),
  ).toHaveValue("0");
  await expect(
    page.getByLabel("Active import quota", { exact: true }),
  ).toHaveValue("0");
  const auth = await request.post(api("/login/access-token"), {
    form: { username: email, password },
  });
  expect(auth.status()).toBe(200);
  const headers = authorization(await auth.json());
  const quota = await request.get(api("/settings/quota"), { headers });
  expect((await quota.json()).effective_active_runs).toBe(0);
  expect(
    (await request.get(api("/settings/users"), { headers })).status(),
  ).toBe(403);
  await page.getByLabel("Account active", { exact: true }).uncheck();
  await page.getByRole("button", { name: "Save account", exact: true }).click();
  await expect(
    page.getByText("Account updated.", { exact: true }),
  ).toBeVisible();
  expect((await request.get(api("/users/me"), { headers })).status()).toBe(401);
});

test("ordinary user defaults save, cancel and survive reload without admin controls", async ({
  page,
}) => {
  await signIn(page, true);
  await page.goto("/settings");
  const defaults = panel(page, "New experiment defaults");
  await expect(defaults).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "User management", exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("Iteration default", { exact: true }).fill("7");
  await defaults
    .getByRole("button", { name: "Save defaults", exact: true })
    .click();
  await expect(
    defaults.getByText(/Defaults saved for new experiments/),
  ).toBeVisible();
  await page.getByLabel("Iteration default", { exact: true }).fill("9");
  await defaults.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(
    page.getByLabel("Iteration default", { exact: true }),
  ).toHaveValue("7");
  await page.reload();
  await expect(
    page.getByLabel("Iteration default", { exact: true }),
  ).toHaveValue("7");
  await expect(
    page.getByText("Backup status: not reported", { exact: true }),
  ).toBeVisible();
});

test("profile network error can retry and cancelled account creation does not persist", async ({
  page,
  context,
}) => {
  await adminSignIn(page);
  const profile = panel(page, "Edit your profile");
  const name = `Settings retry ${crypto.randomUUID()}`;
  await profile.getByLabel("Display name", { exact: true }).fill(name);
  await context.setOffline(true);
  await profile
    .getByRole("button", { name: "Save profile", exact: true })
    .click();
  await expect(profile.getByRole("alert")).toBeVisible();
  await context.setOffline(false);
  await profile.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(
    profile.getByText("Profile saved.", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(profile.getByLabel("Display name", { exact: true })).toHaveValue(
    name,
  );
  await page.getByRole("button", { name: "New account", exact: true }).click();
  const discarded = `discarded-${crypto.randomUUID()}@example.com`;
  await page.getByLabel("New account email").fill(discarded);
  const management = panel(page, "User management");
  await management.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.reload();
  await expect(
    page
      .getByLabel("Manage account", { exact: true })
      .locator("option")
      .filter({ hasText: discarded }),
  ).toHaveCount(0);
});

test("direct management access is denied and unknown references cannot be added", async ({
  page,
  request,
}) => {
  const ordinary = authorization(await token(request, true));
  const me = await request.get(api("/users/me"), { headers: ordinary });
  const ownerId = (await me.json()).id;
  expect(
    (
      await request.post(api("/settings/profile"), {
        headers: ordinary,
        data: { full_name: "No escalation", is_superuser: true },
      })
    ).status(),
  ).toBe(422);
  expect(
    (
      await request.post(api(`/settings/users/${ownerId}/quota`), {
        headers: ordinary,
        data: { max_active_runs: 100 },
      })
    ).status(),
  ).toBe(403);
  await adminSignIn(page);
  const adminLogin = await request.post(api("/login/access-token"), {
    form: { username: adminEmail!, password: adminPassword! },
  });
  const admin = authorization(await adminLogin.json());
  expect(
    (
      await request.post(
        api(`/settings/users/${ownerId}/secret-references/not-allowlisted`),
        { headers: admin, data: { enabled: true } },
      )
    ).status(),
  ).toBe(404);
  await page
    .getByLabel("Manage account", { exact: true })
    .selectOption(ownerId);
  await expect(
    page.getByText(/No raw key, endpoint, environment name/),
  ).toBeVisible();
});
