import { queryOptions, useQuery } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type {
  EventPublic,
  FinalSelectionCreate,
  PortableFinalEvaluationCreate,
  PortableFinalEvaluationDetail,
} from "@/client/types.gen";
import { portableFinalTerminal } from "@/features/portable/final-protocol";
import type { PortableSection } from "@/features/portable/protocol";
import { queryClient } from "./api";
import { mergeEvents, parseEvent } from "./events";
export const PORTABLE_FINAL_PAGE_SIZE = 25;
export const portableFinalKey = (
  owner: string,
  resource: string,
  ...parts: unknown[]
) => ["portable-final", owner, resource, ...parts];
const enabled = (owner: string, id: string) => Boolean(owner && id);
export const portableFinalSelectionsOptions = (
  owner: string,
  splitId: string,
) =>
  queryOptions({
    queryKey: portableFinalKey(owner, "selections", splitId),
    enabled: enabled(owner, splitId),
    queryFn: async ({ signal }) =>
      (
        await sdk.listFinalSelections({
          query: { split_version_id: splitId, limit: 100 },
          signal,
        })
      ).data,
  });
export const portableFinalSelectionOptions = (owner: string, id: string) =>
  queryOptions({
    queryKey: portableFinalKey(owner, "selection", id),
    enabled: enabled(owner, id),
    queryFn: async ({ signal }) =>
      (await sdk.readFinalSelection({ path: { selection_id: id }, signal }))
        .data,
  });
export const portableFrozenSourceOptions = (owner: string, id: string) =>
  queryOptions({
    queryKey: portableFinalKey(owner, "source", id),
    enabled: enabled(owner, id),
    queryFn: async ({ signal }) =>
      (await sdk.readPortableStrategy({ path: { strategy_id: id }, signal }))
        .data,
  });
export const portableSplitOptions = (owner: string, id: string) =>
  queryOptions({
    queryKey: portableFinalKey(owner, "split", id),
    enabled: enabled(owner, id),
    queryFn: async ({ signal }) =>
      (await sdk.readSplitVersion({ path: { split_version_id: id }, signal }))
        .data,
  });
export const portableExposureOptions = (owner: string, id: string) =>
  queryOptions({
    queryKey: portableFinalKey(owner, "exposure", id),
    enabled: enabled(owner, id),
    staleTime: 0,
    queryFn: async ({ signal }) =>
      (await sdk.readSplitExposure({ path: { split_version_id: id }, signal }))
        .data,
  });
export async function freezePortableSelection(
  body: FinalSelectionCreate,
  key: string,
) {
  return (
    await sdk.freezeFinalSelection({
      body,
      headers: { "Idempotency-Key": key },
    })
  ).data;
}
export async function preflightPortableFinal(
  body: PortableFinalEvaluationCreate,
) {
  return (await sdk.portablePreflightFinalEvaluation({ body })).data;
}
export async function submitPortableFinal(
  body: PortableFinalEvaluationCreate,
  key: string,
) {
  return (
    await sdk.portableCreateFinalEvaluation({
      body,
      headers: { "Idempotency-Key": key },
    })
  ).data;
}
export const portableFinalNeedsRefresh = (
  job?: PortableFinalEvaluationDetail,
) =>
  !job ||
  !portableFinalTerminal.has(job.status) ||
  job.attempts.some((attempt) => !attempt.sandbox_stopped);
export const portableFinalOptions = (owner: string, id: string) =>
  queryOptions({
    queryKey: portableFinalKey(owner, "job", id),
    enabled: enabled(owner, id),
    queryFn: async ({ signal }) =>
      (await sdk.portableReadFinalEvaluation({ path: { run_id: id }, signal }))
        .data,
    refetchInterval: (query) =>
      query.state.error
        ? false
        : portableFinalNeedsRefresh(query.state.data)
          ? 2000
          : false,
  });
export const portableFinalJobsOptions = (
  owner: string,
  id: string,
  offset = 0,
) =>
  queryOptions({
    queryKey: portableFinalKey(owner, "jobs", id, offset),
    enabled: enabled(owner, id),
    queryFn: async ({ signal }) =>
      (
        await sdk.portableListFinalEvaluations({
          query: { selection_id: id, offset, limit: PORTABLE_FINAL_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: (query) => (query.state.error ? false : 4000),
  });
export const portableFinalEvidenceOptions = (
  owner: string,
  id: string,
  offset = 0,
  active = false,
) =>
  queryOptions({
    queryKey: portableFinalKey(owner, "evidence", id, offset),
    enabled: enabled(owner, id),
    queryFn: async ({ signal }) =>
      (
        await sdk.portableListFinalEvaluationEvidence({
          path: { run_id: id },
          query: { offset, limit: PORTABLE_FINAL_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: (query) => (active && !query.state.error ? 2000 : false),
  });
export const portableFinalResultsOptions = (
  owner: string,
  id: string,
  section: PortableSection = "account_series",
  offset = 0,
  limit = 50,
  filters: import("@/components/result-table-controls").TableFilters = {},
) =>
  queryOptions({
    queryKey: portableFinalKey(
      owner,
      "result",
      id,
      section,
      offset,
      limit,
      ...(Object.keys(filters).length ? [filters] : []),
    ),
    enabled: enabled(owner, id),
    queryFn: async ({ signal }) =>
      (
        await sdk.portableQueryFinalResult({
          path: { evaluation_id: id },
          query: { section, offset, limit, ...filters },
          signal,
        })
      ).data,
  });
export async function cancelPortableFinal(id: string) {
  return (await sdk.portableCancelFinalEvaluation({ path: { run_id: id } }))
    .data;
}
export async function retryPortableFinal(id: string, key: string) {
  return (
    await sdk.portableRetryFinalEvaluation({
      path: { run_id: id },
      headers: { "Idempotency-Key": key },
    })
  ).data;
}
export async function refreshPortableFinal(owner: string) {
  await queryClient.invalidateQueries({ queryKey: ["portable-final", owner] });
}
export async function downloadPortableFinalResult(id: string) {
  const data: unknown = (
    await sdk.portableDownloadFinalResult({
      path: { evaluation_id: id },
      parseAs: "blob",
    })
  ).data;
  if (!(data instanceof Blob))
    throw Error(
      "The server did not return a downloadable portable final artifact.",
    );
  return data;
}
export async function verifyPortableFinalDownload(
  blob: Blob,
  expected: string,
) {
  if (
    !/^[a-f0-9]{64}$/.test(expected) ||
    blob.size === 0 ||
    blob.size > 8 * 1_048_576
  )
    throw new Error(
      "Portable final artifact checksum or size is invalid. The file was not saved.",
    );
  const actual = Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", await blob.arrayBuffer()),
    ),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
  if (actual !== expected)
    throw new Error(
      "Portable final download checksum differs. The file was not saved.",
    );
}
/** Bounded authenticated SSE recovery. A gap is never silently accepted as a complete history. */
export function mergePortableFinalEvents(
  previous: EventPublic[],
  incoming: EventPublic[],
  runId: string,
) {
  const merged = mergeEvents(
    previous,
    incoming.map((event) => parseEvent(event, runId)),
  );
  if (
    merged.some(
      (event, index) => event.run_id !== runId || event.seq !== index + 1,
    )
  )
    throw Error(
      "Portable final event sequence has a gap. Retry recovery; persisted job state still refreshes independently.",
    );
  return merged;
}
export const portableFinalEventsOptions = (
  owner: string,
  id: string,
  terminal: boolean,
) =>
  queryOptions({
    queryKey: portableFinalKey(owner, "events", id),
    enabled: enabled(owner, id),
    queryFn: async ({ signal }) => {
      const previous =
        queryClient.getQueryData<EventPublic[]>(
          portableFinalKey(owner, "events", id),
        ) || [];
      const incoming: EventPublic[] = [];
      let failure: unknown;
      const response = await sdk.listRunEvents({
        path: { run_id: id },
        query: { after_seq: previous.at(-1)?.seq || 0, follow: false },
        signal,
        sseMaxRetryAttempts: 1,
        onSseError: (error) => {
          failure = error;
        },
      });
      for await (const raw of response.stream)
        incoming.push(parseEvent(raw, id));
      if (failure) throw failure;
      return mergePortableFinalEvents(previous, incoming, id);
    },
    refetchInterval: (query) => (terminal || query.state.error ? false : 2000),
  });
export function usePortableFinalEvents(
  owner: string,
  id: string,
  terminal: boolean,
) {
  return useQuery(portableFinalEventsOptions(owner, id, terminal));
}
