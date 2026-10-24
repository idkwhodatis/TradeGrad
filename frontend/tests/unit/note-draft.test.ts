import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearNoteDraft,
  loadNoteDraft,
  noteFailureIsDefinitive,
  noteRequest,
  prepareNoteSubmission,
  storeNoteDraft,
  validateNoteContent,
  type NoteDraft,
} from "../../src/lib/note-draft";
import { noteContent } from "./note-fixtures";
import { ids } from "./workbench-fixtures";
const values = new Map<string, string>();
const draft = (): NoteDraft => ({
  version: 1,
  note_id: null,
  base_revision: null,
  base_revision_sha256: null,
  content: structuredClone(noteContent),
  change_note: "",
  key: null,
  fingerprint: null,
});
beforeEach(() => {
  values.clear();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
});
afterEach(() => vi.unstubAllGlobals());
describe("durable immutable note submissions", () => {
  it("retains the exact body and key across network loss/refresh without browser-only saved state", () => {
    const pending = prepareNoteSubmission(draft());
    storeNoteDraft("owner", ids.strategy, pending);
    const loaded = loadNoteDraft("owner", ids.strategy)!;
    expect(prepareNoteSubmission(loaded)).toEqual(pending);
    expect(noteRequest(loaded)).toEqual({
      content: noteContent,
      change_note: null,
    });
    expect(loadNoteDraft("other-owner", ids.strategy)).toBeNull();
    expect(loadNoteDraft("owner", ids.candidate)).toBeNull();
  });
  it("does not mistake a changed stored retry for the same accepted request", () => {
    const pending = prepareNoteSubmission(draft());
    storeNoteDraft("owner", ids.strategy, pending);
    const [key, raw] = [...values.entries()][0],
      changed = JSON.parse(raw);
    changed.content.title = "A different request";
    values.set(key, JSON.stringify(changed));
    expect(() => loadNoteDraft("owner", ids.strategy)).toThrow("unreadable");
  });
  it("keeps exact optimistic base when editing or reopening historical revisions", () => {
    const edited = {
      ...draft(),
      note_id: ids.run,
      base_revision: 1,
      base_revision_sha256: "b".repeat(64),
    };
    expect(noteRequest(edited)).toMatchObject({
      base_revision: 1,
      base_revision_sha256: "b".repeat(64),
    });
    expect(prepareNoteSubmission(edited).base_revision).toBe(1);
  });
  it("does not send when browser storage fails and never clears another pending save", () => {
    const pending = prepareNoteSubmission(draft());
    storeNoteDraft("owner", ids.strategy, pending);
    clearNoteDraft("owner", ids.strategy, crypto.randomUUID());
    expect(loadNoteDraft("owner", ids.strategy)?.key).toBe(pending.key);
    vi.stubGlobal("sessionStorage", {
      getItem: () => null,
      setItem: () => undefined,
    });
    expect(() => storeNoteDraft("owner", ids.strategy, pending)).toThrow(
      "No request was sent",
    );
  });
  it("allows incomplete unsent title while editing but validates before submission", () => {
    const empty = draft();
    empty.content.title = "";
    storeNoteDraft("owner", ids.strategy, empty);
    expect(loadNoteDraft("owner", ids.strategy)?.content.title).toBe("");
    expect(() => prepareNoteSubmission(empty)).toThrow("title");
  });
  it.each([
    "note_revision_conflict",
    "note_restore_mismatch",
    "note_evidence_pin_mismatch",
    "note_evidence_unavailable",
    "note_evidence_capacity",
    "note_revision_capacity",
    "invalid_note_pin",
    "source_integrity",
    "spec_integrity",
    "validation_error",
  ])(
    "releases rejected %s drafts for correction, not uncertain requests",
    (code) => expect(noteFailureIsDefinitive({ code })).toBe(true),
  );
  it.each([
    new Error("Failed to fetch"),
    { code: "internal_error" },
    { name: "TimeoutError" },
    null,
  ])("keeps uncertain errors locked: %j", (error) =>
    expect(noteFailureIsDefinitive(error)).toBe(false),
  );
  it.each([
    { tags: ["Tag", "tag"] },
    { tags: [" "] },
    { body: "x".repeat(32001) },
    { hypothesis: "x".repeat(8001) },
    { status: "profitable" },
  ])("rejects malformed/oversized content: %j", (patch) => {
    expect(() =>
      validateNoteContent({ ...noteContent, ...patch } as typeof noteContent),
    ).toThrow();
  });
});
