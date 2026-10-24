import { createElement, type ReactNode } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TraceManifestPublic } from "../../src/client/types.gen";
import { detail, hashes, ids, leaf, spec } from "./workbench-fixtures";
import { noteContent } from "./note-fixtures";
import { expectControlLabel } from "./control-labels";

const network = vi.fn(() => {
  throw new Error(
    "Pure label regression tests must not make network requests.",
  );
});
beforeEach(() => {
  const storage = new Map<string, string>();
  vi.stubGlobal("window", { location: { origin: "https://labels.test" } });
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
  vi.stubGlobal("fetch", network);
  network.mockClear();
});
afterEach(() => vi.unstubAllGlobals());
function render(node: ReactNode, cache: [unknown[], unknown][] = []) {
  const query = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
    },
  });
  for (const [key, value] of cache) query.setQueryData(key, value);
  const html = renderToString(
    createElement(QueryClientProvider, { client: query }, node),
  );
  query.clear();
  expect(network).not.toHaveBeenCalled();
  return html;
}

describe("stable exact names for populated research controls", () => {
  it("keeps prose values out of description revision and rule labels", async () => {
    const { DescriptionRevisions } =
      await import("../../src/features/research-workbench/description-revisions");
    const { newDescriptionDraft, storeDescriptionDraft } =
      await import("../../src/lib/workbench-draft");
    const source = { kind: "strategy_version" as const, id: ids.strategy };
    const draft = newDescriptionDraft(spec);
    draft.body.change_note = "Retained previous explanation";
    storeDescriptionDraft("owner", source, draft);
    const html = render(
      createElement(DescriptionRevisions, {
        owner: "owner",
        source,
        spec,
        onSelect: () => undefined,
      }),
    );
    expectControlLabel(html, "Description revision", "select");
    expectControlLabel(html, "Revision change note", "textarea");
    for (const rule of spec.spec.rules) {
      expectControlLabel(html, `Rule title: ${rule.rule_id}`, "input");
      expectControlLabel(html, `Rule description: ${rule.rule_id}`, "textarea");
      expect(html).toContain(rule.description);
    }
    expect(html).toContain("Retained previous explanation</textarea>");
    expect(html).toContain(spec.spec_sha256);
  });

  it.each(["left", "right"] as const)(
    "names the populated %s source selectors without including choices",
    async (side) => {
      const { ComparisonSourcePicker } =
        await import("../../src/features/research-workbench/comparison-source");
      const { comparisonSidePatch } =
        await import("../../src/lib/workbench-selection");
      const { sideFromEvidence } =
        await import("../../src/lib/comparison-evidence");
      const html = render(
        createElement(ComparisonSourcePicker, {
          side,
          selection: comparisonSidePatch(side, sideFromEvidence(spec, leaf)),
          update: () => undefined,
          currentSpec: spec,
          currentLeaf: leaf,
          currentVerified: true,
        }),
        [
          [
            ["strategy", ids.strategy],
            {
              id: ids.strategy,
              name: "Populated source",
              content_sha256: hashes.code,
            },
          ],
          [
            [
              "workbench-spec",
              "strategy_version",
              ids.strategy,
              1,
              hashes.spec,
            ],
            spec,
          ],
          [["workbench-evaluation", ids.evaluation], leaf],
        ],
      );
      const title = side === "left" ? "Left" : "Right";
      for (const suffix of [
        "source type",
        "saved strategy",
        "actual leaf Evaluation",
      ])
        expectControlLabel(html, `${title} ${suffix}`, "select");
      expect(html).toContain("Populated source");
      expect(html).toContain(ids.evaluation);
    },
  );

  it("keeps saved event identity and option text separate from replay/filter names", async () => {
    const { DecisionReplay } =
      await import("../../src/features/research-workbench/decision-replay");
    const { CandidateFilters } =
      await import("../../src/features/research-workbench/replay-panels");
    const trace: TraceManifestPublic = {
      context: detail().context,
      manifest: {
        schema_version: "tradegrad.decision-trace.v2",
        binding: detail().context.binding!,
        capture: { mode: "research" },
        coverage: "complete",
        artifacts: [],
        decisions_saved: 1,
        decisions_seen: 1,
        detail_rows_saved: 0,
        detail_rows_seen: 0,
        missing_reasons: [],
        source: "strategy_emitted",
        stage_coverage: { liquidity: "complete" },
        trust_boundary: "Same-process observation",
      },
    };
    const selection = {
      code_sha256: hashes.code,
      evaluation_id: ids.evaluation,
      execution_attempt_id: ids.attempt,
      result_manifest_sha256: hashes.result,
      trace_manifest_sha256: hashes.trace,
      decision_id: "exact-saved-event",
      instrument_id: "B",
      stage_id: "liquidity",
    };
    const html = render(
      createElement(DecisionReplay, {
        selection,
        update: () => undefined,
        leaf,
        trace,
        verified: true,
        code: "pass",
        codeSha256: hashes.code,
      }),
    );
    for (const label of [
      "Specific decision event",
      "Observed instrument",
      "Observed stage",
    ])
      expectControlLabel(html, label, "select");
    expectControlLabel(html, "Decision date", "input");
    expect(html).toContain("exact-saved-event");
    expect(html).toContain("liquidity");
    const filters = render(
      createElement(CandidateFilters, {
        selection: { eligibility: "eligible", reason: "rank_not_selected" },
        update: () => undefined,
      }),
    );
    expectControlLabel(filters, "Eligibility filter", "select");
    expectControlLabel(filters, "Reason filter", "select");
    expect(filters).toContain('<option selected="">eligible</option>');
    expect(filters).toContain('<option selected="">rank_not_selected</option>');
  });

  it("names the chart field without including rendered plot choices", async () => {
    const { default: ComparisonChart } =
      await import("../../src/features/research-workbench/comparison-chart");
    const { eventRow } = await import("./comparison-fixtures");
    const html = render(
      createElement(ComparisonChart, {
        rows: [eventRow()],
        onPair: () => undefined,
      }),
    );
    expectControlLabel(html, "Plot saved field", "select");
    expect(html).toContain("Recorded difference categories");
  });

  it("keeps retained note status and change note values out of their labels", async () => {
    const { ResearchNotes } =
      await import("../../src/features/research-workbench/notes");
    const { storeNoteDraft } = await import("../../src/lib/note-draft");
    storeNoteDraft("owner", ids.strategy, {
      version: 1,
      note_id: null,
      base_revision: null,
      base_revision_sha256: null,
      content: noteContent,
      change_note: "Retained note explanation",
      key: null,
      fingerprint: null,
    });
    const html = render(
      createElement(ResearchNotes, {
        owner: "owner",
        strategyId: ids.strategy,
        selection: { tab: "notes" },
        update: () => undefined,
        spec,
        verified: false,
      }),
    );
    expectControlLabel(html, "User-maintained status", "select");
    expectControlLabel(html, "Revision change note", "textarea");
    expect(html).toContain("Retained note explanation</textarea>");
    expect(html).toContain('value="pending" selected=""');
  });

  it("retains all populated note prose under exact editable field labels", async () => {
    const { NoteTextFields } =
      await import("../../src/features/research-workbench/note-panels");
    const { noteTextFields } = await import("../../src/lib/note-evidence");
    const html = render(
      createElement(NoteTextFields, {
        content: noteContent,
        disabled: false,
        onChange: () => undefined,
      }),
    );
    for (const [field, label] of noteTextFields) {
      expectControlLabel(html, label, "textarea");
      expect(html).toContain(`${noteContent[field]}</textarea>`);
    }
  });
});
