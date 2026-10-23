import { describe, expect, it } from "vitest";
import type { UploadMapping } from "../../src/client/types.gen";
import {
  portableUploadAssumptionError,
  portableUploadDeclaration,
} from "../../src/lib/portable-upload";

function mapping(): UploadMapping {
  return {
    symbol: "AAPL",
    mic: "XNAS",
    market: "US",
    currency: "USD",
    timezone: "America/New_York",
    calendar: "XNYS",
    start_date: "2024-01-02",
    end_date: "2024-02-01",
    columns: {},
    volume_unit: "shares",
    price_semantics: {
      native_mode: "raw",
      split_basis: "unadjusted",
      dividend_basis: "unadjusted",
    },
  };
}
function declared(): UploadMapping {
  const value = mapping();
  return {
    ...value,
    portable_declaration: portableUploadDeclaration(
      value,
      true,
      "public document reference",
      "User assumption, not independent proof",
    ),
  };
}

describe("explicit portable upload consent", () => {
  it("defaults to no declaration and never infers absence from raw prices", () => {
    expect(
      portableUploadDeclaration(mapping(), false, "reference", "note"),
    ).toBeNull();
    expect(portableUploadAssumptionError(mapping())).toBeNull();
  });
  it("binds exact current identity and dates without mutating price semantics", () => {
    const value = mapping();
    const before = JSON.stringify(value);
    const claim = portableUploadDeclaration(value, true, " source ", " note ");
    expect(JSON.stringify(value)).toBe(before);
    expect(claim).toMatchObject({
      symbol: "AAPL",
      mic: "XNAS",
      start: "2024-01-02",
      end: "2024-02-01",
      source_reference: "source",
      evidence_note: "note",
      acknowledged_unverified: true,
    });
    expect(
      portableUploadAssumptionError({ ...value, portable_declaration: claim }),
    ).toBeNull();
  });
  it.each(["unknown", "adjusted"] as const)(
    "rejects %s price bases rather than rewriting them",
    (basis) => {
      const value = declared();
      value.price_semantics.split_basis = basis;
      expect(portableUploadAssumptionError(value)).toContain(
        "No semantics are changed",
      );
      expect(value.price_semantics.split_basis).toBe(basis);
    },
  );
  it.each([
    "source",
    "note",
    "changed_identity",
    "changed_range",
    "unknown_unit",
    "unknown_currency",
    "overlong",
    "control",
  ])("rejects incomplete or changed %s declarations", (change) => {
    const value = declared();
    const claim = value.portable_declaration!;
    if (change === "source") claim.source_reference = " ";
    if (change === "note") claim.evidence_note = " ";
    if (change === "changed_identity") value.symbol = "MSFT";
    if (change === "changed_range") value.end_date = "2024-03-01";
    if (change === "unknown_unit") value.volume_unit = "unknown";
    if (change === "unknown_currency") value.currency = "unknown";
    if (change === "overlong") claim.evidence_note = "x".repeat(2001);
    if (change === "control") claim.evidence_note = "bad\ncontrol";
    expect(portableUploadAssumptionError(value)).not.toBeNull();
  });
  it("turning consent off removes the declaration while retaining native upload semantics", () => {
    const value = declared();
    value.price_semantics.split_basis = "unknown";
    value.portable_declaration = portableUploadDeclaration(
      value,
      false,
      "source",
      "note",
    );
    expect(value.portable_declaration).toBeNull();
    expect(value.price_semantics.split_basis).toBe("unknown");
    expect(portableUploadAssumptionError(value)).toBeNull();
  });
});
