import { expectControlLabel } from "./control-labels";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement, type ReactNode } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import type {
  ComparisonAssociation,
  ComparisonRecord,
} from "../../src/client/types.gen";
import {
  ComparisonAvailability,
  ComparisonConfiguration,
  ComparisonMechanisms,
  ComparisonOutcome,
  ComparisonPrestates,
  ComparisonRecords,
  configurationValue,
  exactRecordTarget,
  explicitAssociationTarget,
} from "../../src/features/research-workbench/comparison-panels";
import { comparisonChartOptions } from "../../src/features/research-workbench/comparison-chart";
import {
  comparisonReplayTarget,
  immutableReplayTarget,
} from "../../src/lib/comparison-evidence";
import {
  eventRow,
  manifest,
  result,
  sectionSummary,
  comparisonSelection,
  page,
  status,
  rightId,
  rightSpec,
  rightLeaf,
} from "./comparison-fixtures";
import { detail, hashes, ids, leaf, spec } from "./workbench-fixtures";
vi.hoisted(() => {
  vi.stubGlobal("window", { location: { origin: "https://compare.test" } });
});
const render = (node: ReactNode) =>
  renderToString(node).replaceAll("<!-- -->", "");
const noNetwork = vi.fn(() => {
  throw new Error("SSR projection must not access network");
});
beforeEach(() => {
  vi.stubGlobal("window", { location: { origin: "https://compare.test" } });
  vi.stubGlobal("sessionStorage", {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
  vi.stubGlobal("fetch", noNetwork);
  noNetwork.mockClear();
});
afterEach(() => vi.unstubAllGlobals());
async function routed(
  node: ReactNode,
  query = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
    },
  }),
) {
  const root = createRootRoute({ component: () => node });
  const research = createRoute({
    getParentRoute: () => root,
    path: "/strategies/$strategyId/research",
  });
  const router = createRouter({
    routeTree: root.addChildren([research]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
    isServer: true,
  });
  await router.load();
  const html = render(
    createElement(
      QueryClientProvider,
      { client: query },
      createElement(RouterProvider, { router }),
    ),
  );
  query.clear();
  expect(noNetwork).not.toHaveBeenCalled();
  return html;
}
describe("configuration and mechanism explanation precede interpretation", () => {
  it.each([
    [false, null, "Missing / not recorded"],
    [true, null, "null (explicit recorded value)"],
    [true, false, "false"],
    [true, 0, "0"],
    [true, [], "[]"],
  ])("preserves presence/value %j %j", (present, value, expected) =>
    expect(configurationValue(present as boolean, value)).toBe(expected),
  );
  it("renders required comparison basis, false/zero/unknown and leaf windows", () => {
    const html = render(
      createElement(ComparisonConfiguration, { manifest: manifest() }),
    );
    for (const value of [
      "data",
      "universe",
      "warmup",
      "fees",
      "fill rules",
      "initialization/reset",
      "market",
      "dependencies/image",
      "RNG",
      "Missing / not recorded",
      ">false<",
      ">0<",
      "2024-01-01",
      "not a single-factor",
    ])
      expect(html).toContain(value);
  });
  it("uses actual source diff with all five categories and declared mapping/anchors", () => {
    const html = render(
      createElement(ComparisonMechanisms, {
        result: result(),
        selection: { mechanism_index: 0 },
        update: () => undefined,
      }),
    );
    for (const value of [
      "input",
      "filter",
      "scoring",
      "portfolio",
      "execution",
      "Mapping: controlled",
      "strategy.py",
      hashes.code,
      "Immutable left and right source diff",
      "# original",
      "# changed comment",
      "Left exact anchor",
      "Right exact anchor",
    ])
      expect(html).toContain(value);
  });
  it.each(["inferred", "unmapped", "human_confirmed"] as const)(
    "keeps %s mapping explicit, never promoting it to runtime truth",
    (mapping) => {
      const value = result();
      value.manifest.mechanism_changes[0].mapping = mapping;
      const html = render(
        createElement(ComparisonMechanisms, {
          result: value,
          selection: {},
          update: () => undefined,
        }),
      );
      expect(html).toContain(`Mapping: ${mapping}`);
      expect(html).toContain("not runtime facts");
      expect(html).toContain("No summary establishes");
    },
  );
  it("rejects stale source anchors instead of locating current code", () => {
    const value = result();
    value.manifest.mechanism_changes[0].left_source_ref = {
      ...value.manifest.mechanism_changes[0].left_source_ref!,
      code_sha256: "0".repeat(64),
    };
    const html = render(
      createElement(ComparisonMechanisms, {
        result: value,
        selection: { mechanism_index: 0 },
        update: () => undefined,
      }),
    );
    expect(html).toContain("does not match the selected immutable source");
  });
  it("shows absent summary selection explicitly rather than picking the first rule", () =>
    expect(
      render(
        createElement(ComparisonMechanisms, {
          result: result(),
          selection: { mechanism_index: 55 },
          update: () => undefined,
        }),
      ),
    ).toContain("No other source anchor was selected"));
});
describe("scope-limited behavior outcomes", () => {
  it("states changed code with unchanged observed behavior and exact retained scopes", () => {
    const html = render(
      createElement(ComparisonOutcome, { manifest: manifest() }),
    );
    for (const value of [
      "Code changed, but",
      "unchanged_observed",
      "candidates, targets",
      "2024-01-02",
      "2024-01-03",
      "liquidity",
      "B",
      "observation_window",
      "not diagnostic parity or causal validation",
      "Earlier paths can change cash",
    ])
      expect(html).toContain(value);
  });
  it("separates named unchanged decision sections from full unknown behavior", () => {
    const value = manifest();
    value.observed_behavior = "insufficient_coverage";
    const html = render(createElement(ComparisonOutcome, { manifest: value }));
    expect(html).toContain("Insufficient coverage");
    expect(html).toContain(
      "Code changed; these recorded sections are unchanged: candidates, targets",
    );
    expect(html).toContain("Full behavior remains unknown");
  });
  it.each(["registered_off", "historical_absence"] as const)(
    "keeps %s unavailable, not an empty successful trace",
    (trace_state) => {
      const value = manifest();
      value.left.trace_state = trace_state;
      value.left.trace_coverage = null;
      value.left.capture = null;
      value.observed_behavior = "insufficient_coverage";
      value.observed_equal_sections = [];
      const html = render(
        createElement(ComparisonOutcome, { manifest: value }),
      );
      expect(html).toContain(trace_state);
      expect(html).toContain("Not collected");
      expect(html).not.toContain("these recorded sections are unchanged");
    },
  );
  it.each(["partial", "not_collected", "unsupported"] as const)(
    "renders %s and concrete scope/reason instead of silent empty records",
    (availability) => {
      const html = render(
        createElement(ComparisonAvailability, {
          value: {
            ...sectionSummary(),
            left_availability: availability,
            left_reason: "Not collected by this capture mode",
          },
        }),
      );
      expect(html).toContain(availability);
      expect(html).toContain("Not collected by this capture mode");
      expect(html).toContain("Left observation scope");
      expect(html).toContain("stage_ids");
    },
  );
  it("always asks for explicit pair selection before interpreting prestates", () => {
    const html = render(
      createElement(ComparisonPrestates, { result: result() }),
    );
    expect(html).toContain("Left pre-state");
    expect(html).toContain("Right pre-state");
    expect(html).toContain("Select an explicit event pair");
  });
  it("renders actual zero left cash, missing right state, path caution and distinct immutable event URLs", async () => {
    const html = await routed(
      createElement(ComparisonPrestates, {
        result: result(),
        event: eventRow(),
      }),
    );
    expect(html).toContain(">0<");
    expect(html).toContain("State not collected at this callback");
    expect(html).toContain("decision-1");
    expect(html).toContain("right-decision-7");
    expect(html).toContain(`result_manifest_sha256=${hashes.result}`);
    expect(html).toContain(`spec_sha256=${hashes.spec}`);
    expect(html).toContain("not a local counterfactual");
  });
  it.each(["left", "right"] as const)(
    "qualifies %s native session labels independently of the portable comparison side",
    async (nativeSide) => {
      const value = result();
      const portableSide = nativeSide === "left" ? "right" : "left";
      const caveat =
        "Native daily timestamps are market-local midnight session labels, not observed exchange-close or data-availability instants.";
      value[`${nativeSide}_timestamp_evidence`] = {
        timestamp_semantics: "native_daily_session_labels",
        timestamp_caveat: caveat,
      };
      value[`${portableSide}_timestamp_evidence`] = {
        timestamp_semantics: "portable_engine_instants",
        timestamp_caveat: null,
      };
      const html = await routed(
        createElement(ComparisonPrestates, {
          result: value,
          event: eventRow(),
        }),
      );
      const sideHtml = (side: string) =>
        html.match(
          new RegExp(
            `<section[^>]*data-testid="comparison-prestate-${side}"[\\s\\S]*?</section>`,
          ),
        )![0];
      expect(sideHtml(nativeSide)).toContain(
        "Information cutoff (session label)",
      );
      expect(sideHtml(nativeSide)).toContain("Decision session label");
      expect(sideHtml(nativeSide)).toContain(caveat);
      expect(sideHtml(portableSide)).toContain(">Information cutoff<");
      expect(sideHtml(portableSide)).toContain(">Decision time<");
      expect(sideHtml(portableSide)).not.toContain(caveat);
      expect(html).toContain(`result_manifest_sha256=${hashes.result}`);
      expect(html).toContain("right-decision-7");
    },
  );
  it("keeps older comparison sides unqualified rather than upgrading them to native midnight or actual instants", async () => {
    const value = result();
    value.left_timestamp_evidence = {
      timestamp_semantics: "native_recorded_unqualified",
      timestamp_caveat:
        "Historical source timing has not been independently verified.",
    };
    const html = await routed(
      createElement(ComparisonPrestates, {
        result: value,
        event: eventRow(),
      }),
    );
    expect(html).toContain(
      "Historical source timing has not been independently verified.",
    );
    expect(html).toContain("Saved timestamp semantics are unavailable");
    expect(
      html.match(/Information cutoff \(timing unverified\)/g),
    ).toHaveLength(2);
    expect(html).not.toContain("market-local midnight session labels");
    expect(html).not.toContain("exact saved instants");
  });
});
describe("record and execution drilldown cannot invent a join", () => {
  const predicate = { ...detail().predicates.items![0].observation };
  const candidate = { instrument_id: "B", selected: false, score: 0 };
  it.each(["candidates", "features", "predicates"] as const)(
    "links exact duplicate %s ordinal, event, stage and source",
    (section) => {
      const record =
        section === "candidates"
          ? candidate
          : section === "features"
            ? detail().features.items![0].observation
            : predicate;
      const row = {
        ...eventRow(),
        section,
        stage_id: "liquidity",
        instrument_id: "B",
      };
      const target = exactRecordTarget(
        result(),
        "right",
        eventRow(),
        row,
        { ordinal: 17, record },
        record,
      )!;
      expect(target.search.record_ordinal).toBe(17);
      expect(target.search.stage_id).toBe("liquidity");
      expect(target.search.decision_id).toBe("right-decision-7");
      expect(target.search.code_sha256).toBe(rightSpec.code_sha256);
    },
  );
  it("rejects missing ordinal/stage, wrong pair and timestamp-only orders", () => {
    const row = {
      ...eventRow(),
      section: "predicates",
      stage_id: "liquidity",
      instrument_id: "B",
    };
    expect(
      exactRecordTarget(result(), "left", eventRow(), row, {}, predicate),
    ).toBeNull();
    expect(
      exactRecordTarget(
        result(),
        "left",
        eventRow(),
        { ...row, stage_id: null },
        { ordinal: 0 },
        predicate,
      ),
    ).toBeNull();
    expect(
      exactRecordTarget(
        result(),
        "left",
        { ...eventRow(), event_pair_id: "other" },
        row,
        { ordinal: 0 },
        predicate,
      ),
    ).toBeNull();
    expect(
      exactRecordTarget(
        result(),
        "left",
        eventRow(),
        { ...row, section: "orders" },
        { ordinal: 0 },
        { logical_order_id: "guess" },
      ),
    ).toBeNull();
  });
  const association: ComparisonAssociation = {
    side: "left",
    decision_id: "exact-order-event",
    event_pair_id: "unmatched-pair",
    stage_id: "execution",
    logical_order_id: "logical-9",
    instrument_id: "B",
    link_ordinal: 2,
    source_event_paired: false,
    child_order_ids: ["child-1", "child-2"],
    fill_ids: ["fill-1", "fill-2"],
  };
  it("links exact saved execution associations even when cross-run event is unpaired", () => {
    const target = explicitAssociationTarget(result(), association)!;
    expect(target.search.decision_id).toBe("exact-order-event");
    expect(target.search.record_id).toBe("logical-9");
    expect(target.search.record_kind).toBe("order");
    expect(target.search.execution_attempt_id).toBe(ids.attempt);
    expect(target.search.session_date).toBeUndefined();
  });
  it("never fabricates original decisions for historical/capture-off sources", () => {
    for (const state of ["registered_off", "historical_absence"] as const) {
      const value = result();
      value.manifest.left.trace_state = state;
      expect(immutableReplayTarget(value, "left", "imagined")).toBeNull();
      expect(comparisonReplayTarget(value, "left", eventRow())).toBeNull();
    }
  });
  it("renders multiple saved order/fill associations independently, preserving child and partial-fill evidence", async () => {
    const row: ComparisonRecord = {
      ...eventRow(),
      section: "fills",
      event_pair_id: null,
      status: "changed",
      match_basis: "semantic_multiset",
      associations: [
        association,
        {
          ...association,
          side: "right",
          decision_id: "right-order-event",
          logical_order_id: "right-logical",
        },
      ],
      left: {
        items: [
          {
            ordinal: 2,
            record: { id: "fill-1", order_id: "child-1", volume: 100 },
          },
        ],
      },
      right: {
        items: [
          {
            ordinal: 2,
            record: { id: "fill-3", order_id: "child-3", volume: 50 },
          },
        ],
      },
    };
    const html = await routed(
      createElement(ComparisonRecords, {
        rows: [row],
        result: result(),
        onPair: () => undefined,
      }),
    );
    for (const text of [
      "exact-order-event",
      "right-order-event",
      "logical-9",
      "child-2",
      "fill-2",
      "Source event paired",
      "false",
      "No explicit event pair",
    ])
      expect(html).toContain(text);
  });
  it("does not call an empty bounded page zero behavior", () =>
    expect(
      render(
        createElement(ComparisonRecords, {
          rows: [],
          result: result(),
          onPair: () => undefined,
        }),
      ),
    ).toContain("not evidence of zero positions or equal behavior"));
  it.each(["left_only", "right_only", "ambiguous", "unavailable"] as const)(
    "retains %s rows and their missing/unpaired state",
    async (status) => {
      const html = await routed(
        createElement(ComparisonRecords, {
          rows: [
            { ...eventRow(), status, right: null, right_state: "unpaired" },
          ],
          result: result(),
          onPair: () => undefined,
        }),
      );
      expect(html).toContain(status);
      expect(html).toContain("Evidence unavailable: unpaired");
    },
  );
});
it("plots exact bounded row indexes rather than collapsing same-date events", () => {
  const rows = [
    eventRow(),
    {
      ...eventRow(),
      row_id: "row-2",
      event_pair_id: "pair-2",
      status: "left_only" as const,
    },
  ];
  const option = comparisonChartOptions(rows);
  expect(option.xAxis.data).toHaveLength(2);
  expect(option.xAxis.data[0]).not.toBe(option.xAxis.data[1]);
  expect(option.series[0].data[0].name).toContain("pair-1");
  expect(option.series[0].data[1].name).toContain("pair-2");
  expect(option.series[0].data[1].value[0]).toBe(1);
});
it("orders configuration before mechanisms/outcome/paired evidence in the actual tab", async () => {
  const { MechanismComparison } =
    await import("../../src/features/research-workbench/comparison");
  const query = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
    },
  });
  for (const [key, value] of [
    [["comparison-source-strategies", 0], { data: [], count: 0 }],
    [["mechanism-comparison", comparisonSelection.comparison_id], status()],
    [
      [
        "mechanism-comparison-result",
        comparisonSelection.comparison_id,
        comparisonSelection.comparison_manifest_sha256,
      ],
      result(),
    ],
    [
      [
        "mechanism-comparison-section",
        comparisonSelection.comparison_id,
        comparisonSelection.comparison_manifest_sha256,
        "events",
        undefined,
        undefined,
        0,
      ],
      page(),
    ],
    [["events", status().run_id], []],
    [
      ["workbench-spec", "strategy_version", ids.strategy, 1, hashes.spec],
      spec,
    ],
    [
      ["workbench-spec", "strategy_version", rightId, 1, rightSpec.spec_sha256],
      rightSpec,
    ],
    [
      ["strategy", ids.strategy],
      {
        id: ids.strategy,
        content_sha256: hashes.code,
        code: result().left_source.code,
      },
    ],
    [
      ["strategy", rightId],
      {
        id: rightId,
        content_sha256: rightSpec.code_sha256,
        code: result().right_source.code,
      },
    ],
    [["workbench-evaluation", leaf.evaluation_id], leaf],
    [["workbench-evaluation", rightLeaf.evaluation_id], rightLeaf],
  ] as [unknown[], unknown][])
    query.setQueryData(key, value);
  const html = await routed(
    createElement(MechanismComparison, {
      owner: "owner",
      selection: comparisonSelection,
      update: () => undefined,
      currentSpec: spec,
      currentLeaf: leaf,
      currentVerified: true,
    }),
    query,
  );
  expectControlLabel(html, "Explicit event pair", "select");
  expectControlLabel(html, "Behavior section", "select");
  const headings = [
    "1. Fixed inputs",
    "2. Mechanism",
    "3. Observed behavior",
    "4. Exact event pairs",
  ];
  const positions = headings.map((heading) => html.indexOf(heading));
  expect(positions.every((value) => value >= 0)).toBe(true);
  expect(positions).toEqual([...positions].sort((a, b) => a - b));
});

it("resolves a duplicate logical-order observation only with its saved stage ordinal", async () => {
  const { selectedRecord } = await import("../../src/lib/replay-evidence");
  const { selectionProblem, changeSelection, workbenchSearch } =
    await import("../../src/lib/workbench-selection");
  const recorded = detail();
  const order = {
    logical_order_id: "same-logical",
    instrument_id: "B",
    child_order_ids: ["child-1"],
    fill_ids: [],
    status: "accepted" as const,
    submitted_at: "2024-01-02T07:00:00Z",
    fill_times: [],
    rejection_reason: null,
    source: "adapter_observed" as const,
  };
  recorded.stages![0].order_links = [
    order,
    { ...order, child_order_ids: ["child-2"] },
  ];
  const selection = {
    ...comparisonReplayTarget(result(), "left", eventRow())!.search,
    stage_id: "liquidity",
    instrument_id: "B",
    record_kind: "order" as const,
    record_id: "same-logical",
  };
  expect(
    selectedRecord(selection, recorded, undefined)?.missing_reason,
  ).toContain("ambiguous");
  const exact = { ...selection, record_ordinal: 1 };
  expect(selectionProblem(exact)).toBeNull();
  expect(workbenchSearch(exact).record_ordinal).toBe(1);
  expect(selectedRecord(exact, recorded, undefined)?.value).toMatchObject({
    child_order_ids: ["child-2"],
  });
  expect(
    changeSelection(selection, { record_kind: "order", record_ordinal: 1 })
      .record_ordinal,
  ).toBe(1);
  expect(
    selectedRecord({ ...exact, record_ordinal: 3 }, recorded, undefined)
      ?.missing_reason,
  ).toContain("not present");
});
