import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import {
  test as base,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type {
  CandidatePublic,
  CoveragePublic,
  DatasetPublic,
  EvaluationPublic,
  ImportDetail,
  ImportPublic,
  ResearchCapabilities,
  ResearchRunCreate,
  ResearchRunDetail,
  ResearchRunPublic,
  ResearchTemplate as Template,
  WorkbenchEvaluation as Leaf,
  WorkbenchLineage as Lineage,
  WorkbenchSpec as Spec,
  StrategyDetail,
  StrategyPublic,
  Token,
  TraceManifestPublic,
  UniversePublic,
} from "../../src/client/types.gen";
import { account, api, authorization, signIn, token } from "./helpers";

// I2 acceptance requires the real authenticated, migrated PostgreSQL API.
// The final case additionally requires the data/research workers and isolated
// Docker evaluator. Prices are the frozen synthetic fixture, never market data.
// No successful response or completed trace is fabricated. One lost-response
// test forwards its mutation to the real server before dropping the response.
// Strategy source is never executed on the browser-test host.
// Typechecking and --list collect this suite; they do not execute a browser.
type Owned = { auth: Token; runs: Set<string> };
const test = base.extend<{ owned: Owned }>({
  owned: async ({ request }, use) => {
    await use({ auth: await token(request), runs: new Set() });
  },
});
const sha256 = (value: string | Buffer) =>
  createHash("sha256").update(value).digest("hex");
const terminal = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "paused",
]);

async function read<T>(request: APIRequestContext, auth: Token, path: string) {
  const response = await request.get(api(path), {
    headers: authorization(auth),
  });
  expect(response.status(), `Real GET ${path}`).toBe(200);
  return (await response.json()) as T;
}
async function rows<T>(request: APIRequestContext, auth: Token, path: string) {
  const result: T[] = [];
  for (;;) {
    const next = await read<{ data: T[]; count: number }>(
      request,
      auth,
      `${path}?offset=${result.length}&limit=100`,
    );
    result.push(...next.data);
    if (result.length >= next.count) return result;
    expect(
      next.data.length,
      `Pagination must advance for ${path}`,
    ).toBeGreaterThan(0);
  }
}
const sourceSpec = (request: APIRequestContext, auth: Token, id: string) =>
  read<Spec>(request, auth, `/strategy-versions/${id}/spec`);
const workbench = (
  id: string,
  values: Record<string, string | number> = {},
) => {
  const query = new URLSearchParams(
    Object.entries(values).map(([key, value]) => [key, String(value)]),
  );
  return `/strategies/${id}/research${query.size ? `?${query}` : ""}`;
};
async function expectSelection(
  page: Page,
  values: Record<string, string | number>,
) {
  await expect
    .poll(() => {
      const query = new URL(page.url()).searchParams;
      return Object.fromEntries(
        Object.keys(values).map((key) => [key, query.get(key)]),
      );
    })
    .toEqual(
      Object.fromEntries(
        Object.entries(values).map(([key, value]) => [key, String(value)]),
      ),
    );
}
async function createTemplate(
  page: Page,
  request: APIRequestContext,
  auth: Token,
  templateId: Template["template_id"],
) {
  const templates = await rows<Template>(request, auth, "/research-templates");
  const template = templates.find((item) => item.template_id === templateId);
  expect(
    template,
    "The app must use backend-maintained templates",
  ).toBeDefined();
  expect(sha256(template!.code)).toBe(template!.code_sha256);
  await page.goto("/strategies");
  await page
    .getByRole("button", {
      name:
        templateId === "daily_timing"
          ? "Use Daily timing template"
          : "Use Cross-sectional template",
      exact: true,
    })
    .click();
  await expect(page.getByLabel("Python source", { exact: true })).toHaveValue(
    template!.code,
  );
  const name = `I2 ${templateId} ${crypto.randomUUID()}`;
  await page.getByLabel("Version name", { exact: true }).fill(name);
  const saved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/strategy-versions" &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  const response = await saved;
  expect(response.status()).toBe(201);
  const strategy = (await response.json()) as StrategyDetail;
  expect(strategy.code).toBe(template!.code);
  expect(strategy.content_sha256).toBe(template!.code_sha256);
  expect(strategy.manifest).toEqual(template!.manifest);
  expect(strategy.task).toBe(
    templateId === "daily_timing" ? "cn_time" : "cn_cross",
  );
  await expect(
    page.getByRole("heading", { name, exact: true }).first(),
  ).toBeVisible();
  return strategy;
}
async function openWorkbench(page: Page) {
  await page
    .getByRole("link", { name: "Open research workbench", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Strategy research workbench",
      exact: true,
    }),
  ).toBeVisible();
}

test.beforeAll(async ({ request }) => {
  account();
  account(true);
  expect(
    (await request.get(api("/health/ready"))).ok(),
    "Run against the real migrated PostgreSQL stack, not a mock service",
  ).toBe(true);
});
test.afterEach(async ({ request, owned }) => {
  // Only stop runs accepted by this test; never sweep another suite's work.
  for (const id of owned.runs) {
    const run = await read<ResearchRunDetail>(
      request,
      owned.auth,
      `/research-runs/${id}`,
    );
    if (!terminal.has(run.status) || run.status === "paused") {
      const response = await request.post(api(`/research-runs/${id}/cancel`), {
        headers: authorization(owned.auth),
      });
      expect(response.status()).toBe(202);
    }
    await expect
      .poll(
        async () => {
          const current = await read<ResearchRunDetail>(
            request,
            owned.auth,
            `/research-runs/${id}`,
          );
          const children = await rows<EvaluationPublic>(
            request,
            owned.auth,
            `/research-runs/${id}/evaluations`,
          );
          return (
            terminal.has(current.status) &&
            current.attempts.every((attempt) => attempt.sandbox_stopped) &&
            children.every(
              (child) => child.sandbox_stopped && child.finished_at !== null,
            )
          );
        },
        { timeout: 45_000, intervals: [250, 500, 1000] },
      )
      .toBe(true);
  }
});

for (const templateId of ["daily_timing", "cross_sectional"] as const) {
  test(`${templateId}: real app creation binds graph, rules and exact source; keyboard cannot edit the graph`, async ({
    page,
    request,
    owned,
  }) => {
    await signIn(page);
    const strategy = await createTemplate(
      page,
      request,
      owned.auth,
      templateId,
    );
    const spec = await sourceSpec(request, owned.auth, strategy.id);
    expect(spec.spec.template_id).toBe(templateId);
    expect(spec.code_sha256).toBe(strategy.content_sha256);
    expect(spec.spec.code_sha256).toBe(strategy.content_sha256);
    expect(spec.spec.rules.length).toBeGreaterThan(1);
    expect(spec.spec.edges.length).toBeGreaterThan(0);
    await openWorkbench(page);
    await expect(page).toHaveURL(
      new RegExp(`/strategies/${strategy.id}/research`),
    );
    const graph = page.getByRole("region", {
      name: "Expected template rule graph",
    });
    await expect(graph).toBeVisible();
    await expect(graph).toContainText(
      "Expected template flow, not an execution trace",
    );
    await expect(graph.locator(".react-flow__node")).toHaveCount(
      spec.spec.rules.length,
    );
    await expect(graph.locator(".react-flow__edge")).toHaveCount(
      spec.spec.edges.length,
    );
    await expect(page.getByTestId("workbench-selection")).toContainText(
      "No actual leaf Evaluation selected",
    );
    await expect(
      page.getByRole("region", { name: "Actual selected stage observations" }),
    ).toHaveCount(0);
    for (const rule of spec.spec.rules) {
      expect(rule.mapping).toBe("controlled");
      expect(rule.source_ref.code_sha256).toBe(strategy.content_sha256);
      const card = page.getByTestId(`rule-${rule.rule_id}`);
      await expect(card).toContainText(rule.description);
      await expect(card).toContainText("static_inference");
      await expect(card).toContainText("No selected recorded stage");
      await page
        .getByRole("button", {
          name: `Inspect rule: ${rule.title}`,
          exact: true,
        })
        .click();
      await expect(card).toHaveAttribute("data-selected", "true");
      await expectSelection(page, { rule_id: rule.rule_id });
      const numbered = strategy.code
        .split(/\r\n|\n|\r/)
        .slice(rule.source_ref.line_start - 1, rule.source_ref.line_end)
        .map((line, index) => `${rule.source_ref.line_start + index}: ${line}`)
        .join("\n");
      await expect(
        page
          .getByRole("region", { name: "Selected source lines" })
          .locator("pre"),
      ).toHaveText(numbered);
      await expect(page.getByTestId("source-code-hash")).toContainText(
        strategy.content_sha256,
      );
    }
    const first = spec.spec.rules[0];
    const node = graph.getByLabel(`Expected rule: ${first.title}`, {
      exact: true,
    });
    await node.focus();
    await node.press("Enter");
    await expect(page.getByTestId(`rule-${first.rule_id}`)).toHaveAttribute(
      "data-selected",
      "true",
    );
    const positions = await graph
      .locator(".react-flow__node")
      .evaluateAll((nodes) =>
        nodes.map((item) => ({
          id: item.getAttribute("data-id"),
          transform: (item as HTMLElement).style.transform,
        })),
      );
    await node.press("ArrowRight");
    await node.press("Delete");
    await node.press("Backspace");
    expect(
      await graph.locator(".react-flow__node").evaluateAll((nodes) =>
        nodes.map((item) => ({
          id: item.getAttribute("data-id"),
          transform: (item as HTMLElement).style.transform,
        })),
      ),
    ).toEqual(positions);
    await expect(graph.locator(".react-flow__edge")).toHaveCount(
      spec.spec.edges.length,
    );
    expect(await sourceSpec(request, owned.auth, strategy.id)).toEqual(spec);
    expect(
      await read<StrategyDetail>(
        request,
        owned.auth,
        `/strategy-versions/${strategy.id}`,
      ),
    ).toEqual(strategy);

    if (templateId === "daily_timing") {
      const navigationWrites: string[] = [];
      page.on("request", (wire) => {
        if (
          wire.method() !== "GET" &&
          new URL(wire.url()).pathname.startsWith("/api/v1/")
        ) {
          navigationWrites.push(
            `${wire.method()} ${new URL(wire.url()).pathname}`,
          );
        }
      });
      await page
        .getByRole("link", { name: "Decision replay", exact: true })
        .click();
      await expectSelection(page, { tab: "replay", rule_id: first.rule_id });
      await expect(
        page.getByRole("heading", {
          name: "Decision event selection",
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        page.getByText(
          "Choose an actual leaf Evaluation before opening decision replay.",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(
        page.getByRole("button", {
          name: "Diagnostic replay unavailable until source admission",
          exact: true,
        }),
      ).toBeDisabled();
      // Later increments implemented these tabs. They must preserve the exact
      // selection without fabricating an evaluation or dispatching execution.
      for (const [name, tab, heading] of [
        [
          "Version comparison",
          "comparison",
          "Compare two immutable evaluations",
        ],
        [
          "Verification",
          "verification",
          "Independent diagnostics and manual controls",
        ],
        ["Notes", "notes", "Evidence research notes"],
      ]) {
        await page.getByRole("link", { name, exact: true }).click();
        await expectSelection(page, { tab, rule_id: first.rule_id });
        await expect(
          page.getByRole("heading", { name: heading, exact: true }),
        ).toBeVisible();
        expect(
          new URL(page.url()).searchParams.get("evaluation_id"),
        ).toBeNull();
        if (tab !== "notes") {
          await expect(
            page.getByLabel("Actual leaf Evaluation", { exact: true }),
          ).toHaveValue("");
        }
        await expect(page.getByText(/pending: not implemented/i)).toHaveCount(
          0,
        );
        if (tab === "comparison") {
          await expect(
            page.getByRole("button", {
              name: "Create immutable comparison",
              exact: true,
            }),
          ).toBeDisabled();
          await expect(
            page.getByLabel("Left saved strategy", { exact: true }),
          ).toHaveValue("");
          await expect(
            page.getByLabel("Right saved strategy", { exact: true }),
          ).toHaveValue("");
        }
        if (tab === "verification") {
          await expect(
            page.getByText(
              "Select an admitted successful physical leaf Evaluation before independent verification.",
              { exact: true },
            ),
          ).toBeVisible();
          await expect(
            page.getByRole("button", {
              name: "Create independent diagnostic replay",
              exact: true,
            }),
          ).toHaveCount(0);
        }
        if (tab === "notes") {
          await expect(
            page.getByRole("button", {
              name: "Create note from current evidence",
              exact: true,
            }),
          ).toBeVisible();
        }
      }
      await page
        .getByRole("link", { name: "Description", exact: true })
        .click();
      await expect(page.getByTestId(`rule-${first.rule_id}`)).toHaveAttribute(
        "data-selected",
        "true",
      );
      expect(navigationWrites).toEqual([]);
    }
  });
}

test("human description revisions preserve code and old evidence through reload, history and login", async ({
  page,
  request,
  owned,
}) => {
  await signIn(page);
  const strategy = await createTemplate(
    page,
    request,
    owned.auth,
    "daily_timing",
  );
  const original = await sourceSpec(request, owned.auth, strategy.id);
  await openWorkbench(page);
  const rule = original.spec.rules[0];
  await page
    .getByRole("button", { name: `Inspect rule: ${rule.title}`, exact: true })
    .click();
  await page
    .getByRole("button", { name: "Edit description", exact: true })
    .click();
  await page
    .getByLabel(`Rule title: ${rule.rule_id}`, { exact: true })
    .fill("Unsaved text must not become a revision");
  await page
    .getByRole("button", { name: "Cancel description edit", exact: true })
    .click();
  await expect(
    page.getByRole("form", { name: "Edit human description" }),
  ).toHaveCount(0);
  expect(await sourceSpec(request, owned.auth, strategy.id)).toEqual(original);
  await page
    .getByRole("button", { name: "Edit description", exact: true })
    .click();
  await expect(
    page.getByLabel(`Rule title: ${rule.rule_id}`, { exact: true }),
  ).toHaveValue(rule.title);
  const revisedTitle = `Human interpretation ${crypto.randomUUID()}`;
  const revisedDescription =
    "Human explanation only: this text does not execute a condition or prove an observed branch outcome.";
  await page
    .getByLabel(`Rule title: ${rule.rule_id}`, { exact: true })
    .fill(revisedTitle);
  await page
    .getByLabel(`Rule description: ${rule.rule_id}`, { exact: true })
    .fill(revisedDescription);
  await page
    .getByLabel("Revision change note", { exact: true })
    .fill("I2 immutable prose-revision acceptance");
  const saved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/api/v1/strategy-versions/${strategy.id}/spec/revisions` &&
      response.request().method() === "POST",
  );
  let submissions = 0;
  page.on("request", (wire) => {
    if (
      new URL(wire.url()).pathname ===
        `/api/v1/strategy-versions/${strategy.id}/spec/revisions` &&
      wire.method() === "POST"
    )
      submissions++;
  });
  // Same-turn submits exercise the synchronous in-flight guard, not just a
  // disabled button after React has already rendered its pending state.
  await page
    .getByRole("form", { name: "Edit human description" })
    .evaluate((element) => {
      const form = element as HTMLFormElement;
      form.requestSubmit();
      form.requestSubmit();
    });
  const response = await saved;
  expect(response.status()).toBe(201);
  expect(submissions).toBe(1);
  const revised = (await response.json()) as Spec;
  expect(revised.revision).toBe(original.revision + 1);
  expect(revised.origin).toBe("human");
  expect(revised.spec_sha256).not.toBe(original.spec_sha256);
  expect(revised.code_sha256).toBe(original.code_sha256);
  expect(revised.spec.edges).toEqual(original.spec.edges);
  const changed = revised.spec.rules.find(
    (item) => item.rule_id === rule.rule_id,
  )!;
  expect(changed).toEqual({
    ...rule,
    title: revisedTitle,
    description: revisedDescription,
    source: "human_explanation",
  });
  expect(
    revised.spec.rules.filter((item) => item.rule_id !== rule.rule_id),
  ).toEqual(
    original.spec.rules.filter((item) => item.rule_id !== rule.rule_id),
  );
  await expect(page.getByTestId(`rule-${rule.rule_id}`)).toContainText(
    revisedDescription,
  );
  await expectSelection(page, {
    spec_revision: revised.revision,
    spec_sha256: revised.spec_sha256,
  });
  const newUrl = page.url();
  await page
    .getByLabel("Description revision", { exact: true })
    .selectOption(original.spec_sha256);
  await expect(page.getByTestId(`rule-${rule.rule_id}`)).toContainText(
    rule.description,
  );
  await expectSelection(page, {
    spec_revision: original.revision,
    spec_sha256: original.spec_sha256,
    rule_id: rule.rule_id,
  });
  const oldUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(oldUrl);
  await expect(page.getByTestId(`rule-${rule.rule_id}`)).toContainText(
    rule.description,
  );
  await page.goBack();
  await expect(page).toHaveURL(newUrl);
  await expect(page.getByTestId(`rule-${rule.rule_id}`)).toContainText(
    revisedDescription,
  );
  await page.goForward();
  await expect(page).toHaveURL(oldUrl);
  await expect(page.getByTestId(`rule-${rule.rule_id}`)).toContainText(
    rule.description,
  );

  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.goto(oldUrl);
  await expect(page).toHaveURL(/\/login\?/);
  const returnTo = new URL(page.url()).searchParams.get("next");
  expect(returnTo).toBe(new URL(oldUrl).pathname + new URL(oldUrl).search);
  // Submit the real redirected login form in place, without injecting a token.
  await page.getByLabel("Email address").fill(account().email);
  await page.getByLabel("Password", { exact: true }).fill(account().password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(oldUrl);
  await expect(page.getByTestId(`rule-${rule.rule_id}`)).toContainText(
    rule.description,
  );
  const pinned = await read<Spec>(
    request,
    owned.auth,
    `/strategy-versions/${strategy.id}/spec?revision=${original.revision}&spec_sha256=${original.spec_sha256}`,
  );
  expect(pinned).toEqual(original);
  expect(await sourceSpec(request, owned.auth, strategy.id)).toEqual(revised);
  expect(
    await read<StrategyDetail>(
      request,
      owned.auth,
      `/strategy-versions/${strategy.id}`,
    ),
  ).toEqual(strategy);
});

test("a description save with a lost response reloads and retries the same committed revision", async ({
  page,
  request,
  owned,
}) => {
  await signIn(page);
  const strategy = await createTemplate(
    page,
    request,
    owned.auth,
    "daily_timing",
  );
  const original = await sourceSpec(request, owned.auth, strategy.id);
  const rule = original.spec.rules[0];
  await openWorkbench(page);
  await page
    .getByRole("button", { name: "Edit description", exact: true })
    .click();
  const prose =
    "Persist this human explanation once, even if the successful network response is lost.";
  await page
    .getByLabel(`Rule description: ${rule.rule_id}`, { exact: true })
    .fill(prose);
  let firstKey: string | undefined;
  let dropped = false;
  const endpoint = `/api/v1/strategy-versions/${strategy.id}/spec/revisions`;
  await page.route(`**${endpoint}`, async (route) => {
    if (route.request().method() !== "POST" || dropped) return route.continue();
    dropped = true;
    firstKey = route.request().headers()["idempotency-key"];
    const realCommit = await route.fetch();
    expect(realCommit.status()).toBe(201);
    await route.abort("failed");
  });
  await page
    .getByRole("button", { name: "Save description revision", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Retry same description revision",
      exact: true,
    }),
  ).toBeEnabled();
  await expect(
    page.getByLabel(`Rule description: ${rule.rule_id}`, { exact: true }),
  ).toBeDisabled();
  expect(firstKey).toBeTruthy();
  const committed = await sourceSpec(request, owned.auth, strategy.id);
  expect(committed.revision).toBe(original.revision + 1);
  expect(
    committed.spec.rules.find((item) => item.rule_id === rule.rule_id)
      ?.description,
  ).toBe(prose);
  await page.reload();
  await expect(
    page.getByLabel(`Rule description: ${rule.rule_id}`, { exact: true }),
  ).toHaveValue(prose);
  const replay = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === endpoint &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", {
      name: "Retry same description revision",
      exact: true,
    })
    .click();
  const response = await replay;
  expect(response.status()).toBe(200);
  expect(response.request().headers()["idempotency-key"]).toBe(firstKey);
  expect(await response.json()).toEqual(committed);
  await expect(page.getByTestId(`rule-${rule.rule_id}`)).toContainText(prose);
  await expectSelection(page, {
    spec_revision: committed.revision,
    spec_sha256: committed.spec_sha256,
  });
  expect(
    await rows<Spec>(
      request,
      owned.auth,
      `/strategy-versions/${strategy.id}/spec/revisions`,
    ),
  ).toHaveLength(2);
  expect(
    await read<StrategyDetail>(
      request,
      owned.auth,
      `/strategy-versions/${strategy.id}`,
    ),
  ).toEqual(strategy);
});

test("manual derived lineage stays distinct from algorithm references and arbitrary code has no controlled graph", async ({
  page,
  request,
  owned,
}) => {
  await signIn(page);
  const parent = await createTemplate(
    page,
    request,
    owned.auth,
    "cross_sectional",
  );
  await page
    .getByRole("button", { name: "Edit as new version", exact: true })
    .click();
  const childName = `I2 manual child ${crypto.randomUUID()}`;
  const code =
    "# Arbitrary source is stored and syntax-parsed, never executed here.\nvalue = 1\n";
  await page.getByLabel("Version name", { exact: true }).fill(childName);
  await page.getByLabel("Python source", { exact: true }).fill(code);
  await page
    .getByLabel("Native declaration JSON", { exact: true })
    .fill("null");
  const saved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/strategy-versions" &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Save new version", exact: true })
    .click();
  const response = await saved;
  expect(response.status()).toBe(201);
  const child = (await response.json()) as StrategyDetail;
  expect(child.parent_id).toBe(parent.id);
  expect(child.code).toBe(code);
  expect(child.content_sha256).toBe(sha256(code));
  const spec = await sourceSpec(request, owned.auth, child.id);
  expect(spec.spec.template_id).toBeNull();
  expect(spec.spec.coverage).not.toBe("complete");
  expect(spec.spec.missing_reasons.length).toBeGreaterThan(0);
  expect(spec.spec.edges).toEqual([]);
  expect(
    spec.spec.rules.every(
      (rule) => rule.stage_id === null && rule.mapping === "unmapped",
    ),
  ).toBe(true);
  await openWorkbench(page);
  await expect(
    page.getByText("No controlled rule graph.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Expected template rule graph" }),
  ).toHaveCount(0);
  await expect(page.getByTestId("source-code-hash")).toContainText(
    child.content_sha256,
  );
  const lineage = page.getByRole("complementary", { name: "Version lineage" });
  await expect(lineage).toBeVisible();
  await expect(lineage).toContainText(childName);
  const stored = await read<Lineage>(
    request,
    owned.auth,
    `/strategy-versions/${child.id}/lineage`,
  );
  expect(stored.focus.kind).toBe("derived");
  expect(stored.parent?.id).toBe(parent.id);
  expect(stored.reference_candidates).toEqual([]);
  await expect(
    lineage.getByRole("heading", {
      name: "Algorithm references (not code parents)",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    lineage.getByText("No algorithm references recorded.", { exact: true }),
  ).toBeVisible();
  await lineage
    .getByRole("button", { name: `Open version: ${parent.name}`, exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`/strategies/${parent.id}/research`));
  await expect(page.getByTestId("source-code-hash")).toContainText(
    parent.content_sha256,
  );
  const descendants = lineage.getByText(`Descendants of ${parent.name}`, {
    exact: true,
  });
  if (await descendants.evaluate((summary) => summary.closest("details")!.open))
    await descendants.click();
  await expect(
    lineage.getByRole("button", {
      name: `Open version: ${childName}`,
      exact: true,
    }),
  ).toBeHidden();
  await descendants.click();
  await lineage
    .getByRole("button", { name: `Open version: ${childName}`, exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`/strategies/${child.id}/research`));
  await expect(page.getByTestId("source-code-hash")).toContainText(
    child.content_sha256,
  );
  expect(
    await read<StrategyDetail>(
      request,
      owned.auth,
      `/strategy-versions/${parent.id}`,
    ),
  ).toEqual(parent);
});

test("missing or mismatched source and revision pins never silently load newer evidence or another owner's strategy", async ({
  page,
  request,
  owned,
}) => {
  await signIn(page);
  const strategy = await createTemplate(
    page,
    request,
    owned.auth,
    "daily_timing",
  );
  const original = await sourceSpec(request, owned.auth, strategy.id);
  const wrongHash =
    original.code_sha256 === "0".repeat(64) ? "1".repeat(64) : "0".repeat(64);
  const invalidSelections: Record<string, string | number>[] = [
    { code_sha256: wrongHash },
    { spec_revision: original.revision, spec_sha256: wrongHash },
    {
      spec_revision: original.revision + 999,
      spec_sha256: original.spec_sha256,
    },
    { spec_revision: original.revision },
    { evaluation_id: crypto.randomUUID(), result_manifest_sha256: wrongHash },
  ];
  for (const search of invalidSelections) {
    await page.goto(workbench(strategy.id, search));
    await expect(page.getByRole("alert").first()).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Expected template rule graph" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Edit description", exact: true }),
    ).toHaveCount(0);
    await expectSelection(page, search);
  }
  const missingId = crypto.randomUUID();
  await page.goto(workbench(missingId));
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expect(page.getByTestId("source-code-hash")).toHaveCount(0);
  const stranger = await token(request, true);
  for (const suffix of ["", "/spec", "/spec/revisions", "/lineage"]) {
    const denied = await request.get(
      api(`/strategy-versions/${strategy.id}${suffix}`),
      { headers: authorization(stranger) },
    );
    expect(denied.status()).toBe(404);
    expect(await denied.text()).not.toContain(strategy.code);
  }
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(page, true);
  await page.goto(workbench(strategy.id));
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expect(page.getByTestId("source-code-hash")).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Expected template rule graph" }),
  ).toHaveCount(0);
});

async function runtimeFixture(request: APIRequestContext, auth: Token) {
  const [csv, metadataText, source] = await Promise.all([
    readFile(new URL("../fixtures/research-cn.csv", import.meta.url)),
    readFile(new URL("../fixtures/research-cn.json", import.meta.url), "utf8"),
    readFile(
      new URL("../../../seed_strategies/cn_time.py", import.meta.url),
      "utf8",
    ),
  ]);
  const metadata = JSON.parse(metadataText) as {
    synthetic: boolean;
    sha256: string;
    rows: number;
  };
  expect(metadata.synthetic).toBe(true);
  expect(sha256(csv)).toBe(metadata.sha256);
  expect(csv.toString("utf8").trim().split(/\r?\n/).length - 1).toBe(
    metadata.rows,
  );
  const upload = await request.post(api("/data/uploads"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    multipart: {
      mapping: JSON.stringify({
        name: `I2 isolated leaf fixture ${crypto.randomUUID()}`,
        symbol: "600000",
        market: "CN",
        mic: "XSHG",
        currency: "CNY",
        timezone: "Asia/Shanghai",
        calendar: "XSHG",
        start_date: "2024-01-02",
        end_date: "2024-04-27",
        columns: {
          session_date: "date",
          open: "open",
          high: "high",
          low: "low",
          close: "close",
          volume: "volume",
          amount: "amount",
        },
        volume_unit: "shares",
        price_semantics: {
          native_mode: "user_declared_synthetic",
          split_basis: "unadjusted",
          dividend_basis: "unadjusted",
          note: "Frozen synthetic browser fixture, not market prices or provider evidence",
        },
        unknown_flags: [],
      }),
      file: { name: "research-cn.csv", mimeType: "text/csv", buffer: csv },
    },
  });
  expect(upload.status()).toBe(202);
  const imported = (await upload.json()) as ImportPublic;
  await expect
    .poll(
      async () => {
        const current = await read<ImportDetail>(
          request,
          auth,
          `/data/imports/${imported.id}`,
        );
        if (["failed", "partial", "cancelled"].includes(current.status))
          throw new Error(
            `Real data worker ended ${current.status}; no replacement dataset is permitted`,
          );
        return current.status;
      },
      { timeout: 120_000, intervals: [250, 500, 1000] },
    )
    .toBe("ready");
  const importedDetail = await read<ImportDetail>(
    request,
    auth,
    `/data/imports/${imported.id}`,
  );
  expect(importedDetail.dataset_version_id).not.toBeNull();
  const dataset = await read<DatasetPublic>(
    request,
    auth,
    `/datasets/${importedDetail.dataset_version_id}`,
  );
  expect(dataset.is_synthetic).toBe(true);
  const coverage = await read<CoveragePublic>(
    request,
    auth,
    `/datasets/${dataset.id}/coverage`,
  );
  expect(Array.isArray(coverage.universe.members)).toBe(true);
  expect((coverage.universe.members as unknown[]).length).toBeGreaterThan(0);
  const published = await request.post(api("/data/universes"), {
    headers: authorization(auth),
    data: {
      name: `I2 fixed universe ${crypto.randomUUID()}`,
      state: "user_fixed",
      as_of: new Date().toISOString(),
      members: coverage.universe.members,
      evidence: [],
    },
  });
  expect(published.status()).toBe(201);
  const universe = (await published.json()) as UniversePublic;
  const strategies = await rows<StrategyPublic>(
    request,
    auth,
    "/strategy-versions",
  );
  const seed = strategies.find(
    (item) =>
      item.name === "Original cn_time seed" &&
      item.task === "cn_time" &&
      item.parent_id === null &&
      item.content_sha256 === sha256(source),
  );
  expect(
    seed,
    "Bootstrap the exact repository cn_time seed for the real isolated evaluator",
  ).toBeDefined();
  const capabilities = await read<ResearchCapabilities>(
    request,
    auth,
    "/research-capabilities",
  );
  expect(capabilities.live_model_calls).toBe(false);
  expect(capabilities.model_providers).toEqual(["fixture"]);
  expect(capabilities.defaults?.model).toBeDefined();
  expect(capabilities.defaults?.optimizer).toBeDefined();
  const profile = capabilities.profile_defaults.find(
    (item) =>
      item.task === "cn_time" && item.profile === "corrected_explicit_config",
  );
  expect(profile).toBeDefined();
  const body: ResearchRunCreate = {
    schema_version: "tradegrad.run.v1",
    execution_mode: "optimization",
    task: "cn_time",
    profile: "corrected_explicit_config",
    dataset: { id: dataset.id, sha256: dataset.content_sha256 },
    universe: { id: universe.id, sha256: universe.content_sha256 },
    seed_strategy: { id: seed!.id, sha256: seed!.content_sha256 },
    windows: {
      train: { start: "2024-02-01", end: "2024-03-15" },
      validation: { start: "2024-03-15", end: "2024-04-20" },
      holdout: { start: "2024-04-22", end: "2024-04-27" },
    },
    warmup: { ...profile!.warmup, sessions: 20 },
    market_profile: structuredClone(profile!.market_profile),
    ...structuredClone(capabilities.defaults),
    optimizer: { ...capabilities.defaults!.optimizer, max_iterations: 0 },
    budgets: {
      max_model_calls: 0,
      max_tokens: 0,
      max_runtime_seconds: 150,
      max_repairs: 0,
    },
    prompt_version: "source_prompts.v1",
    environment_digest: capabilities.environment_digest,
    benchmark: null,
    research_capture: { mode: "research" },
  };
  return { body, seed: seed! };
}

test("real isolated candidate leaves keep their own immutable pins, survive login and retain private bookmarks", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(300_000);
  const { body, seed } = await runtimeFixture(request, owned.auth);
  const accepted = await request.post(api("/research-runs"), {
    headers: {
      ...authorization(owned.auth),
      "Idempotency-Key": crypto.randomUUID(),
    },
    data: body,
  });
  expect(accepted.status()).toBe(202);
  const submitted = (await accepted.json()) as ResearchRunPublic;
  owned.runs.add(submitted.id);
  await expect
    .poll(
      async () => {
        const current = await read<ResearchRunDetail>(
          request,
          owned.auth,
          `/research-runs/${submitted.id}`,
        );
        if (terminal.has(current.status) && current.status !== "succeeded")
          throw new Error(
            `Real research ${current.id} ended ${current.status}: ${current.error || "No recorded error"}`,
          );
        return current.status;
      },
      { timeout: 180_000, intervals: [250, 500, 1000] },
    )
    .toBe("succeeded");
  const candidates = await rows<CandidatePublic>(
    request,
    owned.auth,
    `/research-runs/${submitted.id}/candidates`,
  );
  expect(candidates).toHaveLength(1);
  const candidate = candidates[0];
  expect(candidate.kind).toBe("seed");
  expect(candidate.code_sha256).toBe(seed.content_sha256);
  const actual = await rows<EvaluationPublic>(
    request,
    owned.auth,
    `/research-runs/${submitted.id}/evaluations`,
  );
  expect(actual).toHaveLength(2);
  expect(actual.map((item) => item.split).sort()).toEqual([
    "train",
    "validation",
  ]);
  const leaves = await rows<Leaf>(
    request,
    owned.auth,
    `/candidates/${candidate.id}/evaluations`,
  );
  expect(leaves).toHaveLength(2);
  for (const leaf of leaves) {
    const physical = actual.find((item) => item.id === leaf.evaluation_id)!;
    expect(physical).toBeDefined();
    expect(leaf.candidate_id).toBe(candidate.id);
    expect(leaf.execution_attempt_id).toBe(physical.attempt_id);
    expect(leaf.code_sha256).toBe(candidate.code_sha256);
    expect(leaf.status).toBe("succeeded");
    expect(leaf.evidence_status).toBe("registered");
    expect(leaf.result_manifest_sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(leaf.trace_manifest_sha256).toMatch(/^[a-f0-9]{64}$/);
    const pins = new URLSearchParams({
      execution_attempt_id: leaf.execution_attempt_id,
      result_manifest_sha256: leaf.result_manifest_sha256!,
      trace_manifest_sha256: leaf.trace_manifest_sha256!,
    });
    const trace = await read<TraceManifestPublic>(
      request,
      owned.auth,
      `/evaluations/${leaf.evaluation_id}/trace-manifest?${pins}`,
    );
    expect(trace.context.evaluation_id).toBe(leaf.evaluation_id);
    expect(trace.context.execution_attempt_id).toBe(leaf.execution_attempt_id);
    expect(trace.context.result_manifest_sha256).toBe(
      leaf.result_manifest_sha256,
    );
    expect(trace.context.trace_manifest_sha256).toBe(
      leaf.trace_manifest_sha256,
    );
    // Trace publication precedes optimizer checkpoint registration. The exact
    // physical Evaluation is attached to the candidate later, but its sealed
    // pre-checkpoint binding must not be rewritten to add that foreign key.
    expect(trace.context.binding).not.toBeNull();
    expect(trace.context.binding?.candidate_id).toBeNull();
    expect(trace.context.binding?.code_sha256).toBe(candidate.code_sha256);
    expect(trace.manifest?.capture.mode).toBe("research");
  }
  await signIn(page);
  await page.goto(workbench(seed.id, { candidate_id: candidate.id }));
  await expect(
    page.getByRole("heading", {
      name: "Strategy research workbench",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByTestId("workbench-selection")).toContainText(
    "No actual leaf Evaluation selected",
  );
  const selector = page.getByLabel("Actual leaf Evaluation", { exact: true });
  for (const leaf of leaves) {
    await selector.selectOption(leaf.evaluation_id);
    const pins = {
      candidate_id: candidate.id,
      evaluation_id: leaf.evaluation_id,
      execution_attempt_id: leaf.execution_attempt_id,
      result_manifest_sha256: leaf.result_manifest_sha256!,
      trace_manifest_sha256: leaf.trace_manifest_sha256!,
      code_sha256: candidate.code_sha256,
    };
    await expectSelection(page, pins);
    await expect(selector).toHaveValue(leaf.evaluation_id);
    await expect(page.getByTestId("workbench-selection")).toContainText(
      leaf.result_manifest_sha256!,
    );
    const exactUrl = page.url();
    await page.reload();
    await expect(page).toHaveURL(exactUrl);
    await expect(selector).toHaveValue(leaf.evaluation_id);
    await expectSelection(page, pins);
  }
  const selectedUrl = page.url();
  const mismatched = new URL(selectedUrl);
  const wrongHash =
    leaves.at(-1)!.result_manifest_sha256 === "0".repeat(64)
      ? "1".repeat(64)
      : "0".repeat(64);
  mismatched.searchParams.set("result_manifest_sha256", wrongHash);
  await page.goto(mismatched.href);
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "pin no longer matches this leaf" }),
  ).toBeVisible();
  await expectSelection(page, {
    result_manifest_sha256: wrongHash,
    evaluation_id: leaves.at(-1)!.evaluation_id,
  });
  await expect(
    page.getByRole("region", { name: "Actual selected stage observations" }),
  ).toHaveCount(0);
  await page.goto(selectedUrl);
  await expect(selector).toHaveValue(leaves.at(-1)!.evaluation_id);
  await page
    .getByRole("button", { name: "Bookmark candidate", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Remove candidate bookmark",
      exact: true,
    }),
  ).toBeVisible();
  expect(
    (
      await read<Lineage>(
        request,
        owned.auth,
        `/candidates/${candidate.id}/lineage`,
      )
    ).focus.bookmarked,
  ).toBe(true);
  await page.reload();
  await expect(
    page.getByRole("button", {
      name: "Remove candidate bookmark",
      exact: true,
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.goto(selectedUrl);
  await expect(page).toHaveURL(/\/login\?/);
  await page.getByLabel("Email address").fill(account().email);
  await page.getByLabel("Password", { exact: true }).fill(account().password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(selectedUrl);
  await expect(selector).toHaveValue(leaves.at(-1)!.evaluation_id);
  await expect(
    page.getByRole("button", {
      name: "Remove candidate bookmark",
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Remove candidate bookmark", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Bookmark candidate", exact: true }),
  ).toBeVisible();
  expect(
    (
      await read<Lineage>(
        request,
        owned.auth,
        `/candidates/${candidate.id}/lineage`,
      )
    ).focus.bookmarked,
  ).toBe(false);

  const stranger = await token(request, true);
  // Exercise the same POST mutation as the real UI. PUT only tests routing's
  // method-not-allowed response and never reaches owner admission.
  const rejected = await request.post(
    api(`/candidates/${candidate.id}/bookmark`),
    {
      headers: authorization(stranger),
      data: { bookmarked: true },
    },
  );
  expect(rejected.status()).toBe(404);
  expect(
    (
      await read<Lineage>(
        request,
        owned.auth,
        `/candidates/${candidate.id}/lineage`,
      )
    ).focus.bookmarked,
  ).toBe(false);
  const candidateDetail = await read<{ code: string; code_sha256: string }>(
    request,
    owned.auth,
    `/candidates/${candidate.id}`,
  );
  expect(sha256(candidateDetail.code)).toBe(candidate.code_sha256);
  expect(
    await rows<EvaluationPublic>(
      request,
      owned.auth,
      `/research-runs/${submitted.id}/evaluations`,
    ),
  ).toEqual(actual);
});
