import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type { ResearchRunDetail } from "@/client/types.gen";
import { queryClient } from "./api";
import { researchTerminal } from "./research-results";

export const RESEARCH_PAGE_SIZE = 25;
const refresh = (active: boolean) => (active ? 2000 : false);
export const researchRunsOptions = (offset = 0) =>
  queryOptions({
    queryKey: ["research-runs", offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.listResearchRuns({
          query: { offset, limit: RESEARCH_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: 4000,
  });
export function researchNeedsRefresh(run?: ResearchRunDetail) {
  return (
    !run ||
    !researchTerminal.has(run.status) ||
    run.attempts.some((attempt) => !attempt.sandbox_stopped)
  );
}
export const researchRunOptions = (id: string) =>
  queryOptions({
    queryKey: ["research-run", id],
    queryFn: async ({ signal }) =>
      (await sdk.readResearchRun({ path: { run_id: id }, signal })).data,
    refetchInterval: (query) => refresh(researchNeedsRefresh(query.state.data)),
  });
export const candidatesOptions = (
  id: string,
  offset = 0,
  active = false,
  filters: import("@/components/named-list-controls").NamedListFilters = {},
) =>
  queryOptions({
    queryKey: [
      "research-candidates",
      id,
      offset,
      ...(Object.keys(filters).length ? [filters] : []),
    ],
    queryFn: async ({ signal }) =>
      (
        await sdk.listResearchCandidates({
          path: { run_id: id },
          query: { offset, limit: RESEARCH_PAGE_SIZE, ...filters },
          signal,
        })
      ).data,
    refetchInterval: refresh(active),
  });
export const evaluationsOptions = (id: string, offset = 0, active = false) =>
  queryOptions({
    queryKey: ["research-evaluations", id, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.listResearchEvaluations({
          path: { run_id: id },
          query: { offset, limit: RESEARCH_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: refresh(active),
  });
export const checkpointsOptions = (id: string, offset = 0, active = false) =>
  queryOptions({
    queryKey: ["research-checkpoints", id, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.listResearchCheckpoints({
          path: { run_id: id },
          query: { offset, limit: RESEARCH_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: refresh(active),
  });
export const modelCallsOptions = (id: string, offset = 0, active = false) =>
  queryOptions({
    queryKey: ["research-model-calls", id, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.listResearchModelCalls({
          path: { run_id: id },
          query: { offset, limit: RESEARCH_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: refresh(active),
  });
export const candidateOptions = (id: string) =>
  queryOptions({
    queryKey: ["research-candidate", id],
    queryFn: async ({ signal }) =>
      (await sdk.readResearchCandidate({ path: { candidate_id: id }, signal }))
        .data,
    enabled: Boolean(id),
  });
export const checkpointOptions = (id: string) =>
  queryOptions({
    queryKey: ["research-checkpoint", id],
    queryFn: async ({ signal }) =>
      (
        await sdk.readResearchCheckpoint({
          path: { checkpoint_id: id },
          signal,
        })
      ).data,
    enabled: Boolean(id),
  });
export const modelCallOptions = (id: string, runId: string, active = false) =>
  queryOptions({
    queryKey: ["research-model-call", runId, id],
    queryFn: async ({ signal }) =>
      (await sdk.readResearchModelCall({ path: { model_call_id: id }, signal }))
        .data,
    enabled: Boolean(id),
    refetchInterval: refresh(active),
  });
export const researchArtifactsOptions = (
  id: string,
  offset = 0,
  active = false,
) =>
  queryOptions({
    queryKey: ["artifacts", id, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.listRunArtifacts({
          path: { run_id: id },
          query: { offset, limit: RESEARCH_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: refresh(active),
  });
export async function researchCommand(
  id: string,
  action: "pause" | "cancel" | "resume",
) {
  const options = { path: { run_id: id } };
  return (
    await (action === "pause"
      ? sdk.pauseResearchRun(options)
      : action === "cancel"
        ? sdk.cancelResearchRun(options)
        : sdk.resumeResearchRun(options))
  ).data;
}
export async function refreshResearch(id: string) {
  await Promise.all([
    ...[
      "research-run",
      "research-candidates",
      "research-evaluations",
      "research-checkpoints",
      "research-model-calls",
      "research-model-call",
      "research-usage",
      "events",
      "artifacts",
    ].map((key) => queryClient.invalidateQueries({ queryKey: [key, id] })),
    queryClient.invalidateQueries({ queryKey: ["research-runs"] }),
  ]);
}

export const researchUsageOptions = (id: string, active = false) =>
  queryOptions({
    queryKey: ["research-usage", id],
    queryFn: async ({ signal }) =>
      (await sdk.readResearchUsage({ path: { run_id: id }, signal })).data,
    refetchInterval: refresh(active),
  });
export type ResultsQuery = NonNullable<
  import("@/client/types.gen").ReadResearchResultsData["query"]
>;
export const resultsOptions = (id: string, query: ResultsQuery) =>
  queryOptions({
    queryKey: ["research-results", id, query],
    queryFn: async ({ signal }) =>
      (
        await sdk.readResearchResults({
          path: { evaluation_id: id },
          query,
          signal,
        })
      ).data,
    enabled: Boolean(id),
  });
export const comparisonOptions = (ids: string[]) =>
  queryOptions({
    queryKey: ["research-comparison", ids],
    queryFn: async ({ signal }) =>
      (
        await sdk.compareResearchResults({
          body: { evaluation_ids: ids },
          signal,
        })
      ).data,
    enabled: ids.length >= 2 && ids.length <= 4,
  });
export async function downloadResearchResult(
  id: string,
  format: "json" | "html",
) {
  const options = { path: { evaluation_id: id }, parseAs: "blob" as const };
  const data: unknown = (
    await (format === "json"
      ? sdk.downloadResearchResults(options)
      : sdk.downloadResearchReport(options))
  ).data;
  if (!(data instanceof Blob))
    throw new Error(
      "The server did not return a downloadable research artifact.",
    );
  return data;
}

export const returnAnalysisOptions = (id: string) =>
  queryOptions({
    queryKey: ["research-return-analysis", id],
    queryFn: async ({ signal }) =>
      (
        await sdk.readResearchReturnAnalysis({
          path: { evaluation_id: id },
          signal,
        })
      ).data,
    enabled: Boolean(id),
  });
export const priceReplayOptions = (
  id: string,
  query: NonNullable<
    import("@/client/types.gen").ReadResearchPriceReplayData["query"]
  >,
) =>
  queryOptions({
    queryKey: ["research-price-replay", id, query],
    queryFn: async ({ signal }) =>
      (
        await sdk.readResearchPriceReplay({
          path: { evaluation_id: id },
          query,
          signal,
        })
      ).data,
    enabled: Boolean(id && query.listing_id),
  });
