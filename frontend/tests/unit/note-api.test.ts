import { afterEach, expect, it, vi } from "vitest";
import * as sdk from "../../src/client/sdk.gen";
import {
  exportNoteRevision,
  noteHistoryOptions,
  noteListOptions,
  noteRevisionOptions,
  saveNoteDraft,
} from "../../src/lib/note-api";
import { prepareNoteSubmission } from "../../src/lib/note-draft";
import { noteContent, noteRevision, noteSelection } from "./note-fixtures";
import { ids } from "./workbench-fixtures";
afterEach(() => vi.restoreAllMocks());
const context = { signal: new AbortController().signal } as never;
it("uses generated owner-separated bounded note queries and propagates cancellation", async () => {
  const spy = vi
    .spyOn(sdk, "listResearchNotes")
    .mockResolvedValue({ data: { data: [], count: 0 } } as never);
  const options = noteListOptions("owner", "liquidity", 25);
  await options.queryFn!(context);
  expect(options.queryKey).toEqual([
    "research-notes",
    "owner",
    "liquidity",
    25,
  ]);
  expect(spy).toHaveBeenCalledWith({
    query: { q: "liquidity", offset: 25, limit: 25 },
    signal: expect.any(AbortSignal),
  });
});
it("never calls latest for a selected revision, sends paired exact hash and scopes availability cache", async () => {
  const spy = vi
    .spyOn(sdk, "readResearchNoteRevision")
    .mockResolvedValue({ data: noteRevision() } as never);
  const options = noteRevisionOptions("owner", noteSelection);
  await options.queryFn!(context);
  expect(options.staleTime).toBe(0);
  expect(options.queryKey).toContain(noteSelection.note_revision_sha256);
  expect(spy).toHaveBeenCalledWith({
    path: { note_id: ids.run, revision: 1 },
    query: { revision_sha256: noteSelection.note_revision_sha256 },
    signal: expect.any(AbortSignal),
  });
  expect(noteRevisionOptions("owner", { note_id: ids.run }).enabled).toBe(
    false,
  );
});
it("bounds immutable history and uses generated exact create/edit with durable idempotency", async () => {
  const history = vi
    .spyOn(sdk, "listResearchNoteRevisions")
    .mockResolvedValue({ data: { data: [], count: 0 } } as never);
  await noteHistoryOptions("owner", ids.run, 25).queryFn!(context);
  expect(history).toHaveBeenCalledWith({
    path: { note_id: ids.run },
    query: { offset: 25, limit: 25 },
    signal: expect.any(AbortSignal),
  });
  const create = vi
    .spyOn(sdk, "createResearchNote")
    .mockResolvedValue({ data: noteRevision() } as never);
  const revise = vi
    .spyOn(sdk, "createResearchNoteRevision")
    .mockResolvedValue({ data: noteRevision() } as never);
  const draft = prepareNoteSubmission({
    version: 1,
    note_id: null,
    base_revision: null,
    base_revision_sha256: null,
    content: noteContent,
    change_note: "",
    key: null,
    fingerprint: null,
  });
  await saveNoteDraft(draft);
  expect(create).toHaveBeenCalledWith({
    body: { content: noteContent, change_note: null },
    headers: { "Idempotency-Key": draft.key },
  });
  const edit = prepareNoteSubmission({
    ...draft,
    note_id: ids.run,
    base_revision: 1,
    base_revision_sha256: noteSelection.note_revision_sha256,
    key: null,
    fingerprint: null,
  });
  await saveNoteDraft(edit);
  expect(revise).toHaveBeenCalledWith({
    path: { note_id: ids.run },
    body: expect.objectContaining({
      base_revision: 1,
      base_revision_sha256: noteSelection.note_revision_sha256,
    }),
    headers: { "Idempotency-Key": edit.key },
  });
});
it("downloads server-produced Markdown with exact revision admission, not client-rendered HTML", async () => {
  const exportFn = vi
    .spyOn(sdk, "exportResearchNoteRevision")
    .mockResolvedValue({ data: "# Safe server export\nReferences" } as never);
  const blob = await exportNoteRevision(noteSelection);
  expect(blob.type).toBe("text/markdown;charset=utf-8");
  expect(await blob.text()).toContain("References");
  expect(exportFn).toHaveBeenCalledWith({
    path: { note_id: ids.run, revision: 1 },
    query: { revision_sha256: noteSelection.note_revision_sha256 },
    parseAs: "text",
  });
});
