export function researchConfig(value: unknown): boolean {
  return (
    value !== null &&
    typeof value === "object" &&
    "schema_version" in value &&
    value.schema_version === "tradegrad.run.v1"
  );
}

export function runDetailRoute(config: unknown) {
  return researchConfig(config)
    ? ("/research-runs/$runId" as const)
    : ("/runs/$runId" as const);
}

export function comparisonSearch(value: unknown): string {
  if (typeof value !== "string" || value.length > 147) return "";
  const ids = value.split(",");
  const uuid =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (ids.length > 4 || ids.some((id) => !uuid.test(id))) return "";
  return [...new Set(ids.map((id) => id.toLowerCase()))].join(",");
}
