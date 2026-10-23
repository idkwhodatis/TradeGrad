import { queryOptions } from "@tanstack/react-query";
import { readOperations } from "@/client/sdk.gen";
import type { DurationObservation } from "@/client/types.gen";

export const operationsOptions = (
  scope: "owner" | "deployment" = "owner",
  days = 7,
) =>
  queryOptions({
    queryKey: ["operations", scope, days],
    queryFn: async ({ signal }) =>
      (
        await readOperations({
          query: { scope, days },
          signal,
          throwOnError: true,
        })
      ).data,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
export function formatDuration(value: DurationObservation) {
  if (value.mean_seconds === null || value.mean_seconds === undefined)
    return "Unavailable: no complete observed durations";
  return `${value.mean_seconds.toFixed(2)}s mean · ${value.p95_seconds?.toFixed(2)}s p95 · n=${value.count}`;
}
export function observationStale(
  observedAt: string,
  ttlSeconds: number,
  now = Date.now(),
) {
  const time = Date.parse(observedAt);
  return (
    !Number.isFinite(time) ||
    time > now + 5_000 ||
    now - time > ttlSeconds * 1000
  );
}
