import { describe, expect, it } from "vitest";
import {
  changeSelection,
  selectionProblem,
  workbenchSearch,
} from "../../src/lib/workbench-selection";
import {
  noteOperationCurrent,
  noteRestoreSelection,
  safeNoteEvidenceHref,
} from "../../src/lib/note-selection";
import { hashes, ids } from "./workbench-fixtures";
const evidence = {
  tab: "replay" as const,
  code_sha256: hashes.code,
  spec_revision: 1,
  spec_sha256: hashes.spec,
  evaluation_id: ids.evaluation,
  execution_attempt_id: ids.attempt,
  result_manifest_sha256: hashes.result,
  trace_manifest_sha256: hashes.trace,
  decision_id: "decision-2",
  instrument_id: "B",
  stage_id: "filter",
  record_kind: "predicate" as const,
  record_id: "check",
  record_ordinal: 2,
  detail_offset: 50,
};
const note = {
  note_id: ids.run,
  note_revision: 3,
  note_revision_sha256: hashes.code,
};
const href = (values: Record<string, string | number> = evidence) =>
  `/strategies/${ids.strategy}/research?${new URLSearchParams(Object.entries(values).map(([k, v]) => [k, String(v)]))}`;
describe("immutable note selections", () => {
  it("retains exact note revision and full source/observation on refresh and tab changes", () => {
    const selection = {
      ...evidence,
      ...note,
      note_query: "liquidity",
      note_offset: 25,
    };
    expect(workbenchSearch(selection)).toEqual(selection);
    expect(changeSelection(selection, { tab: "notes" })).toEqual({
      ...selection,
      tab: "notes",
    });
    expect(selectionProblem(selection)).toBeNull();
  });
  it.each([
    { note_id: ids.run },
    { note_revision: 1 },
    { note_revision_sha256: hashes.code },
    { note_id: ids.run, note_revision: 2 },
  ])("never upgrades an incomplete note revision to latest: %j", (value) => {
    expect(selectionProblem(value)).toContain("No latest note");
  });
  it.each([
    { note_revision: 0 },
    { note_revision: true },
    { note_revision: 10001 },
    { note_arm: " " },
    { note_arm: false },
    { note_revision: "bad" },
    { note_revision_sha256: "bad" },
    { note_origin_tab: "notes" },
    { note_offset: -1 },
  ])("rejects malformed note selections: %j", (value) => {
    expect(() => workbenchSearch(value)).toThrow();
  });
  it("captures complete evidence but removes note identity/search and remembers launch tab", () => {
    expect(
      noteRestoreSelection({
        ...evidence,
        ...note,
        tab: "notes",
        note_query: "text",
        note_origin_tab: "comparison",
      }),
    ).toEqual({ ...evidence, tab: "comparison" });
  });
  it("only opens exact local validated evidence links", () => {
    expect(safeNoteEvidenceHref(href())).toBe(href());
    for (const bad of [
      "https://evil.test/",
      "javascript:alert(1)",
      "//evil.test/",
      href() + "&unrecognized=latest",
      href() + "&spec_revision=2",
      href() + "#other",
      href({ tab: "replay" }),
    ])
      expect(safeNoteEvidenceHref(bad)).toBeNull();
  });
});

it("fences late capture/save/export completion after owner, route or mount changes", () => {
  const original = JSON.stringify({
    owner: "one",
    strategy: ids.strategy,
    selection: evidence,
  });
  expect(noteOperationCurrent(original, original, true)).toBe(true);
  expect(noteOperationCurrent(original, original, false)).toBe(false);
  for (const current of [
    JSON.stringify({
      owner: "two",
      strategy: ids.strategy,
      selection: evidence,
    }),
    JSON.stringify({
      owner: "one",
      strategy: ids.candidate,
      selection: evidence,
    }),
    JSON.stringify({
      owner: "one",
      strategy: ids.strategy,
      selection: { ...evidence, decision_id: "newer" },
    }),
  ])
    expect(noteOperationCurrent(original, current, true)).toBe(false);
});

it("preserves historical note evidence and arm ordinals with the exact immutable revision", () => {
  const route = { ...note, tab: "notes", note_evidence_index: 2, note_arm: 0 };
  expect(workbenchSearch(route)).toEqual(route);
  expect(selectionProblem(workbenchSearch(route))).toBeNull();
  expect(selectionProblem({ note_arm: 0 })).toContain("Historical arm");
  expect(() => workbenchSearch({ ...route, note_arm: 3 })).toThrow();
  expect(() =>
    workbenchSearch({ ...route, note_evidence_index: 32 }),
  ).toThrow();
});
