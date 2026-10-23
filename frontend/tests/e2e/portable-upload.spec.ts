import { expect, test, type APIRequestContext } from "@playwright/test";
import type {
  ImportDetail,
  PortableRunDetail,
  Token,
  UploadMapping,
} from "../../src/client/types.gen";
import { api, authorization, signIn, token } from "./helpers";

// Hosted acceptance only: real API + PG + workers + both portable images + browser. These fabricated
// CSV bytes test declaration admission, not real observations or engine certification.
const csv =
  "Date,Open,High,Low,Close,Volume\n2024-01-02,100,102,99,101,1000\n2024-01-03,101,103,100,102,1001\n2024-01-04,102,104,101,103,1002\n2024-01-05,103,105,102,104,1003\n";
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

test("@portable-upload explicit unverified declaration persists without rewriting unknown defaults", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000);
  await signIn(page);
  const auth = await token(request);
  await page.goto("/datasets/upload");
  const consent = page.getByRole("checkbox", {
    name: "Enable portable research using my unverified raw/no-actions declaration",
  });
  await expect(consent).not.toBeChecked();
  await page.getByLabel("CSV or Parquet file", { exact: true }).setInputFiles({
    name: "research.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(csv),
  });
  await page
    .getByLabel("Upload snapshot name", { exact: true })
    .fill(`Unverified declaration ${crypto.randomUUID()}`);
  await page.getByLabel("Upload symbol", { exact: true }).fill("E2ECLAIM");
  await page
    .getByLabel("Upload end · exclusive", { exact: true })
    .fill("2024-01-06");
  await consent.check();
  await page
    .getByLabel("Declaration source reference", { exact: true })
    .fill("Hosted test reference, not market evidence");
  await page
    .getByLabel("Basis for raw/no-actions assumption", { exact: true })
    .fill(
      "Fabricated test-only table; declaration is not independent verification",
    );
  await page
    .getByRole("button", { name: "Validate and import file", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "No semantics are changed automatically",
  );
  await expect(
    page.getByLabel("Upload split basis", { exact: true }),
  ).toHaveValue("unknown");
  await page.getByLabel("Native price mode", { exact: true }).fill("raw");
  await page
    .getByLabel("Upload split basis", { exact: true })
    .selectOption("unadjusted");
  await page
    .getByLabel("Upload dividend basis", { exact: true })
    .selectOption("unadjusted");
  await page.getByLabel("Volume unit", { exact: true }).selectOption("shares");
  const submitted = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/data/uploads" &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Validate and import file", exact: true })
    .click();
  expect((await submitted).status()).toBe(202);
  await expect(page).toHaveURL(/\/imports\/[0-9a-f-]{36}$/);
  const id = new URL(page.url()).pathname.split("/").at(-1)!;
  await expect
    .poll(async () => (await readImport(request, auth, id)).status, {
      timeout: 120_000,
    })
    .toBe("ready");
  const imported = await readImport(request, auth, id);
  const claim = (imported.request as UploadMapping).portable_declaration!;
  expect(claim).toMatchObject({
    acknowledged_unverified: true,
    symbol: "E2ECLAIM",
    start: "2024-01-02",
    end: "2024-01-06",
  });
  const response = await request.get(
    api(`/datasets/${imported.dataset_version_id}`),
    { headers: authorization(auth) },
  );
  expect(response.ok()).toBe(true);
  const snapshot = await response.json();
  expect(snapshot.is_synthetic).toBe(false);
  expect(snapshot.manifest.data_evidence).toBe("user_declaration_unverified");
  expect(snapshot.manifest.provenance[0].evidence).toBe("uploaded");
  expect(
    snapshot.manifest.artifact_files["portable-upload-declaration.json"],
  ).toMatch(/^[0-9a-f]{64}$/);
  expect(snapshot.manifest.upload_source.sha256).toBe(
    snapshot.manifest.artifact_files["source.csv"],
  );
  await page
    .getByRole("link", { name: "Open published snapshot", exact: true })
    .click();
  await page.reload();
  await expect(
    page.getByText("User upload · unverified source", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("not independently verified", { exact: false }).first(),
  ).toBeVisible();
  const post = async (path: string, data: unknown, status = 201) => {
    const response = await request.post(api(path), {
      headers: {
        ...authorization(auth),
        "Idempotency-Key": crypto.randomUUID(),
      },
      data,
    });
    expect(response.status()).toBe(status);
    return response.json();
  };
  const universe = await post("/data/universes", {
    name: "Explicit upload research-assumption basket",
    state: "user_fixed",
    as_of: new Date().toISOString(),
    members: snapshot.manifest.listings.map(
      (listing: { listing_id: string }) => ({ listing_id: listing.listing_id }),
    ),
    evidence: [],
  });
  const refs = {
    dataset: { id: snapshot.id, sha256: snapshot.content_sha256 },
    universe: { id: universe.id, sha256: universe.content_sha256 },
  };
  const seed = await post("/portable-strategies", {
    ...refs,
    name: "Declared-upload event research seed",
    strategy_kind: "event",
  });
  const config = await post(
    "/portable-runs/default-config",
    {
      ...refs,
      seed_strategy: { id: seed.id, sha256: seed.content_sha256 },
      execution_mode: "seed_evaluation",
      windows: { train: { start: "2024-01-04", end: "2024-01-06" } },
    },
    200,
  );
  expect(config.model.provider).toBe("fixture");
  const preflight = await post("/portable-runs/preflight", config, 200);
  expect(preflight.is_synthetic).toBe(false);
  expect(preflight.data_evidence).toBe("user_declaration_unverified");
  expect(preflight.warnings.join(" ")).toContain("unverified");
  const run = await post("/portable-runs", config, 202);
  const readRun = async (): Promise<PortableRunDetail> => {
    const response = await request.get(api(`/portable-runs/${run.id}`), {
      headers: authorization(auth),
    });
    expect(response.status()).toBe(200);
    return response.json();
  };
  const terminal = new Set([
    "succeeded",
    "failed",
    "cancelled",
    "paused",
    "interrupted",
  ]);
  try {
    await expect
      .poll(async () => (await readRun()).status, { timeout: 150_000 })
      .toBe("succeeded");
    const evaluations = await request.get(
      api(`/portable-runs/${run.id}/evaluations`),
      { headers: authorization(auth) },
    );
    expect(evaluations.status()).toBe(200);
    const evaluated = (await evaluations.json()).data;
    expect(evaluated).toHaveLength(1);
    const result = await request.get(
      api(`/portable-evaluations/${evaluated[0].id}/results`),
      { headers: authorization(auth) },
    );
    expect(result.status()).toBe(200);
    expect(await result.json()).toMatchObject({
      data_evidence: "user_declaration_unverified",
      is_synthetic: false,
      ledger_origin: "trusted_engine",
      runtime_certified: false,
    });
    await page.goto(`/portable/evaluations/${evaluated[0].id}`);
    await page.reload();
    await expect(
      page
        .getByText("unverified user research assumptions", { exact: false })
        .first(),
    ).toBeVisible();
  } finally {
    if (!terminal.has((await readRun()).status)) {
      const cancelled = await request.post(
        api(`/portable-runs/${run.id}/cancel`),
        { headers: authorization(auth) },
      );
      expect(cancelled.status()).toBe(202);
    }
    await expect
      .poll(
        async () => {
          const value = await readRun();
          return (
            terminal.has(value.status) &&
            value.attempts.every((row) => row.sandbox_stopped)
          );
        },
        { timeout: 45_000 },
      )
      .toBe(true);
  }
});

test("@portable-upload known action columns cannot be omitted by a no-actions mapping", async ({
  request,
}) => {
  const auth = await token(request);
  const mapping: UploadMapping = {
    symbol: "E2EACTION",
    market: "US",
    mic: "XNAS",
    currency: "USD",
    timezone: "America/New_York",
    calendar: "XNYS",
    start_date: "2024-01-02",
    end_date: "2024-01-06",
    volume_unit: "shares",
    columns: {
      session_date: "Date",
      open: "Open",
      high: "High",
      low: "Low",
      close: "Close",
      volume: "Volume",
    },
    price_semantics: {
      native_mode: "raw",
      split_basis: "unadjusted",
      dividend_basis: "unadjusted",
    },
    portable_declaration: {
      kind: "user_declared_raw_no_actions",
      acknowledged_unverified: true,
      symbol: "E2EACTION",
      mic: "XNAS",
      start: "2024-01-02",
      end: "2024-01-06",
      source_reference: "Test source",
      evidence_note: "Deliberately contradictory test declaration",
    },
  };
  const withActions =
    csv
      .trimEnd()
      .split("\n")
      .map((line, index) => `${line},${index === 0 ? "Dividends" : "1"}`)
      .join("\n") + "\n";
  const response = await request.post(api("/data/uploads"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    multipart: {
      mapping: JSON.stringify(mapping),
      file: {
        name: "actions.csv",
        mimeType: "text/csv",
        buffer: Buffer.from(withActions),
      },
    },
  });
  expect(response.status()).toBe(202);
  const submitted = await response.json();
  await expect
    .poll(async () => (await readImport(request, auth, submitted.id)).status, {
      timeout: 120_000,
    })
    .toBe("partial");
  expect(
    (await readImport(request, auth, submitted.id)).dataset_version_id,
  ).toBeNull();
});
