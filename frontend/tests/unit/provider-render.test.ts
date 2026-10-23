import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import type { ProviderPublic } from "../../src/client/types.gen";

beforeEach(() => {
  vi.stubGlobal("sessionStorage", {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  });
  vi.stubGlobal("window", { location: { origin: "http://provider.test" } });
});
afterEach(() => vi.unstubAllGlobals());
async function markup(
  receipt: ProviderPublic["latest_test"],
  connection?: ProviderPublic["connection"],
) {
  const { DataSourcesPage } = await import("../../src/features/data-sources");
  const query = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  query.setQueryData(["me"], { id: "owner-1" });
  query.setQueryData(["data-universes", 0], { data: [], count: 0 });
  query.setQueryData(
    ["data-providers"],
    [
      {
        id: "yfinance",
        name: "yfinance",
        upstream: "yahoo",
        markets: ["US"],
        price_modes: ["provider_native"],
        capabilities: [
          {
            feature: "daily_bars",
            status: "documented",
            connector: "yfinance",
            provider: "yahoo",
            endpoint: "Ticker.history",
            evidence: "documentation",
          },
        ],
        runtime_mode: "fixture",
        fixture_notice: "Not live market evidence",
        latest_test: receipt,
        connection,
      } satisfies ProviderPublic,
    ],
  );
  const router = createRouter({
    routeTree: createRootRoute({
      component: () => createElement(DataSourcesPage),
    }),
    history: createMemoryHistory({ initialEntries: ["/"] }),
    isServer: true,
  });
  await router.load();
  return renderToString(
    createElement(
      QueryClientProvider,
      { client: query },
      createElement(RouterProvider, { router }),
    ),
  );
}
it("offers an explicit bounded probe without credential inputs or fabricated verification", async () => {
  const html = await markup(null);
  expect(html).toContain("Live connectivity is unknown");
  expect(html).toContain("Test connection");
  expect(html).toContain("Test start · inclusive");
  expect(html).toContain("Test end · exclusive");
  expect(html).not.toContain('type="password"');
  expect(html).not.toContain("Last completed test:");
});
it("labels fixture receipts and exact historical scope instead of verified capabilities", async () => {
  const html = await markup({
    schema_version: "provider-test.v1",
    import_id: "00000000-0000-0000-0000-000000000001",
    provider: "yfinance",
    outcome: "fixture_passed",
    capability_status: "documented",
    evidence: "fixture",
    symbol: "AAPL",
    start_date: "2024-01-02",
    end_date: "2024-01-06",
    checked_at: "2026-10-23T00:00:00Z",
    endpoints: ["Ticker.history"],
    package_versions: { yfinance: "1.7.0" },
    error_codes: [],
    note: "Not live evidence",
  });
  expect(html).toContain("fixture_passed");
  expect(html).toContain("2024-01-02");
  expect(html).toContain("2024-01-06");
  expect(html).toContain("Open connection test record");
  expect(html).not.toContain("Live connectivity is unknown");
  expect(html).not.toContain(">verified<");
});

it("renders owner-local public connection controls without credential fields", async () => {
  const html = await markup(null, {
    schema_version: "provider-connection.v1",
    connection_id: "00000000-0000-0000-0000-000000000001",
    provider: "yfinance",
    revision: 3,
    enabled: false,
    is_default: false,
    default_price_mode: "provider_native",
    auth_mode: "public_no_auth",
    config_sha256: "a".repeat(64),
    created_at: "2026-10-23T00:00:00Z",
  });
  expect(html).toContain("My provider connection");
  expect(html).toContain("public_no_auth");
  expect(html).toContain("Save connection settings");
  expect(html).toContain("Discard unsaved changes");
  expect(html).toContain("Existing selections are retained");
  expect(html.replaceAll("<!-- -->", "")).toContain(
    "Connection default (provider_native)",
  );
  expect(html).not.toContain('type="password"');
});
