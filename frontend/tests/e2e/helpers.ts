import { expect, type APIRequestContext, type Page } from "@playwright/test";
import type {
  DatasetPublic,
  RunCreate,
  RunPublic,
  StrategyPublic,
  Token,
} from "../../src/client/types.gen";

export const apiBase = process.env.E2E_API_URL || "http://127.0.0.1:8000";
export const api = (path: string) => `${apiBase}/api/v1${path}`;
export function account(other = false) {
  const email = process.env[other ? "E2E_OTHER_EMAIL" : "E2E_EMAIL"];
  const password = process.env[other ? "E2E_OTHER_PASSWORD" : "E2E_PASSWORD"];
  if (!email || !password)
    throw new Error(
      "Real-stack E2E needs E2E_EMAIL, E2E_PASSWORD, E2E_OTHER_EMAIL, E2E_OTHER_PASSWORD for two synthetic fixture accounts. No default credentials are shipped.",
    );
  return { email, password };
}
export async function signIn(page: Page, other = false) {
  const user = account(other);
  await page.goto("/login");
  await page.getByLabel("Email address").fill(user.email);
  await page.getByLabel("Password", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Research overview" }),
  ).toBeVisible();
}
export async function token(
  request: APIRequestContext,
  other = false,
): Promise<Token> {
  const user = account(other);
  const response = await request.post(api("/login/access-token"), {
    form: { username: user.email, password: user.password },
  });
  expect(response.ok()).toBe(true);
  return response.json() as Promise<Token>;
}
export function authorization(auth: Token) {
  return { Authorization: `Bearer ${auth.access_token}` };
}
export async function inputs(
  request: APIRequestContext,
  auth: Token,
  task: RunCreate["task"] = "cn_time",
): Promise<RunCreate> {
  const headers = authorization(auth);
  const datasetsResponse = await request.get(api("/datasets"), { headers });
  const strategiesResponse = await request.get(api("/strategy-versions"), {
    headers,
  });
  expect(datasetsResponse.ok()).toBe(true);
  expect(strategiesResponse.ok()).toBe(true);
  const datasets = (await datasetsResponse.json()).data as DatasetPublic[];
  const strategies = (await strategiesResponse.json()).data as StrategyPublic[];
  const dataset = datasets.find(
    (item) =>
      item.market.toLowerCase() === task.slice(0, 2) &&
      item.is_synthetic &&
      item.source === "synthetic_fixture",
  );
  const strategy = strategies.find(
    (item) => item.task === task && !item.name.startsWith("E2E "),
  );
  if (!dataset || !strategy)
    throw new Error(
      `Bootstrap synthetic fixture inputs for ${task} before running E2E.`,
    );
  return {
    task,
    dataset_version_id: dataset.id,
    strategy_version_id: strategy.id,
    start_date: dataset.start_date,
    end_date: dataset.end_date,
    profile: "corrected_explicit_config",
  };
}
export async function create(
  request: APIRequestContext,
  auth: Token,
  body?: RunCreate,
  key = crypto.randomUUID(),
): Promise<RunPublic> {
  const response = await request.post(api("/runs"), {
    headers: { ...authorization(auth), "Idempotency-Key": key },
    data: body || (await inputs(request, auth)),
  });
  expect(response.status()).toBe(202);
  return response.json() as Promise<RunPublic>;
}
export async function waitForSuccess(
  request: APIRequestContext,
  auth: Token,
  id: string,
) {
  await expect
    .poll(
      async () => {
        const response = await request.get(api(`/runs/${id}`), {
          headers: authorization(auth),
        });
        expect(response.ok()).toBe(true);
        return (await response.json()).status as string;
      },
      { timeout: 90_000, intervals: [500, 1000, 2000] },
    )
    .toBe("succeeded");
}
