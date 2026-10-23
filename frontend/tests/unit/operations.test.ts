import { describe, expect, it } from "vitest";
import { formatDuration, observationStale } from "../../src/lib/operations";

describe("bounded operational observation presentation", () => {
  it("does not render missing duration as a zero-second success", () => {
    expect(
      formatDuration({
        count: 0,
        unavailable_count: 1,
        mean_seconds: null,
        p95_seconds: null,
        max_seconds: null,
      }),
    ).toContain("Unavailable");
    expect(
      formatDuration({
        count: 2,
        mean_seconds: 1.25,
        p95_seconds: 2,
        max_seconds: 2,
      }),
    ).toBe("1.25s mean · 2.00s p95 · n=2");
  });
  it("expires cached observations and rejects invalid or future timestamps", () => {
    const now = Date.parse("2026-10-23T00:00:00Z");
    expect(observationStale("2026-10-23T00:00:00Z", 60, now)).toBe(false);
    expect(observationStale("2026-10-22T23:58:59Z", 60, now)).toBe(true);
    expect(observationStale("2026-10-23T00:01:00Z", 60, now)).toBe(true);
    expect(observationStale("not-a-date", 60, now)).toBe(true);
  });
});
