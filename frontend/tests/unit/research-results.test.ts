import { describe, expect, it, vi, afterEach } from "vitest";
import type { AttemptPublic } from "../../src/client/types.gen";
import {
  chartSeries,
  comparisonIds,
  metricText,
  rawCell,
  researchControls,
  researchTerminal,
  researchTrustNotice,
  resultWindowError,
  safeDownloadName,
  saveResearchDownload,
} from "../../src/lib/research-results";

const one = "a1111111-1111-4111-8111-111111111111";
const two = "b2222222-2222-4222-8222-222222222222";
const three = "c3333333-3333-4333-8333-333333333333";
const stopped = { sandbox_stopped: true } as AttemptPublic;
const running = { sandbox_stopped: false } as AttemptPublic;
afterEach(() => vi.unstubAllGlobals());

describe("durable research controls", () => {
  it.each(["queued", "preparing", "running"] as const)(
    "only pauses active research (%s)",
    (status) => {
      expect(
        researchControls({
          status,
          attempts: [running],
          latest_checkpoint_id: one,
        }),
      ).toMatchObject({ pause: true, cancel: true, resume: false });
    },
  );
  it("does not call a pause request paused or expose duplicate commands", () => {
    expect(researchTerminal.has("pause_requested")).toBe(false);
    expect(
      researchControls({ status: "pause_requested", attempts: [running] }),
    ).toMatchObject({ pause: false, cancel: true, resume: false });
    expect(
      researchControls({ status: "cancel_requested", attempts: [running] }),
    ).toMatchObject({ pause: false, cancel: false, resume: false });
  });
  it.each(["paused", "failed", "cancelled", "interrupted"] as const)(
    "requires a checkpoint AND stopped attempt to resume %s",
    (status) => {
      expect(researchControls({ status, attempts: [stopped] }).resume).toBe(
        false,
      );
      expect(
        researchControls({
          status,
          attempts: [running],
          latest_checkpoint_id: one,
        }).resume,
      ).toBe(false);
      expect(
        researchControls({
          status,
          attempts: [stopped],
          latest_checkpoint_id: one,
        }).resume,
      ).toBe(true);
    },
  );
  it("does not restart success, and discloses independent sandbox verification", () => {
    expect(
      researchControls({
        status: "succeeded",
        attempts: [stopped],
        latest_checkpoint_id: one,
      }).resume,
    ).toBe(false);
    expect(
      researchControls({
        status: "paused",
        attempts: [stopped],
        latest_checkpoint_id: one,
      }).resumeReason,
    ).toContain("every evaluation sandbox");
  });
});

describe("honest result units and missing data", () => {
  it("does not multiply stored percentages by 100 or erase zero", () => {
    expect(metricText({ value: 12.34, unit: "percent" })).toBe("12.34%");
    expect(metricText({ value: 0, unit: "percent" })).toBe("0%");
    expect(metricText({ value: 0.1, unit: "fraction" })).toBe("0.1 fraction");
    expect(metricText({ value: 100, unit: "USD" })).toBe("100 USD");
  });
  it.each([null, NaN, Infinity])(
    "never turns missing/nonfinite metric %s into zero",
    (value) => expect(metricText({ value, unit: "ratio" })).toBe("Unavailable"),
  );
  it("preserves raw value precision and unknowns", () => {
    expect(rawCell(0.123456789123)).toBe("0.123456789123");
    expect(rawCell(0)).toBe("0");
    expect(rawCell(null)).toBe("Unavailable");
    expect(rawCell(false)).toBe("false");
    expect(researchTrustNotice).toContain("UNTRUSTED");
    expect(researchTrustNotice).toContain(
      "not an independently trusted ledger",
    );
  });
  it("preserves gaps and allows explicitly named categorical comparison axes", () => {
    expect(
      chartSeries({
        columns: [
          { name: "timestamp", dtype: "datetime" },
          { name: "drawdown", dtype: "float", unit: "fraction" },
        ],
        rows: [
          ["2024-01-01", 0],
          ["2024-01-02", null],
          ["2024-01-03", Infinity],
        ],
      })?.series[0].values,
    ).toEqual([0, null, null]);
    const table = {
      columns: [
        { name: "evaluation", dtype: "string" },
        { name: "score", dtype: "float", unit: "ratio" },
      ],
      rows: [
        [one, 0],
        [two, 1],
      ],
    };
    expect(chartSeries(table)).toBeNull();
    expect(chartSeries(table, "evaluation")?.labels).toEqual([one, two]);
  });
  it("does not invent numeric data or a date axis", () => {
    expect(
      chartSeries({
        columns: [{ name: "value", dtype: "float" }],
        rows: [[1]],
      }),
    ).toBeNull();
    expect(
      chartSeries({
        columns: [{ name: "date", dtype: "date" }],
        rows: [["2024-01-01"]],
      }),
    ).toBeNull();
  });
});

describe("bounded comparison and chart filters", () => {
  it("keeps selection order and rejects duplicates and malformed IDs", () => {
    expect(comparisonIds(` ${two}, ${one},${three} `)).toEqual({
      ids: [two, one, three],
      error: null,
    });
    expect(comparisonIds(`${one},${one}`).error).toMatch(/distinct/);
    expect(comparisonIds(`${one},javascript:alert(1)`).ids).toEqual([]);
  });
  it.each([
    undefined,
    "",
    one,
    [one, two],
    `${one},${two},${three},${one},${two}`,
  ])("blocks unbounded or incomplete selection %s", (value) =>
    expect(comparisonIds(value).error).not.toBeNull(),
  );
  it("permits full window but rejects incomplete, reversed and impossible dates", () => {
    expect(resultWindowError("", "")).toBeNull();
    expect(resultWindowError("2024-01-01", "")).not.toBeNull();
    expect(resultWindowError("2024-02-30", "2024-03-01")).not.toBeNull();
    expect(resultWindowError("2024-03-01", "2024-03-01")).not.toBeNull();
    expect(resultWindowError("2024-02-29", "2024-03-01")).toBeNull();
  });
});

describe("artifact downloads never execute report markup", () => {
  it("cleans path separators and control characters in download filenames", () => {
    expect(safeDownloadName("../bad\\name\u0000.html")).toBe(
      ".._bad_name_.html",
    );
    expect(safeDownloadName("")).toBe("research-artifact");
    expect(safeDownloadName("a".repeat(200))).toHaveLength(160);
  });
  it("uses blob download and revokes it, without window navigation or embedding", () => {
    vi.useFakeTimers();
    const anchor = { href: "", download: "", click: vi.fn() };
    const create = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:safe-download");
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockReturnValue();
    vi.stubGlobal("document", { createElement: vi.fn(() => anchor) });
    saveResearchDownload(
      new Blob(["<script>not executed</script>"], { type: "text/html" }),
      "report.html",
    );
    expect(anchor).toMatchObject({
      href: "blob:safe-download",
      download: "report.html",
    });
    expect(anchor.click).toHaveBeenCalledOnce();
    expect(create).toHaveBeenCalledOnce();
    vi.runAllTimers();
    expect(revoke).toHaveBeenCalledWith("blob:safe-download");
    create.mockRestore();
    revoke.mockRestore();
    vi.useRealTimers();
  });
});

describe("dataset and model evidence are independent", () => {
  it("keeps fixture models explicit even with a non-synthetic dataset", async () => {
    const { resultEvidence } = await import("../../src/lib/research-results");
    const evidence = resultEvidence({
      is_synthetic: false,
      config: { model: { provider: "fixture" } },
    });
    expect(evidence.dataset).toContain("non-synthetic");
    expect(evidence.model).toContain("Deterministic model fixture");
    expect(evidence.model).toContain("not live-model");
  });
});
