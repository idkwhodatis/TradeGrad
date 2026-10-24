import type { Response } from "@playwright/test";

// Private response data never enters assertion messages. The Python exporter
// independently validates this versioned, fixed-vocabulary diagnostic envelope.
export const noteHttpCategories = [
  "controlled_note_create",
  "research_note_create",
  "research_note_revision",
] as const;
export const noteHttpCodes = [
  "admission_required",
  "artifact_missing",
  "comparison_unavailable",
  "database_unavailable",
  "diagnostic_parity_pending",
  "final_binding_mismatch",
  "forbidden",
  "idempotency_conflict",
  "independent_integrity",
  "independent_source_depth",
  "internal_error",
  "invalid_artifact",
  "invalid_final_config",
  "invalid_note_pin",
  "invalid_portable_config",
  "invalid_split",
  "not_found",
  "note_admission_capacity",
  "note_admission_unavailable",
  "note_corpus_capacity",
  "note_evidence_capacity",
  "note_evidence_pin_mismatch",
  "note_evidence_unavailable",
  "note_integrity",
  "note_restore_mismatch",
  "note_revision_capacity",
  "note_revision_conflict",
  "note_revision_pin_mismatch",
  "result_unavailable",
  "source_integrity",
  "spec_integrity",
  "spec_unavailable",
  "split_integrity",
  "trace_artifact_missing",
  "trace_integrity",
  "trace_unavailable",
  "unauthorized",
  "unrecognized",
  "unsafe_storage_path",
  "validation_error",
] as const;
export const noteHttpFields = [
  "Idempotency-Key",
  "applicability",
  "arms",
  "base_revision",
  "base_revision_sha256",
  "behavior_evidence",
  "body",
  "change_note",
  "code_sha256",
  "comparison_id",
  "config_sha256",
  "content",
  "control_results",
  "controlled_experiment_id",
  "counterexamples",
  "decision_id",
  "evaluation_id",
  "event_pair_id",
  "evidence",
  "execution_attempt_id",
  "file",
  "generation",
  "hypothesis",
  "input_sha256",
  "instrument_id",
  "kind",
  "label",
  "leaf",
  "left",
  "line_end",
  "line_start",
  "manifest_sha256",
  "mechanism_index",
  "motivation",
  "next_steps",
  "note_id",
  "ordinal",
  "protocol_sha256",
  "record_id",
  "record_kind",
  "record_ordinal",
  "report_sha256",
  "request_sha256",
  "restore",
  "result",
  "result_manifest_sha256",
  "right",
  "rule_id",
  "rules",
  "run_id",
  "section",
  "selected_arm",
  "selection",
  "session_date",
  "source_id",
  "source_kind",
  "source_ref",
  "sources",
  "spec_revision",
  "spec_sha256",
  "stage_id",
  "status",
  "strategy_version_id",
  "symbol",
  "tags",
  "title",
  "trace_manifest_sha256",
] as const;
export type NoteHttpCategory = (typeof noteHttpCategories)[number];
const knownCode = new Set<string>(noteHttpCodes);
const knownField = new Set<string>(noteHttpFields);
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

export async function noteHttpDiagnostic(
  response: Pick<Response, "status" | "text">,
  category: NoteHttpCategory,
): Promise<string> {
  const status = response.status();
  let code: string | null = null;
  const fields = new Set<string>();
  if (status !== 201) {
    code = "unrecognized";
    try {
      const text = await response.text();
      if (text.length <= 65_536) {
        const value: unknown = JSON.parse(text);
        if (record(value)) {
          if (typeof value.code === "string" && knownCode.has(value.code))
            code = value.code;
          if (code === "validation_error" && Array.isArray(value.details)) {
            for (const detail of value.details.slice(0, 64)) {
              if (!record(detail) || !Array.isArray(detail.loc)) continue;
              for (const field of detail.loc.slice(0, 16))
                if (typeof field === "string" && knownField.has(field))
                  fields.add(field);
            }
          }
        }
      }
    } catch {
      // Non-JSON bodies and transport exceptions are private, not diagnostics.
    }
  }
  return (
    "RESEARCH_NOTE_HTTP_DIAGNOSTIC_V1:" +
    JSON.stringify({
      category,
      http_status: status,
      error_code: code,
      validation_fields: [...fields].sort().slice(0, 16),
    })
  );
}
