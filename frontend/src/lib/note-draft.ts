import { z } from "zod";
import type {
  ResearchNoteContent,
  ResearchNoteCreate,
  ResearchNoteEdit,
} from "@/client/types.gen";
import { stableJson } from "./data-utils";
import { noteStatusLabels } from "./note-selection";
import { noteTextFields } from "./note-evidence";

export type NoteDraft = {
  version: 1;
  note_id: string | null;
  base_revision: number | null;
  base_revision_sha256: string | null;
  content: ResearchNoteContent;
  change_note: string;
  key: string | null;
  fingerprint: string | null;
};
const draftSchema = z.strictObject({
  version: z.literal(1),
  note_id: z.uuid().nullable(),
  base_revision: z.number().int().min(1).nullable(),
  base_revision_sha256: z
    .string()
    .regex(/^[a-f0-9]{64}$/)
    .nullable(),
  content: z.record(z.string(), z.unknown()),
  change_note: z.string().max(2000),
  key: z.uuid().nullable(),
  fingerprint: z.string().nullable(),
});
const storageKey = (owner: string, strategyId: string) =>
  `tradegrad.note.draft.v1.${owner}.${strategyId}`;
export function noteRequest(
  draft: NoteDraft,
): ResearchNoteCreate | ResearchNoteEdit {
  const common = {
    content: draft.content,
    change_note: draft.change_note || null,
  };
  return draft.note_id
    ? {
        ...common,
        base_revision: draft.base_revision!,
        base_revision_sha256: draft.base_revision_sha256!,
      }
    : common;
}
export function validateNoteContent(
  content: ResearchNoteContent,
  submission = true,
) {
  if (
    typeof content.title !== "string" ||
    (submission && !content.title.trim()) ||
    content.title.length > 240
  )
    throw new Error("Use a note title of 1–240 characters.");
  if (content.status !== undefined && !(content.status in noteStatusLabels))
    throw new Error("Choose a user-maintained note status.");
  const tags = content.tags ?? [];
  if (
    !Array.isArray(tags) ||
    tags.length > 20 ||
    tags.some(
      (tag) => typeof tag !== "string" || !tag.trim() || tag.length > 80,
    ) ||
    (submission &&
      new Set(tags.map((tag) => tag.toLowerCase())).size !== tags.length)
  )
    throw new Error("Use up to 20 distinct tags, each 1–80 characters.");
  for (const [field] of noteTextFields) {
    const text = content[field] ?? "";
    if (
      typeof text !== "string" ||
      text.length > (field === "body" ? 32000 : 8000)
    )
      throw new Error(`The ${field} text exceeds its supported size.`);
  }
  if (
    !Array.isArray(content.evidence) ||
    content.evidence.length > 32 ||
    content.evidence.some(
      (ref) =>
        !ref ||
        ![
          "source",
          "evaluation",
          "decision",
          "comparison",
          "diagnostic",
          "controlled",
        ].includes(ref.kind),
    )
  )
    throw new Error(
      "The typed evidence list is unreadable. It was not replaced.",
    );
  if (new TextEncoder().encode(JSON.stringify(content)).byteLength > 128 * 1024)
    throw new Error("Note content exceeds the 128 KiB byte budget.");
}
function checkedDraft(value: unknown): NoteDraft {
  const parsed = draftSchema.parse(value);
  const draft = parsed as NoteDraft;
  validateNoteContent(draft.content, Boolean(draft.key));
  if (
    Boolean(draft.note_id) !==
      Boolean(draft.base_revision && draft.base_revision_sha256) ||
    (!draft.note_id &&
      (draft.base_revision !== null || draft.base_revision_sha256 !== null)) ||
    Boolean(draft.key) !== Boolean(draft.fingerprint) ||
    (draft.key && draft.fingerprint !== stableJson(noteRequest(draft)))
  )
    throw new Error("Draft identity or exact retry body changed.");
  return draft;
}
export function loadNoteDraft(
  owner: string,
  strategyId: string,
): NoteDraft | null {
  const raw = sessionStorage.getItem(storageKey(owner, strategyId));
  if (raw === null) return null;
  try {
    return checkedDraft(JSON.parse(raw));
  } catch {
    throw new Error(
      "The saved note draft is unreadable. It was not replaced or submitted. Restore browser storage or explicitly discard the local draft.",
    );
  }
}
export function storeNoteDraft(
  owner: string,
  strategyId: string,
  draft: NoteDraft,
) {
  checkedDraft(draft);
  const serialized = JSON.stringify(draft);
  sessionStorage.setItem(storageKey(owner, strategyId), serialized);
  if (sessionStorage.getItem(storageKey(owner, strategyId)) !== serialized)
    throw new Error(
      "The exact note retry could not be retained. No request was sent.",
    );
}
export function clearNoteDraft(
  owner: string,
  strategyId: string,
  key?: string,
) {
  if (key === undefined || loadNoteDraft(owner, strategyId)?.key === key)
    sessionStorage.removeItem(storageKey(owner, strategyId));
}
export function prepareNoteSubmission(
  draft: NoteDraft,
): NoteDraft & { key: string } {
  checkedDraft(draft);
  validateNoteContent(draft.content);
  if (draft.key) return { ...draft, key: draft.key };
  return {
    ...draft,
    key: crypto.randomUUID(),
    fingerprint: stableJson(noteRequest(draft)),
  };
}
export function noteFailureIsDefinitive(error: unknown) {
  const code = (error as { code?: string } | null)?.code;
  return [
    "note_restore_mismatch",
    "note_evidence_pin_mismatch",
    "note_evidence_unavailable",
    "note_evidence_capacity",
    "note_revision_capacity",
    "invalid_note_pin",
    "source_integrity",
    "spec_integrity",
    "trace_integrity",
    "independent_source_depth",
    "note_revision_pin_mismatch",
    "note_revision_conflict",
    "research_note_revision_conflict",
    "note_evidence_invalid",
    "invalid_note_evidence",
    "note_integrity",
    "invalid_note",
    "invalid_note_restore",
    "idempotency_conflict",
    "not_found",
    "validation_error",
  ].includes(code ?? "");
}
