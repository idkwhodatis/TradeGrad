import { benchmarkSchema } from "@/lib/benchmarks";
import {
  researchCaptureSchema,
  researchModelSchema,
} from "@/lib/research-form";
import { z } from "zod";
import type {
  DatasetPublic,
  PortableRunDetail,
  PortableRunCreate,
  UniversePublic,
} from "@/client/types.gen";
import { asRecord, stableJson } from "@/lib/data-utils";

export const portableNotice =
  "Portable · interface_only. Policy and trusted AKQuant execution are separate contracts. Real-container certification is unverified until actual CI evidence is available; external-framework equivalence is not_certified. No live trading, broker connection or external deployment.";
export const portableDataNotice =
  "Preflight requires pinned raw-price/action semantics, exact exchange sessions and complete snapshots. Synthetic no-actions evidence and an explicitly acknowledged user-declared raw/no-actions upload are distinct paths. User declarations remain unverified research assumptions, never provider or runtime certification. An empty actions list proves nothing; adjusted/unknown provider data is not silently converted.";
export const portableTerminal = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "paused",
  "interrupted",
]);
export const portableSections = [
  "account_series",
  "positions",
  "orders",
  "fills",
  "closed_trades",
  "native_metrics",
  "decision_trace",
  "logical_orders",
] as const;
export type PortableSection = (typeof portableSections)[number];
const integer = (min: number, max: number) =>
  z.number().int().min(min).max(max);
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const reference = z.strictObject({
  id: z.string().min(1).max(255),
  sha256: hash,
});
const window = z.strictObject({
  start: z.string().regex(/^(\d{4}-\d{2}-\d{2})?$/),
  end: z.string().regex(/^(\d{4}-\d{2}-\d{2})?$/),
});
// Browser form/persistence validation only. Transport DTOs come from generated OpenAPI;
// the server remains authoritative for hashes, semantic validation and admission.
export const portableFormSchema = z.strictObject({
  schema_version: z.literal("tradegrad.portable.run.v1"),
  evaluation_mode: z.literal("portable"),
  execution_mode: z.enum(["seed_evaluation", "optimization"]),
  task: z.enum(["cn_time", "us_time", "cn_cross", "us_cross"]),
  profile: z.literal("corrected_explicit_config"),
  strategy_kind: z.enum(["portfolio", "event"]),
  dataset: reference,
  universe: reference,
  seed_strategy: reference,
  seed_manifest: reference,
  seed_parameters: reference,
  research_capture: researchCaptureSchema.nullable().optional(),
  windows: z.strictObject({
    train: window,
    validation: window.nullable(),
    holdout: window.nullable(),
  }),
  warmup: z.strictObject({
    sessions: integer(1, 252),
    history_policy: z.literal("require"),
    lookback_contract: z.literal("declared_seed_minimum"),
  }),
  initial_cash: z.number().positive().max(1e12),
  initial_state: z.strictObject({
    codec_version: z.literal("state-json.v1"),
    schema_name: z.string().min(1),
    schema_version: integer(1, 1000000),
    schema_sha256: hash,
    payload_json: z.string().max(65536),
    payload_sha256: hash,
  }),
  engine_profile: z
    .strictObject({
      profile_version: z.literal(
        "akquant.portable.cash-daily.controller-t1.v3",
      ),
      engine: z.literal("akquant"),
      engine_version: z.literal("0.3.27"),
      market: z.enum(["CN", "US"]),
      initial_cash: z.number().positive().max(1e12),
      commission_rate: z.literal(0.0005),
      stamp_tax_rate: z.number(),
      transfer_fee_rate: z.literal(0),
      min_commission: z.literal(0),
      slippage: z.strictObject({
        type: z.literal("percent"),
        value: z.literal(0.0005),
      }),
      volume_limit_pct: z.literal(0.25),
      t_plus_one: z.literal(false),
      engine_settlement_mode: z.literal("T+0"),
      application_settlement_enforcement: z.literal(
        "trusted_controller_daily_next_open_inventory.v1",
      ),
      application_t_plus_one: z.boolean(),
      initial_positions: z.literal("flat"),
      policy_callback: z.literal("complete_daily_cross_section_after_close"),
      intraday_policy: z.literal("unsupported"),
      external_funding: z.literal("unsupported"),
      corporate_actions: z.literal("reject_window_with_actions"),
      limit_execution: z.literal(
        "reject_non_open_or_limit_breaching_native_fill",
      ),
      lot_size: z.union([z.literal(1), z.literal(100)]),
      timezone: z.enum(["Asia/Shanghai", "America/New_York"]),
      risk_config: z.strictObject({
        account_mode: z.literal("cash"),
        enable_short_sell: z.literal(false),
      }),
      fill_policy: z.literal("NextOpen"),
      time_in_force: z.literal("GTC"),
      sizing_price: z.literal("decision_close"),
      sizing_equity: z.literal("decision_close_native_equity"),
      cash_budget: z.literal(
        "available_cash_without_anticipated_sale_proceeds",
      ),
      sell_budget: z.literal("decision_close_holdings_less_pending_sells"),
      sellability_observation: z.literal("next_open_unreserved_holdings"),
      unsettled_next_open_sell: z.literal(
        "admit_after_close_with_controller_t1",
      ),
      pending_rebalance: z.literal("skip_until_all_native_legs_terminal"),
      final_liquidation: z.literal(false),
    })
    .superRefine((profile, context) => {
      const cn = profile.market === "CN";
      const expected = {
        application_t_plus_one: cn,
        stamp_tax_rate: cn ? 0.001 : 0,
        lot_size: cn ? 100 : 1,
        timezone: cn ? "Asia/Shanghai" : "America/New_York",
      };
      for (const key of Object.keys(expected) as (keyof typeof expected)[]) {
        if (profile[key] !== expected[key]) {
          context.addIssue({
            code: "custom",
            path: [key],
            message:
              "Preserve the exact reviewed engine profile for this market.",
          });
        }
      }
    }),
  objective: z.strictObject({
    windowing_definition: z.literal("calendar_year_train_full_validation.v1"),
    definition_version: z.literal("portable_return_drawdown.v1"),
    metric_definition_version: z.literal("portable_native_summary.v1"),
    reward_formula: z.literal("total_return_pct - max_drawdown_pct"),
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
  prompt_version: z.literal("portable_business_rules.v1"),
  environment_digest: hash,
  benchmark: benchmarkSchema,
  conformance: z.literal("interface_only"),
  external_framework_equivalence: z.literal("not_certified"),
});
export type PortableForm = z.infer<typeof portableFormSchema>;
// Compile-time check against the generated, server-owned request contract.
export const portableRequest = (body: PortableForm): PortableRunCreate => body;
export function portableIssues(body: PortableForm): string[] {
  const issues: string[] = [];
  const present = Object.entries(body.windows).filter(
    ([, value]) => value !== null,
  );
  for (const [name, value] of present) {
    if (!value) continue;
    const valid = (day: string) =>
      /^\d{4}-\d{2}-\d{2}$/.test(day) &&
      Number.isFinite(Date.parse(day)) &&
      new Date(day).toISOString().slice(0, 10) === day;
    if (!valid(value.start) || !valid(value.end) || value.start >= value.end)
      issues.push(
        `${name}: choose valid [start, end) dates with end after start.`,
      );
  }
  for (let i = 1; i < present.length; i++)
    if (present[i - 1][1]!.end > present[i][1]!.start)
      issues.push(
        "Train, validation and metadata-only holdout must be chronological and non-overlapping.",
      );
  if (body.execution_mode === "optimization" && !body.windows.validation)
    issues.push("Optimization requires a distinct validation window.");
  if (
    body.execution_mode === "seed_evaluation" &&
    body.optimizer.max_iterations !== 0
  )
    issues.push("Seed evaluation requires zero optimizer iterations.");
  if (
    body.strategy_kind !== (body.task.endsWith("_time") ? "event" : "portfolio")
  )
    issues.push(
      "Policy kind must match the task; native strategies cannot be converted implicitly.",
    );
  if (body.engine_profile.market !== body.task.slice(0, 2).toUpperCase())
    issues.push("Pinned engine profile and task markets differ.");
  if (body.engine_profile.initial_cash !== body.initial_cash)
    issues.push("Pinned engine profile and initial cash differ.");
  return issues;
}
export function portableUniverses(
  dataset: DatasetPublic | undefined,
  universes: UniversePublic[],
) {
  if (!dataset) return [];
  const pin = asRecord(dataset.manifest.universe);
  const listings = Array.isArray(dataset.manifest.listings)
    ? dataset.manifest.listings.map((item) => asRecord(item).listing_id).sort()
    : [];
  return universes.filter(
    (item) =>
      (!pin.id ||
        (pin.id === item.id && pin.content_sha256 === item.content_sha256)) &&
      listings.length > 0 &&
      stableJson(listings) ===
        stableJson(
          [...new Set(item.members.map((row) => row.listing_id))].sort(),
        ),
  );
}
export function portableControls(
  run: Pick<PortableRunDetail, "status" | "attempts" | "latest_checkpoint_id">,
) {
  const resumable = ["paused", "failed", "cancelled", "interrupted"].includes(
    run.status,
  );
  return {
    pause: ["queued", "preparing", "running"].includes(run.status),
    cancel:
      !portableTerminal.has(run.status) && run.status !== "cancel_requested",
    resume:
      resumable &&
      Boolean(run.latest_checkpoint_id) &&
      run.attempts.every((a) => a.sandbox_stopped),
    resumeReason: !resumable
      ? null
      : !run.latest_checkpoint_id
        ? "No complete checkpoint is published. Resume cannot invent optimizer or policy state."
        : run.attempts.some((a) => !a.sandbox_stopped)
          ? "Waiting for both policy and engine execution environments to be confirmed stopped."
          : "The server verifies checkpoint, immutable inputs and both pinned image identities before resuming.",
  };
}
export const portableFingerprint = (body: unknown) => stableJson(body);
