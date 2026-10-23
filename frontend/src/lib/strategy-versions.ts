import type { StrategyCreate, StrategyPublic } from "@/client/types.gen";

export const MAX_STRATEGY_BYTES = 131_072;
export const strategyTasks = [
  "cn_time",
  "us_time",
  "cn_cross",
  "us_cross",
] as const;
export type StrategySubmission = { key: string; body: StrategyCreate };
export type StrategyDraft = {
  body: StrategyCreate;
  contractText?: string;
  // Keep the exact request after any ambiguous result, including a tab reload.
  pending: StrategySubmission | null;
};
export const emptyStrategy = (): StrategyCreate => ({
  name: "",
  task: "cn_time",
  code: "",
  parent_id: null,
});
export const strategyBytes = (code: string) =>
  new TextEncoder().encode(code).byteLength;
const fingerprint = (body: StrategyCreate) =>
  JSON.stringify([
    body.name,
    body.task,
    body.code,
    body.parent_id ?? null,
    body.manifest ?? null,
  ]);

export function strategySubmissionFor(
  body: StrategyCreate,
  previous?: StrategySubmission | null,
): StrategySubmission {
  return previous && fingerprint(previous.body) === fingerprint(body)
    ? previous
    : { key: crypto.randomUUID(), body: structuredClone(body) };
}

export function strategyInputError(body: StrategyCreate): string | null {
  if (
    !body.name.trim() ||
    [...body.name].length > 255 ||
    /[\u0000-\u001f\u007f]/.test(body.name)
  )
    return "Enter a strategy name with 1–255 characters and no control characters.";
  if (!strategyTasks.includes(body.task))
    return "Choose a supported strategy task.";
  if (!body.code.trim() || body.code.includes("\0"))
    return "Add Python source without NUL bytes.";
  if (strategyBytes(body.code) > MAX_STRATEGY_BYTES)
    return "Python source must not exceed 128 KiB of UTF-8 text.";
  if (
    new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
      new TextEncoder().encode(body.code),
    ) !== body.code
  )
    return "Python source must be valid UTF-8 text.";
  return null;
}

// Never infer upstream provenance from a display name, including the word Original.
export function strategyOrigin(
  strategy: Pick<StrategyPublic, "parent_id">,
): string {
  return strategy.parent_id
    ? "Derived version · parent linked"
    : "Uploaded or provisioned source · origin not verified";
}

const storageKey = (owner: string) => `tradegrad.strategy-draft.v1.${owner}`;
function validDraftBody(value: unknown): value is StrategyCreate {
  if (!value || typeof value !== "object") return false;
  const body = value as StrategyCreate;
  return (
    typeof body.name === "string" &&
    [...body.name].length <= 255 &&
    typeof body.code === "string" &&
    strategyBytes(body.code) <= MAX_STRATEGY_BYTES &&
    strategyTasks.includes(body.task) &&
    (body.parent_id == null ||
      (typeof body.parent_id === "string" &&
        /^[a-f\d-]{36}$/i.test(body.parent_id)))
  );
}
export function loadStrategyDraft(owner: string): StrategyDraft | null {
  try {
    const raw = sessionStorage.getItem(storageKey(owner));
    if (!raw || raw.length > MAX_STRATEGY_BYTES * 12 + 4000) return null;
    const draft = JSON.parse(raw) as StrategyDraft;
    if (!validDraftBody(draft?.body)) return null;
    if (
      draft.pending !== null &&
      (!draft.pending ||
        typeof draft.pending.key !== "string" ||
        !/^[!-~]{1,128}$/.test(draft.pending.key) ||
        !validDraftBody(draft.pending.body) ||
        fingerprint(draft.pending.body) !== fingerprint(draft.body))
    )
      return null;
    return draft;
  } catch {
    return null;
  }
}
export function saveStrategyDraft(owner: string, value: StrategyDraft): void {
  // A failed write must stop submission, not silently lose the retry key.
  sessionStorage.setItem(storageKey(owner), JSON.stringify(value));
}
export function clearStrategyDraft(owner: string): void {
  sessionStorage.removeItem(storageKey(owner));
}
export function isDefinitiveStrategyFailure(error: unknown): boolean {
  const code = (error as { code?: string } | null)?.code;
  return (
    typeof code === "string" &&
    new Set([
      "strategy_syntax_error",
      "validation_error",
      "invalid_idempotency_key",
      "incompatible_parent",
      "not_found",
      "idempotency_conflict",
      "unauthorized",
      "request_too_large",
    ]).has(code)
  );
}
export async function readStrategyUpload(
  file: Pick<File, "name" | "size" | "arrayBuffer">,
): Promise<string> {
  if (!/\.py$/i.test(file.name)) throw new Error("Choose a Python .py file.");
  if (!file.size || file.size > MAX_STRATEGY_BYTES)
    throw new Error("Choose a nonempty Python file no larger than 128 KiB.");
  const bytes = await file.arrayBuffer();
  if (!bytes.byteLength || bytes.byteLength > MAX_STRATEGY_BYTES)
    throw new Error("Python source must not exceed 128 KiB.");
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
      bytes,
    );
  } catch {
    throw new Error(
      "The file is not valid UTF-8. Save it as UTF-8 and upload again.",
    );
  }
}
