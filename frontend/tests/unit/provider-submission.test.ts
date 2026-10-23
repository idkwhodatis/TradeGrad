import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  clearProviderSubmission,
  connectionAdmissionRejected,
  providerSubmission,
  hasPendingProviderSubmission,
} from "../../src/lib/provider-submission";

beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => data.get(key) || null,
    setItem: (key: string, value: string) => data.set(key, value),
    removeItem: (key: string) => data.delete(key),
  });
});
afterEach(() => vi.unstubAllGlobals());
it("keeps uncertain submission identity through first-use/catalog revision changes", () => {
  const body = {
    provider: "akshare",
    price_mode: null,
    expected_connection_revision: 0,
  };
  const first = providerSubmission("owner", "import", body);
  const retry = providerSubmission("owner", "import", {
    ...body,
    expected_connection_revision: 2,
  });
  expect(retry).toEqual(first);
  expect(retry.body.expected_connection_revision).toBe(0);
});
it("does not reuse another owner, operation or changed logical input", () => {
  const body = { symbol: "AAPL", expected_connection_revision: 1 };
  const first = providerSubmission("one", "probe", body);
  expect(providerSubmission("two", "probe", body).key).not.toBe(first.key);
  expect(providerSubmission("one", "import", body).key).not.toBe(first.key);
  const changed = providerSubmission("one", "probe", {
    symbol: "MSFT",
    expected_connection_revision: 2,
  });
  expect(changed.key).not.toBe(first.key);
  expect(changed.body.expected_connection_revision).toBe(2);
});
it("acknowledged or definitely rejected submissions can adopt the next revision", () => {
  const first = providerSubmission("one", "probe", {
    expected_connection_revision: 0,
  });
  clearProviderSubmission("one", "probe");
  const next = providerSubmission("one", "probe", {
    expected_connection_revision: 1,
  });
  expect(next.key).not.toBe(first.key);
  expect(next.body.expected_connection_revision).toBe(1);
  expect(
    connectionAdmissionRejected({ code: "provider_connection_conflict" }),
  ).toBe(true);
  expect(
    connectionAdmissionRejected({ code: "provider_connection_disabled" }),
  ).toBe(true);
  expect(connectionAdmissionRejected(new TypeError("Failed to fetch"))).toBe(
    false,
  );
  expect(connectionAdmissionRejected({ code: "database_unavailable" })).toBe(
    false,
  );
});

it("recognizes a matching pending recovery even after a connection revision changes", () => {
  const input = { symbol: "AAPL", expected_connection_revision: 1 };
  expect(hasPendingProviderSubmission("one", "probe", input)).toBe(false);
  providerSubmission("one", "probe", input);
  expect(
    hasPendingProviderSubmission("one", "probe", {
      ...input,
      expected_connection_revision: 2,
    }),
  ).toBe(true);
  expect(
    hasPendingProviderSubmission("one", "probe", { ...input, symbol: "MSFT" }),
  ).toBe(false);
  clearProviderSubmission("one", "probe");
  expect(hasPendingProviderSubmission("one", "probe", input)).toBe(false);
});
