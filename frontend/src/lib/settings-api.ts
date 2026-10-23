import { queryOptions } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type {
  ManagedUserCreate,
  ManagedUserUpdate,
  QuotaUpdate,
  ResearchCapabilities,
  WorkspaceDefaults,
} from "@/client/types.gen";
import {
  researchModelProfiles,
  type ResearchFormValues,
} from "./research-form";

export const settingsDefaultsOptions = queryOptions({
  queryKey: ["workspace-defaults"],
  queryFn: async ({ signal }) =>
    (await sdk.readWorkspaceDefaults({ signal })).data!,
});
export const settingsStatusOptions = queryOptions({
  queryKey: ["settings-status"],
  refetchInterval: 30_000,
  queryFn: async ({ signal }) =>
    (await sdk.readSettingsStatus({ signal })).data!,
});
export const settingsQuotaOptions = queryOptions({
  queryKey: ["workspace-quota"],
  queryFn: async ({ signal }) =>
    (await sdk.readWorkspaceQuota({ signal })).data!,
});
export const managedUsersOptions = (offset = 0) =>
  queryOptions({
    queryKey: ["managed-users", offset],
    queryFn: async ({ signal }) =>
      (await sdk.listManagedUsers({ signal, query: { offset, limit: 50 } }))
        .data!,
  });
export const managedQuotaOptions = (id: string) =>
  queryOptions({
    queryKey: ["managed-quota", id],
    enabled: !!id,
    queryFn: async ({ signal }) =>
      (await sdk.readManagedQuota({ signal, path: { user_id: id } })).data!,
  });
export const managedReferencesOptions = (id: string) =>
  queryOptions({
    queryKey: ["managed-references", id],
    enabled: !!id,
    queryFn: async ({ signal }) =>
      (await sdk.listManagedSecretReferences({ signal, path: { user_id: id } }))
        .data!,
  });
export const saveProfile = async (full_name: string) =>
  (await sdk.updateSelfProfile({ body: { full_name } })).data!;
export const saveDefaults = async (body: WorkspaceDefaults) =>
  (await sdk.updateWorkspaceDefaults({ body })).data!;
export const createManagedUser = async (body: ManagedUserCreate) =>
  (await sdk.createManagedUser({ body })).data!;
export const saveManagedUser = async (id: string, body: ManagedUserUpdate) =>
  (await sdk.updateManagedUser({ path: { user_id: id }, body })).data!;
export const saveManagedQuota = async (id: string, body: QuotaUpdate) =>
  (await sdk.updateManagedQuota({ path: { user_id: id }, body })).data!;
export const saveManagedReference = async (
  id: string,
  profileId: string,
  enabled: boolean,
) =>
  (
    await sdk.updateManagedSecretReference({
      path: { user_id: id, profile_id: profileId },
      body: { enabled },
    })
  ).data!;

/** Fresh initialization only. Never call on restored drafts or persisted RunConfig. */
export function applyWorkspaceDefaults(
  base: ResearchFormValues,
  saved: WorkspaceDefaults,
  capabilities: ResearchCapabilities,
): ResearchFormValues {
  const profile = researchModelProfiles(capabilities).find(
    (item) => item.id === (saved.model_profile_id || "fixture"),
  );
  if (!profile)
    throw new Error(
      "Your saved default model profile is no longer available. Update your defaults in Settings before starting a new experiment.",
    );
  return {
    ...base,
    market_profile: {
      ...base.market_profile,
      initial_cash: saved.initial_cash ?? base.market_profile.initial_cash,
    },
    optimizer: {
      ...base.optimizer,
      max_iterations: saved.max_iterations ?? base.optimizer.max_iterations,
    },
    budgets: {
      ...base.budgets,
      max_model_calls: saved.max_model_calls ?? base.budgets.max_model_calls,
      max_tokens: saved.max_tokens ?? base.budgets.max_tokens,
      max_runtime_seconds:
        saved.max_runtime_seconds ?? base.budgets.max_runtime_seconds,
    },
    model: profile.config,
  };
}
