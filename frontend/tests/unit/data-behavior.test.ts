import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearDataSubmission,
  dataSubmissionKey,
  dateWindowError,
  importTerminal,
  parseSymbols,
  stableJson,
  universeEvidenceError,
  universeLabels,
  valueText,
} from "../../src/lib/data-utils";

beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => data.set(key, value),
    removeItem: (key: string) => data.delete(key),
  });
});

describe("data workflow boundaries", () => {
  it("preserves unknown price semantics without claiming unadjusted prices", () => {
    expect(valueText(null)).toBe("Unknown");
    expect(valueText("unknown")).toBe("unknown");
    expect(valueText(false)).toBe("false");
  });
  it("normalizes symbol separators without changing a provider symbol's case", () =>
    expect(parseSymbols("AAPL, MSFT;AAPL\nBRK-B\t000001")).toEqual([
      "AAPL",
      "MSFT",
      "BRK-B",
      "000001",
    ]));
  it.each([
    ["2024-02-30", "2024-03-02"],
    ["2024-01-02", "2024-01-02"],
    ["2024-02-02", "2024-01-01"],
    ["2024-1-2", "2024-02-01"],
  ])("rejects invalid or empty exclusive windows", (start, end) =>
    expect(dateWindowError(start, end)).not.toBeNull(),
  );
  it("accepts leap days and exclusive end", () =>
    expect(dateWindowError("2024-02-29", "2024-03-01")).toBeNull());
  it("does not describe current or historical lists as verified PIT", () => {
    expect(universeLabels.current_snapshot).toContain("not historical PIT");
    expect(universeLabels.historical_membership).toContain("unverified");
    expect(universeLabels.user_fixed).toContain("not verified PIT");
  });
  it("stops polling at partial or cancelled without pretending ready", () => {
    expect(importTerminal.has("partial")).toBe(true);
    expect(importTerminal.has("cancelled")).toBe(true);
    expect(importTerminal.has("validating")).toBe(false);
  });
});

describe("owner-scoped import idempotency", () => {
  const body = {
    provider: "yfinance",
    symbols: ["AAPL"],
    start: "2024-01-02",
    mapping: { open: "Open", close: "Close" },
  };
  it("reuses keys across uncertain retries and differently ordered properties", () => {
    const key = dataSubmissionKey("one", "import", body);
    expect(
      dataSubmissionKey("one", "import", {
        ...body,
        mapping: { close: "Close", open: "Open" },
      }),
    ).toBe(key);
    expect(stableJson({ a: 1, b: 2 })).toBe(stableJson({ b: 2, a: 1 }));
  });
  it("separates owners, operations and changed payloads", () => {
    const key = dataSubmissionKey("one", "import", body);
    expect(dataSubmissionKey("two", "import", body)).not.toBe(key);
    expect(dataSubmissionKey("one", "refresh", body)).not.toBe(key);
    expect(
      dataSubmissionKey("one", "import", { ...body, start: "2024-02-01" }),
    ).not.toBe(key);
  });
  it("clears only after acknowledgement, so an intentional new import can fetch revisions", () => {
    const key = dataSubmissionKey("one", "import", body);
    clearDataSubmission("one", "import");
    expect(dataSubmissionKey("one", "import", body)).not.toBe(key);
  });
});

describe("snapshot evidence labels", () => {
  it("never presents a provider fixture as a live provider download", async () => {
    const { snapshotLabel } = await import("../../src/lib/data-utils");
    expect(
      snapshotLabel({
        is_synthetic: true,
        source: "openbb",
        manifest: { provenance: [{ evidence: "fixture" }] },
      }),
    ).toBe("Provider fixture · not live");
    expect(
      snapshotLabel({
        is_synthetic: false,
        source: "yfinance",
        manifest: { provenance: [{ evidence: "live" }] },
      }),
    ).toBe("Live-provider snapshot");
    expect(
      snapshotLabel({ is_synthetic: false, source: "upload", manifest: {} }),
    ).toBe("User upload · unverified source");
    expect(
      snapshotLabel({
        is_synthetic: true,
        source: "synthetic_fixture",
        manifest: {},
      }),
    ).toBe("Synthetic demo");
  });
});

describe("bounded user-supplied universe evidence", () => {
  it.each(["current_snapshot", "historical_membership"])(
    "requires a nonblank reference for %s",
    (state) => {
      expect(universeEvidenceError(state, "   ", "")).toContain(
        "nonempty evidence reference",
      );
      expect(
        universeEvidenceError(
          state,
          "announcement 2024-01",
          "Unverified user source",
        ),
      ).toBeNull();
    },
  );
  it("permits an explicit fixed basket without pretending it has external evidence", () => {
    expect(universeEvidenceError("user_fixed", "", "")).toBeNull();
    expect(
      universeEvidenceError("user_fixed", "", "An orphan note"),
    ).not.toBeNull();
  });
  it("bounds the whole serialized record, including Unicode escaping and notes", () => {
    expect(
      universeEvidenceError("current_snapshot", "source", "x".repeat(16_384)),
    ).toContain("16 KiB");
    expect(
      universeEvidenceError("current_snapshot", "源".repeat(2800), ""),
    ).toContain("16 KiB");
    expect(
      universeEvidenceError(
        "historical_membership",
        "公开来源",
        "有日期的历史记录",
      ),
    ).toBeNull();
  });
});
