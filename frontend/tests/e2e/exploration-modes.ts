import { expect, type APIRequestContext, type Page } from "@playwright/test";
import type {
  PriceReplay,
  ResultReturnAnalysis,
  Token,
} from "../../src/client/types.gen";
import type {
  ExplorationMode,
  ExplorationReference,
} from "../../src/lib/exploration-api";
import { api, authorization } from "./helpers";

// Called by real API/PG/queue/Docker browser scenarios. Never route-mocked.
export async function exerciseModeExplorer(
  page: Page,
  request: APIRequestContext,
  auth: Token,
  other: Token,
  mode: ExplorationMode,
  reference: ExplorationReference,
) {
  const roots = {
    native_research: "/research-evaluations",
    native_final: "/final-evaluations/evaluations",
    portable_research: "/portable-evaluations",
    portable_final: "/portable-final-evaluations/evaluations",
  };
  const base = `${roots[mode]}/${reference.evaluation_id}`;
  const analysisResponse = await request.get(api(base + "/return-analysis"), {
    headers: authorization(auth),
  });
  expect(analysisResponse.status()).toBe(200);
  const analysis = (await analysisResponse.json()) as ResultReturnAnalysis;
  expect(analysis.context).toMatchObject({
    mode,
    evaluation_id: reference.evaluation_id,
    artifact_sha256: reference.artifact_sha256,
    start: reference.window.start,
    end: reference.window.end,
  });
  expect(analysis.context.selection_id || undefined).toBe(
    reference.selection_id,
  );
  const listing = analysis.context.listings[0].listing_id;
  const replayResponse = await request.get(
    api(`${base}/price-replay?listing_id=${listing}`),
    { headers: authorization(auth) },
  );
  expect(replayResponse.status()).toBe(200);
  const replay = (await replayResponse.json()) as PriceReplay;
  expect(replay.context).toEqual(analysis.context);
  expect(replay.bars.length).toBeGreaterThan(0);
  const explorer = page.getByTestId(`${mode}-explorer`);
  await expect(explorer).toBeVisible();
  await expect(
    explorer.getByRole("img", {
      name: "Pinned OHLCV, actual execution markers and declared recorded indicators",
    }),
  ).toBeVisible();
  await expect(
    explorer.getByRole("table", { name: "Monthly return values and coverage" }),
  ).toBeVisible();
  if (mode.startsWith("portable")) {
    expect(replay.indicators_status).toBe("not_persisted");
    expect(replay.indicators).toEqual([]);
    await expect(explorer).toContainText(
      "portable trusted-engine protocol does not persist declared indicator observations",
    );
  }
  if (replay.markers.length) {
    await explorer
      .getByRole("button", {
        name: `Select ${replay.markers[0].id}`,
        exact: true,
      })
      .click();
    await expect(
      explorer.getByRole("region", { name: "Selected execution details" }),
    ).toContainText(replay.markers[0].timestamp);
    if (mode.startsWith("portable"))
      await expect(
        explorer.getByRole("region", { name: "Selected execution details" }),
      ).toContainText(replay.markers[0].economic_reference_time!);
    if (replay.markers.length > 1) {
      await explorer
        .getByRole("button", { name: "Next execution", exact: true })
        .click();
      await expect(
        explorer.getByRole("region", { name: "Selected execution details" }),
      ).toContainText(replay.markers[1].timestamp);
    }
  } else {
    await expect(
      explorer.getByRole("button", { name: "Next execution", exact: true }),
    ).toBeDisabled();
  }
  const resultSuffix = mode.endsWith("final") ? "/result" : "/results";
  const sectionParam = mode.startsWith("portable") ? "section" : "sections";
  const sorted = await request.get(
    api(
      `${base}${resultSuffix}?${sectionParam}=fills&sort_by=price&sort_direction=desc&side=buy&limit=1`,
    ),
    { headers: authorization(auth) },
  );
  expect(sorted.status()).toBe(200);
  expect((await sorted.json()).artifact_sha256).toBe(reference.artifact_sha256);
  for (const suffix of [
    "/return-analysis",
    `/price-replay?listing_id=${listing}`,
  ]) {
    expect(
      (
        await request.get(api(base + suffix), { headers: authorization(other) })
      ).status(),
    ).toBe(404);
  }
  await page.reload();
  await expect(page.getByTestId(`${mode}-explorer`)).toBeVisible();
}
