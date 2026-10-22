// Display helpers do not reinterpret a provider's unknown semantics as raw prices.
export function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Unknown";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function stableJson(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) =>
    item && typeof item === "object" && !Array.isArray(item)
      ? Object.fromEntries(
          Object.entries(item).sort(([left], [right]) =>
            left.localeCompare(right),
          ),
        )
      : item,
  );
}

export const importTerminal = new Set([
  "ready",
  "partial",
  "failed",
  "cancelled",
]);
export const universeLabels: Record<string, string> = {
  user_fixed: "User-fixed basket · not verified PIT",
  current_snapshot: "Current membership · not historical PIT",
  historical_membership: "Historical membership · availability unverified",
  point_in_time_verified:
    "Point-in-time verified · inspect supporting evidence",
};

export function universeEvidenceError(
  state: string,
  reference: string,
  note: string,
): string | null {
  if (!reference.trim())
    return state !== "user_fixed" || note.trim()
      ? "Current and historical memberships need a nonempty evidence reference. Notes also need a reference; supplied evidence remains unverified."
      : null;
  // Match the server's ASCII-escaped JSON budget (three key/value spaces and
  // two separator spaces), including multibyte references and escaped notes.
  const serialized = JSON.stringify({
    reference: reference.trim(),
    note,
    verification: "user_supplied_not_independently_verified",
  }).replace(
    /[\u007f-\uffff]/g,
    (character) =>
      `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
  return serialized.length + 5 > 16_384
    ? "The evidence reference and note must fit within a 16 KiB serialized record. Shorten them before publishing."
    : null;
}

export function parseSymbols(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[\s,;]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

export function dateWindowError(start: string, end: string): string | null {
  const valid = (value: string) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const date = new Date(`${value}T00:00:00Z`);
    return (
      Number.isFinite(date.valueOf()) &&
      date.toISOString().slice(0, 10) === value
    );
  };
  if (!valid(start) || !valid(end)) return "Choose valid start and end dates.";
  return end > start
    ? null
    : "End date must be after start date. The end is exclusive.";
}

// Per-owner and per-operation keys survive uncertain network outcomes. A changed
// request gets a new key. Clearing happens only after an acknowledged response.
export function dataSubmissionKey(
  owner: string,
  operation: string,
  body: unknown,
): string {
  const storageKey = `tradegrad.data-submission.${owner}.${operation}`;
  const fingerprint = stableJson(body);
  try {
    const previous = JSON.parse(sessionStorage.getItem(storageKey) || "null");
    if (
      previous?.fingerprint === fingerprint &&
      typeof previous.key === "string"
    )
      return previous.key;
  } catch {
    /* Corrupt tab storage is not an authorization boundary. */
  }
  const key = crypto.randomUUID();
  sessionStorage.setItem(storageKey, JSON.stringify({ key, fingerprint }));
  return key;
}
export function clearDataSubmission(owner: string, operation: string) {
  sessionStorage.removeItem(`tradegrad.data-submission.${owner}.${operation}`);
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
export function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter(
        (item) => item && typeof item === "object" && !Array.isArray(item),
      )
    : [];
}
export function snapshotLabel(
  dataset: Pick<
    import("@/client/types.gen").DatasetPublic,
    "is_synthetic" | "manifest" | "source"
  >,
): string {
  const evidence = records(dataset.manifest.provenance).map(
    (item) => item.evidence,
  );
  if (evidence.includes("fixture") || dataset.manifest.evidence === "fixture")
    return "Provider fixture · not live";
  if (dataset.is_synthetic) return "Synthetic demo";
  if (dataset.source === "upload") return "User upload · unverified source";
  if (evidence.includes("live")) return "Live-provider snapshot";
  return "Published · inspect provenance";
}
