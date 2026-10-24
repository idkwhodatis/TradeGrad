import { describe, expect, it, vi } from "vitest";
import {
  noteHttpDiagnostic,
  noteHttpFields,
} from "../e2e/research-note-diagnostics";

const prefix = "RESEARCH_NOTE_HTTP_DIAGNOSTIC_V1:";
const response = (status: number, body: unknown) => ({
  status: () => status,
  text: vi.fn(async () => JSON.stringify(body)),
});
const read = (value: string) => {
  expect(value.startsWith(prefix)).toBe(true);
  return JSON.parse(value.slice(prefix.length));
};

describe("bounded private-to-public note HTTP diagnostics, not browser acceptance", () => {
  it("does not read a successful note body or include source values", async () => {
    const value = response(201, { content: "private note content" });
    expect(
      read(await noteHttpDiagnostic(value, "research_note_create")),
    ).toEqual({
      category: "research_note_create",
      http_status: 201,
      error_code: null,
      validation_fields: [],
    });
    expect(value.text).not.toHaveBeenCalled();
  });

  it("retains only known validation field names, never messages, values or indexes", async () => {
    const privateValue = crypto.randomUUID();
    const value = response(422, {
      code: "validation_error",
      message: privateValue,
      correlation_id: privateValue,
      details: [
        {
          loc: [
            "body",
            "content",
            "evidence",
            0,
            "source",
            "source_id",
            privateValue,
          ],
          input: privateValue,
          msg: privateValue,
        },
        { loc: ["content", "evidence"] },
        { loc: { private: privateValue } },
      ],
    });
    const text = await noteHttpDiagnostic(value, "research_note_create");
    expect(text).not.toContain(privateValue);
    expect(read(text)).toEqual({
      category: "research_note_create",
      http_status: 422,
      error_code: "validation_error",
      validation_fields: ["body", "content", "evidence", "source_id"],
    });
  });

  it("preserves a known domain enum and ignores unrelated detail fields", async () => {
    expect(
      read(
        await noteHttpDiagnostic(
          response(503, {
            code: "database_unavailable",
            details: [{ loc: ["content"] }],
          }),
          "controlled_note_create",
        ),
      ),
    ).toEqual({
      category: "controlled_note_create",
      http_status: 503,
      error_code: "database_unavailable",
      validation_fields: [],
    });
  });

  it.each([
    { code: "private response value" },
    { code: ["validation_error"] },
    ["private response value"],
    null,
    { code: "validation_error", message: "x".repeat(65_536) },
  ])("maps unreadable or unknown bodies to a constant enum", async (body) => {
    expect(
      read(
        await noteHttpDiagnostic(response(500, body), "research_note_revision"),
      ),
    ).toEqual({
      category: "research_note_revision",
      http_status: 500,
      error_code: "unrecognized",
      validation_fields: [],
    });
  });

  it("does not leak unreadable text or exceptions", async () => {
    const privateValue = crypto.randomUUID();
    for (const text of [
      async () => `<html>${privateValue}</html>`,
      async () => {
        throw new Error(privateValue);
      },
    ]) {
      const output = await noteHttpDiagnostic(
        { status: () => 502, text },
        "research_note_create",
      );
      expect(output).not.toContain(privateValue);
      expect(read(output).error_code).toBe("unrecognized");
    }
  });

  it("bounds validation detail traversal, location depth and output cardinality", async () => {
    const output = read(
      await noteHttpDiagnostic(
        response(422, {
          code: "validation_error",
          details: [
            ...noteHttpFields.slice(0, 64).map((field) => ({ loc: [field] })),
            { loc: ["title"] },
          ],
        }),
        "research_note_create",
      ),
    );
    expect(output.validation_fields).toHaveLength(16);
    expect(output.validation_fields).toEqual(
      [...noteHttpFields].sort().slice(0, 16),
    );
    const deep = read(
      await noteHttpDiagnostic(
        response(422, {
          code: "validation_error",
          details: [{ loc: [...Array(16).fill("private"), "content"] }],
        }),
        "research_note_create",
      ),
    );
    expect(deep.validation_fields).toEqual([]);
  });
});
