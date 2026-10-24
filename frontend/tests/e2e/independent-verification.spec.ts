import {
  expect,
  test as base,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import type {
  ControlledExperimentPublic,
  DiagnosticParityEventPage,
  DiagnosticParityView,
  IndependentCapability,
  IndependentEvaluationDetail,
  IndependentEvaluationPublic,
  IndependentResultQuery,
  StrategyDetail,
  Token,
  WorkbenchEvaluation,
} from "../../src/client/types.gen";
import { account, api, authorization, signIn, token } from "./helpers";
import {
  createActualSource,
  query,
  read,
  sourcePins,
  terminal,
  verificationUrl,
  type IndependentOwned,
} from "./independent-helpers";
import { requiredDiagnosticChecks } from "../../src/lib/diagnostic-parity-evidence";

// I5 REAL-STACK acceptance: PostgreSQL + actual isolated native source and
// independent evaluators + research/comparison workers + Chromium. No route
// stubs, fake traces, model calls or external provider downloads. --list is
// collection only, never a claim these actual engine/browser paths passed.
const test = base.extend<{ owned: IndependentOwned }>({
  owned: async ({ request }, use) =>
    use({ auth: await token(request), runs: new Set() }),
});
async function accepted(
  page: Page,
  path: string,
  action: () => Promise<unknown>,
) {
  const response = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api/v1${path}` &&
      response.request().method() === "POST",
  );
  await action();
  const value = await response;
  expect(value.status(), await value.text()).toBe(202);
  return value;
}
async function waitIndependent(
  request: APIRequestContext,
  auth: Token,
  id: string,
  expected = "succeeded",
) {
  await expect
    .poll(
      async () => {
        const job = await read<IndependentEvaluationDetail>(
          request,
          auth,
          `/independent-evaluations/${id}`,
        );
        if (terminal.has(job.status) && job.status !== expected)
          throw new Error(
            `Actual independent job ${id} ended ${job.status}: ${job.error}. No fabricated result fallback.`,
          );
        return job.status;
      },
      { timeout: 210000, intervals: [250, 500, 1000] },
    )
    .toBe(expected);
  return read<IndependentEvaluationDetail>(
    request,
    auth,
    `/independent-evaluations/${id}`,
  );
}
async function pinnedUrl(page: Page, values: Record<string, string | number>) {
  await expect
    .poll(() =>
      Object.fromEntries(
        Object.keys(values).map((key) => [
          key,
          new URL(page.url()).searchParams.get(key),
        ]),
      ),
    )
    .toEqual(
      Object.fromEntries(
        Object.entries(values).map(([key, value]) => [key, String(value)]),
      ),
    );
}
async function reloginAtPinnedEvidence(page: Page, url: string) {
  await page.evaluate(() => sessionStorage.removeItem("tradegrad.session.v1"));
  await page.goto(url);
  await expect(page).toHaveURL(/\/login\?returnTo=/);
  const user = account();
  await page.getByLabel("Email address").fill(user.email);
  await page.getByLabel("Password", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(url);
}
async function assertForeignDenied(
  request: APIRequestContext,
  paths: string[],
) {
  const foreign = await token(request, true);
  for (const path of paths) {
    const response = await request.get(api(path), {
      headers: authorization(foreign),
    });
    expect(response.status(), `No cross-owner disclosure: ${path}`).toBe(404);
  }
}

test("actual original-start diagnostic reports complete semantic parity and exact event/hash/history admission", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(600000);
  const source = await createActualSource(request, owned),
    pins = sourcePins(source);
  const response = await request.post(
    api("/independent-evaluations/capability"),
    { headers: authorization(owned.auth), data: pins },
  );
  expect(response.status(), await response.text()).toBe(200);
  const capability = (await response.json()) as IndependentCapability;
  expect(
    capability.can_execute,
    capability.unavailable_reasons.join(", "),
  ).toBe(true);
  expect(capability.source).toEqual(pins);
  expect(capability.original_config).toEqual(source.run.config);
  expect(capability.original_config_sha256).toMatch(/^[a-f0-9]{64}$/);
  // Native trace binds the staged evaluation config, not the entire original run.
  expect(capability.original_config_sha256).not.toBe(
    source.trace.context.binding!.execution_config_sha256,
  );
  expect(capability.parity_prerequisites.status).toBe("potentially_verifiable");
  await signIn(page);
  await page.goto(verificationUrl(source));
  await expect(
    page.getByRole("heading", {
      name: "Original evidence and frozen independent inputs",
    }),
  ).toBeVisible();
  await expect(page.getByTestId("reconstruction-window-notice")).toContainText(
    "original Evaluation start",
  );
  await expect(
    page.getByRole("button", {
      name: "Create independent diagnostic replay",
      exact: true,
    }),
  ).toBeEnabled();
  const posted = await accepted(
    page,
    `/evaluations/${pins.evaluation_id}/diagnostic-replays`,
    () =>
      page
        .getByRole("button", {
          name: "Create independent diagnostic replay",
          exact: true,
        })
        .click(),
  );
  const created = (await posted.json()) as IndependentEvaluationPublic;
  expect(created.purpose).toBe("diagnostic");
  expect(created.config.execution_mode).toBe("independent_evaluation");
  expect(created.config.reconstruction_window).toEqual({
    start: source.leaf.window.start,
    end: source.leaf.window.end,
  });
  expect(created.config.source_config.warmup).toEqual(source.body.warmup);
  expect(created.config.source_config.dataset).toEqual(source.body.dataset);
  expect(created.config.source_config.universe).toEqual(source.body.universe);
  expect(created.config.selected_strategy.sha256).toBe(
    source.strategy.content_sha256,
  );
  expect(created.config.selected_strategy.id).not.toBe(source.strategy.id);
  owned.runs.add(created.id);
  const requestBody = posted.request().postDataJSON(),
    key = posted.request().headers()["idempotency-key"];
  expect(key).toBeTruthy();
  expect(requestBody).not.toHaveProperty("optimizer");
  expect(requestBody).not.toHaveProperty("model");
  const duplicate = await request.post(
    api(`/evaluations/${pins.evaluation_id}/diagnostic-replays`),
    {
      headers: { ...authorization(owned.auth), "Idempotency-Key": key },
      data: requestBody,
    },
  );
  expect(duplicate.status()).toBe(202);
  expect((await duplicate.json()).id).toBe(created.id);
  const job = await waitIndependent(request, owned.auth, created.id);
  expect(job.result_ready).toBe(true);
  expect(job.result_pin).not.toBeNull();
  const result = job.result_pin!;
  expect(result.evaluation_id).not.toBe(source.leaf.evaluation_id);
  expect(result.execution_attempt_id).not.toBe(
    source.leaf.execution_attempt_id,
  );
  await expect(page.getByTestId("diagnostic-parity-outcome")).toHaveText(
    "matched",
    { timeout: 60000 },
  );
  const report = await read<DiagnosticParityView>(
    request,
    owned.auth,
    `/independent-evaluations/${created.id}/parity`,
  );
  expect(report.outcome).toBe("matched");
  expect(report.checks.map((check) => check.key).sort()).toEqual(
    [...requiredDiagnosticChecks].sort(),
  );
  expect(report.checks.every((check) => check.status === "matched")).toBe(true);
  expect(report.source.binding.evaluation_id).toBe(source.leaf.evaluation_id);
  expect(report.replay.binding.evaluation_id).toBe(result.evaluation_id);
  expect(report.replay.binding.execution_config_sha256).toBe(
    result.execution_config_sha256,
  );
  expect(result.execution_config_sha256).not.toBe(job.config_sha256);
  expect(report.source.binding.execution_config_sha256).toBe(
    source.trace.context.binding!.execution_config_sha256,
  );
  expect(report.replay.binding.result_manifest_sha256).toBe(
    result.result_manifest_sha256,
  );
  await pinnedUrl(page, {
    diagnostic_run_id: created.id,
    diagnostic_config_sha256: job.config_sha256,
    diagnostic_report_sha256: report.report_sha256,
    diagnostic_evaluation_id: result.evaluation_id,
    diagnostic_execution_attempt_id: result.execution_attempt_id,
    diagnostic_result_manifest_sha256: result.result_manifest_sha256,
    evaluation_id: source.leaf.evaluation_id,
    result_manifest_sha256: source.leaf.result_manifest_sha256!,
  });
  const saved = page.url();
  await page.reload();
  await expect(page.getByTestId("diagnostic-parity-outcome")).toHaveText(
    "matched",
  );
  await pinnedUrl(page, { diagnostic_report_sha256: report.report_sha256 });
  const mapped = await read<DiagnosticParityEventPage>(
    request,
    owned.auth,
    `/independent-evaluations/${job.id}/parity/events?${query({ report_sha256: report.report_sha256, offset: 0, limit: 25 })}`,
  );
  expect(
    mapped.items.length,
    "Actual template must produce explicit event mappings",
  ).toBeGreaterThan(0);
  expect(
    mapped.total,
    "Use the real multi-session fixture to test server paging",
  ).toBeGreaterThan(25);
  const first = mapped.items[0];
  expect(first.mapping).toBe("verified_original_event");
  expect(first.original_decision_id).toBeTruthy();
  expect(first.replay_decision_id).toBeTruthy();
  expect(first.original_decision_id).not.toBe(first.replay_decision_id);
  const mapping = page.getByTestId("diagnostic-event-mappings");
  await expect(mapping).toContainText(first.original_decision_id!);
  await mapping
    .getByRole("link", { name: "Open original decision and pre-state" })
    .first()
    .click();
  await expect(page.getByTestId("replay-event-identity")).toContainText(
    first.original_decision_id!,
  );
  await pinnedUrl(page, {
    evaluation_id: source.leaf.evaluation_id,
    decision_id: first.original_decision_id!,
    result_manifest_sha256: source.leaf.result_manifest_sha256!,
  });
  await page.goBack();
  await expect(page.getByTestId("diagnostic-parity-outcome")).toHaveText(
    "matched",
  );
  await page
    .getByTestId("diagnostic-event-mappings")
    .getByRole("link", { name: "Open new diagnostic decision and pre-state" })
    .first()
    .click();
  await expect(page.getByTestId("replay-event-identity")).toContainText(
    first.replay_decision_id!,
  );
  await pinnedUrl(page, {
    evaluation_id: result.evaluation_id,
    decision_id: first.replay_decision_id!,
    result_manifest_sha256: result.result_manifest_sha256,
  });
  await page.goBack();
  const mappingPanel = page
    .getByRole("heading", {
      name: "Original-to-diagnostic semantic event mapping",
      exact: true,
    })
    .locator("xpath=../..");
  await mappingPanel
    .getByRole("button", { name: "Next page", exact: true })
    .click();
  await pinnedUrl(page, { diagnostic_event_offset: 25 });
  await page.reload();
  await pinnedUrl(page, {
    diagnostic_event_offset: 25,
    diagnostic_report_sha256: report.report_sha256,
  });
  await reloginAtPinnedEvidence(page, page.url());
  await expect(page.getByTestId("diagnostic-parity-outcome")).toHaveText(
    "matched",
  );
  const stale = new URL(saved);
  stale.searchParams.set("diagnostic_report_sha256", "f".repeat(64));
  await page.goto(stale.toString());
  await expect(page.getByRole("alert")).toContainText(
    /hash|pin|differ|mismatch/i,
  );
  await expect(page.getByTestId("diagnostic-parity-outcome")).toHaveCount(0);
  await assertForeignDenied(request, [
    `/independent-evaluations/${job.id}`,
    `/independent-evaluations/${job.id}/parity`,
    `/independent-evaluations/${job.id}/parity/events?${query({ report_sha256: report.report_sha256 })}`,
    `/independent-evaluations/evaluations/${result.evaluation_id}/result?${query({ execution_attempt_id: result.execution_attempt_id, result_manifest_sha256: result.result_manifest_sha256 })}`,
  ]);
  expect(
    (
      await read<WorkbenchEvaluation>(
        request,
        owned.auth,
        `/evaluations/${source.leaf.evaluation_id}/workbench`,
      )
    ).result_manifest_sha256,
  ).toBe(source.leaf.result_manifest_sha256);
  expect(
    (
      await read<StrategyDetail>(
        request,
        owned.auth,
        `/strategy-versions/${source.strategy.id}`,
      )
    ).code,
  ).toBe(source.strategy.code);
});

test("actual three-arm manual control retains failed outcomes, immutable conditions, retries and exact baseline comparison", async ({
  page,
  request,
  owned,
}) => {
  test.setTimeout(800000);
  const source = await createActualSource(request, owned);
  await signIn(page);
  await page.goto(verificationUrl(source));
  await expect(
    page.getByRole("heading", {
      name: "Create manual two- or three-arm controlled experiment",
    }),
  ).toBeVisible();
  await page
    .getByLabel("Controlled experiment name", { exact: true })
    .fill(`I5 manual actual control ${crypto.randomUUID()}`);
  await page
    .getByLabel("Hypothesis", { exact: true })
    .fill(
      "Reducing max weight changes observed target allocation under identical initial conditions.",
    );
  await page
    .getByLabel("Planned change and isolated mechanism", { exact: true })
    .fill(
      "Change only max_weight for the first variant; retain a deliberately failing multi-factor arm as failure evidence.",
    );
  const parameterField = page.getByLabel("Business parameter values (JSON)");
  const baselineParameters = JSON.parse(
    await parameterField.inputValue(),
  ) as Record<string, unknown>;
  expect(baselineParameters).toHaveProperty("max_weight");
  await parameterField.fill(
    JSON.stringify({ ...baselineParameters, max_weight: 0.15 }, null, 2),
  );
  await page
    .getByLabel("This arm's planned mechanism change", { exact: true })
    .fill("Reduce max_weight only; no code change.");
  await page
    .getByRole("button", { name: "Add third arm", exact: true })
    .click();
  await page
    .getByLabel("Variant name", { exact: true })
    .fill("Deliberate failing arm");
  await page
    .getByLabel("This arm's planned mechanism change", { exact: true })
    .fill(
      "Deliberate code failure and parameter change; not individually validated.",
    );
  await parameterField.fill(
    JSON.stringify({ ...baselineParameters, max_weight: 0.1 }, null, 2),
  );
  await page
    .getByRole("textbox", {
      name: "Controlled variant Python source",
      exact: true,
    })
    .fill(
      `${source.strategy.code}\nraise RuntimeError("I5 deliberate isolated evaluation failure")\n`,
    );
  await expect(
    page.getByText("not individually validated", { exact: true }),
  ).toBeVisible();
  const response = await accepted(page, "/controlled-experiments", () =>
    page
      .getByRole("button", {
        name: "Create 3-arm controlled experiment",
        exact: true,
      })
      .click(),
  );
  const created = (await response.json()) as ControlledExperimentPublic;
  for (const arm of created.arms) owned.runs.add(arm.run_id);
  expect(created.arms).toHaveLength(3);
  expect(created.arms.map((arm) => arm.ordinal)).toEqual([0, 1, 2]);
  expect(new Set(created.arms.map((arm) => arm.strategy_version_id)).size).toBe(
    3,
  );
  expect(new Set(created.arms.map((arm) => arm.run_id)).size).toBe(3);
  expect(created.request.arms[0].code).toBe(source.strategy.code);
  expect(created.request.arms[0].parameters).toEqual(baselineParameters);
  expect(created.request.arms[1].code).toBe(source.strategy.code);
  expect(created.arms[1].interpretation).toBe(
    "single_parameter_change_not_causal_proof",
  );
  expect(created.arms[2].interpretation).toBe("not_individually_validated");
  expect(created.split_version_id).not.toBe(source.run.split_version_id);
  expect(created.parent_experiment_id).toBe(source.run.experiment_id);
  expect(created.root_experiment_id).toBeTruthy();
  const baseline = await waitIndependent(
      request,
      owned.auth,
      created.arms[0].run_id,
    ),
    variant = await waitIndependent(
      request,
      owned.auth,
      created.arms[1].run_id,
    ),
    failed = await waitIndependent(
      request,
      owned.auth,
      created.arms[2].run_id,
      "failed",
    );
  expect(baseline.config.protocol_sha256).toBe(created.protocol_sha256);
  expect(variant.config.protocol_sha256).toBe(created.protocol_sha256);
  expect(failed.config.protocol_sha256).toBe(created.protocol_sha256);
  expect(baseline.config.reconstruction_window).toEqual(
    variant.config.reconstruction_window,
  );
  expect(baseline.config.source_config.warmup).toEqual(
    variant.config.source_config.warmup,
  );
  expect(baseline.config.evaluation_mode).toBe("native_akquant");
  expect(variant.config.evaluation_mode).toBe("native_akquant");
  if (
    baseline.config.evaluation_mode !== "native_akquant" ||
    variant.config.evaluation_mode !== "native_akquant"
  )
    throw new Error(
      "Expected actual native independent configs, not another mode",
    );
  expect(baseline.config.source_config.market_profile).toEqual(
    variant.config.source_config.market_profile,
  );
  expect(failed.result_ready).toBe(false);
  expect(failed.result_pin).toBeNull();
  expect(failed.error).toBeTruthy();
  await expect(page.getByTestId("controlled-arms")).toContainText(
    "No successful return is assigned to this failed arm",
    { timeout: 30000 },
  );
  await page
    .getByRole("button", { name: "Inspect arm 3", exact: true })
    .click();
  await expect(page.getByTestId(`independent-status-${failed.id}`)).toHaveText(
    "failed",
  );
  const retry = await accepted(
    page,
    `/independent-evaluations/${failed.id}/retry`,
    () =>
      page
        .getByRole("button", {
          name: "Retry independent run from original start",
          exact: true,
        })
        .click(),
  );
  const retried = (await retry.json()) as IndependentEvaluationPublic;
  expect(retried.id).toBe(failed.id);
  expect(retried.generation).toBe(failed.generation + 1);
  expect(retried.config_sha256).toBe(failed.config_sha256);
  const retryFailed = await waitIndependent(
    request,
    owned.auth,
    failed.id,
    "failed",
  );
  expect(retryFailed.attempts).toHaveLength(2);
  expect(retryFailed.attempts.every((attempt) => attempt.sandbox_stopped)).toBe(
    true,
  );
  expect(retryFailed.result_pin).toBeNull();
  await page
    .getByRole("button", { name: "Inspect arm 2", exact: true })
    .click();
  const pin = variant.result_pin!;
  expect(pin).toBeTruthy();
  await pinnedUrl(page, {
    controlled_experiment_id: created.id,
    controlled_request_sha256: created.request_sha256,
    controlled_protocol_sha256: created.protocol_sha256,
    controlled_arm: 1,
    controlled_arm_run_id: variant.id,
    controlled_arm_evaluation_id: pin.evaluation_id,
    controlled_arm_result_manifest_sha256: pin.result_manifest_sha256,
  });
  await expect(
    page.getByRole("heading", {
      name: "Actual independent economic observations",
      exact: true,
    }),
  ).toBeVisible();
  const actual = await read<IndependentResultQuery>(
    request,
    owned.auth,
    `/independent-evaluations/evaluations/${pin.evaluation_id}/result?${query({ execution_attempt_id: pin.execution_attempt_id, result_manifest_sha256: pin.result_manifest_sha256, section: "positions", limit: 25 })}`,
  );
  expect(actual.artifact_sha256).toBe(pin.result_manifest_sha256);
  expect(actual.reward_status).toBe("not_applicable");
  expect(
    actual.total,
    "Actual held positions are required, not an optional empty shortcut",
  ).toBeGreaterThan(0);
  const savedControlUrl = page.url();
  await page
    .getByRole("link", {
      name: "Compare copied baseline with this arm",
      exact: true,
    })
    .click();
  await pinnedUrl(page, {
    tab: "comparison",
    left_evaluation_id: baseline.result_pin!.evaluation_id,
    left_result_manifest_sha256: baseline.result_pin!.result_manifest_sha256,
    right_evaluation_id: pin.evaluation_id,
    right_result_manifest_sha256: pin.result_manifest_sha256,
  });
  await page
    .getByRole("button", { name: "Create immutable comparison", exact: true })
    .click();
  await expect(page.getByTestId("comparison-status")).toHaveText("succeeded", {
    timeout: 210000,
  });
  await expect(
    page.getByText("Both decision pre-states must be read", { exact: false }),
  ).toBeVisible();
  await page.goto(savedControlUrl);
  await pinnedUrl(page, {
    controlled_experiment_id: created.id,
    controlled_arm: 1,
  });
  await page.reload();
  await pinnedUrl(page, {
    controlled_protocol_sha256: created.protocol_sha256,
    controlled_arm_result_manifest_sha256: pin.result_manifest_sha256,
  });
  await assertForeignDenied(request, [
    `/controlled-experiments/${created.id}`,
    `/independent-evaluations/${variant.id}`,
  ]);
  const savedControl = await read<ControlledExperimentPublic>(
    request,
    owned.auth,
    `/controlled-experiments/${created.id}`,
  );
  expect(savedControl.arms[2].status).toBe("failed");
  expect(savedControl.arms[2].result_pin).toBeNull();
  expect(
    savedControl.exposure_snapshot.independent_unseen_holdout_established,
  ).toBe(false);
});
