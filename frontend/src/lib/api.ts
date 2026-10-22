import { QueryClient, queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import { client } from "@/client/client.gen";
import type { RunCreate } from "@/client/types.gen";
import { clearSession, readSession } from "./session";
import { terminalStatuses } from "./format";

export function describeError(error: unknown): {
  message: string;
  correlationId?: string;
} {
  if (error && typeof error === "object") {
    const value = error as {
      message?: unknown;
      correlation_id?: unknown;
      code?: unknown;
      name?: unknown;
    };
    if (typeof value.message === "string")
      return {
        message: value.message,
        correlationId:
          typeof value.correlation_id === "string"
            ? value.correlation_id
            : undefined,
      };
    if (value.name === "AbortError" || value.name === "TimeoutError")
      return {
        message:
          "The request timed out. Retry to check the same submission; a timeout does not mean the operation was not accepted.",
      };
  }
  return {
    message:
      "The service could not be reached. Check your connection and try again.",
  };
}

// A single transport covers ordinary requests, downloads, and authenticated SSE.
// Authentication is never put into event/download URLs.
const transport: typeof fetch = async (input, init) => {
  const request = input instanceof Request ? input : new Request(input, init);
  const response = await fetch(request, {
    ...init,
    signal: AbortSignal.any([request.signal, AbortSignal.timeout(20_000)]),
  });
  if (
    response.status === 401 &&
    !new URL(request.url).pathname.endsWith("/login/access-token")
  )
    clearSession(true);
  return response;
};
client.setConfig({
  baseUrl: window.location.origin,
  auth: () => readSession()?.accessToken,
  fetch: transport,
});

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      networkMode: "always",
      staleTime: 15_000,
      retry: (count, error) => {
        const code = (error as { code?: string })?.code || "";
        return count < 1 && !/unauth|forbidden|not_found/i.test(code);
      },
      refetchOnWindowFocus: true,
    },
    mutations: { retry: false, networkMode: "always" },
  },
});
export const login = async (email: string, password: string) =>
  (await sdk.loginAccessToken({ body: { username: email, password } })).data;
export const currentUserOptions = queryOptions({
  queryKey: ["me"],
  queryFn: async ({ signal }) => (await sdk.readMe({ signal })).data,
});
export const datasetsOptions = queryOptions({
  queryKey: ["datasets"],
  queryFn: async ({ signal }) =>
    (await sdk.listDatasets({ query: { limit: 100 }, signal })).data,
});
export const strategiesOptions = queryOptions({
  queryKey: ["strategies"],
  queryFn: async ({ signal }) =>
    (await sdk.listStrategies({ query: { limit: 100 }, signal })).data,
});
export const strategyOptions = (id: string) =>
  queryOptions({
    queryKey: ["strategy", id],
    queryFn: async ({ signal }) =>
      (await sdk.readStrategy({ path: { strategy_id: id }, signal })).data,
  });
export const runsOptions = () =>
  queryOptions({
    queryKey: ["runs"],
    queryFn: async ({ signal }) =>
      (await sdk.listRuns({ query: { limit: 100 }, signal })).data,
    refetchInterval: 4000,
  });
export const runOptions = (id: string) =>
  queryOptions({
    queryKey: ["run", id],
    queryFn: async ({ signal }) =>
      (await sdk.readRun({ path: { run_id: id }, signal })).data,
    refetchInterval: (query) =>
      query.state.data && terminalStatuses.has(query.state.data.status)
        ? false
        : 2000,
  });
export const artifactsOptions = (id: string) =>
  queryOptions({
    queryKey: ["artifacts", id],
    queryFn: async ({ signal }) =>
      (await sdk.listRunArtifacts({ path: { run_id: id }, signal })).data,
  });
export const createRun = async (body: RunCreate, key: string) =>
  (await sdk.createRun({ body, headers: { "Idempotency-Key": key } })).data;
export const cancelRun = async (id: string) =>
  (await sdk.cancelRun({ path: { run_id: id } })).data;
export const resumeRun = async (id: string) =>
  (await sdk.resumeRun({ path: { run_id: id } })).data;
export const getArtifact = async (id: string) =>
  (await sdk.downloadArtifact({ path: { artifact_id: id }, parseAs: "blob" }))
    .data;
