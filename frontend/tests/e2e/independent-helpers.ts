import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { expect, type APIRequestContext } from "@playwright/test";
import type {
  CandidatePublic,
  CoveragePublic,
  DatasetPublic,
  ImportDetail,
  ImportPublic,
  ResearchCapabilities,
  ResearchRunCreate,
  ResearchRunDetail,
  ResearchRunPublic,
  ResearchTemplate,
  StrategyDetail,
  Token,
  TraceManifestPublic,
  UniversePublic,
  WorkbenchEvaluation,
  WorkbenchSpec,
} from "../../src/client/types.gen";
import { api, authorization } from "./helpers";
// Shared only by I5 actual-stack specs. These are real isolated evaluator jobs
// on explicitly synthetic uploaded prices, not manufactured result or trace rows.
export type IndependentOwned = { auth: Token; runs: Set<string> };
type Owned = IndependentOwned;
export const sha256 = (value: string | Buffer) =>
  createHash("sha256").update(value).digest("hex");
export const terminal = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "paused",
]);
const windows = {
  train: { start: "2024-02-01", end: "2024-03-15" },
  validation: { start: "2024-03-15", end: "2024-04-20" },
  holdout: { start: "2024-04-22", end: "2024-04-27" },
};
export const query = (values: Record<string, string | number>) =>
  new URLSearchParams(
    Object.entries(values).map(([key, value]) => [key, String(value)]),
  ).toString();
export const tracePins = (leaf: WorkbenchEvaluation) => ({
  execution_attempt_id: leaf.execution_attempt_id,
  result_manifest_sha256: leaf.result_manifest_sha256!,
  trace_manifest_sha256: leaf.trace_manifest_sha256!,
});
export async function read<T>(
  request: APIRequestContext,
  auth: Token,
  path: string,
) {
  const response = await request.get(api(path), {
    headers: authorization(auth),
  });
  expect(
    response.status(),
    `Actual GET ${path}: ${await response.text()}`,
  ).toBe(200);
  return (await response.json()) as T;
}
export async function rows<T>(
  request: APIRequestContext,
  auth: Token,
  path: string,
) {
  const result: T[] = [];
  for (;;) {
    const page = await read<{ data: T[]; count: number }>(
      request,
      auth,
      `${path}?offset=${result.length}&limit=100`,
    );
    result.push(...page.data);
    if (result.length >= page.count) return result;
    expect(
      page.data.length,
      `Pagination must advance: ${path}`,
    ).toBeGreaterThan(0);
  }
}
async function frozenSnapshot(request: APIRequestContext, auth: Token) {
  const [csv, text] = await Promise.all([
    readFile(new URL("../fixtures/research-cn.csv", import.meta.url)),
    readFile(new URL("../fixtures/research-cn.json", import.meta.url), "utf8"),
  ]);
  const fixture = JSON.parse(text) as {
    synthetic: boolean;
    sha256: string;
    rows: number;
  };
  expect(fixture.synthetic).toBe(true);
  expect(sha256(csv)).toBe(fixture.sha256);
  expect(csv.toString("utf8").trim().split(/\r?\n/).length - 1).toBe(
    fixture.rows,
  );
  const uploaded = await request.post(api("/data/uploads"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    multipart: {
      mapping: JSON.stringify({
        name: `I5 frozen synthetic snapshot ${crypto.randomUUID()}`,
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
  expect(uploaded.status()).toBe(202);
  const imported = (await uploaded.json()) as ImportPublic;
  await expect
    .poll(
      async () => {
        const job = await read<ImportDetail>(
          request,
          auth,
          `/data/imports/${imported.id}`,
        );
        if (["failed", "partial", "cancelled"].includes(job.status))
          throw new Error(
            `Actual data worker ended ${job.status}: ${job.error}`,
          );
        return job.status;
      },
      { timeout: 120_000, intervals: [250, 500, 1000] },
    )
    .toBe("ready");
  const ready = await read<ImportDetail>(
    request,
    auth,
    `/data/imports/${imported.id}`,
  );
  expect(ready.dataset_version_id).not.toBeNull();
  const dataset = await read<DatasetPublic>(
    request,
    auth,
    `/datasets/${ready.dataset_version_id}`,
  );
  expect(dataset.is_synthetic).toBe(true);
  const coverage = await read<CoveragePublic>(
    request,
    auth,
    `/datasets/${dataset.id}/coverage`,
  );
  expect(coverage.dataset_version_id).toBe(dataset.id);
  expect(coverage.universe.members as unknown[]).toHaveLength(1);
  const universeResponse = await request.post(api("/data/universes"), {
    headers: authorization(auth),
    data: {
      name: `I5 fixed synthetic universe ${crypto.randomUUID()}`,
      state: "user_fixed",
      as_of: new Date().toISOString(),
      members: coverage.universe.members,
      evidence: [],
    },
  });
  expect(universeResponse.status()).toBe(201);
  return {
    dataset,
    universe: (await universeResponse.json()) as UniversePublic,
  };
}
export async function saveVersion(
  request: APIRequestContext,
  auth: Token,
  template: ResearchTemplate,
  code: string,
  parent: StrategyDetail | null,
  manifest = template.manifest,
  market: "cn" | "us" = "cn",
) {
  const response = await request.post(api("/strategy-versions"), {
    headers: { ...authorization(auth), "Idempotency-Key": crypto.randomUUID() },
    data: {
      name: `I5 ${parent ? "derived" : "baseline"} ${crypto.randomUUID()}`,
      task: `${market}_${template.task_suffix}`,
      code,
      manifest,
      parent_id: parent?.id ?? null,
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  const strategy = (await response.json()) as StrategyDetail;
  expect(strategy.code).toBe(code);
  expect(strategy.content_sha256).toBe(sha256(code));
  expect(strategy.parent_id).toBe(parent?.id ?? null);
  expect(strategy.manifest).toEqual(manifest);
  return strategy;
}
export async function runActual(
  request: APIRequestContext,
  owned: Owned,
  strategy: StrategyDetail,
  snapshot: Awaited<ReturnType<typeof frozenSnapshot>>,
  capabilities: ResearchCapabilities,
  commissionRate?: number,
  options?: {
    windows: ResearchRunCreate["windows"];
    warmupSessions: number;
    initialCash: number;
  },
) {
  const profile = capabilities.profile_defaults.find(
    (item) =>
      item.task === strategy.task &&
      item.profile === "corrected_explicit_config",
  );
  expect(profile).toBeDefined();
  const body: ResearchRunCreate = {
    schema_version: "tradegrad.run.v1",
    execution_mode: "optimization",
    task: strategy.task,
    profile: "corrected_explicit_config",
    dataset: {
      id: snapshot.dataset.id,
      sha256: snapshot.dataset.content_sha256,
    },
    universe: {
      id: snapshot.universe.id,
      sha256: snapshot.universe.content_sha256,
    },
    seed_strategy: { id: strategy.id, sha256: strategy.content_sha256 },
    strategy_contract: strategy.manifest,
    windows: options?.windows ?? windows,
    warmup: { ...profile!.warmup, sessions: options?.warmupSessions ?? 20 },
    market_profile: {
      ...structuredClone(profile!.market_profile),
      ...(options ? { initial_cash: options.initialCash } : {}),
      ...(commissionRate === undefined
        ? {}
        : { commission_rate: commissionRate }),
    },
    ...structuredClone(capabilities.defaults),
    optimizer: { ...capabilities.defaults!.optimizer, max_iterations: 0 },
    budgets: {
      max_model_calls: 0,
      max_tokens: 0,
      max_runtime_seconds: 180,
      max_repairs: 0,
    },
    prompt_version: "source_prompts.v1",
    environment_digest: capabilities.environment_digest,
    benchmark: null,
    research_capture: { mode: "research" },
  };
  const response = await request.post(api("/research-runs"), {
    headers: {
      ...authorization(owned.auth),
      "Idempotency-Key": crypto.randomUUID(),
    },
    data: body,
  });
  expect(response.status(), await response.text()).toBe(202);
  const run = (await response.json()) as ResearchRunPublic;
  owned.runs.add(run.id);
  await expect
    .poll(
      async () => {
        const detail = await read<ResearchRunDetail>(
          request,
          owned.auth,
          `/research-runs/${run.id}`,
        );
        if (terminal.has(detail.status) && detail.status !== "succeeded")
          throw new Error(
            `Actual isolated native run ended ${detail.status}: ${detail.error}; never replace with fabricated success`,
          );
        return detail.status;
      },
      { timeout: 210_000, intervals: [250, 500, 1000] },
    )
    .toBe("succeeded");
  expect(
    await rows(request, owned.auth, `/research-runs/${run.id}/model-calls`),
  ).toEqual([]);
  const candidates = await rows<CandidatePublic>(
    request,
    owned.auth,
    `/research-runs/${run.id}/candidates`,
  );
  expect(candidates).toHaveLength(1);
  expect(candidates[0].kind).toBe("seed");
  expect(candidates[0].code_sha256).toBe(strategy.content_sha256);
  const leaves = await rows<WorkbenchEvaluation>(
    request,
    owned.auth,
    `/strategy-versions/${strategy.id}/evaluations`,
  );
  expect(leaves.map((leaf) => leaf.split).sort()).toEqual([
    "train",
    "validation",
  ]);
  const leaf = leaves.find((item) => item.split === "train")!;
  expect(leaf.status).toBe("succeeded");
  expect(leaf.evidence_status).toBe("registered");
  expect(leaf.result_manifest_sha256).toMatch(/^[a-f0-9]{64}$/);
  expect(leaf.trace_manifest_sha256).toMatch(/^[a-f0-9]{64}$/);
  const spec = await read<WorkbenchSpec>(
    request,
    owned.auth,
    `/strategy-versions/${strategy.id}/spec`,
  );
  expect(spec.code_sha256).toBe(strategy.content_sha256);
  const trace = await read<TraceManifestPublic>(
    request,
    owned.auth,
    `/evaluations/${leaf.evaluation_id}/trace-manifest?${query(tracePins(leaf))}`,
  );
  expect(trace.context.status).toBe("available");
  expect(trace.context.binding?.code_sha256).toBe(strategy.content_sha256);
  expect(trace.manifest?.capture.mode).toBe("research");
  return {
    strategy,
    run,
    candidate: candidates[0],
    leaves,
    leaf,
    spec,
    body,
    trace,
  };
}

export async function createActualSource(
  request: APIRequestContext,
  owned: IndependentOwned,
) {
  const templates = await rows<ResearchTemplate>(
    request,
    owned.auth,
    "/research-templates",
  );
  const template = templates.find(
    (item) => item.template_id === "daily_timing",
  );
  expect(
    template,
    "Actual maintained deterministic timing template is required",
  ).toBeDefined();
  expect(sha256(template!.code)).toBe(template!.code_sha256);
  const capabilities = await read<ResearchCapabilities>(
    request,
    owned.auth,
    "/research-capabilities",
  );
  expect(capabilities.live_model_calls).toBe(false);
  expect(capabilities.model_providers).toEqual(["fixture"]);
  const snapshot = await frozenSnapshot(request, owned.auth);
  const strategy = await saveVersion(
    request,
    owned.auth,
    template!,
    template!.code,
    null,
  );
  return runActual(request, owned, strategy, snapshot, capabilities);
}
export type ActualSource = Awaited<ReturnType<typeof createActualSource>>;
export function sourcePins(source: ActualSource) {
  return {
    source_kind: "strategy_version" as const,
    source_id: source.strategy.id,
    code_sha256: source.strategy.content_sha256,
    spec_revision: source.spec.revision,
    spec_sha256: source.spec.spec_sha256,
    evaluation_id: source.leaf.evaluation_id,
    ...tracePins(source.leaf),
  };
}
export function verificationUrl(source: ActualSource) {
  return `/strategies/${source.strategy.id}/research?${query({ tab: "verification", evaluation_id: source.leaf.evaluation_id, code_sha256: source.strategy.content_sha256, spec_revision: source.spec.revision, spec_sha256: source.spec.spec_sha256, ...tracePins(source.leaf), trace_status: "available" })}`;
}
