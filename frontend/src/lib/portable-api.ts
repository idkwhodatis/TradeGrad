import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type {
  PortableDefaultRequest,
  PortableRunCreate,
  PortableRunDetail,
  PortableStrategyCreate,
  DatasetPublic,
  UniversePublic,
  PortableStrategyPublic,
  PortableTemplatePublic,
  ResearchCapabilities,
} from "@/client/types.gen";
import { queryClient } from "./api";
import {
  portableTerminal,
  type PortableSection,
} from "@/features/portable/protocol";
export const PORTABLE_PAGE_SIZE = 25;
export const portableKey = (
  owner: string,
  resource: string,
  ...parts: unknown[]
) => ["portable", owner, resource, ...parts];
export async function collectPortablePages<T>(
  read: (offset: number) => Promise<{ data: T[]; count: number }>,
) {
  const rows: T[] = [];
  for (;;) {
    const page = await read(rows.length);
    if (
      !Number.isInteger(page.count) ||
      page.count < 0 ||
      (!page.data.length && rows.length < page.count)
    )
      throw new Error(
        "The portable input catalog changed while loading. Reload before choosing immutable versions.",
      );
    rows.push(...page.data);
    if (rows.length >= page.count) return rows;
  }
}
export const portableCatalogOptions = (owner: string) =>
  queryOptions({
    queryKey: portableKey(owner, "catalog"),
    enabled: Boolean(owner),
    queryFn: async ({ signal }) => {
      const [datasets, universes, strategies, templates, capabilities] =
        await Promise.all([
          collectPortablePages(
            async (offset) =>
              (
                await sdk.listDatasets({
                  query: { offset, limit: 100 },
                  signal,
                })
              ).data,
          ),
          collectPortablePages(
            async (offset) =>
              (
                await sdk.listDataUniverses({
                  query: { offset, limit: 100 },
                  signal,
                })
              ).data,
          ),
          collectPortablePages(
            async (offset) =>
              (
                await sdk.listPortableStrategies({
                  query: { offset, limit: 100 },
                  signal,
                })
              ).data,
          ),
          sdk
            .listPortableTemplates({ signal })
            .then((response) => response.data),
          sdk
            .researchCapabilities({ signal })
            .then((response) => response.data),
        ]);
      return { datasets, universes, strategies, templates, capabilities };
    },
  });
export type PortableCatalog = {
  datasets: DatasetPublic[];
  universes: UniversePublic[];
  strategies: PortableStrategyPublic[];
  templates: PortableTemplatePublic[];
  capabilities: ResearchCapabilities;
};
export async function createPortableStrategy(body: PortableStrategyCreate) {
  return (await sdk.createPortableStrategy({ body })).data;
}
export async function defaultPortableConfig(
  body: PortableDefaultRequest,
  signal?: AbortSignal,
) {
  return (await sdk.defaultPortableConfig({ body, signal })).data;
}
export async function preflightPortable(
  body: PortableRunCreate,
  signal?: AbortSignal,
) {
  return (await sdk.preflightPortableRun({ body, signal })).data;
}
export async function submitPortable(body: PortableRunCreate, key: string) {
  return (
    await sdk.createPortableRun({ body, headers: { "Idempotency-Key": key } })
  ).data;
}
export const portableRunsOptions = (owner: string, offset = 0) =>
  queryOptions({
    queryKey: portableKey(owner, "runs", offset),
    enabled: Boolean(owner),
    queryFn: async ({ signal }) =>
      (
        await sdk.listPortableRuns({
          query: { offset, limit: PORTABLE_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: 4000,
  });
export const portableNeedsRefresh = (run?: PortableRunDetail) =>
  !run ||
  !portableTerminal.has(run.status) ||
  run.attempts.some((a) => !a.sandbox_stopped);
export const portableRunOptions = (owner: string, id: string) =>
  queryOptions({
    queryKey: portableKey(owner, "run", id),
    enabled: Boolean(owner && id),
    queryFn: async ({ signal }) =>
      (await sdk.readPortableRun({ path: { run_id: id }, signal })).data,
    refetchInterval: (query) =>
      portableNeedsRefresh(query.state.data) ? 2000 : false,
  });
export const portableCandidatesOptions = (
  owner: string,
  id: string,
  offset = 0,
  active = false,
) =>
  queryOptions({
    queryKey: portableKey(owner, "candidates", id, offset),
    enabled: Boolean(owner && id),
    queryFn: async ({ signal }) =>
      (
        await sdk.listPortableCandidates({
          path: { run_id: id },
          query: { offset, limit: PORTABLE_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: active ? 2000 : false,
  });
export const portableEvaluationsOptions = (
  owner: string,
  id: string,
  offset = 0,
  active = false,
) =>
  queryOptions({
    queryKey: portableKey(owner, "evaluations", id, offset),
    enabled: Boolean(owner && id),
    queryFn: async ({ signal }) =>
      (
        await sdk.listPortableEvaluations({
          path: { run_id: id },
          query: { offset, limit: PORTABLE_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: active ? 2000 : false,
  });
export const portableCheckpointsOptions = (
  owner: string,
  id: string,
  offset = 0,
  active = false,
) =>
  queryOptions({
    queryKey: portableKey(owner, "checkpoints", id, offset),
    enabled: Boolean(owner && id),
    queryFn: async ({ signal }) =>
      (
        await sdk.listPortableCheckpoints({
          path: { run_id: id },
          query: { offset, limit: PORTABLE_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: active ? 2000 : false,
  });
export const portableModelCallsOptions = (
  owner: string,
  id: string,
  offset = 0,
  active = false,
) =>
  queryOptions({
    queryKey: portableKey(owner, "model-calls", id, offset),
    enabled: Boolean(owner && id),
    queryFn: async ({ signal }) =>
      (
        await sdk.listPortableModelCalls({
          path: { run_id: id },
          query: { offset, limit: PORTABLE_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: active ? 2000 : false,
  });
export const portableUsageOptions = (
  owner: string,
  id: string,
  active = false,
) =>
  queryOptions({
    queryKey: portableKey(owner, "usage", id),
    enabled: Boolean(owner && id),
    queryFn: async ({ signal }) =>
      (await sdk.readPortableUsage({ path: { run_id: id }, signal })).data,
    refetchInterval: active ? 2000 : false,
  });
export const portableCandidateOptions = (owner: string, id: string) =>
  queryOptions({
    queryKey: portableKey(owner, "candidate", id),
    enabled: Boolean(owner && id),
    queryFn: async ({ signal }) =>
      (await sdk.readPortableCandidate({ path: { candidate_id: id }, signal }))
        .data,
  });
export const portableCheckpointOptions = (owner: string, id: string) =>
  queryOptions({
    queryKey: portableKey(owner, "checkpoint", id),
    enabled: Boolean(owner && id),
    queryFn: async ({ signal }) =>
      (
        await sdk.readPortableCheckpoint({
          path: { checkpoint_id: id },
          signal,
        })
      ).data,
  });
export const portableModelCallOptions = (
  owner: string,
  id: string,
  active = false,
) =>
  queryOptions({
    queryKey: portableKey(owner, "model-call", id),
    enabled: Boolean(owner && id),
    queryFn: async ({ signal }) =>
      (await sdk.readPortableModelCall({ path: { model_call_id: id }, signal }))
        .data,
    refetchInterval: active ? 2000 : false,
  });
export const portableResultsOptions = (
  owner: string,
  id: string,
  section: PortableSection = "account_series",
  offset = 0,
  limit = 50,
) =>
  queryOptions({
    queryKey: portableKey(owner, "results", id, section, offset, limit),
    enabled: Boolean(owner && id),
    queryFn: async ({ signal }) =>
      (
        await sdk.readPortableResults({
          path: { evaluation_id: id },
          query: { section, offset, limit },
          signal,
        })
      ).data,
  });
export async function portableCommand(
  id: string,
  action: "pause" | "cancel" | "resume",
) {
  const options = { path: { run_id: id } };
  return (
    await (action === "pause"
      ? sdk.pausePortableRun(options)
      : action === "cancel"
        ? sdk.cancelPortableRun(options)
        : sdk.resumePortableRun(options))
  ).data;
}
export async function refreshPortable(owner: string) {
  await queryClient.invalidateQueries({ queryKey: ["portable", owner] });
}
export async function downloadPortableResult(id: string) {
  const data: unknown = (
    await sdk.downloadPortableResults({
      path: { evaluation_id: id },
      parseAs: "blob",
    })
  ).data;
  if (!(data instanceof Blob))
    throw new Error(
      "The server did not return a downloadable portable result artifact.",
    );
  return data;
}
