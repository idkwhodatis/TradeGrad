import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => vi.unstubAllGlobals());

it("keeps the account selector label independent from option text", async () => {
  vi.stubGlobal("window", { location: { origin: "http://settings.test" } });
  const { SettingsManagement } =
    await import("../../src/features/settings-management");
  const query = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  query.setQueryData(["me"], { id: "admin", is_superuser: true });
  query.setQueryData(["managed-users", 0], {
    count: 1,
    data: [
      { id: "ordinary", email: "ordinary@example.com", is_superuser: false },
    ],
  });
  const html = renderToString(
    createElement(
      QueryClientProvider,
      { client: query },
      createElement(SettingsManagement),
    ),
  );
  expect(html).toContain(
    '<label for="managed-account-selector">Manage account</label>',
  );
  expect(html).toContain('<select id="managed-account-selector">');
  expect(html).toContain('<option value="ordinary">ordinary@example.com');
  query.clear();
});
