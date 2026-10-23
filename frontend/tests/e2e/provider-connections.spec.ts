import { test, expect, type APIRequestContext } from "@playwright/test";
import type {
  ImportDetail,
  ProviderPublic,
  ProviderConnectionPublic,
  Token,
} from "../../src/client/types.gen";
import { api, authorization, signIn, token } from "./helpers";

// Real authenticated API, migrated PostgreSQL and data queue. No route mocks,
// external credentials, live probes, evaluator processes or paid calls.
async function providers(request: APIRequestContext, auth: Token) {
  const result = await request.get(api("/data/providers"), {
    headers: authorization(auth),
  });
  expect(result.status()).toBe(200);
  return (await result.json()) as ProviderPublic[];
}
async function config(request: APIRequestContext, auth: Token, id: string) {
  return (await providers(request, auth)).find((item) => item.id === id)!
    .connection!;
}
async function save(
  request: APIRequestContext,
  auth: Token,
  value: ProviderConnectionPublic,
  changes: Partial<ProviderConnectionPublic>,
) {
  const result = await request.post(
    api(`/data/provider-connections/${value.provider}`),
    {
      headers: authorization(auth),
      data: {
        expected_revision: value.revision,
        enabled: value.enabled,
        is_default: value.is_default,
        default_price_mode: value.default_price_mode,
        ...changes,
      },
    },
  );
  expect(result.status()).toBe(200);
  return (await result.json()) as ProviderConnectionPublic;
}
async function job(request: APIRequestContext, auth: Token, id: string) {
  const response = await request.get(api(`/data/imports/${id}`), {
    headers: authorization(auth),
  });
  expect(response.status()).toBe(200);
  return (await response.json()) as ImportDetail;
}
async function ready(request: APIRequestContext, auth: Token, id: string) {
  await expect
    .poll(
      async () => {
        const item = await job(request, auth, id);
        if (["failed", "partial", "cancelled"].includes(item.status))
          throw new Error(`Public fixture job ended ${item.status}`);
        return item.status;
      },
      { timeout: 90_000, intervals: [500, 1000] },
    )
    .toBe("ready");
  return job(request, auth, id);
}

test("owner connection preferences persist, reject stale edits and freeze real data jobs @provider-connections", async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const auth = await token(request);
  const other = await token(request, true);
  const catalog = await providers(request, auth);
  test.skip(
    catalog.some((item) => item.runtime_mode !== "fixture"),
    "This deterministic gate never makes live provider requests",
  );
  const originals = catalog
    .filter((item) => item.connection)
    .map((item) => item.connection!);
  const otherBefore = await config(request, other, "akshare");
  let pendingImport = "";
  try {
    const baseline = await save(
      request,
      auth,
      await config(request, auth, "akshare"),
      { enabled: true, is_default: true, default_price_mode: "raw" },
    );
    await signIn(page);
    await page.goto("/data-sources");
    const settings = page.getByRole("region", {
      name: "akshare connection settings",
    });
    await expect(
      settings.getByText(/^Public access · public_no_auth\./),
    ).toBeVisible();
    await expect(page.locator('input[type="password"]')).toHaveCount(0);
    await settings
      .getByLabel("Default price mode", { exact: true })
      .selectOption("qfq");
    await settings
      .getByRole("button", { name: "Discard unsaved changes" })
      .click();
    await expect(
      settings.getByLabel("Default price mode", { exact: true }),
    ).toHaveValue("raw");
    await settings
      .getByLabel("Default price mode", { exact: true })
      .selectOption("qfq");
    const savedResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/data/provider-connections/akshare") &&
        response.request().method() === "POST",
    );
    await settings
      .getByRole("button", { name: "Save connection settings" })
      .click();
    expect((await savedResponse).status()).toBe(200);
    await page.reload();
    await expect(
      settings.getByLabel("Default price mode", { exact: true }),
    ).toHaveValue("qfq");
    const saved = await config(request, auth, "akshare");
    expect(saved.revision).toBe(baseline.revision + 1);
    expect(saved.auth_mode).toBe("public_no_auth");
    expect(await config(request, other, "akshare")).toEqual(otherBefore);

    // A stale browser edit is rejected, not silently replayed against a new head.
    await save(request, auth, saved, { default_price_mode: "hfq" });
    await settings
      .getByLabel("Default price mode", { exact: true })
      .selectOption("raw");
    const conflict = page.waitForResponse(
      (response) =>
        response.url().endsWith("/data/provider-connections/akshare") &&
        response.request().method() === "POST",
    );
    await settings
      .getByRole("button", { name: "Save connection settings" })
      .click();
    expect((await conflict).status()).toBe(409);
    await expect(
      settings.getByText(
        "Connection changed since this form opened. Discard unsaved changes to load the latest settings before saving.",
      ),
    ).toBeVisible();
    await expect(
      settings.getByRole("button", { name: "Save connection settings" }),
    ).toBeDisabled();
    await settings
      .getByRole("button", { name: "Discard unsaved changes" })
      .click();
    await expect(
      settings.getByLabel("Default price mode", { exact: true }),
    ).toHaveValue("hfq");

    // Omitted provider and mode really use the owner's defaults, not UI-only fields.
    const key = crypto.randomUUID();
    const body = {
      market: "CN",
      symbols: ["600000"],
      name: "Connection-bound SDK fixture, not live data",
      start_date: "2024-01-02",
      end_date: "2024-01-06",
    };
    const response = await request.post(api("/data/imports"), {
      headers: { ...authorization(auth), "Idempotency-Key": key },
      data: body,
    });
    expect(response.status()).toBe(202);
    const admitted = (await response.json()) as ImportDetail;
    pendingImport = admitted.id;
    const frozen = admitted.request
      ._provider_connection as ProviderConnectionPublic;
    expect(admitted.provider).toBe("akshare");
    expect(admitted.request.price_mode).toBe("hfq");
    expect(frozen.revision).toBe(saved.revision + 1);
    await settings
      .getByRole("checkbox", { name: "akshare enabled", exact: true })
      .uncheck();
    await settings
      .getByRole("button", { name: "Save connection settings" })
      .click();
    await expect
      .poll(async () => (await config(request, auth, "akshare")).enabled)
      .toBe(false);
    await expect(
      page
        .getByRole("region", { name: "akshare connection test" })
        .getByRole("button", { name: "Test connection" }),
    ).toBeDisabled();
    const denied = await request.post(api("/data/resolve"), {
      headers: {
        ...authorization(auth),
        "Idempotency-Key": crypto.randomUUID(),
      },
      data: { provider: "akshare", market: "CN", symbols: ["600000"] },
    });
    expect(denied.status()).toBe(422);
    expect((await denied.json()).code).toBe("provider_connection_disabled");
    const replay = await request.post(api("/data/imports"), {
      headers: { ...authorization(auth), "Idempotency-Key": key },
      data: body,
    });
    expect(replay.status()).toBe(202);
    expect((await replay.json()).id).toBe(admitted.id);
    const completed = await ready(request, auth, admitted.id);
    pendingImport = "";
    expect(completed.request._provider_connection).toEqual(frozen);
    const datasetResponse = await request.get(
      api(`/datasets/${completed.dataset_version_id}`),
      { headers: authorization(auth) },
    );
    expect(datasetResponse.status()).toBe(200);
    const dataset = await datasetResponse.json();
    expect(dataset.manifest.provider_connection).toEqual(frozen);
    expect(dataset.manifest.import_request.price_mode).toBe("hfq");
    expect(dataset.is_synthetic).toBe(true);
    expect(
      dataset.manifest.provenance.every(
        (source: { evidence: string }) => source.evidence === "fixture",
      ),
    ).toBe(true);
    const foreign = await request.get(api(`/data/imports/${admitted.id}`), {
      headers: authorization(other),
    });
    expect(foreign.status()).toBe(404);

    await save(request, auth, await config(request, auth, "akshare"), {
      enabled: true,
      is_default: true,
      default_price_mode: "raw",
    });
    await page.goto("/data-sources");
    await page.reload();
    const probeSection = page.getByRole("region", {
      name: "akshare connection test",
    });
    const probeResponse = page.waitForResponse(
      (item) =>
        item.url().endsWith("/data/providers/akshare/test") &&
        item.request().method() === "POST",
    );
    await probeSection.getByRole("button", { name: "Test connection" }).click();
    const probe = await probeResponse;
    expect(probe.status()).toBe(202);
    const queued = (await probe.json()) as ImportDetail;
    expect(queued.request.price_mode).toBe("raw");
    const checked = await ready(request, auth, queued.id);
    const receipt = checked.progress.probe_receipt as {
      provider_connection: unknown;
      outcome: string;
      price_mode: string;
    };
    expect(receipt.provider_connection).toEqual(
      queued.request._provider_connection,
    );
    expect(receipt.outcome).toBe("fixture_passed");
    expect(receipt.price_mode).toBe("raw");
    expect(checked.dataset_version_id).toBeNull();
    await page.goto("/data-sources");
    await expect(page.getByTestId("provider-test-akshare")).toContainText(
      "fixture_passed",
    );
  } finally {
    if (pendingImport)
      await request.post(api(`/data/imports/${pendingImport}/cancel`), {
        headers: authorization(auth),
      });
    // Restore preferences, never erase immutable revisions/evidence or other users' state.
    for (const original of [
      ...originals.filter((item) => !item.is_default),
      ...originals.filter((item) => item.is_default),
    ]) {
      const current = await config(request, auth, original.provider);
      await save(request, auth, current, {
        enabled: original.enabled,
        is_default: original.is_default,
        default_price_mode: original.default_price_mode,
      });
    }
  }
});
