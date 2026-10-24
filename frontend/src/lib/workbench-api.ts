import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type { SpecRevisionCreate, WorkbenchSpec } from "@/client/types.gen";
import { tracePins, type WorkbenchSelection } from "./workbench-selection";
export type SourceResource = {
  kind: "candidate" | "strategy_version";
  id: string;
};
const resourceKey = (source: SourceResource) =>
  [source.kind, source.id] as const;
export const workbenchSpecOptions = (
  source: SourceResource,
  selection: WorkbenchSelection = {},
) =>
  queryOptions({
    queryKey: [
      "workbench-spec",
      ...resourceKey(source),
      selection.spec_revision,
      selection.spec_sha256,
    ],
    queryFn: async ({ signal }) =>
      (source.kind === "candidate"
        ? await sdk.readCandidateSpec({
            path: { candidate_id: source.id },
            query: {
              revision: selection.spec_revision,
              spec_sha256: selection.spec_sha256,
            },
            signal,
          })
        : await sdk.readStrategySpec({
            path: { strategy_id: source.id },
            query: {
              revision: selection.spec_revision,
              spec_sha256: selection.spec_sha256,
            },
            signal,
          })
      ).data,
    staleTime: selection.spec_revision ? Infinity : 15_000,
  });
export const specRevisionsOptions = (source: SourceResource, offset = 0) =>
  queryOptions({
    queryKey: ["workbench-revisions", ...resourceKey(source), offset],
    queryFn: async ({ signal }) =>
      (source.kind === "candidate"
        ? await sdk.listCandidateSpecRevisions({
            path: { candidate_id: source.id },
            query: { offset, limit: 25 },
            signal,
          })
        : await sdk.listStrategySpecRevisions({
            path: { strategy_id: source.id },
            query: { offset, limit: 25 },
            signal,
          })
      ).data,
  });
export const workbenchLineageOptions = (source: SourceResource, offset = 0) =>
  queryOptions({
    queryKey: ["workbench-lineage", ...resourceKey(source), offset],
    queryFn: async ({ signal }) =>
      (source.kind === "candidate"
        ? await sdk.readCandidateLineage({
            path: { candidate_id: source.id },
            query: { offset, limit: 25 },
            signal,
          })
        : await sdk.readStrategyLineage({
            path: { strategy_id: source.id },
            query: { offset, limit: 25 },
            signal,
          })
      ).data,
  });
export const workbenchEvaluationsOptions = (
  source: SourceResource,
  offset = 0,
) =>
  queryOptions({
    queryKey: ["workbench-evaluations", ...resourceKey(source), offset],
    queryFn: async ({ signal }) =>
      (source.kind === "candidate"
        ? await sdk.listCandidateEvaluations({
            path: { candidate_id: source.id },
            query: { offset, limit: 25 },
            signal,
          })
        : await sdk.listStrategyEvaluations({
            path: { strategy_id: source.id },
            query: { offset, limit: 25 },
            signal,
          })
      ).data,
  });
export const workbenchEvaluationOptions = (id?: string) =>
  queryOptions({
    queryKey: ["workbench-evaluation", id],
    queryFn: async ({ signal }) =>
      (
        await sdk.readWorkbenchEvaluation({
          path: { evaluation_id: id! },
          signal,
        })
      ).data,
    enabled: Boolean(id),
  });
export const workbenchTraceOptions = (selection: WorkbenchSelection) =>
  queryOptions({
    queryKey: [
      "workbench-trace",
      selection.evaluation_id,
      tracePins(selection),
    ],
    queryFn: async ({ signal }) =>
      (
        await sdk.readTraceManifest({
          path: { evaluation_id: selection.evaluation_id! },
          query: tracePins(selection),
          signal,
        })
      ).data,
    enabled: Boolean(
      selection.evaluation_id &&
      selection.execution_attempt_id &&
      selection.result_manifest_sha256 &&
      selection.trace_status !== "not_collected",
    ),
    staleTime: Infinity,
  });
export const selectedDecisionOptions = (selection: WorkbenchSelection) =>
  queryOptions({
    queryKey: [
      "workbench-decision",
      selection.evaluation_id,
      tracePins(selection),
      selection.decision_id,
      selection.instrument_id,
      selection.stage_id,
    ],
    queryFn: async ({ signal }) =>
      (
        await sdk.readTraceDecision({
          path: {
            evaluation_id: selection.evaluation_id!,
            decision_id: selection.decision_id!,
          },
          query: {
            ...tracePins(selection),
            instrument_id: selection.instrument_id,
            stage_id: selection.stage_id,
            limit: 50,
            offset: 0,
          },
          signal,
        })
      ).data,
    enabled: Boolean(
      selection.evaluation_id &&
      selection.execution_attempt_id &&
      selection.result_manifest_sha256 &&
      selection.trace_manifest_sha256 &&
      selection.decision_id &&
      selection.stage_id,
    ),
    staleTime: Infinity,
  });
export async function saveSpecRevision(
  source: SourceResource,
  body: SpecRevisionCreate,
  key: string,
): Promise<WorkbenchSpec> {
  return (
    source.kind === "candidate"
      ? await sdk.createCandidateSpecRevision({
          path: { candidate_id: source.id },
          body,
          headers: { "Idempotency-Key": key },
        })
      : await sdk.createStrategySpecRevision({
          path: { strategy_id: source.id },
          body,
          headers: { "Idempotency-Key": key },
        })
  ).data;
}
export async function setCandidateBookmark(id: string, bookmarked: boolean) {
  return (
    await sdk.setCandidateBookmark({
      path: { candidate_id: id },
      body: { bookmarked },
    })
  ).data;
}
export const researchTemplatesOptions = queryOptions({
  queryKey: ["research-templates"],
  queryFn: async ({ signal }) =>
    (await sdk.listResearchTemplates({ signal })).data,
  staleTime: Infinity,
});
