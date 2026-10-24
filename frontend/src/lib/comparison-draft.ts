import { z } from "zod";
import type { ComparisonCreate } from "@/client/types.gen";
import {
  comparisonFingerprint,
  comparisonRequest,
} from "./comparison-evidence";
import {
  comparisonSidePatch,
  comparisonSideSelection,
  workbenchSearch,
  type WorkbenchSelection,
} from "./workbench-selection";
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const side = z.strictObject({
  evaluation_id: z.uuid(),
  execution_attempt_id: z.uuid(),
  result_manifest_sha256: hash,
  trace_manifest_sha256: hash.nullable(),
  source_kind: z.enum(["candidate", "strategy_version"]),
  source_id: z.uuid(),
  code_sha256: hash,
  spec_revision: z.number().int().min(1),
  spec_sha256: hash,
});
const bodySchema = z.strictObject({
  left: side,
  right: side,
  options: z.strictObject({ max_runtime_seconds: z.literal(300) }),
});
const draftSchema = z.strictObject({
  version: z.literal(1),
  key: z.uuid(),
  body: bodySchema,
  selection: z.record(z.string(), z.union([z.string(), z.number()])),
});
export type ComparisonDraft = {
  version: 1;
  key: string;
  body: ComparisonCreate;
  selection: WorkbenchSelection;
};
const storageKey = (owner: string) =>
  `tradegrad.comparison.pending.v1.${owner}`;
export function prepareComparisonDraft(
  selection: WorkbenchSelection,
): ComparisonDraft {
  const body = comparisonRequest(selection);
  return {
    version: 1,
    key: crypto.randomUUID(),
    body,
    selection: {
      ...comparisonSidePatch(
        "left",
        comparisonSideSelection(selection, "left"),
      ),
      ...comparisonSidePatch(
        "right",
        comparisonSideSelection(selection, "right"),
      ),
    },
  };
}
export function loadComparisonDraft(owner: string): ComparisonDraft | null {
  const raw = sessionStorage.getItem(storageKey(owner));
  if (raw === null) return null;
  try {
    const parsed = draftSchema.parse(JSON.parse(raw));
    const selection = workbenchSearch(parsed.selection);
    if (
      comparisonFingerprint(comparisonRequest(selection)) !==
      comparisonFingerprint(parsed.body)
    )
      throw new Error("mismatch");
    return { ...parsed, selection };
  } catch {
    throw new Error(
      "The pending comparison request is unreadable. It was not replaced or submitted. Restore browser storage or explicitly clear the unreadable local request.",
    );
  }
}
export function storeComparisonDraft(owner: string, value: ComparisonDraft) {
  draftSchema.parse(JSON.parse(JSON.stringify(value)));
  sessionStorage.setItem(storageKey(owner), JSON.stringify(value));
  if (loadComparisonDraft(owner)?.key !== value.key)
    throw new Error(
      "The exact retry request could not be retained. No request was sent.",
    );
}
export function comparisonCompletionCurrent(
  completed: ComparisonDraft,
  current: {
    active: boolean;
    selection: WorkbenchSelection;
    key: string | null;
  },
) {
  if (!current.active || completed.key !== current.key) return false;
  try {
    return (
      comparisonFingerprint(completed.body) ===
      comparisonFingerprint(comparisonRequest(current.selection))
    );
  } catch {
    return false;
  }
}
export function clearComparisonDraft(owner: string, key: string) {
  if (loadComparisonDraft(owner)?.key === key)
    sessionStorage.removeItem(storageKey(owner));
}
export function clearUnreadableComparisonDraft(owner: string) {
  sessionStorage.removeItem(storageKey(owner));
}
export type ComparisonAction = "cancel" | "retry";
const actionKey = (
  owner: string,
  id: string,
  action: ComparisonAction,
  generation: number,
) => `tradegrad.comparison.action.v1.${owner}.${id}.${action}.${generation}`;
/** Generation-scoped intents survive refresh/lost responses, never become a new retry. */
export function comparisonActionKey(
  owner: string,
  id: string,
  action: ComparisonAction,
  generation: number,
) {
  const name = actionKey(owner, id, action, generation);
  const old = sessionStorage.getItem(name);
  if (old !== null) return z.uuid().parse(old);
  const key = crypto.randomUUID();
  sessionStorage.setItem(name, key);
  if (sessionStorage.getItem(name) !== key)
    throw new Error(
      "The exact action retry key could not be retained. No request was sent.",
    );
  return key;
}
const pendingActionSchema = z.strictObject({
  action: z.enum(["cancel", "retry"]),
  key: z.uuid(),
  generation: z.number().int().min(0),
  input_sha256: hash,
});
export type PendingComparisonAction = z.infer<typeof pendingActionSchema>;
const pendingActionName = (owner: string, id: string) =>
  `tradegrad.comparison.pending-action.v1.${owner}.${id}`;
export function loadComparisonAction(
  owner: string,
  id: string,
): PendingComparisonAction | null {
  const raw = sessionStorage.getItem(pendingActionName(owner, id));
  if (raw === null) return null;
  try {
    return pendingActionSchema.parse(JSON.parse(raw));
  } catch {
    throw new Error(
      "The pending comparison action is unreadable. No replacement cancel or retry was sent. Refresh durable status before explicitly clearing this local request.",
    );
  }
}
export function storeComparisonAction(
  owner: string,
  id: string,
  value: PendingComparisonAction,
) {
  pendingActionSchema.parse(value);
  sessionStorage.setItem(pendingActionName(owner, id), JSON.stringify(value));
  if (loadComparisonAction(owner, id)?.key !== value.key)
    throw new Error(
      "The exact action could not be retained. No request was sent.",
    );
}
export function clearComparisonAction(owner: string, id: string, key: string) {
  if (loadComparisonAction(owner, id)?.key === key)
    sessionStorage.removeItem(pendingActionName(owner, id));
}
export function comparisonActionResolved(
  pending: PendingComparisonAction,
  status: { input_sha256: string; generation: number; status: string },
) {
  return (
    pending.input_sha256 === status.input_sha256 &&
    (pending.action === "retry"
      ? status.generation > pending.generation
      : [
          "cancel_requested",
          "cancelled",
          "succeeded",
          "failed",
          "interrupted",
        ].includes(status.status))
  );
}
