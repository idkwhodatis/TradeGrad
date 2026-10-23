import { benchmarkSchema, benchmarkSelectionIssue } from "./benchmarks";
import { z } from "zod";
import type {
  DatasetPublic,
  ResearchCapabilities,
  ResearchRunCreate,
  StrategyPublic,
  UniversePublic,
} from "@/client/types.gen";
import { asRecord, stableJson } from "./data-utils";

export type ResearchCatalog = {
  datasets: DatasetPublic[];
  strategies: StrategyPublic[];
  universes: UniversePublic[];
};
export type ResearchIssue = { path: string; message: string };
const integer = (min: number, max: number) =>
  z.number().int().min(min).max(max);
const rate = z
  .number()
  .min(0)
  .max(0.1, "Enter a fractional rate, e.g. 0.0005 = 0.05%.");
const reference = z.strictObject({
  id: z.uuid("Choose an immutable published version."),
  sha256: z.string().regex(/^[0-9a-f]{64}$/, "A verified SHA-256 is required."),
});
const windowSchema = z.strictObject({
  start: z.iso.date("Choose a valid inclusive start date."),
  end: z.iso.date("Choose a valid exclusive end date."),
});

const modelIdentifier = z
  .string()
  .min(1)
  .max(160)
  .regex(/^[A-Za-z0-9][A-Za-z0-9_.:/-]*$/)
  .refine(
    (value) => !value.includes("://") && !/^(sk-|sk_|bearer|eyj)/i.test(value),
    "Select a server-managed identifier, never a URL or credential.",
  );
// Fixture requests retain their historical four-field wire shape. Adding null
// profile fields or an output-token default would change immutable hashes and
// the body of an already pending idempotent submission.
export const researchModelSchema = z.discriminatedUnion("provider", [
  z.strictObject({
    provider: z.literal("fixture"),
    model: z.literal("deterministic-fixture-v1"),
    credential_ref: z.null(),
    temperature: z.literal(0),
  }),
  z.strictObject({
    provider: z.enum(["openai", "openrouter", "openai-compatible"]),
    model: modelIdentifier,
    credential_ref: z.null(),
    profile_id: z.string().regex(/^[a-z][a-z0-9_-]{0,63}$/),
    profile_sha256: z.string().regex(/^[0-9a-f]{64}$/),
    temperature: z.number().min(0).max(2),
    max_output_tokens: integer(1, 16384),
  }),
]);

/** Only expose complete, internally consistent owner-scoped capability choices. */
export function researchModelProfiles(
  capabilities: ResearchCapabilities,
  mode: "native_akquant" | "portable" = "native_akquant",
) {
  return (capabilities.model_profiles || []).flatMap((profile, _, profiles) => {
    const parsed = researchModelSchema.safeParse(profile.config);
    if (
      !parsed.success ||
      !profile.strategy_modes?.includes(mode) ||
      profiles.filter((item) => item.id === profile.id).length !== 1 ||
      (parsed.data.provider !== "fixture" &&
        parsed.data.profile_id !== profile.id) ||
      !capabilities.model_providers?.includes(parsed.data.provider) ||
      !capabilities.models?.includes(parsed.data.model) ||
      (!profile.synthetic && !capabilities.live_model_calls) ||
      (parsed.data.provider === "fixture" && !profile.synthetic)
    )
      return [];
    return [{ ...profile, config: parsed.data }];
  });
}

export function researchModelProfileFor(
  model: z.infer<typeof researchModelSchema>,
  capabilities: ResearchCapabilities,
  mode: "native_akquant" | "portable" = "native_akquant",
) {
  return researchModelProfiles(capabilities, mode).find(
    (profile) => stableJson(profile.config) === stableJson(model),
  );
}

export const nativeContractSchema = z.strictObject({
  contract_version: z.literal("native-declarations.v1").optional(),
  required_fields: z
    .array(z.enum(["open", "high", "low", "close", "volume", "amount"]))
    .min(1)
    .max(6)
    .optional(),
  lookback_bars: integer(0, 252),
  warmup_bars: integer(0, 252),
  requires_shorting: z.boolean(),
  parameter_binding: z.literal("context.tradegrad_parameters.v1").optional(),
  evidence: z.literal("user_declared_not_code_verified").optional(),
  parameters: z
    .array(
      z.strictObject({
        name: z.string().regex(/^[A-Za-z][A-Za-z0-9_]{0,63}$/),
        kind: z.enum(["integer", "number", "boolean", "string"]),
        value: z.union([z.boolean(), z.number(), z.string()]),
        minimum: z.number().nullable().optional(),
        maximum: z.number().nullable().optional(),
        choices: z
          .array(z.union([z.boolean(), z.number(), z.string()]))
          .max(32)
          .optional(),
      }),
    )
    .max(32)
    .optional(),
});

// These are the implemented RunConfig fields, not a second API transport contract.
// Literal controls remain visible and pinned rather than implying unsupported options.
export const researchFormSchema = z
  .strictObject({
    schema_version: z.literal("tradegrad.run.v1"),
    execution_mode: z.literal("optimization"),
    task: z.enum(["cn_time", "us_time", "cn_cross", "us_cross"]),
    profile: z.enum(["source_compat", "corrected_explicit_config"]),
    dataset: reference,
    universe: reference,
    seed_strategy: reference,
    strategy_contract: nativeContractSchema.nullable().optional(),
    windows: z.strictObject({
      train: windowSchema,
      validation: windowSchema,
      holdout: windowSchema.nullable(),
    }),
    warmup: z.strictObject({
      sessions: integer(0, 252),
      history_policy: z.enum(["require", "source_cold_start"]),
      lookback_contract: z.literal("declared_seed_minimum"),
    }),
    market_profile: z.strictObject({
      market: z.enum(["CN", "US"]),
      currency: z.enum(["CNY", "USD"]),
      calendar: z.enum(["XSHG", "XNYS"]),
      calendar_note: z.string().min(1).max(500),
      timezone: z.enum(["Asia/Shanghai", "America/New_York"]).nullable(),
      lot_size: integer(1, 10000).nullable(),
      initial_cash: z.number().positive().max(1_000_000_000_000),
      commission_rate: rate,
      stamp_tax_rate: rate,
      slippage: z.strictObject({ type: z.literal("percent"), value: rate }),
      history_depth: z.literal(252),
      t_plus_one: z.boolean(),
      risk_config: z
        .strictObject({
          account_mode: z.enum(["cash", "margin"]),
          enable_short_sell: z.boolean(),
        })
        .nullable(),
      decision_timing: z.literal("native_akquant"),
      assumptions: z.literal("historical_demonstration_not_broker_terms"),
    }),
    objective: z.strictObject({
      windowing_definition: z.literal("calendar_year_train_full_validation.v1"),
      definition_version: z.literal("source_reward.v1"),
      metric_definition_version: z.literal("source_metrics.v1"),
      ar_weight: z.literal(0.5),
      ar_reference: z.literal(20),
      mdd_reference: z.literal(15),
      freq_sharpness: z.literal(1),
      frequency_floor_version: z.literal("source_market.v1"),
      reward_rounding: z.literal("source_metrics_before_reward"),
    }),
    optimizer: z.strictObject({
      algorithm: z.literal("trade_tgd"),
      max_iterations: integer(0, 1000),
      sample_size: integer(1, 100),
      sample_method: z.enum(["power_law", "uniform", "top_k"]),
      large_revision_prob: z.number().min(0).max(1),
      memory_window: integer(1, 1000),
      seed: integer(0, 2 ** 32 - 1),
      worst_ratio: z.number().positive().max(1),
    }),
    budgets: z.strictObject({
      max_model_calls: integer(0, 10000),
      max_tokens: integer(0, 100000000),
      max_runtime_seconds: integer(1, 86400),
      max_repairs: integer(0, 20),
    }),
    model: researchModelSchema,
    prompt_version: z.literal("source_prompts.v1"),
    environment_digest: z
      .string()
      .regex(
        /^[0-9a-f]{64}$/,
        "Reload deployment capabilities to obtain the environment digest.",
      ),
    benchmark: benchmarkSchema,
  })
  .superRefine((body, ctx) => {
    const add = (path: string, message: string) =>
      ctx.addIssue({ code: "custom", path: path.split("."), message });
    const windows = Object.entries(body.windows).filter(
      ([, value]) => value !== null,
    );
    for (const [name, value] of windows) {
      if (!value) continue;
      if (value.start >= value.end)
        add(
          `windows.${name}.end`,
          "End must follow start; every window is [start, end). ",
        );
      if (Date.parse(value.end) - Date.parse(value.start) > 36525 * 86400000)
        add(`windows.${name}.end`, "A window cannot exceed 100 years.");
    }
    for (let index = 1; index < windows.length; index++) {
      const previous = windows[index - 1][1];
      const [name, current] = windows[index];
      if (previous && current && previous.end > current.start)
        add(
          `windows.${name}.start`,
          "Train, validation and holdout must be chronological and non-overlapping. Adjacent exclusive boundaries are allowed.",
        );
    }
    const cn = body.task.startsWith("cn_");
    const market = body.market_profile;
    if (market.market !== (cn ? "CN" : "US"))
      add("task", "Task and market must agree.");
    if (market.currency !== (cn ? "CNY" : "USD"))
      add("market_profile.currency", "Currency must match the market.");
    if (market.calendar !== (cn ? "XSHG" : "XNYS"))
      add("market_profile.calendar", "Calendar must match the market.");
    if (
      market.timezone !== null &&
      market.timezone !== (cn ? "Asia/Shanghai" : "America/New_York")
    )
      add("market_profile.timezone", "Timezone must match the market.");
    if (market.lot_size !== null && market.lot_size !== (cn ? 100 : 1))
      add(
        "market_profile.lot_size",
        "Only the declared CN/US equity order lot is supported. Volume units are separate.",
      );
    if (market.t_plus_one !== cn)
      add(
        "market_profile.t_plus_one",
        "Settlement must match the supported market.",
      );
    if (cn && market.risk_config?.enable_short_sell)
      add("market_profile.risk_config", "CN short-selling is not certified.");
    if (
      ((body.strategy_contract?.requires_shorting ?? !cn) &&
        !market.risk_config?.enable_short_sell) ||
      (market.risk_config?.enable_short_sell &&
        market.risk_config.account_mode !== "margin")
    )
      add(
        "market_profile.risk_config",
        "US seeds require explicit margin and short-selling support.",
      );
    const minimum = body.strategy_contract
      ? Math.max(
          body.strategy_contract.lookback_bars,
          body.strategy_contract.warmup_bars,
        )
      : body.task.endsWith("_time")
        ? 20
        : 3;
    if (body.profile === "source_compat") {
      if (body.strategy_contract)
        add(
          "strategy_contract",
          "Declared contracts require corrected_explicit_config; legacy source compatibility is unchanged.",
        );
      if (
        body.warmup.sessions !== 0 ||
        body.warmup.history_policy !== "source_cold_start"
      )
        add(
          "warmup.sessions",
          "Source compatibility preserves zero-history cold starts.",
        );
    } else {
      if (
        body.warmup.sessions < minimum ||
        body.warmup.history_policy !== "require"
      )
        add(
          "warmup.sessions",
          `This corrected task needs at least ${minimum} previous available sessions per listing.`,
        );
      if (market.timezone === null || market.lot_size === null)
        add(
          "market_profile.timezone",
          "Corrected execution requires explicit timezone and order lot size.",
        );
    }
  });
export type ResearchFormValues = z.infer<typeof researchFormSchema>;

export const profileDescriptions = {
  corrected_explicit_config:
    "Default: explicit market, timezone and order lots, with required pre-start history. This is not a claim of paper reproduction or broker-realistic fees.",
  source_compat:
    "Historical source-compatible engine defaults: zero-history cold starts, engine-default timezone and lot size, fixed demonstration fees and capital. US market/shorting are still explicit. Matching source behavior is not full paper reproduction.",
};

export function matchingUniverses(
  dataset: DatasetPublic | undefined,
  universes: UniversePublic[],
) {
  if (!dataset) return [];
  const pinned = asRecord(dataset.manifest.universe);
  const listings = Array.isArray(dataset.manifest.listings)
    ? dataset.manifest.listings.map((row) => asRecord(row).listing_id).sort()
    : [];
  return universes.filter((universe) => {
    if (
      pinned.id &&
      (pinned.id !== universe.id ||
        pinned.content_sha256 !== universe.content_sha256)
    )
      return false;
    const members = [
      ...new Set(universe.members.map((row) => row.listing_id)),
    ].sort();
    return listings.length > 0 && stableJson(listings) === stableJson(members);
  });
}

export function suggestWindows(
  dataset: DatasetPublic,
  sessions: number,
): ResearchFormValues["windows"] {
  const start = Date.parse(dataset.start_date);
  const days = Math.max(
    3,
    Math.round((Date.parse(dataset.end_date) - start) / 86400000),
  );
  // Suggestions only. Calendar days never certify warm-up session availability.
  const reserve = Math.min(Math.ceil(sessions * 1.7), Math.floor(days / 3));
  const boundary = (offset: number) =>
    new Date(start + offset * 86400000).toISOString().slice(0, 10);
  const validation = reserve + Math.max(1, Math.floor((days - reserve) * 0.7));
  return {
    train: { start: boundary(reserve), end: boundary(validation) },
    validation: { start: boundary(validation), end: dataset.end_date },
    holdout: null,
  };
}
const emptyRef = { id: "", sha256: "" };
const versionRef = (item?: { id: string; content_sha256: string }) =>
  item ? { id: item.id, sha256: item.content_sha256 } : { ...emptyRef };
export { versionRef };

export function researchDefaults(
  capabilities: ResearchCapabilities,
  catalog: ResearchCatalog,
  preferred?: string,
): ResearchFormValues {
  if (preferred && !catalog.datasets.some((row) => row.id === preferred))
    throw new Error(
      "The requested published dataset is unavailable for this owner. Return to Datasets and choose an accessible immutable version.",
    );
  const dataset =
    catalog.datasets.find((row) => row.id === preferred) ||
    catalog.datasets.find(
      (row) => matchingUniverses(row, catalog.universes).length > 0,
    ) ||
    catalog.datasets[0];
  const basket =
    Array.isArray(dataset?.manifest.listings) &&
    dataset.manifest.listings.length > 1;
  const task =
    `${dataset?.market.toLowerCase() || "cn"}_${basket ? "cross" : "time"}` as ResearchFormValues["task"];
  const profile = "corrected_explicit_config";
  const defaults = capabilities.profile_defaults.find(
    (row) => row.task === task && row.profile === profile,
  );
  if (!defaults)
    throw new Error(
      "The server has no defaults for this task/profile. Reload deployment capabilities.",
    );
  const common = z
    .strictObject({
      optimizer: researchFormSchema.shape.optimizer,
      objective: researchFormSchema.shape.objective,
      budgets: researchFormSchema.shape.budgets,
      model: researchFormSchema.shape.model,
    })
    .safeParse(capabilities.defaults);
  const market = researchFormSchema.shape.market_profile.safeParse(
    defaults.market_profile,
  );
  const warmup = researchFormSchema.shape.warmup.safeParse(defaults.warmup);
  if (!common.success || !market.success || !warmup.success)
    throw new Error(
      "The deployment returned incomplete or unsupported research defaults. Reload capabilities before configuring a run.",
    );
  if (
    common.data.model.provider !== "fixture" ||
    !researchModelProfileFor(common.data.model, capabilities)
  )
    throw new Error(
      "The deployment must provide an available deterministic fixture default. Reload capabilities before configuring a run.",
    );
  const body: ResearchRunCreate = {
    schema_version: "tradegrad.run.v1",
    execution_mode: "optimization",
    task,
    profile,
    dataset: versionRef(dataset),
    universe: versionRef(matchingUniverses(dataset, catalog.universes)[0]),
    seed_strategy: versionRef(
      catalog.strategies.find(
        (row) => row.task === task && row.mode === "native_akquant",
      ),
    ),
    windows: dataset
      ? suggestWindows(dataset, defaults.warmup.sessions)
      : {
          train: { start: "", end: "" },
          validation: { start: "", end: "" },
          holdout: null,
        },
    warmup: warmup.data,
    market_profile: market.data,
    ...common.data,
    prompt_version: "source_prompts.v1",
    environment_digest: capabilities.environment_digest,
    benchmark: null,
  };
  const selectedStrategy = catalog.strategies.find(
    (row) => row.id === body.seed_strategy.id,
  );
  if (selectedStrategy?.manifest) {
    body.strategy_contract = selectedStrategy.manifest;
    body.warmup.sessions = Math.max(
      selectedStrategy.manifest.lookback_bars,
      selectedStrategy.manifest.warmup_bars,
    );
    if (dataset) body.windows = suggestWindows(dataset, body.warmup.sessions);
  }
  // Defaults are server-owned and complete; local schema validates before use/submission.
  return structuredClone(body) as ResearchFormValues;
}

export function researchSelectionIssues(
  body: ResearchFormValues,
  catalog: ResearchCatalog,
  capabilities: ResearchCapabilities,
): ResearchIssue[] {
  const errors: ResearchIssue[] = [];
  const benchmarkError = benchmarkSelectionIssue(
    body.benchmark,
    catalog.datasets,
    body.market_profile.market,
  );
  if (benchmarkError)
    errors.push({ path: "benchmark", message: benchmarkError });
  const add = (path: string, message: string) => errors.push({ path, message });
  if (!researchModelProfileFor(body.model, capabilities))
    add(
      "model.profile_id",
      "The model profile is unavailable or changed for this owner. Select an available profile with its exact configuration, then run preflight again.",
    );
  const dataset = catalog.datasets.find((row) => row.id === body.dataset.id);
  const strategy = catalog.strategies.find(
    (row) => row.id === body.seed_strategy.id,
  );
  const universe = catalog.universes.find((row) => row.id === body.universe.id);
  if (
    stableJson(strategy?.manifest ?? null) !==
    stableJson(body.strategy_contract ?? null)
  )
    add(
      "seed_strategy.id",
      "Reselect the seed to pin its exact immutable declaration and parameter values.",
    );
  for (const [path, value, reference] of [
    ["dataset.id", dataset, body.dataset],
    ["seed_strategy.id", strategy, body.seed_strategy],
    ["universe.id", universe, body.universe],
  ] as const) {
    if (!value || value.content_sha256 !== reference.sha256)
      add(
        path,
        "Select an available immutable version with its exact current hash.",
      );
  }
  if (dataset) {
    if (dataset.market !== body.market_profile.market)
      add("dataset.id", "The dataset market must match the task.");
    if (
      !matchingUniverses(dataset, catalog.universes).some(
        (row) => row.id === body.universe.id,
      )
    )
      add(
        "universe.id",
        "Choose the exact pinned universe and complete listing membership for this snapshot.",
      );
    const listings = Array.isArray(dataset.manifest.listings)
      ? dataset.manifest.listings
      : [];
    if (body.task.endsWith("_time") && listings.length !== 1)
      add(
        "dataset.id",
        "A time-series task needs exactly one listing. Choose a single-listing snapshot or a cross-sectional task.",
      );
    const compatibility = asRecord(dataset.manifest.evaluation_compatibility);
    const quality = asRecord(dataset.manifest.quality);
    if (
      compatibility.compatible === false ||
      (Array.isArray(quality.unknown_flags)
        ? quality.unknown_flags.length > 0
        : Boolean(quality.unknown_flags))
    )
      add(
        "dataset.id",
        `Snapshot units or semantics need correction in a new snapshot. ${Array.isArray(compatibility.reasons) ? compatibility.reasons.join(" ") : "Inspect the coverage and quality report."}`,
      );
    for (const [name, window] of Object.entries(body.windows)) {
      if (!window) continue;
      if (window.start < dataset.start_date)
        add(
          `windows.${name}.start`,
          "Start lies before immutable snapshot coverage.",
        );
      if (window.end > dataset.end_date)
        add(
          `windows.${name}.end`,
          "Exclusive end lies after immutable snapshot coverage.",
        );
      if (
        universe?.state === "current_snapshot" &&
        window.start < universe.as_of.slice(0, 10)
      )
        add(
          `windows.${name}.start`,
          "Current membership cannot be applied before its as-of date.",
        );
    }
    if (
      body.warmup.sessions > 0 &&
      body.windows.train.start <= dataset.start_date
    )
      add(
        "windows.train.start",
        "Reserve earlier available sessions for warm-up; snapshot start cannot be the corrected training start.",
      );
  }
  if (
    strategy &&
    (strategy.task !== body.task || strategy.mode !== "native_akquant")
  )
    add(
      "seed_strategy.id",
      "The native seed must match this market and task; portable execution is unavailable.",
    );
  if (body.environment_digest !== capabilities.environment_digest)
    add(
      "environment_digest",
      "Deployment environment changed. Reload defaults and run preflight again.",
    );
  const defaults = capabilities.profile_defaults.find(
    (row) => row.task === body.task && row.profile === body.profile,
  );
  if (
    !defaults ||
    !capabilities.tasks?.includes(body.task) ||
    !capabilities.profiles?.includes(body.profile)
  )
    add("profile", "Task/profile is not supported by this deployment.");
  if (!capabilities.sample_methods?.includes(body.optimizer.sample_method))
    add(
      "optimizer.sample_method",
      "Choose a sampling method supported by this deployment.",
    );
  if (body.profile === "source_compat" && defaults) {
    const keys = [
      "initial_cash",
      "commission_rate",
      "stamp_tax_rate",
      "slippage",
      "history_depth",
      "t_plus_one",
      "risk_config",
      "timezone",
      "lot_size",
    ] as const;
    if (
      keys.some(
        (key) =>
          stableJson(body.market_profile[key]) !==
          stableJson(defaults.market_profile[key]),
      )
    )
      add(
        "profile",
        "Source compatibility fixes the source engine defaults. Restore defaults or use corrected explicit configuration.",
      );
  }
  return errors;
}

export function researchStepFor(path: string) {
  if (
    /^(task|profile|dataset|universe|seed_strategy|benchmark|strategy_contract)/.test(
      path,
    )
  )
    return 0;
  if (/^(windows|warmup)/.test(path)) return 1;
  return 2;
}
