import { useQuery } from "@tanstack/react-query";
import { listRunEvents } from "@/client/sdk.gen";
import type { EventPublic } from "@/client/types.gen";
import { queryClient } from "./api";
import { mergeEvents, parseEvent } from "./events";

export function useRunEvents(runId: string, terminal: boolean) {
  return useQuery({
    queryKey: ["events", runId],
    queryFn: async ({ signal }) => {
      const previous =
        queryClient.getQueryData<EventPublic[]>(["events", runId]) || [];
      const after = previous.at(-1)?.seq || 0;
      const incoming: EventPublic[] = [];
      let failure: unknown;
      const result = await listRunEvents({
        path: { run_id: runId },
        query: { after_seq: after, follow: false },
        signal,
        sseMaxRetryAttempts: 1,
        onSseError: (error) => {
          failure = error;
        },
      });
      for await (const raw of result.stream)
        incoming.push(parseEvent(raw, runId));
      if (failure) throw failure;
      return mergeEvents(previous, incoming);
    },
    refetchInterval: terminal ? false : 2000,
  });
}
