import type {
  ResearchNoteContent,
  ResearchNoteRevision,
} from "../../src/client/types.gen";
import { hashes, ids, rule } from "./workbench-fixtures";
export const noteSelection = {
  tab: "notes" as const,
  note_id: ids.run,
  note_revision: 1,
  note_revision_sha256: "e".repeat(64),
  code_sha256: hashes.code,
  spec_revision: 1,
  spec_sha256: hashes.spec,
};
export const sourceEvidence = {
  kind: "source" as const,
  label: "Exact rule",
  source_kind: "strategy_version" as const,
  source_id: ids.strategy,
  code_sha256: hashes.code,
  spec_revision: 1,
  spec_sha256: hashes.spec,
  rule_id: rule.rule_id,
  source_ref: rule.source_ref,
};
export const noteContent: ResearchNoteContent = {
  title: "Liquidity mechanism",
  status: "pending",
  tags: ["liquidity"],
  hypothesis: "A liquidity filter changes the selected set.",
  motivation: "Inspect a higher-score excluded candidate.",
  rules: "Keep eligibility separate from rank.",
  sources: "strategy_emitted, static_inference",
  behavior_evidence: "No runtime result claimed by this pure fixture.",
  control_results: "Not run in this fixture.",
  counterexamples: "A valid liquid low-rank listing can still be excluded.",
  applicability: "Frozen synthetic unit-test evidence only.",
  next_steps: "Run an isolated comparison.",
  body: "Research, not investment advice.",
  evidence: [sourceEvidence],
  restore: {
    strategy_version_id: ids.strategy,
    selection: {
      tab: "description",
      code_sha256: hashes.code,
      spec_revision: 1,
      spec_sha256: hashes.spec,
      rule_id: rule.rule_id,
    },
  },
};
export const sourceHref = `/strategies/${ids.strategy}/research?tab=description&code_sha256=${hashes.code}&spec_revision=1&spec_sha256=${hashes.spec}&rule_id=${rule.rule_id}`;
export function noteRevision(): ResearchNoteRevision {
  return {
    id: ids.attempt,
    note_id: ids.run,
    revision: 1,
    revision_sha256: "e".repeat(64),
    created_at: "2026-10-23T00:00:00Z",
    change_note: null,
    content: structuredClone(noteContent),
    evidence: [
      {
        index: 0,
        state: "available",
        reason: null,
        bindings: [],
        href: sourceHref,
      },
    ],
    restore_href: sourceHref,
    retained_pin_count: 1,
    retention_reason: "immutable_note_revision",
    status_basis: "human_maintained_not_automatic_validation",
  };
}
