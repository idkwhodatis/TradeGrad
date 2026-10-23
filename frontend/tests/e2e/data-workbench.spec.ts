import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type {
  DatasetPublic,
  ImportCreate,
  ImportDetail,
  ImportPublic,
  ListingPublic,
  ProviderPublic,
  Token,
  UniversePublic,
} from "../../src/client/types.gen";
import {
  account,
  api,
  authorization,
  signIn,
  token,
  waitForSuccess,
} from "./helpers";

// Default acceptance uses synthetic SDK-schema responses downloaded through three
// independent adapters by the REAL data worker, not live verification. The explicit
// E2E_LIVE_PROVIDERS=1 mode runs only @provider-flow against real public providers.
// No page.route, route.fulfill, mocked queue or fabricated successful run response.
const liveProviders = process.env.E2E_LIVE_PROVIDERS === "1";
const start = "2024-01-02";
const end = "2024-07-01";
const scenarios = [
  {
    provider: "akshare",
    symbols: ["600000", "000001"],
    mics: ["XSHG", "XSHE"],
    market: "CN",
    mode: "raw",
    task: "cn_cross",
    split: "unadjusted",
    dividend: "unadjusted",
  },
  {
    provider: "yfinance",
    symbols: ["AAPL"],
    mics: ["XNAS"],
    market: "US",
    mode: "provider_native",
    task: "us_time",
    split: "adjusted",
    dividend: "unadjusted",
  },
  {
    provider: "openbb",
    symbols: ["MSFT"],
    mics: ["XNAS"],
    market: "US",
    mode: "provider_native",
    task: "us_time",
    split: "unknown",
    dividend: "unknown",
  },
] as const;

async function readImport(
  request: APIRequestContext,
  auth: Token,
  id: string,
): Promise<ImportDetail> {
  const response = await request.get(api(`/data/imports/${id}`), {
    headers: authorization(auth),
  });
  expect(response.ok()).toBe(true);
  return response.json();
}
async function waitImport(
  request: APIRequestContext,
  auth: Token,
  id: string,
  status: string,
) {
  await expect
    .poll(async () => (await readImport(request, auth, id)).status, {
      timeout: liveProviders ? 480_000 : 120_000,
      intervals: [500, 1000, 2000],
    })
    .toBe(status);
  return readImport(request, auth, id);
}
async function submitImport(
  request: APIRequestContext,
  auth: Token,
  body: ImportCreate,
  key = crypto.randomUUID(),
): Promise<ImportPublic> {
  const response = await request.post(api("/data/imports"), {
    headers: { ...authorization(auth), "Idempotency-Key": key },
    data: body,
  });
  expect(response.status()).toBe(202);
  return response.json();
}
async function dataset(
  request: APIRequestContext,
  auth: Token,
  id: string,
): Promise<DatasetPublic> {
  const response = await request.get(api(`/datasets/${id}`), {
    headers: authorization(auth),
  });
  expect(response.ok()).toBe(true);
  return response.json();
}
async function resolveInBrowser(
  page: Page,
  provider: string,
  symbols: readonly string[],
  mics: readonly string[],
) {
  await page.goto("/data-sources");
  await page.getByLabel("Provider", { exact: true }).selectOption(provider);
  await page
    .getByLabel("Vendor symbols", { exact: true })
    .fill(symbols.join(", "));
  await page
    .getByRole("button", { name: "Resolve securities", exact: true })
    .click();
  for (const [index, symbol] of symbols.entries()) {
    await page
      .getByRole("checkbox", {
        name: `Select ${symbol} on ${mics[index]}`,
        exact: true,
      })
      .check({ timeout: liveProviders ? 180_000 : 60_000 });
  }
}
const baseImport: ImportCreate = {
  provider: "yfinance",
  market: "US",
  symbols: ["AAPL"],
  start_date: start,
  end_date: end,
  price_mode: "provider_native",
};

test.beforeAll(async ({ request }) => {
  account();
  if (!liveProviders) account(true);
  expect(
    (await request.get(api("/health/ready"))).ok(),
    "Real migrated PostgreSQL API is required",
  ).toBe(true);
  const auth = await token(request);
  const response = await request.get(api("/data/providers"), {
    headers: authorization(auth),
  });
  expect(response.ok()).toBe(true);
  const providers = (await response.json()) as ProviderPublic[];
  expect(providers.map((item) => item.id).sort()).toEqual([
    "akshare",
    "openbb",
    ...(liveProviders ? [] : ["portable_fixture"]),
    "yfinance",
  ]);
  for (const provider of providers)
    expect(
      provider.runtime_mode,
      "Browser evidence mode must match the explicitly selected server-side provider transport",
    ).toBe(liveProviders ? "live" : "fixture");
});

test.beforeEach(async ({}, testInfo) => {
  test.skip(
    liveProviders && !testInfo.title.includes("@provider-flow"),
    "Negative scenarios require deterministic server fixtures",
  );
});

for (const scenario of scenarios) {
  test(`${scenario.provider}: browser import, immutable preview and actual Docker seed evaluation @provider-flow`, async ({
    page,
    request,
  }) => {
    test.setTimeout(liveProviders ? 600_000 : 240_000);
    await signIn(page);
    const auth = await token(request);
    await resolveInBrowser(
      page,
      scenario.provider,
      scenario.symbols,
      scenario.mics,
    );
    const source = page.getByRole("article", {
      name: `${scenario.provider} provider`,
      exact: true,
    });
    await expect(
      source.getByText(
        liveProviders ? "Live transport" : "Fixture transport · not live",
        { exact: true },
      ),
    ).toBeVisible();
    if (!liveProviders)
      await expect(
        source.getByRole("cell", { name: "verified", exact: true }),
      ).toHaveCount(0);
    const name = `E2E ${scenario.provider} immutable ${crypto.randomUUID()}`;
    await page.getByLabel("Snapshot name", { exact: true }).fill(name);
    await page
      .getByLabel("Import start · inclusive", { exact: true })
      .fill(start);
    await page.getByLabel("Import end · exclusive", { exact: true }).fill(end);
    await page
      .getByLabel("Price mode", { exact: true })
      .selectOption(scenario.mode);
    await page.reload();
    await expect(page.getByLabel("Snapshot name", { exact: true })).toHaveValue(
      name,
    );
    for (const [index, symbol] of scenario.symbols.entries())
      await expect(
        page.getByRole("checkbox", {
          name: `Select ${symbol} on ${scenario.mics[index]}`,
          exact: true,
        }),
      ).toBeChecked();
    for (const [index, symbol] of scenario.symbols.entries()) {
      if (scenario.mics[index] === "XSHE" || scenario.mics[index] === "XNAS") {
        await expect(
          page
            .getByRole("checkbox", {
              name: `Select ${symbol} on ${scenario.mics[index]}`,
              exact: true,
            })
            .locator(".."),
        ).toContainText(/proxy|not a native XSHE calendar/);
      }
    }
    let submissions = 0;
    page.on("request", (request) => {
      if (
        new URL(request.url()).pathname === "/api/v1/data/imports" &&
        request.method() === "POST"
      )
        submissions++;
    });
    await page
      .getByRole("button", { name: "Import selected listings", exact: true })
      .evaluate((button: HTMLButtonElement) => {
        button.click();
        button.click();
      });
    await expect(page).toHaveURL(/\/imports\/[0-9a-f-]{36}$/);
    const id = new URL(page.url()).pathname.split("/").at(-1)!;
    expect(submissions).toBe(1);
    await page.reload();
    await expect(
      page.getByRole("heading", { name: "Durable progress", exact: true }),
    ).toBeVisible();
    const ready = await waitImport(request, auth, id, "ready");
    expect(ready.dataset_version_id).not.toBeNull();
    expect(ready.blocks.length).toBeGreaterThanOrEqual(
      6 * scenario.symbols.length,
    );
    expect(
      ready.blocks.every(
        (block) => block.status === "ready" && block.content_sha256,
      ),
    ).toBe(true);
    await expect(page.getByTestId("import-status")).toHaveText("ready", {
      timeout: 20_000,
    });
    if (ready.blocks_total > 10) {
      const nextBlocks = page.waitForRequest((request) => {
        const url = new URL(request.url());
        return (
          url.pathname === `/api/v1/data/imports/${id}` &&
          url.searchParams.get("block_offset") === "10" &&
          url.searchParams.get("block_limit") === "10"
        );
      });
      await page
        .getByRole("button", { name: "Next page", exact: true })
        .click();
      await nextBlocks;
      await expect(
        page
          .getByRole("region", { name: "Import blocks", exact: true })
          .getByRole("row"),
      ).toHaveCount(Math.min(10, ready.blocks_total - 10) + 1);
    }
    await page
      .getByRole("link", { name: "Open published snapshot", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(
        liveProviders
          ? "Live-provider snapshot"
          : "Provider fixture · not live",
        { exact: true },
      ),
    ).toBeVisible();
    const snapshot = await dataset(request, auth, ready.dataset_version_id!);
    const pinned = snapshot.manifest.listings as ListingPublic[];
    expect(pinned.map((item) => `${item.symbol}:${item.mic}`).sort()).toEqual(
      scenario.symbols
        .map((symbol, index) => `${symbol}:${scenario.mics[index]}`)
        .sort(),
    );
    await expect(
      page.getByRole("region", {
        name: "Immutable listing mappings",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByText(
        "The legacy evaluator's INDEX.csv is a compatibility alias",
        { exact: false },
      ),
    ).toBeVisible();
    for (const listing of pinned) {
      if (listing.calendar_note)
        await expect(
          page.getByRole("region", {
            name: "Immutable listing mappings",
            exact: true,
          }),
        ).toContainText(listing.calendar_note);
    }
    const semantics = snapshot.manifest.price_semantics as Record<
      string,
      unknown
    >;
    expect(semantics.split_basis).toBe(scenario.split);
    expect(semantics.dividend_basis).toBe(scenario.dividend);
    const provenance = snapshot.manifest.provenance as Record<
      string,
      unknown
    >[];
    expect(snapshot.is_synthetic).toBe(!liveProviders);
    expect(provenance.length).toBeGreaterThan(0);
    expect(
      provenance.every(
        (item) => item.evidence === (liveProviders ? "live" : "fixture"),
      ),
    ).toBe(true);
    if (liveProviders)
      await expect(
        page.getByText("Provider fixture · not live", { exact: true }),
      ).toHaveCount(0);
    await expect(
      page
        .locator("dt")
        .filter({ hasText: /^Split basis$/ })
        .locator("..")
        .locator("dd"),
    ).toHaveText(scenario.split);
    const previewRequests: URL[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname.endsWith("/preview")) previewRequests.push(url);
    });
    await page
      .getByRole("button", { name: "Show OHLC chart", exact: true })
      .click();
    await expect(
      page.getByRole("img", {
        name: "Daily OHLC candlesticks and volume for the selected bounded page",
        exact: true,
      }),
    ).toBeVisible();
    const bars = page.getByRole("region", { name: "Daily bars", exact: true });
    await expect(bars.getByRole("row")).toHaveCount(101);
    await page.getByRole("button", { name: "Next page", exact: true }).click();
    await expect
      .poll(() =>
        previewRequests.some((url) => url.searchParams.get("offset") === "100"),
      )
      .toBe(true);
    for (const url of previewRequests) {
      expect(url.searchParams.get("limit")).toBe("100");
      expect(url.searchParams.get("listing_id")).toMatch(/^[0-9a-f-]{36}$/);
      expect(url.searchParams.get("start_date")).toBe(start);
      expect(url.searchParams.get("end_date")).toBe(end);
    }
    await page
      .getByRole("link", {
        name: "Evaluate this immutable snapshot",
        exact: true,
      })
      .click();
    await expect(
      page.getByLabel("Dataset snapshot", { exact: true }),
    ).toHaveValue(snapshot.id);
    await expect(page.getByTestId("native-window-warning")).toContainText(
      "cold-start windows with no pre-start warm-up",
    );
    await expect(page.getByTestId("native-window-warning")).toContainText(
      "For corrected warm-up-aware optimization, use New Experiment",
    );
    await expect(page.getByLabel("Market & task", { exact: true })).toHaveValue(
      scenario.task,
    );
    await page
      .getByLabel("Seed strategy", { exact: true })
      .selectOption({ label: `Original ${scenario.task} seed` });
    await page
      .getByRole("button", { name: "Start evaluation", exact: true })
      .click();
    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
    const runId = new URL(page.url()).pathname.split("/").at(-1)!;
    await waitForSuccess(request, auth, runId);
    await expect(page.getByTestId("run-status")).toHaveText("Succeeded", {
      timeout: 20_000,
    });
    await expect(
      page.getByText("Native · untrusted result", { exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId("native-window-warning")).toContainText(
      "Early history-dependent decisions retain original-source behavior",
    );
    const runResponse = await request.get(api(`/runs/${runId}`), {
      headers: authorization(auth),
    });
    const run = await runResponse.json();
    expect(run.dataset_version_id).toBe(snapshot.id);
    expect(run.config.dataset_sha256).toBe(snapshot.content_sha256);
    expect(run.result).not.toBeNull();
    await page.reload();
    await expect(
      page.getByRole("heading", { name: "Evaluation results", exact: true }),
    ).toBeVisible();
  });
}

test("partial imports expose missing blocks; retry reuses completed hashes without refetching", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const auth = await token(request);
  const job = await submitImport(request, auth, {
    ...baseImport,
    name: `E2E partial ${crypto.randomUUID()}`,
    end_date: "2024-08-01",
  });
  const partial = await waitImport(request, auth, job.id, "partial");
  expect(partial.dataset_version_id).toBeNull();
  // 31-day blocks do not align to months. The fixture ends at July 1, so
  // both the June 5–July 6 block and the final July 6–August 1 block are partial.
  const plannedBlocks = [
    ["2024-01-02", "2024-02-02", "ready"],
    ["2024-02-02", "2024-03-04", "ready"],
    ["2024-03-04", "2024-04-04", "ready"],
    ["2024-04-04", "2024-05-05", "ready"],
    ["2024-05-05", "2024-06-05", "ready"],
    ["2024-06-05", "2024-07-06", "partial"],
    ["2024-07-06", "2024-08-01", "partial"],
  ];
  const blockStates = (item: ImportDetail) =>
    item.blocks
      .map((block) => [block.start_date, block.end_date, block.status])
      .sort((a, b) => a[0].localeCompare(b[0]));
  expect(partial.blocks_total).toBe(plannedBlocks.length);
  expect(blockStates(partial)).toEqual(plannedBlocks);
  const completed = partial.blocks.filter((block) => block.status === "ready");
  const missing = partial.blocks.filter((block) => block.status !== "ready");
  for (const block of completed)
    expect(block.content_sha256).toMatch(/^[0-9a-f]{64}$/);
  for (const block of missing) expect(block.error).toBe("incomplete_coverage");
  await signIn(page);
  await page.goto(`/imports/${job.id}`);
  await expect(page.getByTestId("import-status")).toHaveText("partial");
  await expect(
    page.getByText("Partial is not a published dataset.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Import blocks", exact: true }),
  ).toContainText("incomplete_coverage");
  await page.reload();
  await page
    .getByRole("button", { name: "Retry incomplete blocks", exact: true })
    .click();
  await expect
    .poll(async () => (await readImport(request, auth, job.id)).generation)
    .toBe(partial.generation + 1);
  const retried = await waitImport(request, auth, job.id, "partial");
  expect(retried.blocks_total).toBe(plannedBlocks.length);
  expect(blockStates(retried)).toEqual(plannedBlocks);
  for (const block of completed) {
    const current = retried.blocks.find((item) => item.id === block.id)!;
    expect(current.attempts).toBe(block.attempts);
    expect(current.content_sha256).toBe(block.content_sha256);
  }
  for (const block of missing) {
    const current = retried.blocks.find((item) => item.id === block.id)!;
    expect(current.attempts).toBe(block.attempts + 1);
    expect(current.error).toBe("incomplete_coverage");
  }
  expect(retried.dataset_version_id).toBeNull();
  await page.reload();
  await expect(page.getByTestId("import-status")).toHaveText("partial");
  await expect(
    page.getByRole("link", { name: "Open published snapshot", exact: true }),
  ).toHaveCount(0);
});

test("cancel is durable after reload and never publishes an incomplete snapshot", async ({
  page,
  request,
}) => {
  await signIn(page);
  const auth = await token(request);
  const job = await submitImport(request, auth, {
    ...baseImport,
    name: `E2E cancel ${crypto.randomUUID()}`,
    symbols: ["AAPL", "MSFT"],
  });
  await page.goto(`/imports/${job.id}`);
  await page
    .getByRole("button", { name: "Cancel import", exact: true })
    .click();
  await expect(page.getByTestId("import-status")).toHaveText("cancelled");
  await page.reload();
  await expect(page.getByTestId("import-status")).toHaveText("cancelled");
  const stopped = await readImport(request, auth, job.id);
  expect(stopped.dataset_version_id).toBeNull();
  expect(stopped.cancel_requested).toBe(true);
  await expect(
    page.getByRole("link", { name: "Open published snapshot", exact: true }),
  ).toHaveCount(0);
});

test("refresh makes a new version and old snapshot hash and preview stay fixed", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const auth = await token(request);
  const job = await submitImport(request, auth, {
    ...baseImport,
    name: `E2E refresh ${crypto.randomUUID()}`,
  });
  const ready = await waitImport(request, auth, job.id, "ready");
  const original = await dataset(request, auth, ready.dataset_version_id!);
  const listing = (original.manifest.listings as ListingPublic[])[0];
  const before = await request.get(
    api(
      `/datasets/${original.id}/preview?listing_id=${listing.listing_id}&limit=5`,
    ),
    { headers: authorization(auth) },
  );
  const beforePreview = await before.json();
  await signIn(page);
  await page.goto(`/datasets/${original.id}`);
  await page
    .getByRole("button", { name: "Refresh as new snapshot", exact: true })
    .click();
  await expect(page).toHaveURL(/\/imports\/[0-9a-f-]{36}$/);
  const refreshed = await waitImport(
    request,
    auth,
    new URL(page.url()).pathname.split("/").at(-1)!,
    "ready",
  );
  expect(refreshed.dataset_version_id).not.toBe(original.id);
  expect(refreshed.parent_version_id).toBe(original.id);
  const unchanged = await dataset(request, auth, original.id);
  expect(unchanged.content_sha256).toBe(original.content_sha256);
  expect(unchanged.manifest).toEqual(original.manifest);
  const after = await request.get(
    api(
      `/datasets/${original.id}/preview?listing_id=${listing.listing_id}&limit=5`,
    ),
    { headers: authorization(auth) },
  );
  expect(await after.json()).toEqual(beforePreview);
});

test("mapped uploads reject duplicate and missing columns, then publish explicit unknown price semantics", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  await signIn(page);
  const auth = await token(request);
  const csv = Buffer.from(
    "Date,Open,High,Low,Close,Volume\n2024-01-02,100,102,99,101,1000\n2024-01-03,101,103,100,102,1001\n2024-01-04,102,104,101,103,1002\n2024-01-05,103,105,102,104,1003\n",
  );
  async function fillUpload() {
    await page.goto("/datasets/upload");
    await page
      .getByLabel("CSV or Parquet file", { exact: true })
      .setInputFiles({ name: "bars.csv", mimeType: "text/csv", buffer: csv });
    await page
      .getByLabel("Upload snapshot name", { exact: true })
      .fill(`E2E uploaded ${crypto.randomUUID()}`);
    await page.getByLabel("Upload symbol", { exact: true }).fill("E2EUPLOAD");
    await page
      .getByLabel("Upload start · inclusive", { exact: true })
      .fill(start);
    await page
      .getByLabel("Upload end · exclusive", { exact: true })
      .fill("2024-01-06");
    await page
      .getByLabel("Volume unit", { exact: true })
      .selectOption("shares");
  }
  await fillUpload();
  await page
    .getByLabel("Source column for close", { exact: true })
    .fill("Open");
  await page
    .getByRole("button", { name: "Validate and import file", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("distinct source column");
  await page
    .getByLabel("Source column for close", { exact: true })
    .fill("Close");
  await page
    .getByLabel("Source column for volume", { exact: true })
    .fill("MissingVolume");
  await page
    .getByRole("button", { name: "Validate and import file", exact: true })
    .click();
  await expect(page).toHaveURL(/\/imports\/[0-9a-f-]{36}$/);
  const badId = new URL(page.url()).pathname.split("/").at(-1)!;
  const bad = await waitImport(request, auth, badId, "failed");
  expect(bad.dataset_version_id).toBeNull();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("import-status")).toHaveText("failed");
  await fillUpload();
  await page
    .getByRole("button", { name: "Validate and import file", exact: true })
    .click();
  await expect(page).toHaveURL(/\/imports\/[0-9a-f-]{36}$/);
  const good = await waitImport(
    request,
    auth,
    new URL(page.url()).pathname.split("/").at(-1)!,
    "ready",
  );
  await page
    .getByRole("link", { name: "Open published snapshot", exact: true })
    .click();
  await expect(
    page.getByText("User upload · unverified source", { exact: true }),
  ).toBeVisible();
  const snapshot = await dataset(request, auth, good.dataset_version_id!);
  const prices = snapshot.manifest.price_semantics as Record<string, unknown>;
  expect(prices.split_basis).toBe("unknown");
  expect(prices.dividend_basis).toBe("unknown");
  await expect(
    page
      .getByRole("region", { name: "Daily bars", exact: true })
      .getByRole("row"),
  ).toHaveCount(5);
  await expect(
    page.getByRole("button", { name: "Refresh as new snapshot", exact: true }),
  ).toHaveCount(0);
});

test("universe UI preserves fixed, current and historical evidence without promoting it to PIT", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  await signIn(page);
  await resolveInBrowser(page, "yfinance", ["AAPL"], ["XNAS"]);
  const auth = await token(request);
  for (const state of [
    "user_fixed",
    "current_snapshot",
    "historical_membership",
  ] as const) {
    await page.goto("/universes");
    const name = `E2E ${state} ${crypto.randomUUID()}`;
    await page.getByLabel("Universe name", { exact: true }).fill(name);
    await page
      .getByLabel("Membership evidence type", { exact: true })
      .selectOption(state);
    await page
      .getByLabel("Universe as of · UTC", { exact: true })
      .fill("2024-01-02T00:00");
    await page
      .getByRole("checkbox", { name: "Include AAPL on XNAS", exact: true })
      .check();
    const controls = page.getByRole("group", {
      name: "Listing server query controls",
    });
    await controls
      .getByLabel("Listing search", { exact: true })
      .fill("B6-NO-SUCH-LISTING");
    await controls
      .getByLabel("Listing server sort", { exact: true })
      .selectOption("symbol");
    await controls
      .getByLabel("Listing sort direction", { exact: true })
      .selectOption("desc");
    const filtered = page.waitForResponse(
      (response) =>
        response.url().includes("/data/listings?") &&
        response.url().includes("search=B6-NO-SUCH-LISTING"),
    );
    await controls
      .getByRole("button", { name: "Apply listing query", exact: true })
      .click();
    expect((await (await filtered).json()).count).toBe(0);
    await expect(
      page.getByRole("checkbox", { name: "Include AAPL on XNAS", exact: true }),
    ).toHaveCount(0);
    await controls
      .getByRole("button", { name: "Reset listing query", exact: true })
      .click();
    await expect(
      page.getByRole("checkbox", { name: "Include AAPL on XNAS", exact: true }),
    ).toBeChecked();

    if (state === "historical_membership") {
      await page.getByLabel("AAPL valid from", { exact: true }).fill(start);
      await page
        .getByLabel("AAPL valid to · exclusive", { exact: true })
        .fill(end);
      await page
        .getByLabel("AAPL information known at · UTC (optional)", {
          exact: true,
        })
        .fill("2024-01-01T12:00");
    }
    if (state !== "user_fixed") {
      const reference = page.getByLabel("Membership evidence reference", {
        exact: true,
      });
      await expect(reference).toHaveAttribute("required", "");
      await reference.fill("   ");
      await page
        .getByRole("button", { name: "Publish universe version", exact: true })
        .click();
      await expect(page.getByRole("alert")).toContainText(
        "nonempty evidence reference",
      );
      await reference.fill(
        "Synthetic fixture membership reference, not live evidence",
      );
    }
    await page
      .getByRole("button", { name: "Publish universe version", exact: true })
      .click();
    await expect(page.getByRole("status")).toContainText(`Published ${name}`);
    await page.reload();
    const article = page
      .getByRole("article")
      .filter({ has: page.getByRole("heading", { name, exact: true }) });
    await expect(article).toBeVisible();
    await expect(article).toContainText(
      state === "historical_membership"
        ? "availability unverified"
        : state === "current_snapshot"
          ? "not historical PIT"
          : "not verified PIT",
    );
    const response = await request.get(api("/data/universes?limit=100"), {
      headers: authorization(auth),
    });
    const stored = (await response.json()).data.find(
      (item: UniversePublic) => item.name === name,
    ) as UniversePublic;
    expect(stored.state).toBe(state);
    if (state !== "user_fixed")
      expect(stored.evidence).toEqual([
        {
          reference:
            "Synthetic fixture membership reference, not live evidence",
          note: "",
          verification: "user_supplied_not_independently_verified",
          verification_status: "user_supplied_unverified",
        },
      ]);
    if (state === "historical_membership") {
      expect(stored.members[0].valid_from).toBe(start);
      expect(stored.members[0].known_at).toContain("2024-01-01T12:00");
    }
  }
  await expect(
    page.getByRole("option", {
      name: "Verified PIT requires independently verified evidence (unavailable for manual creation)",
      exact: true,
    }),
  ).toBeDisabled();
});

test("cross-owner import, dataset, upload and universe endpoints fail closed", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const owner = await token(request);
  const stranger = await token(request, true);
  const job = await submitImport(request, owner, {
    ...baseImport,
    name: `E2E private ${crypto.randomUUID()}`,
    end_date: "2024-01-06",
  });
  const ready = await waitImport(request, owner, job.id, "ready");
  const snapshot = await dataset(request, owner, ready.dataset_version_id!);
  const listing = (snapshot.manifest.listings as ListingPublic[])[0];
  const universeResponse = await request.post(api("/data/universes"), {
    headers: authorization(owner),
    data: {
      name: `Private ${crypto.randomUUID()}`,
      state: "user_fixed",
      as_of: "2024-01-02T00:00:00Z",
      members: [{ listing_id: listing.listing_id }],
    },
  });
  expect(universeResponse.status()).toBe(201);
  const universe = (await universeResponse.json()) as UniversePublic;
  for (const path of [
    `/data/imports/${job.id}`,
    `/datasets/${snapshot.id}`,
    `/datasets/${snapshot.id}/coverage`,
    `/datasets/${snapshot.id}/preview?listing_id=${listing.listing_id}`,
    `/data/universes/${universe.id}`,
  ]) {
    const forbidden = await request.get(api(path), {
      headers: authorization(stranger),
    });
    expect(forbidden.status(), path).toBe(404);
  }
  for (const path of [
    `/data/imports/${job.id}/cancel`,
    `/data/imports/${job.id}/retry`,
    `/datasets/${snapshot.id}/refresh`,
  ]) {
    const forbidden = await request.post(api(path), {
      headers: {
        ...authorization(stranger),
        "Idempotency-Key": crypto.randomUUID(),
      },
    });
    expect(forbidden.status(), path).toBe(404);
  }
  const foreignSelection = await request.post(api("/data/imports"), {
    headers: {
      ...authorization(stranger),
      "Idempotency-Key": crypto.randomUUID(),
    },
    data: { ...baseImport, symbols: [], listing_ids: [listing.listing_id] },
  });
  expect(foreignSelection.status()).toBe(404);
  const listings = await request.get(
    api(`/data/listings?listing_ids=${listing.listing_id}`),
    { headers: authorization(stranger) },
  );
  expect((await listings.json()).count).toBe(0);
  // Upload form cannot be submitted anonymously. No real user files are used.
  const anonymous = await request.post(api("/data/uploads"), {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    multipart: {
      mapping: "{}",
      file: {
        name: "test.csv",
        mimeType: "text/csv",
        buffer: Buffer.from("Date,Open\n"),
      },
    },
  });
  expect(anonymous.status()).toBe(401);
  await signIn(page, true);
  await page.goto(`/imports/${job.id}`);
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Per-symbol and date-block results",
      exact: true,
    }),
  ).toHaveCount(0);
});

test("concurrent duplicate import submissions create one durable logical job", async ({
  request,
}) => {
  test.setTimeout(180_000);
  const auth = await token(request);
  const key = crypto.randomUUID();
  const body: ImportCreate = {
    ...baseImport,
    name: `E2E duplicate ${crypto.randomUUID()}`,
    end_date: "2024-01-06",
  };
  const [one, two] = await Promise.all([
    submitImport(request, auth, body, key),
    submitImport(request, auth, body, key),
  ]);
  expect(one.id).toBe(two.id);
  expect((await submitImport(request, auth, body, key)).id).toBe(one.id);
  const changed = await request.post(api("/data/imports"), {
    headers: { ...authorization(auth), "Idempotency-Key": key },
    data: { ...body, name: `${body.name} changed` },
  });
  expect(changed.status()).toBe(409);
  await waitImport(request, auth, one.id, "ready");
});

test("actual offline import failure retries the same request; narrow viewport remains usable", async ({
  page,
  request,
  context,
}) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  const auth = await token(request);
  await resolveInBrowser(page, "yfinance", ["AAPL"], ["XNAS"]);
  await page
    .getByLabel("Snapshot name", { exact: true })
    .fill(`E2E offline ${crypto.randomUUID()}`);
  await page
    .getByLabel("Import end · exclusive", { exact: true })
    .fill("2024-01-06");
  const keys: string[] = [];
  page.on("request", (request) => {
    if (
      new URL(request.url()).pathname === "/api/v1/data/imports" &&
      request.method() === "POST"
    )
      keys.push(request.headers()["idempotency-key"]);
  });
  await context.setOffline(true);
  await page
    .getByRole("button", { name: "Import selected listings", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("could not be reached", {
    timeout: 30_000,
  });
  await context.setOffline(false);
  await page
    .getByRole("button", { name: "Import selected listings", exact: true })
    .click();
  await expect(page).toHaveURL(/\/imports\/[0-9a-f-]{36}$/);
  expect(keys.length).toBe(2);
  expect(keys[0]).toBe(keys[1]);
  const ready = await waitImport(
    request,
    auth,
    new URL(page.url()).pathname.split("/").at(-1)!,
    "ready",
  );
  await page
    .getByRole("link", { name: "Open published snapshot", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Daily bars", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.reload();
  await expect(page).toHaveURL(
    new RegExp(`/datasets/${ready.dataset_version_id}$`),
  );
  await expect(
    page.getByText("Provider fixture · not live", { exact: true }),
  ).toBeVisible();
});
