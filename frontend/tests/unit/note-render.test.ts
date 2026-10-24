import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NoteReadOnly } from "../../src/features/research-workbench/note-panels";
import { noteRevision, noteSelection } from "./note-fixtures";
import { ids } from "./workbench-fixtures";
const render = (value = noteRevision()) =>
  renderToString(createElement(NoteReadOnly, { note: value })).replaceAll(
    "<!-- -->",
    "",
  );
beforeEach(() => {
  vi.stubGlobal("window", { location: { origin: "https://notes.test" } });
  vi.stubGlobal("sessionStorage", { getItem: () => null });
  vi.stubGlobal("fetch", () => {
    throw new Error("Pure projection must not fetch.");
  });
});
afterEach(() => vi.unstubAllGlobals());
describe("human note and admitted evidence rendering", () => {
  it("shows exact revision, manual status, structured content and retention limitations", () => {
    const html = render();
    for (const expected of [
      "Liquidity mechanism",
      "Hypothesis",
      "Counterexamples",
      "Applicability and limitations",
      "Next steps",
      "Pending validation",
      "never inferred from profit",
      "Retained artifact pins",
      "Restore full saved research selection",
      "Open exact source evidence",
    ])
      expect(html).toContain(expected);
  });
  it.each(["missing", "unavailable", "corrupt"] as const)(
    "keeps %s evidence visible without a replacement link",
    (state) => {
      const note = noteRevision();
      note.evidence[0] = {
        ...note.evidence[0],
        state,
        reason: "Exact artifact could not be admitted",
        href: null,
      };
      note.restore_href = null;
      const html = render(note);
      expect(html).toContain(`Evidence state: ${state}`);
      expect(html).toContain("Exact artifact could not be admitted");
      expect(html).not.toContain("Open exact source evidence");
      expect(html).toContain("Full selection restoration is unavailable");
    },
  );
  it("escapes arbitrary note markup and refuses javascript/external or partial evidence URLs", () => {
    const note = noteRevision();
    note.content.body =
      '<img src=x onerror="alert(1)"><script>alert(1)</script>';
    note.evidence[0].href = "javascript:alert(1)";
    note.restore_href = "https://evil.test/";
    const html = render(note);
    expect(html).toContain("&lt;img");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain('href="javascript:');
    expect(html).not.toContain('href="https://evil');
  });
  it("does not promote a human supported status into observed evidence availability", () => {
    const note = noteRevision();
    note.content.status = "supported_under_current_tests";
    note.evidence[0].state = "corrupt";
    const html = render(note);
    expect(html).toContain("Supported under current tests");
    expect(html).toContain("Evidence state: corrupt");
    expect(html).not.toContain("Open exact source evidence");
  });
  it("does not silently choose duplicate/missing evidence resolutions", () => {
    const note = noteRevision();
    note.evidence.push(note.evidence[0]);
    const html = render(note);
    expect(html).toContain("did not resolve this exact reference uniquely");
    expect(html).not.toContain("Open exact source evidence");
  });
  it("reads an owned note even if its current source has disappeared", async () => {
    const { ResearchNotes } =
      await import("../../src/features/research-workbench/notes");
    const query = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnMount: false } },
    });
    query.setQueryData(["research-notes", "owner", "", 0], {
      data: [],
      count: 0,
    });
    query.setQueryData(
      [
        "research-note",
        "owner",
        ids.run,
        1,
        noteSelection.note_revision_sha256,
      ],
      noteRevision(),
    );
    query.setQueryData(["research-note-history", "owner", ids.run, 0], {
      data: [],
      count: 0,
    });
    const html = renderToString(
      createElement(
        QueryClientProvider,
        { client: query },
        createElement(ResearchNotes, {
          owner: "owner",
          strategyId: ids.strategy,
          selection: noteSelection,
          verified: false,
          update: () => undefined,
          sourceProblem: new Error("Source deleted"),
        }),
      ),
    );
    query.clear();
    expect(html).toContain("Current source context is unavailable");
    expect(html).toContain("Liquidity mechanism");
    expect(html).toContain("Source deleted");
    expect(html).toContain("Download Markdown with references");
  });
});

it("opens stopped-arm history from the pinned note rather than a mutable current run", async () => {
  const { HistoricalControlArm } =
    await import("../../src/features/research-workbench/note-panels");
  const note = noteRevision();
  const failedAttempt = "50000000-0000-4000-8000-000000000001";
  note.content.evidence = [
    {
      kind: "controlled",
      label: "Historical failures",
      controlled_experiment_id: ids.strategy,
      request_sha256: "a".repeat(64),
      protocol_sha256: "b".repeat(64),
      selected_arm: 1,
      arms: [
        {
          ordinal: 0,
          run_id: ids.run,
          status: "failed",
          execution_attempt_id: ids.attempt,
          generation: 1,
          result: null,
        },
        {
          ordinal: 1,
          run_id: ids.candidate,
          status: "failed",
          execution_attempt_id: failedAttempt,
          generation: 1,
          result: null,
        },
      ],
    },
  ];
  note.evidence = [
    { index: 0, state: "available", reason: null, bindings: [], href: null },
  ];
  note.restore_href = null;
  const html = renderToString(
    createElement(HistoricalControlArm, {
      note,
      selection: { ...noteSelection, note_evidence_index: 0, note_arm: 1 },
    }),
  ).replaceAll("<!-- -->", "");
  expect(html).toContain("Saved control arm 2");
  expect(html).toContain(failedAttempt);
  expect(html).toContain("not current run status");
  expect(html).toContain("No successful result");
  expect(html).not.toContain("href=");
  const missing = renderToString(
    createElement(HistoricalControlArm, {
      note,
      selection: { ...noteSelection, note_evidence_index: 0, note_arm: 2 },
    }),
  );
  expect(missing).toContain(
    "No other reference, arm or latest run was substituted",
  );
});

it.each(["denied", "checking"] as const)(
  "suppresses cached note body/list/history and copied edit draft when admission is %s",
  async (state) => {
    const { ResearchNotes } =
      await import("../../src/features/research-workbench/notes");
    const { storeNoteDraft } = await import("../../src/lib/note-draft");
    const storage = new Map<string, string>();
    vi.stubGlobal("sessionStorage", {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => storage.set(key, value),
      removeItem: (key: string) => storage.delete(key),
    });
    const note = noteRevision();
    note.content.title = "Sensitive prior outcome";
    storeNoteDraft("owner", ids.strategy, {
      version: 1,
      note_id: note.note_id,
      base_revision: note.revision,
      base_revision_sha256: note.revision_sha256,
      content: {
        ...note.content,
        body: "Copied outcome draft must stay hidden",
      },
      change_note: "",
      key: null,
      fingerprint: null,
    });
    const query = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const summary = {
      id: ids.run,
      revision: 1,
      revision_sha256: note.revision_sha256,
      title: note.content.title,
      tags: [],
      status: "pending",
      evidence_count: 1,
      created_at: note.created_at,
      updated_at: note.created_at,
      change_note: "Sensitive prior history",
    };
    const keys = [
      ["research-notes", "owner", "", 0],
      ["research-note", "owner", ids.run, 1, note.revision_sha256],
      ["research-note-history", "owner", ids.run, 0],
    ];
    query.setQueryData(keys[0], { data: [summary], count: 1 });
    query.setQueryData(keys[1], note);
    query.setQueryData(keys[2], { data: [summary], count: 1 });
    for (const key of keys)
      query
        .getQueryCache()
        .find({ queryKey: key })!
        .setState(
          state === "denied"
            ? {
                status: "error",
                error: new Error("Final admission revoked"),
                fetchStatus: "idle",
              }
            : { fetchStatus: "fetching" },
        );
    const html = renderToString(
      createElement(
        QueryClientProvider,
        { client: query },
        createElement(ResearchNotes, {
          owner: "owner",
          strategyId: ids.strategy,
          selection: noteSelection,
          verified: false,
          update: () => undefined,
        }),
      ),
    );
    query.clear();
    expect(html).not.toContain("Sensitive prior outcome");
    expect(html).not.toContain("Sensitive prior history");
    expect(html).not.toContain("Copied outcome draft must stay hidden");
    expect(html).toContain("Copied note content is hidden");
  },
);
