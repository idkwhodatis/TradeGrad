import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { saveSession } from "../../src/lib/session";
import {
  controlledBody,
  diagnosticBody,
  independentSource,
  independentPin,
  verificationHashes as h,
  verificationIds as id,
} from "./verification-fixtures";
import type * as Api from "../../src/lib/verification-api";
let api: typeof Api;
const bearer = crypto.randomUUID(),
  fetchMock = vi.fn<typeof fetch>();
beforeEach(async () => {
  const values = new Map<string, string>();
  vi.stubGlobal(
    "window",
    Object.assign(new EventTarget(), {
      location: { origin: "https://independent.example" },
    }),
  );
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  saveSession(bearer, 3600);
  api = await import("../../src/lib/verification-api");
  (await import("../../src/lib/api")).queryClient.clear();
});
afterEach(() => vi.unstubAllGlobals());
function respond(body: unknown = {}, status = 200) {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}
const sent = () => fetchMock.mock.calls.at(-1)![0] as Request;
async function query<T, Key extends import("@tanstack/react-query").QueryKey>(
  options: import("@tanstack/react-query").FetchQueryOptions<T, Error, T, Key>,
) {
  return (await import("../../src/lib/api")).queryClient.fetchQuery(options);
}

describe("generated dedicated independent API, no optimizer dispatch", () => {
  it("uses exact source-only read capability and no optimizer route", async () => {
    respond();
    await query(api.independentCapabilityOptions("owner", independentSource));
    expect(new URL(sent().url).pathname).toBe(
      "/api/v1/independent-evaluations/capability",
    );
    expect(sent().method).toBe("POST");
    expect(await sent().clone().json()).toEqual(independentSource);
    expect(sent().headers.get("Authorization")).toBe(`Bearer ${bearer}`);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("submits diagnostic source/capture with exact idempotency key and native typed path", async () => {
    respond({}, 202);
    const key = crypto.randomUUID();
    await api.createDiagnostic(diagnosticBody, key);
    expect(new URL(sent().url).pathname).toBe(
      `/api/v1/evaluations/${independentSource.evaluation_id}/diagnostic-replays`,
    );
    expect(await sent().clone().json()).toEqual(diagnosticBody);
    expect(sent().headers.get("Idempotency-Key")).toBe(key);
    expect(JSON.stringify(await sent().clone().json())).not.toContain(
      "optimizer",
    );
  });
  it("preserves ordered copied baseline and manual patches without calling a provider/model", async () => {
    respond({}, 202);
    await api.createControlled(controlledBody, crypto.randomUUID());
    expect(new URL(sent().url).pathname).toBe("/api/v1/controlled-experiments");
    expect(await sent().clone().json()).toEqual(controlledBody);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it.each(["cancel", "retry"] as const)(
    "uses dedicated idempotent %s and never optimizer pause/resume",
    async (action) => {
      respond({}, 202);
      const key = crypto.randomUUID();
      await api.changeIndependent(id.run, action, key);
      expect(new URL(sent().url).pathname).toBe(
        `/api/v1/independent-evaluations/${id.run}/${action}`,
      );
      expect(sent().headers.get("Idempotency-Key")).toBe(key);
      expect(sent().headers.get("Authorization")).toBe(`Bearer ${bearer}`);
    },
  );
  it("keeps owner-specific query identity separate from run and control resources", async () => {
    expect(api.independentStatusOptions("owner", id.run).queryKey).not.toEqual(
      api.independentStatusOptions("other", id.run).queryKey,
    );
    respond();
    await query(api.independentStatusOptions("owner", id.run));
    expect(new URL(sent().url).pathname).toBe(
      `/api/v1/independent-evaluations/${id.run}`,
    );
    respond();
    await query(api.controlledExperimentOptions("owner", id.control));
    expect(new URL(sent().url).pathname).toBe(
      `/api/v1/controlled-experiments/${id.control}`,
    );
  });
  it("pins report and bounded event pages separately from existing replay/comparison", async () => {
    respond();
    await query(api.diagnosticParityOptions("owner", id.run, h.report));
    expect(new URL(sent().url).searchParams.get("report_sha256")).toBe(
      h.report,
    );
    respond();
    await query(
      api.diagnosticParityEventsOptions("owner", id.run, h.report, 25),
    );
    const url = new URL(sent().url);
    expect(url.pathname).toBe(
      `/api/v1/independent-evaluations/${id.run}/parity/events`,
    );
    expect(Object.fromEntries(url.searchParams)).toEqual({
      report_sha256: h.report,
      offset: "25",
      limit: "25",
    });
  });
  it("sends exact physical pins and bounded economic result page", async () => {
    respond();
    await query(
      api.independentResultOptions("owner", independentPin, "fills", 50),
    );
    const url = new URL(sent().url);
    expect(url.pathname).toBe(
      `/api/v1/independent-evaluations/evaluations/${independentPin.evaluation_id}/result`,
    );
    expect(Object.fromEntries(url.searchParams)).toEqual({
      section: "fills",
      offset: "50",
      limit: "25",
      execution_attempt_id: independentPin.execution_attempt_id,
      result_manifest_sha256: independentPin.result_manifest_sha256,
    });
  });
  it("does not hide structured unauthorized/mismatched source failure or create a fallback job", async () => {
    respond(
      { code: "source_pin_mismatch", message: "Exact source pin rejected" },
      409,
    );
    await expect(
      api.createDiagnostic(diagnosticBody, crypto.randomUUID()),
    ).rejects.toMatchObject({ code: "source_pin_mismatch" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
