import type {
  ControlledExperimentPublic,
  DiagnosticParityEventPage,
  DiagnosticParityView,
  IndependentCapability,
  IndependentEvaluationDetail,
  IndependentResultPin,
  RunConfig,
} from "../../src/client/types.gen";
import { requiredDiagnosticChecks } from "../../src/lib/diagnostic-parity-evidence";
import type {
  ControlledRequest,
  DiagnosticRequest,
} from "../../src/lib/verification-draft";
import { verificationSource } from "../../src/lib/verification-evidence";
import { leafSelection } from "../../src/lib/workbench-evidence";
import { detail, hashes, ids, leaf, spec } from "./workbench-fixtures";
import { researchFixture } from "./research-fixtures";
import { finalExposure } from "./portable-final-fixtures";
export const verificationIds = {
  run: "30000000-0000-4000-8000-000000000001",
  evaluation: "30000000-0000-4000-8000-000000000002",
  attempt: "30000000-0000-4000-8000-000000000003",
  strategy: "30000000-0000-4000-8000-000000000004",
  control: "30000000-0000-4000-8000-000000000005",
  armRun: "30000000-0000-4000-8000-000000000006",
  variant: "30000000-0000-4000-8000-000000000007",
};
export const verificationHashes = {
  config: "0".repeat(64),
  evaluationConfig: "e".repeat(64),
  request: "1".repeat(64),
  protocol: "2".repeat(64),
  report: "3".repeat(64),
  result: "4".repeat(64),
  trace: "5".repeat(64),
};
export const verificationSelection = leafSelection(
  {
    tab: "verification",
    spec_revision: spec.revision,
    spec_sha256: spec.spec_sha256,
  },
  leaf,
);
export const independentSource = verificationSource(
  verificationSelection,
  spec,
  leaf,
  true,
);
export const diagnosticBody: DiagnosticRequest = {
  source: independentSource,
  research_capture: { mode: "research" },
  max_runtime_seconds: 300,
};
export const controlledBody: ControlledRequest = {
  ...diagnosticBody,
  name: "Test liquidity mechanism",
  hypothesis: "Liquidity threshold changes selected listings",
  planned_change: "Only increase minimum liquidity",
  arms: [
    {
      name: "Copied baseline",
      code: "source",
      parameters: { minimum: 50 },
      planned_change: null,
    },
    {
      name: "Variant 1",
      code: "source",
      parameters: { minimum: 100 },
      planned_change: "Increase minimum liquidity",
    },
  ],
};
export const independentPin: IndependentResultPin = {
  execution_config_sha256: verificationHashes.evaluationConfig,
  source_kind: "strategy_version",
  source_id: verificationIds.strategy,
  code_sha256: hashes.code,
  evaluation_id: verificationIds.evaluation,
  execution_attempt_id: verificationIds.attempt,
  result_manifest_sha256: verificationHashes.result,
  trace_manifest_sha256: verificationHashes.trace,
};
export function capability(): IndependentCapability {
  return {
    source: independentSource,
    source_run_id: ids.run,
    original_config_sha256: "6".repeat(64),
    original_config: researchFixture().body,
    evaluation_mode: "native_akquant",
    can_execute: true,
    can_control: true,
    control_unavailable_reasons: [],
    unavailable_reasons: [],
    parity_prerequisites: {
      status: "potentially_verifiable",
      missing: [],
      rng_scope: "python_random_numpy_legacy",
      limitations: ["OS entropy not covered"],
    },
    fixed_conditions: {
      reconstruction_window: leaf.window,
      warmup: { sessions: 20, history_policy: "require" },
      state_policy: "original_start_fresh_engine_with_original_warmup.v1",
      dataset: { id: ids.strategy, sha256: "6".repeat(64) },
      fees: { commission_rate: 0.0005 },
      initial_cash: 1000000,
      lookback: 20,
    },
    protocol_sha256: verificationHashes.protocol,
    source_code: "source",
    source_parameters: { minimum: 50 },
    exposure_snapshot: finalExposure,
  };
}
export function independentJob(): IndependentEvaluationDetail {
  return {
    id: verificationIds.run,
    independent_id: verificationIds.run,
    status: "succeeded",
    generation: 1,
    purpose: "diagnostic",
    request: diagnosticBody,
    request_sha256: verificationHashes.request,
    config_sha256: verificationHashes.config,
    config: {
      schema_version: "tradegrad.independent-evaluation.v1",
      execution_mode: "independent_evaluation",
      evaluation_mode: "native_akquant",
      independent_id: verificationIds.run,
      purpose: "diagnostic",
      source: independentSource,
      source_run_id: ids.run,
      original_config_sha256: "6".repeat(64),
      source_config_sha256: "7".repeat(64),
      source_config: researchFixture().body as RunConfig,
      selected_strategy: { id: verificationIds.strategy, sha256: hashes.code },
      selected_contract: null,
      split: "train",
      reconstruction_window: leaf.window as { start: string; end: string },
      state_policy: "original_start_fresh_engine_with_original_warmup.v1",
      research_capture: diagnosticBody.research_capture,
      environment_digest: "8".repeat(64),
      research_image_digest: `sha256:${"9".repeat(64)}`,
      runtime_observation_sha256: "a".repeat(64),
      rng_restore: { state: "bounded fixture only" },
      protocol_sha256: verificationHashes.protocol,
      max_runtime_seconds: 300,
    },
    source: independentSource,
    experiment_id: ids.strategy,
    split_version_id: ids.strategy,
    active_attempt_id: verificationIds.attempt,
    next_event_seq: 5,
    evaluation_id: verificationIds.evaluation,
    result_ready: true,
    result_pin: independentPin,
    error: null,
    created_at: "2026-10-23T12:00:00Z",
    updated_at: "2026-10-23T12:01:00Z",
    attempts: [
      {
        id: verificationIds.attempt,
        generation: 1,
        status: "succeeded",
        sandbox_stopped: true,
        exit_code: 0,
        error: null,
        heartbeat_at: "2026-10-23T12:00:00Z",
        started_at: "2026-10-23T12:00:00Z",
        finished_at: "2026-10-23T12:01:00Z",
      },
    ],
  };
}
export function controlledExperiment(): ControlledExperimentPublic {
  return {
    id: verificationIds.control,
    experiment_id: verificationIds.control,
    split_version_id: verificationIds.control,
    parent_experiment_id: ids.strategy,
    root_experiment_id: ids.strategy,
    request: controlledBody,
    source: independentSource,
    request_sha256: verificationHashes.request,
    name: controlledBody.name,
    hypothesis: controlledBody.hypothesis,
    planned_change: controlledBody.planned_change,
    protocol: capability().fixed_conditions,
    protocol_sha256: verificationHashes.protocol,
    arms: [
      {
        ordinal: 0,
        name: "Copied baseline",
        planned_change: null,
        strategy_version_id: verificationIds.strategy,
        code_sha256: hashes.code,
        run_id: verificationIds.run,
        status: "succeeded",
        evaluation_id: verificationIds.evaluation,
        result_pin: independentPin,
        changed_fields: [],
        interpretation: "baseline",
        parameters: { minimum: 50 },
        error: null,
      },
      {
        ordinal: 1,
        name: "Variant 1",
        planned_change: "Increase minimum liquidity",
        strategy_version_id: verificationIds.variant,
        code_sha256: hashes.code,
        run_id: verificationIds.armRun,
        status: "failed",
        evaluation_id: null,
        result_pin: null,
        changed_fields: ["parameters.minimum"],
        interpretation: "single_parameter_change_not_causal_proof",
        parameters: { minimum: 100 },
        error: "execution_failed",
      },
    ],
    exposure_snapshot: finalExposure,
    created_at: "2026-10-23T12:00:00Z",
  };
}
export function parityReport(): DiagnosticParityView {
  const original = detail().context.binding!;
  return {
    schema_version: "tradegrad.diagnostic-parity.v1",
    report_sha256: verificationHashes.report,
    algorithm_version: "tradegrad.original-start-parity.v1",
    tolerance_version: "tradegrad.diagnostic-tolerances.v1",
    source: {
      binding: original,
      source_kind: "strategy_version",
      source_id: ids.strategy,
      spec_revision: spec.revision,
      spec_sha256: spec.spec_sha256,
      trace_manifest_sha256: hashes.trace,
      runtime_observation_sha256: "a".repeat(64),
    },
    replay: {
      binding: {
        ...original,
        run_id: verificationIds.run,
        strategy_version_id: verificationIds.strategy,
        candidate_id: null,
        evaluation_id: verificationIds.evaluation,
        execution_attempt_id: verificationIds.attempt,
        result_manifest_sha256: verificationHashes.result,
        execution_config_sha256: verificationHashes.evaluationConfig,
      },
      source_kind: "strategy_version",
      source_id: verificationIds.strategy,
      spec_revision: 1,
      spec_sha256: hashes.spec,
      trace_manifest_sha256: verificationHashes.trace,
      runtime_observation_sha256: "b".repeat(64),
    },
    outcome: "matched",
    checks: requiredDiagnosticChecks.map((key) => ({
      key,
      status: "matched",
      definition: `Compare ${key} using actual observed fields`,
      reason: null,
      source_count: 1,
      replay_count: 1,
      mismatch_count: 0,
      examples: [],
    })),
    original_event_count: 1,
    replay_event_count: 1,
    mapping_counts: { verified_original_event: 1 },
    observation_scope: {
      original: "full",
      replay: "full",
      warmup_complete: true,
    },
    field_tolerances: { price: { abs: 1e-8, rel: 1e-8 } },
    claim: "scoped_observed_reexecution_parity_not_causal_or_ledger_proof",
    limitations: [
      "Native strategy and engine share a process",
      "This is not a local counterfactual or causal mechanism proof",
    ],
  };
}
export function parityPage(): DiagnosticParityEventPage {
  return {
    report_sha256: verificationHashes.report,
    offset: 0,
    limit: 25,
    total: 1,
    items: [
      {
        pairing_key: ["data-event-1", "on_bar", "portfolio", 1, null],
        original_decision_id: "original-decision",
        replay_decision_id: "new-replay-decision",
        original_pre_state_sha256: "c".repeat(64),
        replay_pre_state_sha256: "c".repeat(64),
        pre_state_status: "matched",
        observation_status: "matched",
        mapping: "verified_original_event",
        reason: null,
      },
    ],
  };
}
