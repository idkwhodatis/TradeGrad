import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const bytes = readFileSync(
  new URL("../fixtures/research-cn.csv", import.meta.url),
);
const metadata = JSON.parse(
  readFileSync(
    new URL("../fixtures/research-cn.json", import.meta.url),
    "utf8",
  ),
);
const hash = (value: Buffer) =>
  createHash("sha256").update(value).digest("hex");
describe("configured browser fixture provenance", () => {
  it("pins synthetic bytes and their original baseline source without claiming live data", () => {
    expect(hash(bytes)).toBe(metadata.sha256);
    const source = readFileSync(
      new URL("../../../tests/baseline/fixtures/CN/INDEX.csv", import.meta.url),
    );
    expect(hash(source)).toBe(metadata.source_sha256);
    expect(metadata.synthetic).toBe(true);
    expect(metadata.purpose).toContain("not observed market data");
  });
  it("keeps ordered calendar-filtered sessions inside the declared half-open interval", () => {
    const lines = bytes.toString("utf8").trim().split("\n");
    const dates = lines.slice(1).map((line) => line.split(",")[0]);
    expect(dates.length).toBe(metadata.rows);
    expect(new Set(dates).size).toBe(dates.length);
    expect(dates).toEqual([...dates].sort());
    expect(
      dates.every((date) => date >= "2024-01-02" && date < "2024-04-27"),
    ).toBe(true);
    expect(metadata.filter).toContain("exchange_calendars 4.13.2 XSHG");
  });
});
