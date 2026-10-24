import { z } from "zod";
import { stableJson } from "./data-utils";
import { researchCaptureSchema } from "./research-capture";
import {
  workbenchSearch,
  type WorkbenchSelection,
} from "./workbench-selection";

const hash = z.string().regex(/^[a-f0-9]{64}$/);
export const independentSourceSchema = z.strictObject({
  source_kind: z.enum(["candidate", "strategy_version"]),
  source_id: z.uuid(),
  code_sha256: hash,
  spec_revision: z.number().int().min(1),
  spec_sha256: hash,
  evaluation_id: z.uuid(),
  execution_attempt_id: z.uuid(),
  result_manifest_sha256: hash,
  trace_manifest_sha256: hash.nullable(),
});
export const diagnosticRequestSchema = z.strictObject({
  source: independentSourceSchema,
  research_capture: researchCaptureSchema,
  max_runtime_seconds: z.literal(300),
});
export const controlledRequestSchema = z.strictObject({
  name: z.string().trim().min(1).max(255),
  hypothesis: z.string().trim().min(1).max(4000),
  planned_change: z.string().trim().min(1).max(4000),
  source: independentSourceSchema,
  arms: z
    .array(
      z.strictObject({
        name: z.string().trim().min(1).max(120),
        code: z
          .string()
          .min(1)
          .max(900000)
          .refine(
            (code) => new TextEncoder().encode(code).byteLength <= 900000,
            "Source must be at most 900000 UTF-8 bytes.",
          ),
        parameters: z.record(z.string(), z.json()).nullable().optional(),
        planned_change: z.string().max(4000).nullable().optional(),
      }),
    )
    .min(2)
    .max(3)
    .refine(
      (arms) => new Set(arms.map((arm) => arm.name)).size === arms.length,
      "Each arm needs a distinct name.",
    ),
  research_capture: researchCaptureSchema,
  max_runtime_seconds: z.literal(300),
});
export type DiagnosticRequest = z.infer<typeof diagnosticRequestSchema>;
export type ControlledRequest = z.infer<typeof controlledRequestSchema>;
export type IndependentSource = z.infer<typeof independentSourceSchema>;
const selectionSchema = z.record(z.string(), z.union([z.string(), z.number()]));
const draftSchema = z.discriminatedUnion("kind", [
  z.strictObject({
    version: z.literal(1),
    kind: z.literal("diagnostic"),
    key: z.uuid(),
    strategy_version_id: z.uuid(),
    body: diagnosticRequestSchema,
    selection: selectionSchema,
  }),
  z.strictObject({
    version: z.literal(1),
    kind: z.literal("controlled"),
    key: z.uuid(),
    strategy_version_id: z.uuid(),
    body: controlledRequestSchema,
    selection: selectionSchema,
  }),
]);
export type VerificationDraft = Omit<
  z.infer<typeof draftSchema>,
  "selection"
> & { selection: WorkbenchSelection };
export type VerificationKind = VerificationDraft["kind"];
const name = (owner: string, kind: VerificationKind) =>
  `tradegrad.verification.pending.v1.${owner}.${kind}`;

export function sourceSelection(source: IndependentSource): WorkbenchSelection {
  return {
    tab: "verification",
    candidate_id:
      source.source_kind === "candidate" ? source.source_id : undefined,
    code_sha256: source.code_sha256,
    spec_revision: source.spec_revision,
    spec_sha256: source.spec_sha256,
    evaluation_id: source.evaluation_id,
    execution_attempt_id: source.execution_attempt_id,
    result_manifest_sha256: source.result_manifest_sha256,
    trace_manifest_sha256: source.trace_manifest_sha256 ?? undefined,
    trace_status: source.trace_manifest_sha256 ? "available" : "not_collected",
  };
}
export function prepareVerificationDraft(
  kind: "diagnostic",
  body: DiagnosticRequest,
  strategyVersionId: string,
): VerificationDraft;
export function prepareVerificationDraft(
  kind: "controlled",
  body: ControlledRequest,
  strategyVersionId: string,
): VerificationDraft;
export function prepareVerificationDraft(
  kind: VerificationKind,
  body: DiagnosticRequest | ControlledRequest,
  strategyVersionId: string,
): VerificationDraft {
  const draft = {
    version: 1,
    kind,
    key: crypto.randomUUID(),
    strategy_version_id: strategyVersionId,
    body,
    selection: sourceSelection(body.source),
  };
  return validateDraft(JSON.parse(JSON.stringify(draft)));
}
function validateDraft(value: unknown): VerificationDraft {
  const parsed = draftSchema.parse(value);
  const selection = workbenchSearch(parsed.selection);
  if (stableJson(selection) !== stableJson(sourceSelection(parsed.body.source)))
    throw new Error(
      "The retained source selection differs from the immutable request.",
    );
  return { ...parsed, selection };
}
export function loadVerificationDraft(
  owner: string,
  kind: VerificationKind,
): VerificationDraft | null {
  const raw = sessionStorage.getItem(name(owner, kind));
  if (raw === null) return null;
  try {
    return validateDraft(JSON.parse(raw));
  } catch {
    throw new Error(
      "The pending verification request is unreadable. No replacement job was submitted. Restore storage or explicitly clear this local request after checking your existing jobs.",
    );
  }
}
export function storeVerificationDraft(
  owner: string,
  draft: VerificationDraft,
) {
  validateDraft(JSON.parse(JSON.stringify(draft)));
  const existing = loadVerificationDraft(owner, draft.kind);
  if (existing && stableJson(existing) !== stableJson(draft))
    throw new Error(
      "An unresolved verification request is already retained. Resolve the original outcome before creating another job.",
    );
  sessionStorage.setItem(name(owner, draft.kind), JSON.stringify(draft));
  if (
    stableJson(loadVerificationDraft(owner, draft.kind)) !== stableJson(draft)
  )
    throw new Error(
      "The exact verification request could not be retained. No request was sent.",
    );
}
export function clearVerificationDraft(
  owner: string,
  kind: VerificationKind,
  key: string,
) {
  if (loadVerificationDraft(owner, kind)?.key === key)
    sessionStorage.removeItem(name(owner, kind));
}
export function clearUnreadableVerificationDraft(
  owner: string,
  kind: VerificationKind,
) {
  sessionStorage.removeItem(name(owner, kind));
}
export function verificationCompletionCurrent(
  draft: VerificationDraft,
  current: {
    active: boolean;
    key: string | null;
    source: IndependentSource | null;
    body: DiagnosticRequest | ControlledRequest | null;
  },
) {
  return (
    current.active &&
    current.key === draft.key &&
    current.source !== null &&
    stableJson(current.source) === stableJson(draft.body.source) &&
    stableJson(current.body) === stableJson(draft.body)
  );
}
const actionSchema = z.strictObject({
  action: z.enum(["cancel", "retry"]),
  key: z.uuid(),
  generation: z.number().int().min(0),
  config_sha256: hash,
});
export type VerificationAction = z.infer<typeof actionSchema>;
const actionName = (owner: string, run: string) =>
  `tradegrad.verification.action.v1.${owner}.${run}`;
export function loadVerificationAction(
  owner: string,
  run: string,
): VerificationAction | null {
  const raw = sessionStorage.getItem(actionName(owner, run));
  if (raw === null) return null;
  try {
    return actionSchema.parse(JSON.parse(raw));
  } catch {
    throw new Error(
      "The pending independent action is unreadable. No replacement cancel or retry was sent.",
    );
  }
}
export function storeVerificationAction(
  owner: string,
  run: string,
  action: VerificationAction,
) {
  actionSchema.parse(action);
  const previous = loadVerificationAction(owner, run);
  if (previous && stableJson(previous) !== stableJson(action))
    throw new Error(
      "Resolve the retained independent action before sending another action.",
    );
  sessionStorage.setItem(actionName(owner, run), JSON.stringify(action));
  if (stableJson(loadVerificationAction(owner, run)) !== stableJson(action))
    throw new Error(
      "The exact independent action could not be retained. No request was sent.",
    );
}
export function clearVerificationAction(
  owner: string,
  run: string,
  key: string,
) {
  if (loadVerificationAction(owner, run)?.key === key)
    sessionStorage.removeItem(actionName(owner, run));
}
export function verificationActionResolved(
  action: VerificationAction,
  status: { config_sha256: string; generation: number; status: string },
) {
  return (
    action.config_sha256 === status.config_sha256 &&
    status.generation >= action.generation &&
    (action.action === "retry"
      ? status.generation > action.generation
      : [
          "cancel_requested",
          "cancelled",
          "failed",
          "interrupted",
          "succeeded",
        ].includes(status.status))
  );
}

/** Pydantic publishes declared defaults; normalize those only for request equality. */
export function verificationRequestFingerprint(
  request: DiagnosticRequest | ControlledRequest | Record<string, unknown>,
): string {
  const capture = request.research_capture as
    Record<string, unknown> | undefined;
  const normalized = {
    ...request,
    max_runtime_seconds: request.max_runtime_seconds ?? 300,
    research_capture: {
      mode: "research",
      observation_window: null,
      stage_ids: [],
      instrument_ids: [],
      max_decisions: 10000,
      max_detail_rows: 2000000,
      max_uncompressed_bytes: 268435456,
      ...capture,
    },
    ...("arms" in request
      ? {
          arms: (request.arms as ControlledRequest["arms"]).map((arm) => ({
            ...arm,
            parameters: arm.parameters ?? null,
            planned_change: arm.planned_change ?? null,
          })),
        }
      : {}),
  };
  return stableJson(normalized);
}
