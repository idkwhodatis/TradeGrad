import { expect, it } from "vitest";
import { nativeContractSchema } from "../../src/lib/research-form";
import { strategySubmissionFor } from "../../src/lib/strategy-versions";
const manifest = {
  lookback_bars: 20,
  warmup_bars: 20,
  requires_shorting: false,
  parameters: [
    {
      name: "n",
      kind: "integer" as const,
      value: 20,
      minimum: 1,
      maximum: 252,
    },
  ],
};
it("declared params are detached in the frozen pending body and change retry identity", () => {
  const body = {
    name: "x",
    task: "cn_time" as const,
    code: "# inert",
    manifest: structuredClone(manifest),
  };
  const first = strategySubmissionFor(body);
  body.manifest.parameters[0].value = 30;
  expect(first.body.manifest?.parameters?.[0].value).toBe(20);
  expect(strategySubmissionFor(body, first).key).not.toBe(first.key);
});
it("browser declarations accept amount but reject unknown fields/types", () => {
  expect(
    nativeContractSchema.parse({ ...manifest, required_fields: ["amount"] })
      .required_fields,
  ).toEqual(["amount"]);
  expect(
    nativeContractSchema.safeParse({ ...manifest, lookback_bars: 253 }).success,
  ).toBe(false);
  expect(
    nativeContractSchema.safeParse({ ...manifest, requires_shorting: "false" })
      .success,
  ).toBe(false);
});
