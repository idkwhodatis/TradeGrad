import {
  clearDataSubmission,
  dataSubmissionKey,
  stableJson,
} from "./data-utils";

// An automatic catalog refresh (including revision 0 -> 1 on first use) must
// never turn an uncertain response into a second job. Preserve the submission's
// expected revision until acknowledged, rejected, or the actual user input changes.
export function providerSubmission<
  T extends { expected_connection_revision?: number | null },
>(owner: string, operation: string, input: T) {
  const storageKey = `tradegrad.provider-submission.${owner}.${operation}`;
  const { expected_connection_revision, ...logical } = input;
  const fingerprint = stableJson(logical);
  let revision = expected_connection_revision;
  try {
    const previous = JSON.parse(sessionStorage.getItem(storageKey) || "null");
    if (
      previous?.fingerprint === fingerprint &&
      (previous.revision === null ||
        (Number.isInteger(previous.revision) && previous.revision >= 0))
    ) {
      revision = previous.revision;
    }
  } catch {
    /* Malformed tab state is not an authorization boundary. */
  }
  const body = { ...input, expected_connection_revision: revision ?? null };
  sessionStorage.setItem(
    storageKey,
    JSON.stringify({ fingerprint, revision: revision ?? null }),
  );
  return { body, key: dataSubmissionKey(owner, operation, body) };
}

export function clearProviderSubmission(owner: string, operation: string) {
  sessionStorage.removeItem(
    `tradegrad.provider-submission.${owner}.${operation}`,
  );
  clearDataSubmission(owner, operation);
}

export function connectionAdmissionRejected(error: unknown) {
  return Boolean(
    error &&
    typeof error === "object" &&
    "code" in error &&
    ["provider_connection_conflict", "provider_connection_disabled"].includes(
      String(error.code),
    ),
  );
}

// Read-only render check. A disabled source cannot admit anything new, but the
// backend can safely replay a previously accepted idempotency key.
export function hasPendingProviderSubmission<
  T extends { expected_connection_revision?: number | null },
>(owner: string, operation: string, input: T) {
  const { expected_connection_revision: _revision, ...logical } = input;
  try {
    const previous = JSON.parse(
      sessionStorage.getItem(
        `tradegrad.provider-submission.${owner}.${operation}`,
      ) || "null",
    );
    const submission = JSON.parse(
      sessionStorage.getItem(
        `tradegrad.data-submission.${owner}.${operation}`,
      ) || "null",
    );
    return (
      previous?.fingerprint === stableJson(logical) &&
      typeof submission?.key === "string"
    );
  } catch {
    return false;
  }
}
