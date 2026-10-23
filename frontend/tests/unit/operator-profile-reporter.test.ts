import { createRequire } from "node:module";
import { afterEach, describe, expect, it, vi } from "vitest";

const Reporter = createRequire(import.meta.url)(
  "../../../tests/system/operator_profile_reporter.cjs",
);
afterEach(() => vi.unstubAllEnvs());
const selected = {
  native_akquant: ["configured-research.spec.ts", "@operator-model-profile"],
  portable: ["portable-research.spec.ts", "@portable-operator-profile"],
} as const;

function report(
  mode: keyof typeof selected,
  change: Record<string, unknown> = {},
) {
  const [file, tag] = selected[mode];
  return {
    title: `${tag} bounded profile test`,
    location: { file: `/approved/frontend/tests/e2e/${file}` },
    results: [{ status: "passed" }],
    ...change,
  };
}

async function outcome(mode: string, tests: unknown[], status = "passed") {
  vi.stubEnv("E2E_MODEL_BROWSER_MODE", mode);
  const reporter = new Reporter();
  reporter.onBegin({}, { allTests: () => tests });
  return reporter.onEnd({ status });
}

describe("silent single-success operator browser reporter", () => {
  it.each(["native_akquant", "portable"] as const)(
    "accepts exactly the intended %s case",
    async (mode) => {
      expect(await outcome(mode, [report(mode)])).toEqual({ status: "passed" });
    },
  );
  it.each(["native_akquant", "portable"] as const)(
    "rejects the other mode under %s",
    async (mode) => {
      const other = mode === "portable" ? "native_akquant" : "portable";
      expect(await outcome(mode, [report(other)])).toEqual({
        status: "failed",
      });
    },
  );
  it.each(["", "all", "__proto__", "constructor"])(
    "rejects unsupported selector %s",
    async (mode) => {
      expect(await outcome(mode, [report("portable")])).toEqual({
        status: "failed",
      });
    },
  );
  it.each([
    ["wrong source file", { location: { file: "/approved/other.spec.ts" } }],
    ["wrong tag", { title: "unrelated passing test" }],
    ["skipped", { results: [{ status: "skipped" }] }],
    ["failed", { results: [{ status: "failed" }] }],
    ["retried", { results: [{ status: "failed" }, { status: "passed" }] }],
    ["never ran", { results: [] }],
  ])("rejects %s", async (_label, change) => {
    expect(
      await outcome("portable", [
        report("portable", change as Record<string, unknown>),
      ]),
    ).toEqual({ status: "failed" });
  });
  it("rejects empty, duplicate and globally failed runs", async () => {
    for (const [tests, status] of [
      [[], "passed"],
      [[report("portable"), report("portable")], "passed"],
      [[report("portable")], "failed"],
    ] as const) {
      expect(await outcome("portable", [...tests], status)).toEqual({
        status: "failed",
      });
    }
  });
});
