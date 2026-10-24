import type { SpecRevisionCreate, WorkbenchSpec } from "@/client/types.gen";
import type { SourceResource } from "./workbench-api";
export type DescriptionDraft = { body: SpecRevisionCreate; key: string | null };
const storageKey = (owner: string, source: SourceResource) =>
  `tradegrad.description.${owner}.${source.kind}.${source.id}`;
export function newDescriptionDraft(spec: WorkbenchSpec): DescriptionDraft {
  return {
    key: null,
    body: {
      code_sha256: spec.code_sha256,
      base_revision: spec.revision,
      base_spec_sha256: spec.spec_sha256,
      change_note: "",
      rules: spec.spec.rules.map(({ rule_id, title, description }) => ({
        rule_id,
        title,
        description,
      })),
    },
  };
}
export function loadDescriptionDraft(
  owner: string,
  source: SourceResource,
): DescriptionDraft | null {
  const raw = sessionStorage.getItem(storageKey(owner, source));
  if (raw === null) return null;
  try {
    const value = JSON.parse(raw) as DescriptionDraft;
    if (
      !value?.body ||
      !(value.key === null || typeof value.key === "string") ||
      !/^[a-f0-9]{64}$/.test(value.body.code_sha256) ||
      !/^[a-f0-9]{64}$/.test(value.body.base_spec_sha256) ||
      !Number.isSafeInteger(value.body.base_revision) ||
      value.body.base_revision < 1 ||
      !Array.isArray(value.body.rules) ||
      !value.body.rules.length ||
      value.body.rules.some(
        (rule) =>
          typeof rule.rule_id !== "string" ||
          typeof rule.title !== "string" ||
          typeof rule.description !== "string",
      )
    )
      throw new Error("invalid");
    return value;
  } catch {
    throw new Error(
      "The saved description draft is unreadable. It was not replaced or submitted. Clear the local draft only if you no longer need it.",
    );
  }
}
export function storeDescriptionDraft(
  owner: string,
  source: SourceResource,
  draft: DescriptionDraft,
) {
  sessionStorage.setItem(storageKey(owner, source), JSON.stringify(draft));
}
export function forgetDescriptionDraft(owner: string, source: SourceResource) {
  sessionStorage.removeItem(storageKey(owner, source));
}
export function revisionFailureIsDefinitive(error: unknown) {
  const code = (error as { code?: string } | null)?.code;
  return [
    "spec_revision_conflict",
    "invalid_spec_revision",
    "idempotency_conflict",
    "source_integrity",
    "spec_integrity",
    "not_found",
    "validation_error",
  ].includes(code || "");
}

export function prepareDescriptionSubmission(
  draft: DescriptionDraft,
  spec: WorkbenchSpec,
): DescriptionDraft & { key: string } {
  if (draft.key) return { body: draft.body, key: draft.key };
  if (
    draft.body.code_sha256 !== spec.code_sha256 ||
    draft.body.base_revision !== spec.revision ||
    draft.body.base_spec_sha256 !== spec.spec_sha256
  )
    throw new Error(
      "Return to the draft's exact base description revision before saving. It was not rebased automatically.",
    );
  const rules = draft.body.rules.filter((rule) => {
    const original = spec.spec.rules.find(
      (item) => item.rule_id === rule.rule_id,
    );
    return (
      !original ||
      original.title !== rule.title ||
      original.description !== rule.description
    );
  });
  if (!rules.length)
    throw new Error(
      "Change at least one rule title or description before saving.",
    );
  return { body: { ...draft.body, rules }, key: crypto.randomUUID() };
}
