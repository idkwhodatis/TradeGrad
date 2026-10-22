export function shortId(value: string) {
  return value.slice(0, 8);
}
export function label(value: string) {
  return value.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
}
export function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("en", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "UTC",
      }).format(date);
}
export function formatBytes(bytes: number) {
  return bytes < 1024
    ? `${bytes} B`
    : bytes < 1024 * 1024
      ? `${(bytes / 1024).toFixed(1)} KB`
      : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
export const taskLabels: Record<string, string> = {
  cn_time: "CN · Time series",
  cn_cross: "CN · Cross-sectional",
  us_time: "US · Time series",
  us_cross: "US · Cross-sectional",
};
export const terminalStatuses = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "paused",
]);
