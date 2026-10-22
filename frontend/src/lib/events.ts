import type { EventPublic } from "@/client/types.gen";

export function parseEvent(value: unknown, runId: string): EventPublic {
  const event = typeof value === "string" ? JSON.parse(value) : value;
  if (
    !event ||
    typeof event !== "object" ||
    event.schema_version !== 1 ||
    event.run_id !== runId ||
    !Number.isSafeInteger(event.seq) ||
    event.seq < 1 ||
    typeof event.type !== "string" ||
    typeof event.occurred_at !== "string" ||
    !event.payload ||
    typeof event.payload !== "object"
  ) {
    throw new Error(
      "The server returned an incompatible run event. Refresh or contact your administrator.",
    );
  }
  return event as EventPublic;
}
export function mergeEvents(
  previous: EventPublic[],
  incoming: EventPublic[],
): EventPublic[] {
  const entries = new Map(previous.map((event) => [event.seq, event]));
  for (const event of incoming) entries.set(event.seq, event);
  return [...entries.values()].sort((a, b) => a.seq - b.seq);
}
