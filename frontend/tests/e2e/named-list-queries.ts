import { expect, type Page } from "@playwright/test";

export async function exerciseCandidateQueries(
  page: Page,
  runId: string,
  portable: boolean,
) {
  await page.goto(
    portable ? `/portable/runs/${runId}` : `/research-runs/${runId}`,
  );
  const group = page.getByRole("group", {
    name: "Candidate server query controls",
  });
  await expect(group).toBeVisible();
  await group
    .getByLabel("Candidate server sort", { exact: true })
    .selectOption("candidate_key");
  await group
    .getByLabel("Candidate sort direction", { exact: true })
    .selectOption("desc");
  await group
    .getByLabel("Candidate kind", { exact: true })
    .selectOption("seed");
  const pending = page.waitForResponse(
    (response) =>
      response.url().includes(`/${runId}/candidates?`) &&
      response.url().includes("sort_by=candidate_key") &&
      response.url().includes("kind=seed"),
  );
  await group
    .getByRole("button", { name: "Apply candidate query", exact: true })
    .click();
  const response = await pending;
  expect(response.status()).toBe(200);
  const rows = await response.json();
  expect(rows.count).toBeGreaterThan(0);
  expect(rows.data.every((row: { kind: string }) => row.kind === "seed")).toBe(
    true,
  );
  await group
    .getByRole("button", { name: "Reset candidate query", exact: true })
    .click();
}
