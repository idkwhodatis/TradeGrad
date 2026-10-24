import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import { tracePins, type WorkbenchSelection } from "./workbench-selection";
import {
  EVENT_PAGE_SIZE,
  REPLAY_PAGE_SIZE,
  nextSession,
  replayReady,
} from "./replay-evidence";

export const replayEventsOptions = (selection: WorkbenchSelection) => {
  const query = {
    ...tracePins(selection),
    start: selection.session_date,
    end: selection.session_date
      ? nextSession(selection.session_date)
      : undefined,
    offset: selection.event_offset ?? 0,
    limit: EVENT_PAGE_SIZE,
  };
  return queryOptions({
    queryKey: ["replay-events", selection.evaluation_id, query],
    queryFn: async ({ signal }) =>
      (
        await sdk.readTraceDecisions({
          path: { evaluation_id: selection.evaluation_id! },
          query,
          signal,
        })
      ).data,
    enabled: replayReady(selection),
    staleTime: Infinity,
  });
};
export const replayCandidatesOptions = (
  selection: WorkbenchSelection,
  selected = false,
) => {
  const query = {
    ...tracePins(selection),
    stage_id: selection.stage_id,
    instrument_id: selected ? selection.instrument_id : undefined,
    record_ordinal: selected ? selection.record_ordinal : undefined,
    eligibility: selected ? undefined : selection.eligibility,
    reason: selected ? undefined : selection.reason,
    rank_min: selected ? undefined : selection.rank_min,
    rank_max: selected ? undefined : selection.rank_max,
    score_min: selected ? undefined : selection.score_min,
    score_max: selected ? undefined : selection.score_max,
    offset: selected ? 0 : (selection.candidate_offset ?? 0),
    limit: REPLAY_PAGE_SIZE,
  };
  return queryOptions({
    queryKey: [
      "replay-candidates",
      selection.evaluation_id,
      selection.decision_id,
      query,
    ],
    queryFn: async ({ signal }) =>
      (
        await sdk.readTraceCandidates({
          path: {
            evaluation_id: selection.evaluation_id!,
            decision_id: selection.decision_id!,
          },
          query,
          signal,
        })
      ).data,
    enabled:
      replayReady(selection) &&
      Boolean(selection.decision_id) &&
      (!selected ||
        Boolean(
          selection.instrument_id && selection.record_kind === "candidate",
        )),
    staleTime: Infinity,
  });
};
export const replayInstrumentsOptions = (selection: WorkbenchSelection) => {
  const query = {
    ...tracePins(selection),
    session_date: selection.session_date,
    offset: selection.instrument_offset ?? 0,
    limit: REPLAY_PAGE_SIZE,
  };
  return queryOptions({
    queryKey: ["replay-instruments", selection.evaluation_id, query],
    queryFn: async ({ signal }) =>
      (
        await sdk.readReplayInstruments({
          path: { evaluation_id: selection.evaluation_id! },
          query,
          signal,
        })
      ).data,
    enabled: replayReady(selection),
    staleTime: Infinity,
  });
};
export const replayMarketOptions = (selection: WorkbenchSelection) => {
  const query = {
    ...tracePins(selection),
    instrument_id: selection.instrument_id!,
    start: selection.market_start,
    end: selection.market_end,
    offset: selection.market_offset ?? 0,
    limit: 100,
  };
  return queryOptions({
    queryKey: [
      "replay-market",
      selection.evaluation_id,
      selection.decision_id,
      query,
    ],
    queryFn: async ({ signal }) =>
      (
        await sdk.readReplayMarket({
          path: {
            evaluation_id: selection.evaluation_id!,
            decision_id: selection.decision_id!,
          },
          query,
          signal,
        })
      ).data,
    enabled:
      replayReady(selection) &&
      Boolean(selection.decision_id && selection.instrument_id),
    staleTime: Infinity,
  });
};
export const replayExecutionOptions = (selection: WorkbenchSelection) => {
  const query = {
    ...tracePins(selection),
    instrument_id: selection.instrument_id,
    stage_id: selection.stage_id,
    logical_order_id:
      selection.record_kind === "order" ? selection.record_id : undefined,
    offset: selection.execution_offset ?? 0,
    limit: REPLAY_PAGE_SIZE,
  };
  return queryOptions({
    queryKey: [
      "replay-execution",
      selection.evaluation_id,
      selection.decision_id,
      query,
    ],
    queryFn: async ({ signal }) =>
      (
        await sdk.readReplayExecution({
          path: {
            evaluation_id: selection.evaluation_id!,
            decision_id: selection.decision_id!,
          },
          query,
          signal,
        })
      ).data,
    enabled: replayReady(selection) && Boolean(selection.decision_id),
    staleTime: Infinity,
  });
};
