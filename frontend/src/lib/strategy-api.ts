import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type { StrategySubmission } from "./strategy-versions";

export const STRATEGY_PAGE_SIZE = 24;
export const strategyPageOptions = (owner: string, offset: number) =>
  queryOptions({
    queryKey: ["strategies", owner, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.listStrategies({
          query: { offset, limit: STRATEGY_PAGE_SIZE },
          signal,
        })
      ).data,
  });
export async function saveStrategyVersion(submission: StrategySubmission) {
  return (
    await sdk.createStrategyVersion({
      body: submission.body,
      headers: { "Idempotency-Key": submission.key },
    })
  ).data;
}
