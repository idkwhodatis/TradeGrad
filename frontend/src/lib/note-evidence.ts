import * as sdk from "@/client/sdk.gen";
import type {
  ComparisonSideCreate,
  IndependentResultPin,
  ResearchNoteContent,
  ResearchNoteRevision,
  SourceRef,
  WorkbenchEvaluation,
  WorkbenchSpec,
} from "@/client/types.gen";
import {
  comparisonResultProblem,
  comparisonStatusProblem,
} from "./comparison-evidence";
import { noteRestoreSelection } from "./note-selection";
import {
  controlledStatusProblem,
  diagnosticResultPins,
  independentStatusProblem,
  verificationSource,
} from "./verification-evidence";
import { tracePins, type WorkbenchSelection } from "./workbench-selection";
import {
  candidatePageMatches,
  decisionMatches,
  selectedRecord,
} from "./replay-evidence";

export type NoteEvidence = NonNullable<ResearchNoteContent["evidence"]>[number];
export const noteTextFields = [
  ["hypothesis", "Hypothesis"],
  ["motivation", "Motivation"],
  ["rules", "Rules and mechanism"],
  ["sources", "Sources and provenance"],
  ["behavior_evidence", "Behavior evidence"],
  ["control_results", "Control results"],
  ["counterexamples", "Counterexamples"],
  ["applicability", "Applicability and limitations"],
  ["next_steps", "Next steps"],
  ["body", "Note body"],
] as const;

/** New result descriptions are captured once at this explicit user action. */
async function resultSource(
  pin: IndependentResultPin,
): Promise<ComparisonSideCreate> {
  const spec = (
    await sdk.readStrategySpec({ path: { strategy_id: pin.source_id } })
  ).data;
  if (
    spec.resource_kind !== "strategy_version" ||
    spec.resource_id !== pin.source_id ||
    spec.code_sha256 !== pin.code_sha256
  )
    throw new Error(
      "The independent result description does not match its exact executed source.",
    );
  return {
    source_kind: "strategy_version",
    source_id: pin.source_id,
    code_sha256: pin.code_sha256,
    spec_revision: spec.revision,
    spec_sha256: spec.spec_sha256,
    evaluation_id: pin.evaluation_id,
    execution_attempt_id: pin.execution_attempt_id,
    result_manifest_sha256: pin.result_manifest_sha256,
    trace_manifest_sha256: pin.trace_manifest_sha256,
  };
}

/** This performs bounded admitted reads, not a rerun, provider lookup or model call. */
export async function captureNoteContext(
  selection: WorkbenchSelection,
  spec: WorkbenchSpec,
  leaf?: WorkbenchEvaluation,
  verified = false,
): Promise<ResearchNoteContent> {
  if (
    selection.code_sha256 !== spec.code_sha256 ||
    selection.spec_revision !== spec.revision ||
    selection.spec_sha256 !== spec.spec_sha256
  )
    throw new Error(
      "The exact source and description must be loaded before capturing a note. No latest revision was substituted.",
    );
  const restore = noteRestoreSelection(selection);
  const rule = spec.spec.rules.find(
    (item) => item.rule_id === selection.rule_id,
  );
  if (selection.rule_id && !rule)
    throw new Error(
      "The selected rule is missing from this exact description.",
    );
  const evidence: NoteEvidence[] = [
    {
      kind: "source",
      label: rule
        ? `${rule.title.slice(0, 150)} (${rule.source})`
        : "Exact strategy source and description",
      source_kind: spec.resource_kind,
      source_id: spec.resource_id,
      code_sha256: spec.code_sha256,
      spec_revision: spec.revision,
      spec_sha256: spec.spec_sha256,
      rule_id: rule?.rule_id ?? null,
      source_ref: rule?.source_ref ?? null,
    },
  ];
  const source = selection.evaluation_id
    ? verificationSource(selection, spec, leaf, verified)
    : null;
  let observationAnchor: SourceRef | null = null;
  let observationLimitation = "";
  if (source && selection.decision_id && selection.record_kind) {
    const recordSelection = { ...selection, detail_offset: 0 };
    const detail = (
      await sdk.readTraceDecision({
        path: {
          evaluation_id: source.evaluation_id,
          decision_id: selection.decision_id,
        },
        query: {
          ...tracePins(selection),
          instrument_id: selection.instrument_id,
          stage_id: selection.stage_id,
          offset: 0,
          limit: 50,
          record_ordinal: ["feature", "predicate"].includes(
            selection.record_kind,
          )
            ? selection.record_ordinal
            : undefined,
        },
      })
    ).data;
    if (!decisionMatches(recordSelection, detail))
      throw new Error(
        "The recorded source anchor did not match the exact selected decision. No nearby source was substituted.",
      );
    const candidates =
      selection.record_kind === "candidate"
        ? (
            await sdk.readTraceCandidates({
              path: {
                evaluation_id: source.evaluation_id,
                decision_id: selection.decision_id,
              },
              query: {
                ...tracePins(selection),
                instrument_id: selection.instrument_id,
                stage_id: selection.stage_id,
                record_ordinal: selection.record_ordinal,
                offset: 0,
                limit: 50,
              },
            })
          ).data
        : undefined;
    if (candidates && !candidatePageMatches(selection, candidates, true))
      throw new Error(
        "The selected candidate source record differs from its saved pins.",
      );
    const recorded = selectedRecord(selection, detail, candidates);
    if (!recorded || recorded.value === null)
      throw new Error(
        recorded?.missing_reason ??
          "The exact observation was not found. No source anchor was guessed.",
      );
    observationAnchor = recorded.source_ref;
    observationLimitation = recorded.source_ref
      ? "Selected observation source anchor copied from its recorded immutable row."
      : recorded.missing_reason ||
        "No source anchor was recorded for this observation; no nearby rule was guessed.";
  }
  if (source) {
    evidence.push(
      selection.decision_id
        ? {
            kind: "decision",
            label: "Exact recorded decision selection",
            leaf: source,
            decision_id: selection.decision_id,
            instrument_id: selection.instrument_id ?? null,
            stage_id: selection.stage_id ?? null,
            session_date: selection.session_date ?? null,
            record_kind: selection.record_kind ?? null,
            record_id: selection.record_id ?? null,
            record_ordinal: selection.record_ordinal ?? null,
            source_ref: observationAnchor,
          }
        : {
            kind: "evaluation",
            label: "Exact physical leaf Evaluation",
            leaf: source,
          },
    );
  }
  if (selection.comparison_id) {
    if (!selection.comparison_manifest_sha256)
      throw new Error(
        "Wait for a pinned comparison result before creating a result note.",
      );
    const status = (
      await sdk.readComparison({
        path: { comparison_id: selection.comparison_id },
      })
    ).data;
    const problem = comparisonStatusProblem(selection, status);
    if (problem) throw new Error(problem);
    const result = (
      await sdk.readComparisonResult({
        path: { comparison_id: status.id },
        query: { manifest_sha256: selection.comparison_manifest_sha256 },
      })
    ).data;
    const resultProblem = comparisonResultProblem(selection, status, result);
    if (resultProblem) throw new Error(resultProblem);
    evidence.push({
      kind: "comparison",
      label: "Immutable two-sided comparison",
      comparison_id: status.id,
      input_sha256: status.input_sha256,
      manifest_sha256: result.manifest_sha256,
      left: status.request.left,
      right: status.request.right,
      event_pair_id: selection.event_pair_id ?? null,
      section: selection.comparison_section ?? "events",
      instrument_id: selection.comparison_instrument_id ?? null,
      mechanism_index: selection.mechanism_index ?? null,
    });
  }
  if (selection.diagnostic_run_id) {
    if (
      !source ||
      !selection.diagnostic_config_sha256 ||
      !selection.diagnostic_report_sha256
    )
      throw new Error(
        "Diagnostic notes require the original leaf, exact configuration and published report hash.",
      );
    const status = (
      await sdk.readIndependentEvaluation({
        path: { run_id: selection.diagnostic_run_id },
      })
    ).data;
    const problem =
      independentStatusProblem(source, status, {
        id: selection.diagnostic_run_id,
        purpose: "diagnostic",
        config_sha256: selection.diagnostic_config_sha256,
      }) || diagnosticResultPins(selection, status.result_pin);
    if (problem) throw new Error(problem);
    if (!status.result_pin)
      throw new Error(
        "The diagnostic has no admitted physical result. No successful result was invented.",
      );
    evidence.push({
      kind: "diagnostic",
      label: "Diagnostic report, not automatic original-execution fact",
      run_id: status.id,
      config_sha256: status.config_sha256,
      report_sha256: selection.diagnostic_report_sha256,
      result: await resultSource(status.result_pin),
    });
  }
  if (selection.controlled_experiment_id) {
    if (
      !source ||
      !selection.controlled_request_sha256 ||
      !selection.controlled_protocol_sha256
    )
      throw new Error(
        "Controlled notes require the original leaf and exact request/protocol hashes.",
      );
    const control = (
      await sdk.readControlledExperiment({
        path: { controlled_id: selection.controlled_experiment_id },
      })
    ).data;
    const problem = controlledStatusProblem(source, selection, control);
    if (problem) throw new Error(problem);
    const arms = await Promise.all(
      control.arms.map(async (arm) => {
        const status = (
          await sdk.readIndependentEvaluation({ path: { run_id: arm.run_id } })
        ).data;
        const problem = independentStatusProblem(source, status, {
          id: arm.run_id,
          purpose: "controlled",
          result_pin: arm.result_pin,
        });
        if (problem) throw new Error(problem);
        if (
          !status.active_attempt_id ||
          status.status !== arm.status ||
          !["succeeded", "failed", "cancelled", "interrupted"].includes(
            status.status,
          )
        )
          throw new Error(
            "Every control arm must have an exact stopped physical attempt before capturing results; no running or failed arm becomes zero profit.",
          );
        return {
          ordinal: arm.ordinal,
          run_id: arm.run_id,
          status: status.status as
            "succeeded" | "failed" | "cancelled" | "interrupted",
          execution_attempt_id: status.active_attempt_id,
          generation: status.generation,
          result: status.result_pin
            ? await resultSource(status.result_pin)
            : null,
        };
      }),
    );
    evidence.push({
      kind: "controlled",
      label: "Complete controlled experiment, including failed arms",
      controlled_experiment_id: control.id,
      request_sha256: control.request_sha256,
      protocol_sha256: control.protocol_sha256,
      selected_arm: selection.controlled_arm ?? null,
      arms,
    });
  }
  return {
    title: rule ? `Research: ${rule.title}` : "Research note",
    status: "pending",
    tags: [],
    hypothesis: "",
    motivation: "",
    rules: "",
    sources: `Description revision ${spec.revision}: ${spec.origin}. ${rule ? `Rule provenance: ${rule.source}. ` : ""}Description coverage: ${spec.spec.coverage}. ${spec.spec.missing_reasons.join(" ")} Descriptions are not execution records; a source label is not a confidence score. ${observationLimitation}`,
    behavior_evidence: "",
    control_results: "",
    counterexamples: "",
    applicability: "",
    next_steps: "",
    body: "",
    evidence,
    restore: {
      strategy_version_id: spec.strategy_version_id,
      selection: Object.fromEntries(
        Object.entries(restore).filter(([, value]) => value !== undefined),
      ),
    },
  };
}

export function noteRevisionMatches(
  selection: WorkbenchSelection,
  note: ResearchNoteRevision,
) {
  return (
    note.note_id === selection.note_id &&
    note.revision === selection.note_revision &&
    note.revision_sha256 === selection.note_revision_sha256
  );
}
