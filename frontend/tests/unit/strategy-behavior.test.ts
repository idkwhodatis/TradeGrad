import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StrategyCreate } from "../../src/client/types.gen";
import {
  clearStrategyDraft,
  emptyStrategy,
  isDefinitiveStrategyFailure,
  loadStrategyDraft,
  MAX_STRATEGY_BYTES,
  readStrategyUpload,
  saveStrategyDraft,
  strategyBytes,
  strategyInputError,
  strategyOrigin,
  strategySubmissionFor,
} from "../../src/lib/strategy-versions";

const body: StrategyCreate = {
  name: "Native",
  task: "cn_time",
  code: "value = 1\r\n",
  parent_id: null,
};
beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
});

describe("immutable native strategy submissions", () => {
  it("requires named, bounded source without assuming executable safety", () => {
    expect(strategyInputError(body)).toBeNull();
    expect(
      strategyInputError({ ...body, code: "import arbitrary_module\n" }),
    ).toBeNull();
    expect(strategyInputError(emptyStrategy())).toContain("name");
  });
  it.each([
    { ...body, name: " " },
    { ...body, name: "x".repeat(256) },
    { ...body, name: "line\nbreak" },
    { ...body, code: " " },
    { ...body, code: "\0" },
    { ...body, code: "#" + "中".repeat(50_000) },
    { ...body, code: "#\ud800" },
    { ...body, task: "mixed" as StrategyCreate["task"] },
  ])("rejects invalid authoring input", (input) =>
    expect(strategyInputError(input)).not.toBeNull(),
  );
  it("measures UTF-8 bytes rather than JavaScript characters", () => {
    expect(strategyBytes("中😀")).toBe(7);
    expect(
      strategyInputError({
        ...body,
        code: "#" + "a".repeat(MAX_STRATEGY_BYTES - 1),
      }),
    ).toBeNull();
  });
  it("reuses the exact body key and differentiates line endings, name, parent and task", () => {
    const first = strategySubmissionFor(body);
    expect(strategySubmissionFor({ ...body }, first)).toBe(first);
    expect(
      strategySubmissionFor({ ...body, parent_id: undefined }, first),
    ).toBe(first);
    for (const change of [
      { code: "value = 1\n" },
      { name: "Renamed" },
      { parent_id: crypto.randomUUID() },
      { task: "us_time" as const },
    ])
      expect(strategySubmissionFor({ ...body, ...change }, first).key).not.toBe(
        first.key,
      );
  });
  it("copies request input so later form mutation cannot corrupt the saved body", () => {
    const current = { ...body };
    const submission = strategySubmissionFor(current);
    current.code = "changed = True\n";
    expect(submission.body.code).toBe(body.code);
  });
  it("never guesses upstream provenance from a display name", () => {
    expect(strategyOrigin({ parent_id: null })).toContain(
      "origin not verified",
    );
    expect(strategyOrigin({ parent_id: crypto.randomUUID() })).toContain(
      "Derived",
    );
  });
});

describe("owner-scoped draft and ambiguous-save recovery", () => {
  it("restores the exact pending body and key after reload without sharing another account", () => {
    const draft = { body, pending: strategySubmissionFor(body) };
    saveStrategyDraft("owner-a", draft);
    expect(loadStrategyDraft("owner-a")).toEqual(draft);
    expect(loadStrategyDraft("owner-b")).toBeNull();
    clearStrategyDraft("owner-a");
    expect(loadStrategyDraft("owner-a")).toBeNull();
  });
  it("keeps empty and syntax-invalid local drafts editable", () => {
    const draft = { body: emptyStrategy(), pending: null };
    saveStrategyDraft("owner", draft);
    expect(loadStrategyDraft("owner")).toEqual(draft);
    saveStrategyDraft("owner", {
      body: { ...body, code: "def invalid(\n" },
      pending: null,
    });
    expect(loadStrategyDraft("owner")?.body.code).toBe("def invalid(\n");
  });
  it.each([
    "{corrupt",
    JSON.stringify({
      body,
      pending: { key: "a", body: { ...body, name: "different" } },
    }),
    JSON.stringify({ body, pending: { key: "with space", body } }),
    JSON.stringify({
      body: { ...body, code: "#".repeat(MAX_STRATEGY_BYTES + 1) },
      pending: null,
    }),
    JSON.stringify({ body: { ...body, task: "portable" }, pending: null }),
  ])("fails closed on corrupt or mismatched persisted requests", (raw) => {
    sessionStorage.setItem("tradegrad.strategy-draft.v1.owner", raw);
    expect(loadStrategyDraft("owner")).toBeNull();
  });
  it("does not silently ignore a storage write failure before a network mutation", () => {
    vi.stubGlobal("sessionStorage", {
      setItem: () => {
        throw new Error("quota");
      },
    });
    expect(() =>
      saveStrategyDraft("owner", {
        body,
        pending: strategySubmissionFor(body),
      }),
    ).toThrow("quota");
  });
  it.each([
    new Error("network"),
    { code: "internal_error" },
    { name: "TimeoutError" },
    undefined,
  ])("keeps uncertain outcomes frozen for exact replay", (error) => {
    expect(isDefinitiveStrategyFailure(error)).toBe(false);
  });
  it.each([
    "strategy_syntax_error",
    "validation_error",
    "not_found",
    "idempotency_conflict",
    "incompatible_parent",
  ])("allows correction after definitive %s rejection", (code) => {
    expect(isDefinitiveStrategyFailure({ code })).toBe(true);
  });
});

function file(name: string, content: Uint8Array, size = content.byteLength) {
  return { name, size, arrayBuffer: async () => content.slice().buffer };
}
describe("Python file upload", () => {
  it("preserves exact UTF-8 source including CRLF, trailing blanks and Unicode", async () => {
    const code = '# 中文\r\nvalue = "é"\r\n\r\n';
    await expect(
      readStrategyUpload(file("source.py", new TextEncoder().encode(code))),
    ).resolves.toBe(code);
  });
  it("does not strip UTF-8 BOM bytes or silently replace invalid encoding", async () => {
    await expect(
      readStrategyUpload(
        file("bom.py", new TextEncoder().encode("\ufeff# exact\n")),
      ),
    ).resolves.toBe("\ufeff# exact\n");
    await expect(
      readStrategyUpload(file("invalid.py", new Uint8Array([0xff]))),
    ).rejects.toThrow("UTF-8");
  });
  it("rejects non-Python, empty, oversized and inconsistent file bytes", async () => {
    await expect(
      readStrategyUpload(file("source.txt", new Uint8Array([35]))),
    ).rejects.toThrow(".py");
    await expect(
      readStrategyUpload(file("source.py", new Uint8Array())),
    ).rejects.toThrow("nonempty");
    await expect(
      readStrategyUpload(
        file("source.py", new Uint8Array([35]), MAX_STRATEGY_BYTES + 1),
      ),
    ).rejects.toThrow("128 KiB");
    await expect(
      readStrategyUpload(
        file("source.py", new Uint8Array(MAX_STRATEGY_BYTES + 1), 1),
      ),
    ).rejects.toThrow("128 KiB");
  });
});
