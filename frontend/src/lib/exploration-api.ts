import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type {
  ExplorationContext,
  ReadResearchPriceReplayData,
} from "@/client/types.gen";

export type ExplorationMode = ExplorationContext["mode"];
export type ExplorationReference = {
  evaluation_id: string;
  artifact_sha256: string;
  window: { start: string; end: string };
  split?: string;
  selection_id?: string;
};
export function explorationMatches(
  context: ExplorationContext | undefined,
  mode: ExplorationMode,
  reference: ExplorationReference,
) {
  return Boolean(
    context &&
    context.mode === mode &&
    context.evaluation_id === reference.evaluation_id &&
    context.artifact_sha256 === reference.artifact_sha256 &&
    context.start === reference.window.start &&
    context.end === reference.window.end &&
    (!reference.split || context.split === reference.split) &&
    (context.selection_id || undefined) === reference.selection_id &&
    (mode.endsWith("final")
      ? context.split === "holdout" && Boolean(context.selection_id)
      : context.split !== "holdout" && !context.selection_id) &&
    (mode.startsWith("portable")
      ? context.result_trust === "isolated_policy_trusted_akquant_engine" &&
        context.ledger_origin === "trusted_engine"
      : context.result_trust === "native_strategy_and_engine_share_process" &&
        context.ledger_origin === "native_same_process"),
  );
}
const analysisMethods = {
  native_research: sdk.readResearchReturnAnalysis,
  native_final: sdk.readFinalReturnAnalysis,
  portable_research: sdk.readPortableReturnAnalysis,
  portable_final: sdk.readPortableFinalReturnAnalysis,
};
const replayMethods = {
  native_research: sdk.readResearchPriceReplay,
  native_final: sdk.readFinalPriceReplay,
  portable_research: sdk.readPortablePriceReplay,
  portable_final: sdk.readPortableFinalPriceReplay,
};
export const modeAnalysisOptions = (
  mode: ExplorationMode,
  reference: ExplorationReference,
) =>
  queryOptions({
    queryKey: [
      "result-exploration",
      mode,
      reference.evaluation_id,
      reference.artifact_sha256,
      "returns",
    ],
    queryFn: async ({ signal }) => {
      const value = (
        await analysisMethods[mode]({
          path: { evaluation_id: reference.evaluation_id },
          signal,
        })
      ).data;
      if (!explorationMatches(value.context, mode, reference))
        throw new Error(
          "Exploration response differs from the verified mode, artifact or frozen holdout identity. No values are displayed.",
        );
      return value;
    },
  });
export const modeReplayOptions = (
  mode: ExplorationMode,
  reference: ExplorationReference,
  query: NonNullable<ReadResearchPriceReplayData["query"]>,
) =>
  queryOptions({
    queryKey: [
      "result-exploration",
      mode,
      reference.evaluation_id,
      reference.artifact_sha256,
      "prices",
      query,
    ],
    enabled: Boolean(query.listing_id),
    queryFn: async ({ signal }) => {
      const value = (
        await replayMethods[mode]({
          path: { evaluation_id: reference.evaluation_id },
          query,
          signal,
        })
      ).data;
      if (
        !explorationMatches(value.context, mode, reference) ||
        value.listing_id !== query.listing_id
      )
        throw new Error(
          "Price replay differs from the verified mode, artifact, listing or frozen holdout identity. No values are displayed.",
        );
      return value;
    },
  });
