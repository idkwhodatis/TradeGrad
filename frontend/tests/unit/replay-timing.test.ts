import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { TraceContext } from "../../src/client/types.gen";
import ReplayChart, {
  replayChartOptions,
  type ReplayChartProps,
} from "../../src/features/research-workbench/replay-chart";
import { ConditionsAndFeatures } from "../../src/features/research-workbench/replay-panels";
import {
  ReplayDecisionIdentity,
  replayTimestampPresentation,
} from "../../src/features/research-workbench/replay-timing";
import { RuleCards } from "../../src/features/research-workbench/rule-cards";
import { detail, ids, spec } from "./workbench-fixtures";

const nativeCaveat =
  "Native daily decision, feature and information-cutoff timestamps are market-local midnight session labels, not observed exchange-close or data-availability instants. Same-session OHLC observations do not prove availability at midnight, intraday causality or absence of look-ahead. Saved labels are not rewritten to guessed exchange closes.";
const nativeContext: TraceContext = {
  ...detail().context,
  timestamp_semantics: "native_daily_session_labels",
  timestamp_caveat: nativeCaveat,
};
const props: ReplayChartProps = {
  bars: [
    {
      listing_id: ids.strategy,
      session_date: "2024-01-17",
      open: 12,
      high: 12.1,
      low: 11.9,
      close: 12,
      volume: 2000,
      volume_unit: "shares",
      currency: "USD",
      flags: [],
    },
  ],
  features: [],
  instrument: "B",
  cutoff: "2024-01-17T05:00:00Z",
  decisionTime: "2024-01-17T05:00:00Z",
  timezone: "America/New_York",
  context: nativeContext,
};
const render = (node: ReturnType<typeof createElement>) =>
  renderToStaticMarkup(node);

describe("admitted replay timestamp semantics", () => {
  it("labels corrected native decision/cutoff coordinates and retains exact saved values", () => {
    const decision = {
      ...detail().decision!,
      session_date: "2024-01-17",
      decision_time: props.decisionTime,
      information_cutoff: props.cutoff,
      market_timezone: props.timezone,
    };
    const html = render(
      createElement(ReplayDecisionIdentity, {
        decision,
        context: nativeContext,
      }),
    );
    expect(html).toContain("Information cutoff (session label)");
    expect(html).toContain("Decision session label");
    expect(html).toContain(props.cutoff);
    expect(html).toContain("17/01/2024, 00:00:00");
    expect(html).toContain(nativeCaveat);
    expect(html).not.toContain("16:00:00");
  });

  it("shows the caveat beside feature cutoffs in both replay and selected-rule observations", () => {
    const value = detail();
    value.context = nativeContext;
    const featurePanel = render(
      createElement(ConditionsAndFeatures, {
        detail: value,
        selection: {},
        update: () => undefined,
      }),
    );
    const rulePanel = render(
      createElement(RuleCards, {
        spec: spec.spec,
        detail: value,
        selectedRule: "filter",
        onSelect: () => undefined,
      }),
    );
    for (const html of [featurePanel, rulePanel]) {
      expect(html).toContain("Information cutoff (session label)");
      expect(html).toContain(nativeCaveat);
      expect(html).toContain("2024-01-02T07:00:00Z");
    }
  });

  it("discloses native session labels in chart caption, accessibility and markers without shifting coordinates", () => {
    const options = replayChartOptions(props);
    const html = render(createElement(ReplayChart, props));
    expect(html).toContain(nativeCaveat);
    expect(html).toContain("saved market-local midnight session labels");
    expect(html).not.toContain("exact saved instants");
    expect(html.match(/aria-description="([^"]*)"/)?.[1]).toContain(
      nativeCaveat,
    );
    expect(options.aria.description).toBe(
      "Retrospective market prices, information cutoff, decision time and actually saved feature points",
    );
    expect(options.xAxis[0].name).toContain("not availability instants");
    const first = options.series[0];
    expect("markLine" in first ? first.markLine.data : []).toMatchObject([
      {
        name: "Information cutoff (session label)",
        xAxis: Date.parse(props.cutoff),
      },
      {
        name: "Decision session label",
        xAxis: Date.parse(props.decisionTime),
      },
    ]);
    expect(first.data[0][0]).toBe(Date.parse(props.bars[0].session_date));
  });

  it("preserves portable engine-instant semantics even at market-local midnight", () => {
    const context: TraceContext = {
      ...detail().context,
      timestamp_semantics: "portable_engine_instants",
      timestamp_caveat: null,
    };
    const timing = replayTimestampPresentation(context);
    expect(timing.cutoffLabel).toBe("Information cutoff");
    expect(timing.decisionLabel).toBe("Decision time");
    expect(timing.caveat).toBeNull();
    const html = render(createElement(ReplayChart, { ...props, context }));
    expect(html).toContain("exact saved instants");
    expect(html).not.toContain("market-local midnight session labels");
    const options = replayChartOptions({ ...props, context });
    expect(options.xAxis[0].name).toBe(
      "UTC timestamp · market zone shown in evidence",
    );
    expect(html.match(/aria-description="([^"]*)"/)?.[1]).not.toContain(
      "unverified",
    );
  });

  it.each(["native_recorded_unqualified", "unknown", undefined] as const)(
    "does not reinterpret historical or absent semantics: %s",
    (semantics) => {
      const context: TraceContext = {
        ...detail().context,
        timestamp_semantics: semantics,
      };
      const timing = replayTimestampPresentation(context);
      expect(timing.instants).toBe(false);
      expect(timing.cutoffLabel).toContain("timing unverified");
      const html = render(createElement(ReplayChart, { ...props, context }));
      expect(html).not.toContain("exact saved instants");
      expect(html).not.toContain("market-local midnight session labels");
      expect(html).toContain(timing.caveat!);
      expect(html).toContain("actual data-availability timing is unverified");
      expect(replayChartOptions({ ...props, context }).xAxis[0].name).toContain(
        "availability unverified",
      );
    },
  );

  it("does not silently drop native caveats when an older service omits the text", () => {
    const context = { ...nativeContext, timestamp_caveat: null };
    expect(replayTimestampPresentation(context).caveat).toContain(
      "does not prove availability at midnight",
    );
  });
});
