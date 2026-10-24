import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import { independentTerminal } from "./verification-evidence";
import type {
  DiagnosticRequest,
  ControlledRequest,
  IndependentSource,
} from "./verification-draft";

export const independentCapabilityOptions = (
  owner: string,
  source: IndependentSource | null,
) =>
  queryOptions({
    queryKey: ["independent-capability", owner, source],
    queryFn: async ({ signal }) =>
      (await sdk.independentCapability({ body: source!, signal })).data,
    enabled: Boolean(source),
  });
export const independentStatusOptions = (owner: string, runId?: string) =>
  queryOptions({
    queryKey: ["independent-evaluation", owner, runId],
    queryFn: async ({ signal }) =>
      (
        await sdk.readIndependentEvaluation({
          path: { run_id: runId! },
          signal,
        })
      ).data,
    enabled: Boolean(runId),
    refetchInterval: (query) =>
      query.state.data && independentTerminal.has(query.state.data.status)
        ? false
        : 2000,
  });
export const controlledExperimentOptions = (owner: string, id?: string) =>
  queryOptions({
    queryKey: ["controlled-experiment", owner, id],
    queryFn: async ({ signal }) =>
      (
        await sdk.readControlledExperiment({
          path: { controlled_id: id! },
          signal,
        })
      ).data,
    enabled: Boolean(id),
    refetchInterval: (query) =>
      query.state.data &&
      query.state.data.arms.every((arm) => independentTerminal.has(arm.status))
        ? false
        : 2000,
  });
export async function createDiagnostic(body: DiagnosticRequest, key: string) {
  return (
    await sdk.createDiagnosticReplay({
      path: { evaluation_id: body.source.evaluation_id },
      body,
      headers: { "Idempotency-Key": key },
    })
  ).data;
}
export async function createControlled(body: ControlledRequest, key: string) {
  return (
    await sdk.createControlledExperiment({
      body,
      headers: { "Idempotency-Key": key },
    })
  ).data;
}
export async function changeIndependent(
  runId: string,
  action: "cancel" | "retry",
  key: string,
) {
  const options = {
    path: { run_id: runId },
    headers: { "Idempotency-Key": key },
  };
  return (
    action === "cancel"
      ? await sdk.cancelIndependentEvaluation(options)
      : await sdk.retryIndependentEvaluation(options)
  ).data;
}

export const diagnosticParityOptions = (
  owner: string,
  runId: string,
  hash?: string,
) =>
  queryOptions({
    queryKey: ["diagnostic-parity", owner, runId, hash],
    queryFn: async ({ signal }) =>
      (
        await sdk.readDiagnosticParity({
          path: { run_id: runId },
          query: { report_sha256: hash },
          signal,
        })
      ).data,
    staleTime: hash ? Infinity : 15000,
  });
export const diagnosticParityEventsOptions = (
  owner: string,
  runId: string,
  hash: string,
  offset = 0,
) =>
  queryOptions({
    queryKey: ["diagnostic-parity-events", owner, runId, hash, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.queryDiagnosticParityEvents({
          path: { run_id: runId },
          query: { report_sha256: hash, offset, limit: 25 },
          signal,
        })
      ).data,
    staleTime: Infinity,
  });

export const independentResultOptions = (
  owner: string,
  pin: import("@/client/types.gen").IndependentResultPin,
  section: string,
  offset: number,
) =>
  queryOptions({
    queryKey: ["independent-result", owner, pin, section, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.queryIndependentResult({
          path: { evaluation_id: pin.evaluation_id },
          query: {
            section,
            offset,
            limit: 25,
            execution_attempt_id: pin.execution_attempt_id,
            result_manifest_sha256: pin.result_manifest_sha256,
          },
          signal,
        })
      ).data,
    staleTime: Infinity,
  });
