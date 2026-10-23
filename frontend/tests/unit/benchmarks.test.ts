import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  benchmarkKey,
  benchmarkOptions,
  benchmarkSchema,
  benchmarkSelectionIssue,
} from "../../src/lib/benchmarks";
import { BenchmarkSelector } from "../../src/components/benchmark-selector";
import { BenchmarkResult } from "../../src/components/benchmark-result";
import { researchFixture } from "./research-fixtures";
import { researchFormSchema } from "../../src/lib/research-form";
import { researchFingerprint } from "../../src/lib/research-submit";

const listingId = "00000000-0000-4000-8000-000000000001";
function selection() {
  const { body, catalog } = researchFixture();
  const dataset = {
    ...catalog.datasets[0],
    id: "00000000-0000-4000-8000-000000000002",
    manifest: {
      ...catalog.datasets[0].manifest,
      listings: [{ listing_id: listingId, symbol: "DECLARED" }],
    },
  };
  const option = benchmarkOptions([dataset], "CN")[0];
  return { body, dataset, option };
}
describe("explicit immutable benchmark", () => {
  it("requires exact dataset/listing/hash and never silently selects strategy input", () => {
    const { dataset, option } = selection();
    expect(benchmarkSchema.parse(null)).toBeNull();
    expect(benchmarkOptions([dataset], "US")).toEqual([]);
    expect(benchmarkSelectionIssue(option.value, [dataset], "CN")).toBeNull();
    expect(benchmarkSelectionIssue(option.value, [], "CN")).toContain(
      "unavailable",
    );
    expect(
      benchmarkSelectionIssue(
        {
          ...option.value,
          dataset: { ...option.value.dataset, sha256: "f".repeat(64) },
        },
        [dataset],
        "CN",
      ),
    ).toContain("hash");
    expect(benchmarkKey(option.value)).toBe(`${dataset.id}:${listingId}`);
    expect(
      benchmarkSchema.safeParse({ ...option.value, price_field: "adj_close" })
        .success,
    ).toBe(false);
  });
  it("pins selection in strict reloadable form identity and invalidates preflight", () => {
    const { body, option } = selection();
    const selected = researchFormSchema.parse({
      ...body,
      benchmark: option.value,
    });
    expect(
      researchFormSchema.parse(JSON.parse(JSON.stringify(selected))).benchmark,
    ).toEqual(option.value);
    expect(researchFingerprint(selected)).not.toBe(researchFingerprint(body));
    expect(researchFormSchema.parse(body).benchmark).toBeNull();
  });
  it("renders explicit None, synthetic label, immutable hashes, and restrictions", () => {
    const { dataset, option } = selection();
    const html = renderToString(
      createElement(BenchmarkSelector, {
        datasets: [dataset],
        market: "CN",
        value: option.value,
        onChange: () => undefined,
      }),
    );
    expect(html).toContain("None · no comparison requested");
    expect(html).toContain(dataset.content_sha256);
    expect(html).toContain("never fabricated starting capital");
    expect(html).toContain(
      "not dividends, total return or a cost-matched portfolio",
    );
    expect(html).toContain("Holdout");
  });
  it("renders historical None honestly and rejects incomplete comparison evidence", () => {
    const html = renderToString(
      createElement(BenchmarkResult, {
        value: {
          status: "not_configured",
          series: null,
          reason: "No requested input",
        },
      }),
    );
    expect(html).toContain("strategy data is not substituted");
    const invalid = renderToString(
      createElement(BenchmarkResult, { value: { status: "available" } }),
    );
    expect(invalid).toContain("invalid or incomplete");
    expect(invalid).not.toContain('role="img"');
  });
});
