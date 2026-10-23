import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";
import * as sdk from "@/client/sdk.gen";
import type {
  ResearchRunCreate,
  ResearchPreflightPublic,
} from "@/client/types.gen";
import { stableJson } from "./data-utils";
import {
  researchFormSchema,
  type ResearchCatalog,
  type ResearchFormValues,
} from "./research-form";

export const researchCapabilitiesOptions = queryOptions({
  queryKey: ["research-capabilities"],
  staleTime: 0,
  queryFn: async ({ signal }) =>
    (await sdk.researchCapabilities({ signal })).data,
});

// Selectors must not silently omit versions past the first catalog page.
export async function collectResearchPages<T>(
  read: (offset: number) => Promise<{ data: T[]; count: number }>,
): Promise<T[]> {
  const rows: T[] = [];
  for (;;) {
    const page = await read(rows.length);
    rows.push(...page.data);
    if (rows.length >= page.count) return rows;
    if (!page.data.length)
      throw new Error(
        "The version catalog changed while loading. Reload the catalog before choosing inputs.",
      );
  }
}
export const researchCatalogOptions = queryOptions({
  queryKey: ["research-input-catalog"],
  queryFn: async ({ signal }): Promise<ResearchCatalog> => {
    const [datasets, strategies, universes] = await Promise.all([
      collectResearchPages(
        async (offset) =>
          (await sdk.listDatasets({ query: { offset, limit: 100 }, signal }))
            .data,
      ),
      collectResearchPages(
        async (offset) =>
          (await sdk.listStrategies({ query: { offset, limit: 100 }, signal }))
            .data,
      ),
      collectResearchPages(
        async (offset) =>
          (
            await sdk.listDataUniverses({
              query: { offset, limit: 100 },
              signal,
            })
          ).data,
      ),
    ]);
    return { datasets, strategies, universes };
  },
});
export async function preflightResearch(body: ResearchRunCreate) {
  return (await sdk.preflightResearchRun({ body })).data;
}
export async function submitResearch(body: ResearchRunCreate, key: string) {
  return (
    await sdk.createResearchRun({ body, headers: { "Idempotency-Key": key } })
  ).data;
}

export const researchFingerprint = (body: ResearchRunCreate) =>
  stableJson(body);
export type ResearchSubmission = { key: string; body: ResearchFormValues };
export type ResearchDraft = {
  version: 1;
  body: ResearchFormValues;
  step: number;
  submission: ResearchSubmission | null;
};
export type ResearchPreflight = {
  fingerprint: string;
  result: ResearchPreflightPublic;
};
export function matchingPreflight(
  body: ResearchRunCreate,
  preflight: ResearchPreflight | null,
) {
  return (
    preflight?.result.valid === true &&
    preflight.fingerprint === researchFingerprint(body)
  );
}
export function researchSubmissionFor(
  body: ResearchFormValues,
  previous?: ResearchSubmission | null,
): ResearchSubmission {
  const parsed = researchFormSchema.parse(body);
  return previous &&
    researchFingerprint(previous.body) === researchFingerprint(parsed)
    ? previous
    : { key: crypto.randomUUID(), body: structuredClone(parsed) };
}

const storageKey = (owner: string) => `tradegrad.research-draft.v1.${owner}`;
export function hasStoredResearchDraft(owner: string): boolean {
  try {
    return sessionStorage.getItem(storageKey(owner)) !== null;
  } catch {
    return false;
  }
}
// Incomplete dates/references are useful draft state, but unknown fields and any
// credential-bearing model are rejected. Persist form data, never auth transport.
const draftRef = z.strictObject({
  id: z.string().regex(/^[a-f0-9-]{0,36}$/),
  sha256: z.string().regex(/^[a-f0-9]{0,64}$/),
});
const draftWindow = z.strictObject({
  start: z.string().regex(/^(\d{4}-\d{2}-\d{2})?$/),
  end: z.string().regex(/^(\d{4}-\d{2}-\d{2})?$/),
});
const draftBodySchema = z.strictObject({
  ...researchFormSchema.shape,
  dataset: draftRef,
  universe: draftRef,
  seed_strategy: draftRef,
  windows: z.strictObject({
    train: draftWindow,
    validation: draftWindow,
    holdout: draftWindow.nullable(),
  }),
});
const submissionSchema = z.strictObject({
  key: z.uuid(),
  body: researchFormSchema,
});
const draftSchema = z.strictObject({
  version: z.literal(1),
  body: draftBodySchema,
  step: z.number().int().min(0).max(3),
  submission: submissionSchema.nullable(),
});
export function loadResearchDraft(owner: string): ResearchDraft | null {
  try {
    const parsed = draftSchema.safeParse(
      JSON.parse(sessionStorage.getItem(storageKey(owner)) || "null"),
    );
    return parsed.success ? (parsed.data as ResearchDraft) : null;
  } catch {
    return null;
  }
}
export function saveResearchDraft(
  owner: string,
  draft: ResearchDraft,
): boolean {
  try {
    const parsed = draftSchema.safeParse(draft);
    if (!parsed.success) return false;
    sessionStorage.setItem(storageKey(owner), JSON.stringify(parsed.data));
    return true;
  } catch {
    return false;
  }
}
/** An accepted request must still belong to this mounted form, not a newer visit. */
export function researchCompletionIsCurrent(
  completed: ResearchSubmission,
  current: {
    active: boolean;
    body: ResearchFormValues;
    submission: ResearchSubmission | null;
  },
): boolean {
  return (
    current.active &&
    current.submission?.key === completed.key &&
    researchFingerprint(current.submission.body) ===
      researchFingerprint(completed.body) &&
    researchFingerprint(current.body) === researchFingerprint(completed.body)
  );
}

/** Compare and remove synchronously: an old response never erases a newer draft/key. */
export function clearAcceptedResearchDraft(
  owner: string,
  completed: ResearchSubmission,
): boolean {
  try {
    const current = loadResearchDraft(owner);
    if (
      !current ||
      !researchCompletionIsCurrent(completed, {
        active: true,
        body: current.body,
        submission: current.submission,
      })
    )
      return false;
    sessionStorage.removeItem(storageKey(owner));
    return true;
  } catch {
    return false;
  }
}

export function forgetResearchDraft(owner: string) {
  try {
    sessionStorage.removeItem(storageKey(owner));
  } catch {
    /* A retained retry key remains safe if browser storage cannot be cleared. */
  }
}

/** Synchronous guard includes rapid repeated clicks before React re-renders. */
export function singleFlight<T>() {
  let pending: Promise<T> | null = null;
  return (action: () => Promise<T>): Promise<T> => {
    if (pending) return pending;
    pending = Promise.resolve()
      .then(action)
      .finally(() => {
        pending = null;
      });
    return pending;
  };
}
