import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type {
  FinalEvaluationCreate,
  FinalEvaluationDetail,
  FinalSelectionCreate,
  StrategyPackageCreate,
} from "@/client/types.gen";
import { queryClient } from "./api";
import { finalTerminal } from "@/features/final-tests/protocol";
export const FINAL_PAGE_SIZE = 25;
export const finalSelectionsOptions = (splitId: string) =>
  queryOptions({
    queryKey: ["final-selections", splitId],
    queryFn: async ({ signal }) =>
      (
        await sdk.listFinalSelections({
          query: { split_version_id: splitId, limit: 100 },
          signal,
        })
      ).data,
    enabled: Boolean(splitId),
  });
export const finalSelectionOptions = (id: string) =>
  queryOptions({
    queryKey: ["final-selection", id],
    queryFn: async ({ signal }) =>
      (await sdk.readFinalSelection({ path: { selection_id: id }, signal }))
        .data,
    enabled: Boolean(id),
  });
export const splitVersionOptions = (id: string) =>
  queryOptions({
    queryKey: ["split-version", id],
    queryFn: async ({ signal }) =>
      (await sdk.readSplitVersion({ path: { split_version_id: id }, signal }))
        .data,
    enabled: Boolean(id),
  });
export const splitExposureOptions = (id: string) =>
  queryOptions({
    queryKey: ["split-exposure", id],
    queryFn: async ({ signal }) =>
      (await sdk.readSplitExposure({ path: { split_version_id: id }, signal }))
        .data,
    enabled: Boolean(id),
    staleTime: 0,
  });
export async function freezeFinalSelection(
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
export async function preflightFinalTest(body: FinalEvaluationCreate) {
  return (await sdk.preflightFinalEvaluation({ body })).data;
}
export async function submitFinalTest(
  body: FinalEvaluationCreate,
  key: string,
) {
  return (
    await sdk.createFinalEvaluation({
      body,
      headers: { "Idempotency-Key": key },
    })
  ).data;
}
export function finalNeedsRefresh(run?: FinalEvaluationDetail) {
  return (
    !run ||
    !finalTerminal.has(run.status) ||
    run.attempts.some((attempt) => !attempt.sandbox_stopped)
  );
}
export const finalTestOptions = (id: string) =>
  queryOptions({
    queryKey: ["final-test", id],
    queryFn: async ({ signal }) =>
      (await sdk.readFinalEvaluation({ path: { run_id: id }, signal })).data,
    refetchInterval: (query) =>
      finalNeedsRefresh(query.state.data) ? 2000 : false,
  });
export const finalTestsOptions = (selectionId: string, offset = 0) =>
  queryOptions({
    queryKey: ["final-tests", selectionId, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.listFinalEvaluations({
          query: { selection_id: selectionId, offset, limit: FINAL_PAGE_SIZE },
          signal,
        })
      ).data,
    enabled: Boolean(selectionId),
    refetchInterval: 4000,
  });
export const finalEvidenceOptions = (id: string, offset = 0, active = false) =>
  queryOptions({
    queryKey: ["final-evidence", id, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.listFinalEvaluationEvidence({
          path: { run_id: id },
          query: { offset, limit: FINAL_PAGE_SIZE },
          signal,
        })
      ).data,
    refetchInterval: active ? 2000 : false,
  });
export async function cancelFinalTest(id: string) {
  return (await sdk.cancelFinalEvaluation({ path: { run_id: id } })).data;
}
export async function retryFinalTest(id: string, key: string) {
  return (
    await sdk.retryFinalEvaluation({
      path: { run_id: id },
      headers: { "Idempotency-Key": key },
    })
  ).data;
}
export async function refreshFinalTest(id: string, selectionId?: string) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["final-test", id] }),
    queryClient.invalidateQueries({ queryKey: ["final-evidence", id] }),
    queryClient.invalidateQueries({ queryKey: ["events", id] }),
    queryClient.invalidateQueries({
      queryKey: selectionId ? ["final-tests", selectionId] : ["final-tests"],
    }),
    queryClient.invalidateQueries({ queryKey: ["split-exposure"] }),
  ]);
}
export async function createStrategyPackage(
  id: string,
  body: StrategyPackageCreate,
) {
  return (await sdk.createStrategyPackage({ path: { selection_id: id }, body }))
    .data;
}
export const strategyPackageOptions = (id: string) =>
  queryOptions({
    queryKey: ["strategy-package", id],
    queryFn: async ({ signal }) =>
      (await sdk.getStrategyPackage({ path: { package_id: id }, signal })).data,
    enabled: Boolean(id),
  });
export async function downloadStrategyPackage(id: string) {
  const data: unknown = (
    await sdk.downloadStrategyPackage({
      path: { package_id: id },
      parseAs: "blob",
    })
  ).data;
  if (!(data instanceof Blob))
    throw new Error(
      "The server did not return a downloadable strategy package.",
    );
  return data;
}
export type FinalResultsQuery = NonNullable<
  import("@/client/types.gen").QueryFinalResultData["query"]
>;
export const finalResultsOptions = (id: string, query: FinalResultsQuery) =>
  queryOptions({
    queryKey: ["final-results", id, query],
    queryFn: async ({ signal }) =>
      (
        await sdk.queryFinalResult({
          path: { evaluation_id: id },
          query,
          signal,
        })
      ).data,
    enabled: Boolean(id),
  });
export async function downloadFinalResult(id: string) {
  const data: unknown = (
    await sdk.downloadFinalResult({
      path: { evaluation_id: id },
      parseAs: "blob",
    })
  ).data;
  if (!(data instanceof Blob))
    throw new Error("The server did not return a downloadable final result.");
  return data;
}
