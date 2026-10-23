import savedConfig from "../fixtures/portable-config.json";
import type {
  PortableResultQuery,
  PortableRunDetail,
} from "../../src/client/types.gen";
import type { PortableCatalog } from "../../src/lib/portable-api";
import { portableFormSchema } from "../../src/features/portable/protocol";
import { capabilityFixture } from "./research-fixtures";
export const portableFixture = () => {
  // Serialized by the real Python PortableRunConfig. Local fixture only, not execution evidence.
  const body = portableFormSchema.parse(structuredClone(savedConfig));
  const listing = "d4444444-4444-4444-8444-444444444444";
  const catalog: PortableCatalog = {
    capabilities: capabilityFixture(),
    datasets: [
      {
        id: body.dataset.id,
        name: "Synthetic portable raw/no-actions fixture",
        market: "CN",
        start_date: "2024-01-02",
        end_date: "2025-01-01",
        source: "portable_fixture",
        is_synthetic: true,
        content_sha256: body.dataset.sha256,
        manifest: {
          listings: [{ listing_id: listing }],
          universe: {
            id: body.universe.id,
            content_sha256: body.universe.sha256,
          },
        },
        created_at: "2026-10-22T12:00:00Z",
      },
    ],
    universes: [
      {
        id: body.universe.id,
        name: "User-fixed synthetic basket",
        state: "user_fixed",
        as_of: "2026-10-22T12:00:00Z",
        members: [{ listing_id: listing }],
        evidence: [],
        content_sha256: body.universe.sha256,
        created_at: "2026-10-22T12:00:00Z",
      },
    ],
    strategies: [
      {
        id: body.seed_strategy.id,
        name: "Momentum reference",
        task: "cn_cross",
        mode: "portable",
        content_sha256: body.seed_strategy.sha256,
        manifest: { universe: { snapshot_sha256: body.universe.sha256 } },
        parameters: { top_n: 1 },
        created_at: "2026-10-22T12:00:00Z",
      },
    ],
    templates: [
      {
        strategy_kind: "portfolio",
        template_version: "portable_business_rules.v1",
        source: "# Explicit display-only reference fixture",
        code_sha256: body.seed_strategy.sha256,
        editable_region: "business_rules_only",
        conformance: "interface_only",
        runtime_certified: false,
        external_framework_equivalence: "not_certified",
      },
      {
        strategy_kind: "event",
        template_version: "portable_business_rules.v1",
        source: "# Explicit display-only event reference",
        code_sha256: body.seed_strategy.sha256,
        editable_region: "business_rules_only",
        conformance: "interface_only",
        runtime_certified: false,
        external_framework_equivalence: "not_certified",
      },
    ],
  };
  return { body, catalog };
};
export function portableRunFixture(): PortableRunDetail {
  const { body } = portableFixture();
  return {
    id: "e5555555-5555-4555-8555-555555555555",
    status: "running",
    generation: 1,
    config: body,
    dataset_version_id: body.dataset.id,
    strategy_version_id: body.seed_strategy.id,
    experiment_id: null,
    split_version_id: null,
    active_attempt_id: null,
    next_event_seq: 0,
    research_image_digest: `sha256:${"1".repeat(64)}`,
    portable_policy_image_digest: `sha256:${"2".repeat(64)}`,
    latest_checkpoint_id: null,
    resume_checkpoint_id: null,
    result: null,
    error: null,
    created_at: "2026-10-22T12:00:00Z",
    updated_at: "2026-10-22T12:00:00Z",
    execution_mode: "optimization",
    attempts: [],
  };
}
export function portableResultFixture(): PortableResultQuery {
  return {
    evaluation_id: "f6666666-6666-4666-8666-666666666666",
    run_id: portableRunFixture().id,
    image_digest: `sha256:${"1".repeat(64)}`,
    policy_image_digest: `sha256:${"2".repeat(64)}`,
    environment_digest: "f".repeat(64),
    is_synthetic: true,
    currency: "CNY",
    schema_version: "tradegrad.portable-result.v1",
    evaluation_mode: "portable",
    result_trust: "isolated_policy_trusted_akquant_engine",
    ledger_origin: "trusted_engine",
    runtime_certified: false,
    external_framework_equivalence: "not_certified",
    artifact_sha256: "7".repeat(64),
    identity: { run_config_sha256: "8".repeat(64) },
    window: { split: "train", start: "2024-01-08", end: "2024-03-01" },
    summary: {
      initial_cash: 1000000,
      final_equity: 1000234.567,
      total_return_pct: 0.0234567,
      max_drawdown_pct: 0.01,
    },
    combined_score: 0.0134567,
    section: "account_series",
    rows: [
      {
        native_at: "2024-01-08T07:00:00Z",
        native_timestamp_ns: "1704697200000000000",
        session_date: "2024-01-08",
        equity: 1000234.567,
        cash: 800000.1,
        drawdown: 0.0001,
      },
    ],
    count: 1,
    offset: 0,
    limit: 50,
  };
}
