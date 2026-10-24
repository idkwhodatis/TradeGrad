import type {
  ComparisonManifest,
  ComparisonPublic,
  ComparisonRecord,
  ComparisonResult,
  ComparisonSectionPage,
  ComparisonSectionSummary,
  ComparisonSidePins,
} from "../../src/client/types.gen";
import {
  comparisonRequest,
  sideFromEvidence,
} from "../../src/lib/comparison-evidence";
import {
  comparisonSections,
  comparisonSidePatch,
  type WorkbenchSelection,
} from "../../src/lib/workbench-selection";
import { detail, hashes, ids, leaf, rule, spec } from "./workbench-fixtures";
export const comparisonId = "20000000-0000-4000-8000-000000000001";
export const comparisonRun = "20000000-0000-4000-8000-000000000002";
export const comparisonAttempt = "20000000-0000-4000-8000-000000000003";
export const comparisonArtifact = "20000000-0000-4000-8000-000000000004";
export const rightId = "20000000-0000-4000-8000-000000000005";
export const comparisonHash = "1".repeat(64);
export const inputHash = "2".repeat(64);
export const engineHash = "3".repeat(64);
export const rightSpec = {
  ...spec,
  resource_id: rightId,
  strategy_version_id: rightId,
  code_sha256: "4".repeat(64),
  spec_sha256: "5".repeat(64),
  spec: {
    ...spec.spec,
    code_sha256: "4".repeat(64),
    rules: [
      {
        ...rule,
        source_ref: { ...rule.source_ref, code_sha256: "4".repeat(64) },
      },
    ],
  },
};
export const rightLeaf = {
  ...leaf,
  evaluation_id: "20000000-0000-4000-8000-000000000006",
  strategy_version_id: rightId,
  execution_attempt_id: "20000000-0000-4000-8000-000000000007",
  code_sha256: rightSpec.code_sha256,
  result_manifest_sha256: "6".repeat(64),
  trace_manifest_sha256: "7".repeat(64),
  source_association: {
    ...leaf.source_association,
    strategy_version_id: rightId,
  },
};
export const comparisonSelection: WorkbenchSelection = {
  tab: "comparison",
  comparison_id: comparisonId,
  comparison_manifest_sha256: comparisonHash,
  ...comparisonSidePatch("left", sideFromEvidence(spec, leaf)),
  ...comparisonSidePatch("right", sideFromEvidence(rightSpec, rightLeaf)),
};
export const comparisonBody = comparisonRequest(comparisonSelection);
export function status(): ComparisonPublic {
  return {
    id: comparisonId,
    run_id: comparisonRun,
    status: "succeeded",
    generation: 1,
    input_sha256: inputHash,
    engine_input_sha256: engineHash,
    request: structuredClone(comparisonBody),
    active_attempt_id: comparisonAttempt,
    next_event_seq: 4,
    artifact_ready: true,
    manifest_artifact_id: comparisonArtifact,
    manifest_sha256: comparisonHash,
    error_code: null,
    created_at: "2026-10-23T15:00:00Z",
    updated_at: "2026-10-23T15:01:00Z",
  };
}
export function sectionSummary(): ComparisonSectionSummary {
  return {
    left_availability: "available",
    right_availability: "available",
    left_reason: null,
    right_reason: null,
    observed_equality: "equal",
    rows: 1,
    equal: 1,
    changed: 0,
    left_only: 0,
    right_only: 0,
    ambiguous: 0,
    unavailable: 0,
    definition: "Only retained observations, not diagnostic parity.",
    left_scope: {
      start: "2024-01-02",
      end: "2024-01-03",
      stage_ids: ["liquidity"],
      instrument_ids: ["B"],
    },
    right_scope: {
      start: "2024-01-02",
      end: "2024-01-03",
      stage_ids: ["liquidity"],
      instrument_ids: ["B"],
    },
    equality_scope: "named_observations_only",
  };
}
export function manifest(): ComparisonManifest {
  const left: ComparisonSidePins = {
    binding: detail().context.binding!,
    source_kind: "strategy_version",
    source_id: ids.strategy,
    window: { start: "2024-01-01", end: "2024-01-03" },
    spec_revision: 1,
    spec_sha256: hashes.spec,
    trace_manifest_sha256: hashes.trace,
    trace_state: "registered",
    trace_coverage: "complete",
    capture: {
      mode: "research",
      observation_window: { start: "2024-01-02", end: "2024-01-03" },
      stage_ids: ["liquidity"],
      instrument_ids: ["B"],
    },
    trace_missing_reasons: [],
    common_config_sha256: "8".repeat(64),
    economic_bundle_sha256: "9".repeat(64),
    namespace: null,
  };
  return {
    publication: {
      comparison_id: comparisonId,
      run_id: comparisonRun,
      execution_attempt_id: comparisonAttempt,
    },
    input_hash: engineHash,
    left,
    right: {
      ...left,
      binding: {
        ...left.binding,
        evaluation_id: rightLeaf.evaluation_id,
        execution_attempt_id: rightLeaf.execution_attempt_id,
        code_sha256: rightSpec.code_sha256,
        strategy_version_id: rightId,
        result_manifest_sha256: rightLeaf.result_manifest_sha256,
      },
      source_id: rightId,
      spec_sha256: rightSpec.spec_sha256,
      trace_manifest_sha256: rightLeaf.trace_manifest_sha256,
    },
    field_tolerances: { cash: { absolute: 0.0000001, relative: 0.0000000001 } },
    limits: {},
    namespace_status: "common",
    namespace_reason: "Same frozen source event namespace",
    pairing_fields: ["data_event_id", "callback_phase", "scope", "event_seq"],
    code_changed: true,
    configuration_differences: [
      {
        field: "data",
        left_present: true,
        right_present: true,
        left: "frozen",
        right: "frozen",
        status: "equal",
      },
      {
        field: "fees",
        left_present: true,
        right_present: true,
        left: 0,
        right: 0.001,
        status: "different",
      },
      {
        field: "RNG",
        left_present: true,
        right_present: false,
        left: false,
        right: null,
        status: "unknown",
      },
    ],
    mechanism_changes: [
      {
        category: "filter",
        rule_id: "filter",
        status: "changed",
        mapping: "controlled",
        left_source_ref: rule.source_ref,
        right_source_ref: rightSpec.spec.rules[0].source_ref,
        left: { ...rule },
        right: { ...rightSpec.spec.rules[0] },
        reason: "Maintained template declaration",
      },
    ],
    sections: Object.fromEntries(
      comparisonSections.map((key) => [key, sectionSummary()]),
    ),
    observed_behavior: "unchanged_observed",
    observed_equal_sections: ["candidates", "targets"],
    diagnostic_parity_certified: false,
    local_counterfactual: false,
    causal_claim: false,
    artifacts: [],
    warnings: ["Engine-only events may be unpaired."],
  };
}
export function result(): ComparisonResult {
  return {
    comparison_id: comparisonId,
    input_sha256: inputHash,
    manifest_artifact_id: comparisonArtifact,
    manifest_sha256: comparisonHash,
    manifest: manifest(),
    left_source: {
      source_kind: "strategy_version",
      source_id: ids.strategy,
      source_strategy_version_id: ids.strategy,
      code_sha256: hashes.code,
      code: "# original\npass\n",
      spec: spec.spec,
      spec_sha256: hashes.spec,
      spec_missing_reason: null,
    },
    right_source: {
      source_kind: "strategy_version",
      source_id: rightId,
      source_strategy_version_id: rightId,
      code_sha256: rightSpec.code_sha256,
      code: "# changed comment\npass\n",
      spec: rightSpec.spec,
      spec_sha256: rightSpec.spec_sha256,
      spec_missing_reason: null,
    },
  };
}
export function eventRow(): ComparisonRecord {
  return {
    row_id: "row-1",
    section: "events",
    event_pair_id: "pair-1",
    session_date: "2024-01-02",
    instrument_id: null,
    stage_id: null,
    status: "equal",
    left_state: "observed",
    right_state: "observed",
    left: {
      ...detail().decision!,
      pre_state: { cash: 0, positions: [], pending_orders: [] },
    },
    right: {
      ...detail().decision!,
      decision_id: "right-decision-7",
      pre_state: null,
      pre_state_missing_reason: "State not collected at this callback",
    },
    match_basis: "source_event",
    ambiguous: false,
    missing_reason: null,
  };
}
export function page(
  section = "events",
  items = [eventRow()],
): ComparisonSectionPage {
  return {
    comparison_id: comparisonId,
    input_sha256: inputHash,
    manifest_sha256: comparisonHash,
    availability: sectionSummary(),
    section,
    items,
    total: items.length,
    offset: 0,
    limit: 25,
  };
}
