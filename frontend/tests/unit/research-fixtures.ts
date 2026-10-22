import type { ResearchCapabilities } from "../../src/client/types.gen";
import type {
  ResearchCatalog,
  ResearchFormValues,
} from "../../src/lib/research-form";

const ids = {
  dataset: "fa27479d-cd45-4fa7-899c-eac11d706184",
  universe: "e4664b4f-eb89-415c-947e-a40d4c55abb4",
  seed: "d6f4e598-ae5d-4b38-bfe6-1a3a72769c53",
};
export function researchFixture(
  task: ResearchFormValues["task"] = "cn_time",
  profile: ResearchFormValues["profile"] = "corrected_explicit_config",
) {
  const cn = task.startsWith("cn_");
  const corrected = profile === "corrected_explicit_config";
  const body: ResearchFormValues = {
    schema_version: "tradegrad.run.v1",
    execution_mode: "optimization",
    task,
    profile,
    dataset: { id: ids.dataset, sha256: "d".repeat(64) },
    universe: { id: ids.universe, sha256: "e".repeat(64) },
    seed_strategy: { id: ids.seed, sha256: "f".repeat(64) },
    environment_digest: "a".repeat(64),
    prompt_version: "source_prompts.v1",
    benchmark: null,
    windows: {
      train: { start: "2024-03-01", end: "2024-08-01" },
      validation: { start: "2024-08-01", end: "2024-11-01" },
      holdout: { start: "2024-11-01", end: "2025-01-01" },
    },
    warmup: {
      sessions: corrected ? (task.endsWith("_time") ? 20 : 3) : 0,
      history_policy: corrected ? "require" : "source_cold_start",
      lookback_contract: "declared_seed_minimum",
    },
    market_profile: {
      market: cn ? "CN" : "US",
      currency: cn ? "CNY" : "USD",
      calendar: cn ? "XSHG" : "XNYS",
      calendar_note: "Declared supported regular-equity calendar proxy.",
      timezone: corrected ? (cn ? "Asia/Shanghai" : "America/New_York") : null,
      lot_size: corrected ? (cn ? 100 : 1) : null,
      initial_cash: 1_000_000,
      commission_rate: 0.0005,
      stamp_tax_rate: cn ? 0.001 : 0,
      slippage: { type: "percent", value: 0.0005 },
      history_depth: 252,
      t_plus_one: cn,
      risk_config: cn
        ? null
        : { account_mode: "margin", enable_short_sell: true },
      decision_timing: "native_akquant",
      assumptions: "historical_demonstration_not_broker_terms",
    },
    objective: {
      windowing_definition: "calendar_year_train_full_validation.v1",
      definition_version: "source_reward.v1",
      metric_definition_version: "source_metrics.v1",
      ar_weight: 0.5,
      ar_reference: 20,
      mdd_reference: 15,
      freq_sharpness: 1,
      frequency_floor_version: "source_market.v1",
      reward_rounding: "source_metrics_before_reward",
    },
    optimizer: {
      algorithm: "trade_tgd",
      max_iterations: 1,
      sample_size: 3,
      sample_method: "power_law",
      large_revision_prob: 0.7,
      memory_window: 5,
      seed: 0,
      worst_ratio: 0.3,
    },
    model: {
      provider: "fixture",
      model: "deterministic-fixture-v1",
      temperature: 0,
      credential_ref: null,
    },
    budgets: {
      max_model_calls: 10,
      max_tokens: 100000,
      max_runtime_seconds: 300,
      max_repairs: 2,
    },
  };
  const listings = [
    { listing_id: "listing-a" },
    ...(task.endsWith("_cross") ? [{ listing_id: "listing-b" }] : []),
  ];
  const catalog: ResearchCatalog = {
    datasets: [
      {
        id: ids.dataset,
        content_sha256: body.dataset.sha256,
        created_at: "2026-01-01T00:00:00Z",
        name: "Published fixture",
        source: "synthetic_fixture",
        is_synthetic: true,
        market: cn ? "CN" : "US",
        start_date: "2024-01-01",
        end_date: "2025-01-01",
        manifest: {
          listings,
          universe: {
            id: ids.universe,
            content_sha256: body.universe.sha256,
            state: "user_fixed",
          },
          evaluation_compatibility: { compatible: true },
          quality: { unknown_flags: [] },
        },
      },
    ],
    universes: [
      {
        id: ids.universe,
        content_sha256: body.universe.sha256,
        name: "Fixed fixture universe",
        as_of: "2024-01-01T00:00:00Z",
        created_at: "2026-01-01T00:00:00Z",
        state: "user_fixed",
        members: structuredClone(listings),
        evidence: [],
      },
    ],
    strategies: [
      {
        id: ids.seed,
        content_sha256: body.seed_strategy.sha256,
        created_at: "2026-01-01T00:00:00Z",
        task,
        mode: "native_akquant",
        name: "Unchanged seed",
        parent_id: null,
      },
    ],
  };
  return { body, catalog };
}
export function capabilityFixture(): ResearchCapabilities {
  const { body } = researchFixture();
  return {
    environment_digest: body.environment_digest,
    schema_version: "tradegrad.run.v1",
    tasks: ["cn_time", "us_time", "cn_cross", "us_cross"],
    profiles: ["source_compat", "corrected_explicit_config"],
    sample_methods: ["power_law", "uniform", "top_k"],
    preflight: true,
    holdout_execution: false,
    live_model_calls: false,
    portable_execution: false,
    model_providers: ["fixture"],
    models: ["deterministic-fixture-v1"],
    execution_modes: ["optimization"],
    limits: {
      max_active_runs_per_user: 3,
      max_active_imports_per_user: 3,
      max_artifact_bytes: 64000000,
      max_upload_bytes: 64000000,
    },
    defaults: {
      optimizer: body.optimizer,
      objective: body.objective,
      budgets: body.budgets,
      model: body.model,
    },
    profile_defaults: (
      ["cn_time", "us_time", "cn_cross", "us_cross"] as const
    ).flatMap((task) =>
      (["source_compat", "corrected_explicit_config"] as const).map(
        (profile) => {
          const { body: selected } = researchFixture(task, profile);
          return {
            task,
            profile,
            market_profile: selected.market_profile,
            warmup: selected.warmup,
          };
        },
      ),
    ),
  };
}
