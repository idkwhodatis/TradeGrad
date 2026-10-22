import { describe, expect, it } from "vitest";
import {
  comparisonSearch,
  researchConfig,
  runDetailRoute,
} from "../../src/lib/navigation";
import { terminalStatuses } from "../../src/lib/format";

const a = "d6f4e598-ae5d-4b38-bfe6-1a3a72769c53";
const b = "fa27479d-cd45-4fa7-899c-eac11d706184";
describe("research route identities", () => {
  it("keeps legacy and configured results separate", () => {
    expect(runDetailRoute({ task: "cn_time" })).toBe("/runs/$runId");
    expect(runDetailRoute({ schema_version: "tradegrad.run.v1" })).toBe(
      "/research-runs/$runId",
    );
    for (const value of [
      undefined,
      null,
      true,
      12,
      [],
      "tradegrad.run.v1",
      { schema_version: "other" },
    ])
      expect(researchConfig(value)).toBe(false);
  });
  it("stops transient streaming for paused runs without losing their resume path", () => {
    expect(terminalStatuses.has("paused")).toBe(true);
    expect(terminalStatuses.has("pause_requested")).toBe(false);
  });
});
describe("bounded comparison selection", () => {
  it("preserves valid selection order and deduplicates canonical UUIDs", () => {
    expect(comparisonSearch(`${a.toUpperCase()},${b},${a}`)).toBe(`${a},${b}`);
  });
  it.each([
    undefined,
    null,
    [],
    {},
    "",
    "javascript:alert(1)",
    "/other",
    "../other",
    `${a},bad`,
    `${a},${a},${a},${a},${a}`,
    `${a},`,
  ])("rejects invalid or excessive input: %s", (value) =>
    expect(comparisonSearch(value)).toBe(""),
  );
});
