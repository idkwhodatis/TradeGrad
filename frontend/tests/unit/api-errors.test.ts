import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

let describeError: typeof import("../../src/lib/api").describeError;
const unreachableMessage =
  "The service could not be reached. Check your connection and try again.";
const timeoutMessage =
  "The request timed out. Retry to check the same submission; a timeout does not mean the operation was not accepted.";

beforeAll(async () => {
  vi.stubGlobal("window", {
    location: { origin: "https://workbench.example" },
  });
  ({ describeError } = await import("../../src/lib/api"));
});
afterAll(() => vi.unstubAllGlobals());

describe("API error guidance", () => {
  it.each([
    "Failed to fetch",
    "NetworkError when attempting to fetch resource.",
    "Load failed",
  ])("normalizes the browser network TypeError: %s", (message) => {
    expect(describeError(new TypeError(message)).message).toBe(
      unreachableMessage,
    );
  });

  it.each(["TimeoutError", "AbortError"])(
    "prioritizes %s guidance over the browser's raw message",
    (name) => {
      const error = new DOMException("The operation was aborted.", name);
      expect(describeError(error).message).toBe(timeoutMessage);
    },
  );

  it.each([
    new TypeError("Failed to fetch"),
    new DOMException("The operation timed out.", "TimeoutError"),
    new DOMException("The operation was aborted.", "AbortError"),
  ])("preserves correlation IDs when normalizing $name", (error) => {
    const result = describeError(
      Object.assign(error, { correlation_id: "request-123" }),
    );
    expect(result.correlationId).toBe("request-123");
    expect(result.message).toBe(
      error.name === "TypeError" ? unreachableMessage : timeoutMessage,
    );
  });

  it.each([
    { code: "validation_error", message: "End date must follow start date" },
    { code: "incomplete_coverage", message: "Import coverage is incomplete" },
    { code: "provider_failed", name: "TypeError", message: "Failed to fetch" },
  ])("preserves backend $code details and correlation ID", (error) => {
    expect(describeError({ ...error, correlation_id: "request-456" })).toEqual({
      message: error.message,
      correlationId: "request-456",
    });
  });

  it.each([
    new TypeError("Cannot read properties of undefined (reading 'id')"),
    new TypeError("Failed to execute 'fetch' on 'Window': Invalid URL"),
    new Error("Failed to fetch"),
  ])("does not mask unrelated $name errors", (error) => {
    expect(describeError(error).message).toBe(error.message);
  });

  it.each([null, undefined, {}, "unknown failure"])(
    "provides safe fallback guidance for an unstructured error: %s",
    (error) => {
      expect(describeError(error)).toEqual({ message: unreachableMessage });
    },
  );
});
