import type {
  CheckpointDetail,
  ExposureSnapshot,
  FinalSelectionPublic,
  PortableFinalEvaluationDetail,
  PortableFinalEvaluationPreflight,
  PortableFinalResultQuery,
} from "../../src/client/types.gen";
import {
  portableFixture,
  portableResultFixture,
  portableRunFixture,
} from "./portable-fixtures";
export const finalIds = {
  selection: "a1111111-1111-4111-8111-111111111111",
  split: "b2222222-2222-4222-8222-222222222222",
  checkpoint: "c3333333-3333-4333-8333-333333333333",
  candidate: "d4444444-4444-4444-8444-444444444444",
  strategy: "f7777777-7777-4777-8777-777777777777",
  job: "a8888888-8888-4888-8888-888888888888",
  attempt: "b9999999-9999-4999-8999-999999999999",
};
export const finalExposure: ExposureSnapshot = {
  schema_version: "tradegrad.exposure.v1",
  classification: "no_tracked_overlap",
  matching_exposure_ids: [],
  matching_purposes: [],
  matching_count: 0,
  identities: [],
  as_of: "2026-10-22T12:00:00Z",
  tracking_coverage: "historical tracking incomplete/external use unknown",
  independent_unseen_holdout_established: false,
  evidence_truncated: false,
};
export function portableSelectionFixture(): FinalSelectionPublic {
  return {
    id: finalIds.selection,
    experiment_id: finalIds.split,
    split_version_id: finalIds.split,
    source_run_id: portableRunFixture().id,
    source_candidate_id: finalIds.candidate,
    source_checkpoint_id: finalIds.checkpoint,
    strategy_version_id: finalIds.strategy,
    request_sha256: "1".repeat(64),
    source_config_sha256: "2".repeat(64),
    source_code_sha256: "3".repeat(64),
    source_checkpoint_sha256: "4".repeat(64),
    selection_evidence: {
      source_config: portableFixture().body,
      candidate_key: "not-auto-best",
      chosen_candidate_id: finalIds.candidate,
      valid: true,
      complete: true,
    },
    exposure_snapshot: finalExposure,
    created_at: "2026-10-22T12:00:00Z",
  };
}
export function portableFinalFixture(): PortableFinalEvaluationDetail {
  const source = portableRunFixture(),
    selection = portableSelectionFixture();
  const config = {
    schema_version: "tradegrad.portable.final-evaluation.v1" as const,
    evaluation_mode: "portable" as const,
    execution_mode: "final_evaluation" as const,
    selection_id: selection.id,
    split_version_id: selection.split_version_id,
    split_sha256: "5".repeat(64),
    selected_strategy: {
      id: selection.strategy_version_id,
      sha256: selection.source_code_sha256,
    },
    selected_manifest: {
      id: `${selection.strategy_version_id}/manifest`,
      sha256: "6".repeat(64),
    },
    selected_parameters: {
      id: `${selection.strategy_version_id}/parameters`,
      sha256: source.config.seed_parameters.sha256,
    },
    source_config: source.config,
    source_config_sha256: selection.source_config_sha256,
    holdout: source.config.windows.holdout!,
    environment_digest: source.config.environment_digest,
    max_runtime_seconds: 300,
  };
  return {
    ...source,
    research_image_digest: source.research_image_digest!,
    portable_policy_image_digest: source.portable_policy_image_digest!,
    id: finalIds.job,
    execution_mode: "final_evaluation",
    config,
    experiment_id: selection.experiment_id,
    split_version_id: selection.split_version_id,
    final_selection_id: selection.id,
    strategy_version_id: selection.strategy_version_id,
    status: "succeeded",
    active_attempt_id: finalIds.attempt,
    attempts: [
      {
        id: finalIds.attempt,
        generation: source.generation,
        status: "succeeded",
        sandbox_stopped: true,
        exit_code: 0,
        error: null,
        started_at: "2026-10-22T12:00:00Z",
        finished_at: "2026-10-22T12:00:10Z",
        heartbeat_at: "2026-10-22T12:00:10Z",
      },
    ],
    result: {
      execution_mode: "final_evaluation",
      selection_id: selection.id,
      evaluation_id: portableResultFixture().evaluation_id,
      metrics: { total_return_pct: 1, max_drawdown_pct: 0.1 },
      combined_score: 0.9,
      is_synthetic: true,
      evaluation_mode: "portable",
      result_trust: "isolated_policy_trusted_akquant_engine",
      ledger_origin: "trusted_engine",
      runtime_certified: false,
      external_framework_equivalence: "not_certified",
      objective_definition: "portable_return_drawdown.v1",
      policy_image_digest: source.portable_policy_image_digest ?? null,
      holdout_executed: true,
      image_digest: source.research_image_digest ?? null,
      exposure_snapshot: finalExposure,
    },
  };
}
export function portableFinalPreflightFixture(): PortableFinalEvaluationPreflight {
  const job = portableFinalFixture();
  return {
    eligible: true,
    selection_id: job.final_selection_id,
    config: job.config,
    exposure_snapshot: finalExposure,
    warnings: ["Historical tracking incomplete/external use unknown"],
    quota_remaining: 2,
  };
}
export function portableFinalResultFixture(): PortableFinalResultQuery {
  const job = portableFinalFixture();
  return {
    ...portableResultFixture(),
    run_id: job.id,
    schema_version: "tradegrad.portable.final-result-query.v1",
    result_schema_version: "tradegrad.portable-result.v1",
    config: job.config,
    selection_id: job.final_selection_id,
    selection_feedback: false,
    exposure_snapshot: finalExposure,
    environment_digest: job.config.environment_digest,
    window: { ...job.config.holdout, interval: "half_open" },
    identity: {
      strategy_sha256: job.config.selected_strategy.sha256,
      parameters_sha256: job.config.selected_parameters.sha256,
      run_config_sha256: "8".repeat(64),
    },
  };
}
export function portableFrozenRunFixture() {
  const run = portableRunFixture();
  return {
    ...run,
    status: "succeeded" as const,
    experiment_id: finalIds.split,
    split_version_id: finalIds.split,
    latest_checkpoint_id: finalIds.checkpoint,
  };
}
export function portableCheckpointFixture(): CheckpointDetail {
  const run = portableFrozenRunFixture();
  return {
    id: finalIds.checkpoint,
    run_id: run.id,
    attempt_id: finalIds.job,
    complete: true,
    config_sha256: "2".repeat(64),
    manifest_sha256: "4".repeat(64),
    input_digest: "5".repeat(64),
    generation: run.generation,
    schema_version: 1,
    sequence: 1,
    iteration: 1,
    next_stage: "completed",
    created_at: "2026-10-22T12:00:00Z",
    summary: {
      candidates: {
        "not-auto-best": {
          id: finalIds.candidate,
          valid: true,
          complete: true,
          combined_score: 1,
        },
      },
    },
  };
}

export function rejectedPortableFinalReceipts(): [
  string,
  PortableFinalEvaluationDetail,
][] {
  const job = portableFinalFixture(),
    attempt = job.attempts[0]!;
  return [
    ["missing active attempt", { ...job, active_attempt_id: null }],
    ["empty attempts", { ...job, attempts: [] }],
    ["different active attempt", { ...job, active_attempt_id: "replacement" }],
    ["new generation", { ...job, generation: job.generation + 1 }],
    ["foreign selection", { ...job, final_selection_id: "foreign" }],
    ["cancelled lifecycle", { ...job, status: "cancelled" }],
    ["failed lifecycle", { ...job, status: "failed" }],
    ["missing result", { ...job, result: null }],
    [
      "unexecuted holdout",
      { ...job, result: { ...job.result!, holdout_executed: false } },
    ],
    ...[
      { status: "failed" },
      { sandbox_stopped: false },
      { finished_at: null },
      { exit_code: 1 },
      { exit_code: null },
      { generation: job.generation + 1 },
    ].map((patch): [string, PortableFinalEvaluationDetail] => [
      `invalid current attempt ${JSON.stringify(patch)}`,
      { ...job, attempts: [{ ...attempt, ...patch }] },
    ]),
    [
      "unstopped older attempt",
      {
        ...job,
        attempts: [
          attempt,
          {
            ...attempt,
            id: "older",
            generation: job.generation - 1,
            sandbox_stopped: false,
          },
        ],
      },
    ],
  ];
}
