import { readFileSync } from "node:fs";
import { Children, createElement, isValidElement, type ReactNode } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { ResearchCaptureControls } from "../../src/components/research-capture-controls";
import {
  captureBudgetFields,
  captureSummary,
  parseCaptureScope,
  researchCaptureDraftSchema,
  researchCaptureSchema,
  withResearchCapture,
  type ResearchCapture,
} from "../../src/lib/research-capture";
import {
  researchFormSchema,
  researchStepFor,
} from "../../src/lib/research-form";
import {
  portableFormSchema,
  portableIssues,
} from "../../src/features/portable/protocol";
import { researchFixture } from "./research-fixtures";
import { portableFixture } from "./portable-fixtures";

const capture: ResearchCapture = {
  mode: "research",
  observation_window: { start: "2024-04-01", end: "2024-05-01" },
  stage_ids: ["selection", "allocation"],
  instrument_ids: ["listing-a"],
  max_decisions: 100,
  max_detail_rows: 10000,
  max_uncompressed_bytes: 1048576,
};

describe("capture scope and immutable authoring contract", () => {
  it.each(["off", "summary", "research"] as const)(
    "keeps %s explicit but never invents optional defaults",
    (mode) => {
      expect(researchCaptureSchema.parse({ mode })).toEqual({ mode });
      for (const body of [researchFixture().body, portableFixture().body]) {
        const changed = withResearchCapture(body, { mode });
        expect(changed.research_capture).toEqual({ mode });
        expect(withResearchCapture(changed, undefined)).toEqual(body);
      }
    },
  );
  it("retains omitted and stored null defaults in both transport forms", () => {
    const native = researchFixture().body;
    const portable = portableFixture().body;
    expect(researchFormSchema.parse(native)).not.toHaveProperty(
      "research_capture",
    );
    expect(portableFormSchema.parse(portable)).not.toHaveProperty(
      "research_capture",
    );
    expect(
      researchFormSchema.parse({ ...native, research_capture: null })
        .research_capture,
    ).toBeNull();
    expect(
      portableFormSchema.parse({ ...portable, research_capture: null })
        .research_capture,
    ).toBeNull();
    expect(captureSummary(undefined)).toContain("no capture override sent");
    expect(captureSummary(null)).toContain("stored null retained");
  });
  it("never narrows immutable data, universe, execution windows or warm-up", () => {
    for (const original of [researchFixture().body, portableFixture().body]) {
      const before = structuredClone(original);
      const changed = withResearchCapture(original, capture);
      expect(original).toEqual(before);
      expect(changed.dataset).toBe(original.dataset);
      expect(changed.universe).toBe(original.universe);
      expect(changed.windows).toBe(original.windows);
      expect(changed.warmup).toBe(original.warmup);
      expect(withResearchCapture(changed, undefined)).toEqual(original);
      expect(Object.keys(withResearchCapture(changed, undefined))).toEqual(
        Object.keys(original),
      );
    }
  });
  it("matches all API capture fields and numeric default/range limits", () => {
    const api = JSON.parse(
      readFileSync(
        new URL("../../../packages/contracts/openapi.json", import.meta.url),
        "utf8",
      ),
    );
    const properties = api.components.schemas.CaptureConfig.properties;
    expect(Object.keys(researchCaptureSchema.shape).sort()).toEqual(
      Object.keys(properties).sort(),
    );
    for (const field of captureBudgetFields) {
      expect(properties[field.key]).toMatchObject({
        minimum: field.min,
        maximum: field.max,
        default: field.defaultValue,
      });
    }
  });
  it("parses exact comma/newline IDs without silently deduplicating", () => {
    expect(parseCaptureScope(" stage-a, stage-b\r\n\n stage-c , ")).toEqual([
      "stage-a",
      "stage-b",
      "stage-c",
    ]);
    expect(parseCaptureScope("listing-a,listing-a")).toEqual([
      "listing-a",
      "listing-a",
    ]);
    expect(parseCaptureScope(" , \n ")).toEqual([]);
  });
  it.each([
    { observation_window: { start: "2024-02-30", end: "2024-03-01" } },
    { observation_window: { start: "2024-03-01", end: "2024-03-01" } },
    { observation_window: { start: "2024-04-01", end: "2024-03-01" } },
    { observation_window: { start: "", end: "2024-03-01" } },
    { stage_ids: [""] },
    { stage_ids: [" "] },
    { stage_ids: ["a".repeat(201)] },
    { stage_ids: ["selection", "selection"] },
    { stage_ids: Array.from({ length: 101 }, (_, index) => `stage-${index}`) },
    { instrument_ids: ["listing-a", "listing-a"] },
    {
      instrument_ids: Array.from(
        { length: 10001 },
        (_, index) => `listing-${index}`,
      ),
    },
    { mode: "all" },
    { calculation_universe: ["listing-a"] },
  ])("rejects invalid capture scope (%#) in both submit schemas", (patch) => {
    const research_capture = { mode: "research", ...patch };
    expect(
      researchFormSchema.safeParse({
        ...researchFixture().body,
        research_capture,
      }).success,
    ).toBe(false);
    expect(
      portableFormSchema.safeParse({
        ...portableFixture().body,
        research_capture,
      }).success,
    ).toBe(false);
    expect(
      portableIssues({
        ...portableFixture().body,
        research_capture,
      } as ReturnType<typeof portableFixture>["body"]).join(" "),
    ).toContain("Research capture");
  });
  it.each(captureBudgetFields)(
    "validates $key without clamping the requested budget",
    ({ key, min, max }) => {
      for (const value of [min, max])
        expect(
          researchCaptureSchema.parse({ mode: "research", [key]: value }),
        ).toEqual({ mode: "research", [key]: value });
      for (const value of [min - 1, max + 1, min + 0.5, Infinity, NaN])
        expect(
          researchCaptureSchema.safeParse({ mode: "research", [key]: value })
            .success,
        ).toBe(false);
    },
  );
  it("keeps partial observation dates as drafts but cannot admit them", () => {
    const partial = {
      ...capture,
      observation_window: { start: "2024-04-01", end: "" },
    };
    expect(researchCaptureDraftSchema.parse(partial)).toEqual(partial);
    expect(researchCaptureSchema.safeParse(partial).success).toBe(false);
    expect(researchStepFor("research_capture.observation_window.end")).toBe(2);
  });
});

// Component callback + SSR contracts only. Browser interaction is separately
// covered by the real-stack Playwright gate, never claimed by these unit tests.
function control(node: ReactNode, id: string): Record<string, any> | undefined {
  for (const child of Children.toArray(node)) {
    if (!isValidElement<Record<string, any>>(child)) continue;
    if (child.props.id === id && typeof child.props.onChange === "function")
      return child.props;
    const found = control(child.props.children, id);
    if (found) return found;
  }
}
function change(
  value: ResearchCapture | undefined,
  field: string,
  input: unknown,
) {
  const onChange = vi.fn();
  const tree = ResearchCaptureControls({ value, onChange });
  const item = control(tree, `research_capture.${field}`);
  expect(item, field).toBeDefined();
  item!.onChange(input);
  return onChange.mock.calls[0][0] as ResearchCapture | undefined;
}
describe("visible capture controls", () => {
  it("renders mode, scope and all budgets without modifying a default request", () => {
    const onChange = vi.fn();
    const html = renderToString(
      createElement(ResearchCaptureControls, { value: undefined, onChange }),
    );
    for (const text of [
      "Observation capture mode",
      "Observation stage IDs",
      "Observation instrument IDs",
      "Maximum saved decisions",
      "Maximum saved detail rows",
      "Maximum uncompressed capture bytes",
      "full pinned dataset",
      "no override",
    ])
      expect(html).toContain(text);
    expect(html).toContain('<option value="" selected="">');
    expect(html).toContain('<fieldset disabled=""');
    expect(html).not.toContain(
      'id="research_capture.observation_window.start"',
    );
    expect(onChange).not.toHaveBeenCalled();
  });
  it("renders restored values, exclusive dates and truthful portable coverage", () => {
    const html = renderToString(
      createElement(ResearchCaptureControls, {
        value: capture,
        onChange: () => {},
        portable: true,
        prefix: "portable.research_capture",
      }),
    );
    expect(html).toContain('value="research" selected=""');
    expect(html).toContain(
      'id="portable.research_capture.observation_window.start"',
    );
    expect(html).toContain('value="2024-04-01"');
    expect(html).toContain('value="1048576"');
    expect(html).toContain("Observation end · exclusive");
    expect(html).toContain("100-listing ABI cap is unchanged");
    expect(html).toContain(
      "missing coverage, never false conditions or zero positions",
    );
  });
  it("switches explicit modes and restores omitted defaults", () => {
    expect(change(undefined, "mode", { target: { value: "summary" } })).toEqual(
      { mode: "summary" },
    );
    expect(
      change(undefined, "mode", { target: { value: "research" } }),
    ).toEqual({ mode: "research" });
    expect(change(capture, "mode", { target: { value: "summary" } })).toEqual({
      ...capture,
      mode: "summary",
    });
    expect(change(capture, "mode", { target: { value: "off" } })).toEqual({
      mode: "off",
    });
    expect(change(capture, "mode", { target: { value: "" } })).toBeUndefined();
  });
  it("edits dates, lists and budgets only inside capture", () => {
    const original = structuredClone(capture);
    expect(
      change(capture, "observation_window.start", {
        target: { value: "2024-04-02" },
      }),
    ).toEqual({
      ...capture,
      observation_window: {
        ...capture.observation_window,
        start: "2024-04-02",
      },
    });
    expect(change(capture, "stage_ids", ["allocation"])).toEqual({
      ...capture,
      stage_ids: ["allocation"],
    });
    expect(change(capture, "instrument_ids", [])).not.toHaveProperty(
      "instrument_ids",
    );
    expect(
      change(capture, "max_decisions", {
        target: { value: "20", valueAsNumber: 20 },
      }),
    ).toEqual({ ...capture, max_decisions: 20 });
    expect(
      change(capture, "max_decisions", {
        target: { value: "", valueAsNumber: NaN },
      }),
    ).not.toHaveProperty("max_decisions");
    expect(
      change(capture, "observation_window", { target: { checked: false } }),
    ).not.toHaveProperty("observation_window");
    expect(
      change({ mode: "research" }, "observation_window", {
        target: { checked: true },
      }),
    ).toEqual({ mode: "research", observation_window: { start: "", end: "" } });
    expect(capture).toEqual(original);
  });
  it("shows validation failures and disables controls while submission is busy", () => {
    const html = renderToString(
      createElement(ResearchCaptureControls, {
        value: { ...capture, max_decisions: 0 },
        onChange: () => {},
        disabled: true,
      }),
    );
    expect(html).toContain('<fieldset disabled=""');
    expect(html).toContain('role="alert"');
    expect(html).toContain('href="#research_capture.max_decisions"');
  });
});
