import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type { ResearchNoteEdit } from "@/client/types.gen";
import type { WorkbenchSelection } from "./workbench-selection";
import { noteRequest, type NoteDraft } from "./note-draft";
export const noteListOptions = (owner: string, q = "", offset = 0) =>
  queryOptions({
    queryKey: ["research-notes", owner, q, offset],
    staleTime: 0,
    queryFn: async ({ signal }) =>
      (
        await sdk.listResearchNotes({
          query: { q: q || undefined, offset, limit: 25 },
          signal,
        })
      ).data,
  });
export const noteRevisionOptions = (
  owner: string,
  selection: WorkbenchSelection,
) =>
  queryOptions({
    queryKey: [
      "research-note",
      owner,
      selection.note_id,
      selection.note_revision,
      selection.note_revision_sha256,
    ],
    queryFn: async ({ signal }) =>
      (
        await sdk.readResearchNoteRevision({
          path: {
            note_id: selection.note_id!,
            revision: selection.note_revision!,
          },
          query: { revision_sha256: selection.note_revision_sha256! },
          signal,
        })
      ).data,
    enabled: Boolean(
      selection.note_id &&
      selection.note_revision &&
      selection.note_revision_sha256,
    ),
    // Content is immutable; availability/admission is not. Recheck on revisit.
    staleTime: 0,
  });
export const noteHistoryOptions = (owner: string, id?: string, offset = 0) =>
  queryOptions({
    queryKey: ["research-note-history", owner, id, offset],
    staleTime: 0,
    queryFn: async ({ signal }) =>
      (
        await sdk.listResearchNoteRevisions({
          path: { note_id: id! },
          query: { offset, limit: 25 },
          signal,
        })
      ).data,
    enabled: Boolean(id),
  });
export async function saveNoteDraft(draft: NoteDraft & { key: string }) {
  const body = noteRequest(draft);
  return (
    draft.note_id
      ? await sdk.createResearchNoteRevision({
          path: { note_id: draft.note_id },
          body: body as ResearchNoteEdit,
          headers: { "Idempotency-Key": draft.key },
        })
      : await sdk.createResearchNote({
          body,
          headers: { "Idempotency-Key": draft.key },
        })
  ).data;
}
export async function exportNoteRevision(selection: WorkbenchSelection) {
  const text = (
    await sdk.exportResearchNoteRevision({
      path: { note_id: selection.note_id!, revision: selection.note_revision! },
      query: { revision_sha256: selection.note_revision_sha256! },
      parseAs: "text",
    })
  ).data;
  return new Blob([text], { type: "text/markdown;charset=utf-8" });
}
export async function readLatestNote(id: string) {
  return (await sdk.readResearchNote({ path: { note_id: id } })).data;
}
