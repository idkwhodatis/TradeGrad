import { readFile } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";
import type {
  DecisionDetail,
  DecisionIndex,
  DecisionPage,
  ResearchNoteRevision,
  ResearchTemplate,
  StrategyDetail,
  WorkbenchSpec,
} from "../../src/client/types.gen";
import { account, api, authorization, signIn, token } from "./helpers";
import {
  createActualSource,
  query,
  read,
  tracePins,
  verificationUrl,
} from "./independent-helpers";

// I6 actual-stack scenarios: authenticated PostgreSQL + real browser. The
// second case additionally runs the actual isolated maintained native strategy
// on the explicitly synthetic frozen upload. No route stubs, manufactured
// results/traces, model calls or provider downloads. --list is collection only.
async function save(page: Page, path = "/research-notes") {
  const response = page.waitForResponse(
    (value) =>
      new URL(value.url()).pathname === `/api/v1${path}` &&
      value.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save immutable note revision", exact: true })
    .click();
  const saved = await response;
  expect(saved.status(), await saved.text()).toBe(201);
  const note = (await saved.json()) as ResearchNoteRevision;
  await expect(page.getByTestId("saved-research-note")).toContainText(
    note.content.title,
  );
  await expect
    .poll(() => new URL(page.url()).searchParams.get("note_revision_sha256"))
    .toBe(note.revision_sha256);
  return note;
}
async function relogin(page: Page, url: string) {
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.goto(url);
  await expect(page).toHaveURL(/\/login\?next=/);
  expect(new URL(page.url()).searchParams.get("next")).toBe(
    new URL(url).pathname + new URL(url).search,
  );
  await page.getByLabel("Email address").fill(account().email);
  await page.getByLabel("Password", { exact: true }).fill(account().password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(url);
}

test("rule notes persist exact revisions, search, optimistic conflicts, escaped Markdown and owner admission", async ({
  page,
  request,
}) => {
  const auth = await token(request);
  const templates = await read<{ data: ResearchTemplate[] }>(
    request,
    auth,
    "/research-templates",
  );
  const template = templates.data.find(
    (item) => item.template_id === "cross_sectional",
  );
  expect(template).toBeDefined();
  const created = await request.post(api("/strategy-versions"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    data: {
      name: `I6 notes source ${crypto.randomUUID()}`,
      task: "cn_cross",
      code: template!.code,
      manifest: template!.manifest,
      parent_id: null,
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const strategy = (await created.json()) as StrategyDetail;
  const spec = await read<WorkbenchSpec>(
    request,
    auth,
    `/strategy-versions/${strategy.id}/spec`,
  );
  const rule = spec.spec.rules.find((item) => item.source_ref)!;
  expect(rule).toBeDefined();
  await signIn(page);
  await page.goto(
    `/strategies/${strategy.id}/research?${query({ tab: "description", code_sha256: spec.code_sha256, spec_revision: spec.revision, spec_sha256: spec.spec_sha256, rule_id: rule.rule_id, ...(rule.stage_id ? { stage_id: rule.stage_id } : {}) })}`,
  );
  await page
    .getByRole("button", { name: "Write note from this evidence", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: "Create note from current evidence",
      exact: true,
    })
    .click();
  const title = `I6 immutable rule note ${crypto.randomUUID()}`;
  await page.getByLabel("Note title", { exact: true }).fill(title);
  await page
    .getByLabel("Tags, comma-separated", { exact: true })
    .fill("liquidity, i6-note");
  await page
    .getByLabel("Hypothesis", { exact: true })
    .fill("Liquidity eligibility and ranking must remain separate mechanisms.");
  await page
    .getByLabel("Counterexamples", { exact: true })
    .fill("A liquid candidate may still lose on rank.");
  await page
    .getByLabel("Applicability and limitations", { exact: true })
    .fill("Source-only note: no runtime or causal result is claimed.");
  await page
    .getByLabel("Note body", { exact: true })
    .fill(
      '<script>alert("not executable")</script> Profit text must not change pending status.',
    );
  const first = await save(page);
  expect(first.content.status).toBe("pending");
  expect(first.evidence.every((item) => item.state === "available")).toBe(true);
  expect(first.content.evidence?.[0]).toMatchObject({
    kind: "source",
    source_id: strategy.id,
    spec_revision: spec.revision,
    spec_sha256: spec.spec_sha256,
    code_sha256: spec.code_sha256,
    source_ref: rule.source_ref,
  });
  const firstUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(firstUrl);
  await expect(page.getByTestId("saved-research-note")).toContainText(
    "Profit text must not change pending status.",
  );
  expect(
    await page.locator("script").filter({ hasText: "not executable" }).count(),
  ).toBe(0);
  await page
    .getByLabel("Search titles, tags and body", { exact: true })
    .fill("i6-note");
  await page.getByRole("button", { name: "Search notes", exact: true }).click();
  await expect(
    page.getByRole("button", { name: `Open note: ${title}`, exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Edit this note revision", exact: true })
    .click();
  await page
    .getByLabel("Motivation", { exact: true })
    .fill("This draft must survive a genuine concurrent revision.");
  const concurrent = await request.post(
    api(`/research-notes/${first.note_id}/revisions`),
    {
      headers: {
        ...authorization(auth),
        "Idempotency-Key": crypto.randomUUID(),
      },
      data: {
        base_revision: first.revision,
        base_revision_sha256: first.revision_sha256,
        content: { ...first.content, status: "mixed" },
        change_note: "Concurrent real API revision",
      },
    },
  );
  expect(concurrent.status(), await concurrent.text()).toBe(201);
  const second = (await concurrent.json()) as ResearchNoteRevision;
  const conflict = page.waitForResponse(
    (value) =>
      new URL(value.url()).pathname ===
        `/api/v1/research-notes/${first.note_id}/revisions` &&
      value.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save immutable note revision", exact: true })
    .click();
  expect((await conflict).status()).toBe(409);
  await expect(
    page.getByRole("alert").filter({ hasText: "changed since editing began" }),
  ).toBeVisible();
  await expect(page.getByLabel("Motivation", { exact: true })).toHaveValue(
    "This draft must survive a genuine concurrent revision.",
  );
  await expect(
    page.getByRole("button", { name: "Discard local note draft", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Discard local note draft", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: "Open latest saved note explicitly",
      exact: true,
    })
    .click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("note_revision_sha256"))
    .toBe(second.revision_sha256);
  await page
    .getByRole("button", { name: "Open note revision 1", exact: true })
    .click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("note_revision_sha256"))
    .toBe(first.revision_sha256);
  await expect(page.getByTestId("saved-research-note")).toContainText(
    "Pending validation",
  );
  const downloadPromise = page.waitForEvent("download");
  await page
    .getByRole("button", {
      name: "Download Markdown with references",
      exact: true,
    })
    .click();
  const download = await downloadPromise,
    path = await download.path();
  expect(path).not.toBeNull();
  const markdown = await readFile(path!, "utf8");
  expect(markdown).toContain(first.revision_sha256);
  expect(markdown).toContain(spec.code_sha256);
  expect(markdown).not.toContain("<script>");
  const pinned = page.url();
  await relogin(page, pinned);
  await expect(page.getByTestId("saved-research-note")).toContainText(
    first.revision_sha256,
  );
  const other = await token(request, true);
  for (const suffix of [
    "",
    `/revisions/1?revision_sha256=${first.revision_sha256}`,
    "/revisions",
    `/revisions/1/export?revision_sha256=${first.revision_sha256}`,
  ]) {
    const denied = await request.get(
      api(`/research-notes/${first.note_id}${suffix}`),
      { headers: authorization(other) },
    );
    expect(denied.status()).toBe(404);
    expect(await denied.text()).not.toContain(title);
  }
  const unchanged = await read<ResearchNoteRevision>(
    request,
    auth,
    `/research-notes/${first.note_id}/revisions/1?revision_sha256=${first.revision_sha256}`,
  );
  expect(unchanged.content).toEqual(first.content);
});

test("actual recorded decision note restores original code/spec/leaf/event/instrument/stage after spec edit and relogin", async ({
  page,
  request,
}) => {
  test.setTimeout(360000);
  const owned = { auth: await token(request), runs: new Set<string>() };
  const source = await createActualSource(request, owned),
    pins = tracePins(source.leaf);
  const events = await read<DecisionPage>(
    request,
    owned.auth,
    `/evaluations/${source.leaf.evaluation_id}/decisions?${query({ ...pins, limit: 25, offset: 0 })}`,
  );
  let event: DecisionIndex | undefined;
  let observation:
    | NonNullable<DecisionDetail["predicates"]["items"]>[number]
    | NonNullable<DecisionDetail["features"]["items"]>[number]
    | undefined;
  for (const candidate of events.items ?? []) {
    const detail = await read<DecisionDetail>(
      request,
      owned.auth,
      `/evaluations/${source.leaf.evaluation_id}/decisions/${candidate.decision_id}?${query({ ...pins, limit: 50, offset: 0 })}`,
    );
    observation =
      detail.predicates.items?.find((row) => row.observation.instrument_id) ??
      detail.features.items?.find((row) => row.observation.instrument_id);
    if (observation) {
      event = candidate;
      break;
    }
  }
  expect(event).toBeDefined();
  expect(
    observation,
    "The maintained template must emit a real instrument-scoped condition or feature",
  ).toBeDefined();
  const kind =
    "condition_id" in observation!.observation ? "predicate" : "feature";
  const record =
    "condition_id" in observation!.observation
      ? observation!.observation.condition_id
      : observation!.observation.feature_id;
  const url = new URL(verificationUrl(source), "http://placeholder.invalid");
  const expected = {
    tab: "replay",
    decision_id: event!.decision_id,
    session_date: event!.session_date,
    instrument_id: observation!.observation.instrument_id!,
    stage_id: observation!.stage_id,
    record_kind: kind,
    record_id: record,
    record_ordinal: observation!.ordinal,
  };
  Object.entries(expected).forEach(([key, value]) =>
    url.searchParams.set(key, String(value)),
  );
  await signIn(page);
  await page.goto(url.pathname + url.search);
  await page
    .getByRole("button", { name: "Write note from this evidence", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: "Create note from current evidence",
      exact: true,
    })
    .click();
  await page
    .getByLabel("Note title", { exact: true })
    .fill(`I6 real decision ${crypto.randomUUID()}`);
  await page
    .getByLabel("Counterexamples", { exact: true })
    .fill(
      "Observation from one frozen execution; future behavior remains untested.",
    );
  const note = await save(page);
  expect(
    note.content.evidence?.find((ref) => ref.kind === "decision"),
  ).toMatchObject({
    kind: "decision",
    decision_id: event!.decision_id,
    instrument_id: expected.instrument_id,
    stage_id: expected.stage_id,
    record_ordinal: expected.record_ordinal,
    leaf: { evaluation_id: source.leaf.evaluation_id, ...pins },
  });
  const revised = await request.post(
    api(`/strategy-versions/${source.strategy.id}/spec/revisions`),
    {
      headers: {
        ...authorization(owned.auth),
        "Idempotency-Key": crypto.randomUUID(),
      },
      data: {
        code_sha256: source.spec.code_sha256,
        base_revision: source.spec.revision,
        base_spec_sha256: source.spec.spec_sha256,
        rules: [
          {
            rule_id: source.spec.spec.rules[0].rule_id,
            title: source.spec.spec.rules[0].title,
            description:
              "A later human revision, not the saved note description.",
          },
        ],
        change_note: "Must not retarget prior note",
      },
    },
  );
  expect(revised.status(), await revised.text()).toBe(201);
  const noteUrl = page.url();
  await relogin(page, noteUrl);
  await expect(page.getByTestId("saved-research-note")).toContainText(
    note.revision_sha256,
  );
  await page
    .getByRole("link", {
      name: "Restore full saved research selection",
      exact: true,
    })
    .click();
  for (const [key, value] of Object.entries({
    ...expected,
    ...pins,
    evaluation_id: source.leaf.evaluation_id,
    code_sha256: source.spec.code_sha256,
    spec_revision: source.spec.revision,
    spec_sha256: source.spec.spec_sha256,
  }))
    await expect
      .poll(() => new URL(page.url()).searchParams.get(key))
      .toBe(String(value));
  const restored = page.url();
  await page.reload();
  await expect(page).toHaveURL(restored);
  await expect(page.getByTestId("workbench-selection")).toContainText(
    source.leaf.result_manifest_sha256!,
  );
  await expect(
    page.getByRole("alert").filter({ hasText: "No newer" }),
  ).toHaveCount(0);
});
