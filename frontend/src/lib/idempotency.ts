import type { RunCreate } from "@/client/types.gen";

// The same logical submission keeps its key across network failures and reloads.
// Any changed input is a different submission, never an ambiguous reuse of a key.
export type Submission = { key: string; body: RunCreate };
const fingerprint = (body: RunCreate) =>
  JSON.stringify([
    body.task,
    body.dataset_version_id,
    body.strategy_version_id,
    body.start_date,
    body.end_date,
    body.profile || "corrected_explicit_config",
  ]);
export function submissionFor(
  body: RunCreate,
  previous?: Submission | null,
): Submission {
  return previous && fingerprint(previous.body) === fingerprint(body)
    ? previous
    : { key: crypto.randomUUID(), body };
}
export function loadSubmission(owner: string): Submission | null {
  try {
    const data = JSON.parse(
      sessionStorage.getItem(`tradegrad.submission.${owner}`) || "null",
    ) as Submission | null;
    return data && typeof data.key === "string" && data.body ? data : null;
  } catch {
    return null;
  }
}
export function saveSubmission(owner: string, value: Submission) {
  sessionStorage.setItem(
    `tradegrad.submission.${owner}`,
    JSON.stringify(value),
  );
}
export function forgetSubmission(owner: string) {
  sessionStorage.removeItem(`tradegrad.submission.${owner}`);
}
