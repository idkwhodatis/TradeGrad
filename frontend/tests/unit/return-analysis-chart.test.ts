import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ResultReturnAnalysis } from "../../src/client/types.gen";

const chart = vi.hoisted(() => ({
  setOption: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
}));
const lifecycle = vi.hoisted(() => ({
  cleanup: undefined as (() => void) | undefined,
}));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useRef: () => ({ current: {} }),
  useEffect: (callback: () => () => void) => {
    lifecycle.cleanup = callback();
  },
}));
vi.mock("echarts/core", () => ({ use: vi.fn(), init: () => chart }));
import ResultReturnAnalysisChart from "../../src/components/return-analysis-chart";

const value = {
  months: [{ month: "2024-01", value: 0.02, status: "partial_period" }],
  bins: [{ lower: -0.01, upper: 0.01, count: 2 }],
} as ResultReturnAnalysis;

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});
afterEach(() => {
  lifecycle.cleanup?.();
  vi.unstubAllGlobals();
});

describe("rendered return-analysis accessibility", () => {
  it.each([
    [
      "monthly",
      "Monthly observed equity returns heatmap; asterisk means partial period",
      "heatmap",
    ],
    [
      "distribution",
      "Observed close-to-close return distribution; not certified daily returns",
      "bar",
    ],
  ] as const)(
    "preserves the %s caveat after ECharts replaces the DOM label",
    (kind, description, seriesType) => {
      const element = ResultReturnAnalysisChart({ value, kind });
      expect(element.props.role).toBe("img");
      expect(element.props["aria-label"]).toBe(description);
      expect(chart.setOption).toHaveBeenCalledOnce();
      const options = chart.setOption.mock.calls[0][0];
      expect(options.aria).toEqual({ enabled: true, description });
      expect(options.series[0].type).toBe(seriesType);
      expect(options.animation).toBe(false);
    },
  );
});
