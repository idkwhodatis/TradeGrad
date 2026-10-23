import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  QuotaPublic,
  ResearchCapabilities,
  UserPublic,
  WorkspaceDefaults,
} from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { currentUserOptions, queryClient } from "@/lib/api";
import { researchModelProfiles } from "@/lib/research-form";
import { useDataAction } from "@/lib/use-data-action";
import {
  createManagedUser,
  managedQuotaOptions,
  managedReferencesOptions,
  managedUsersOptions,
  saveDefaults,
  saveManagedQuota,
  saveManagedReference,
  saveManagedUser,
  saveProfile,
  settingsDefaultsOptions,
  settingsQuotaOptions,
  settingsStatusOptions,
} from "@/lib/settings-api";

async function invalidateSettings() {
  await Promise.all(
    [
      "me",
      "workspace-defaults",
      "workspace-quota",
      "settings-status",
      "managed-users",
      "managed-quota",
      "managed-references",
      "research-capabilities",
    ].map((key) => queryClient.invalidateQueries({ queryKey: [key] })),
  );
}
function Saved({ text }: { text: string }) {
  return text ? <p role="status">{text}</p> : null;
}
function Actions({
  pending,
  save,
  cancel,
  name = "Save changes",
}: {
  pending: boolean;
  save: () => void;
  cancel: () => void;
  name?: string;
}) {
  return (
    <div className="flex gap-3 mt-3">
      <Button type="submit" disabled={pending} onClick={save}>
        {pending ? "Saving…" : name}
      </Button>
      <Button
        type="button"
        variant="outline"
        disabled={pending}
        onClick={cancel}
      >
        Cancel
      </Button>
    </div>
  );
}
function ProfileEditor({ user }: { user: UserPublic }) {
  const [name, setName] = useState(user.full_name || "");
  const [saved, setSaved] = useState("");
  const action = useDataAction(async () => {
    await saveProfile(name);
    await invalidateSettings();
    setSaved("Profile saved.");
  });
  return (
    <Panel title="Edit your profile">
      <label>
        Display name
        <Input
          maxLength={255}
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setSaved("");
          }}
        />
      </label>
      <p className="text-xs muted">
        Email and administrator permissions cannot be changed here.
      </p>
      <Actions
        pending={action.pending}
        save={() => void action.run()}
        cancel={() => {
          setName(user.full_name || "");
          action.resetError();
          setSaved("Changes discarded.");
        }}
        name="Save profile"
      />
      {!!action.error && (
        <ErrorState error={action.error} retry={() => void action.run()} />
      )}
      <Saved text={saved} />
    </Panel>
  );
}
function DefaultsEditor({
  initial,
  capabilities,
}: {
  initial: WorkspaceDefaults;
  capabilities: ResearchCapabilities;
}) {
  const [draft, setDraft] = useState(initial);
  const [saved, setSaved] = useState("");
  const action = useDataAction(async () => {
    const value = await saveDefaults(draft);
    setDraft(value);
    await invalidateSettings();
    setSaved(
      "Defaults saved for new experiments. Existing runs and recovered drafts are unchanged.",
    );
  });
  const number = (
    key: keyof WorkspaceDefaults,
    title: string,
    max: number,
    min = 0,
  ) => (
    <label key={key}>
      {title}
      <Input
        type="number"
        min={min}
        max={max}
        value={Number(draft[key])}
        onChange={(e) => {
          setDraft({ ...draft, [key]: Number(e.target.value) });
          setSaved("");
        }}
      />
    </label>
  );
  return (
    <Panel title="New experiment defaults">
      <div className="grid gap-3 sm:grid-cols-2">
        {number("initial_cash", "Initial cash default", 1e9, 1)}
        {number("max_iterations", "Iteration default", 1000)}
        {number("max_model_calls", "Model call default", 10000)}
        {number("max_tokens", "Token default", 1e8)}
        {number("max_runtime_seconds", "Runtime seconds default", 86400, 1)}
        <label>
          Default model profile
          <select
            value={draft.model_profile_id}
            onChange={(e) => {
              setDraft({ ...draft, model_profile_id: e.target.value });
              setSaved("");
            }}
          >
            {!researchModelProfiles(capabilities).some(
              (p) => p.id === draft.model_profile_id,
            ) && (
              <option value={draft.model_profile_id}>
                Unavailable saved profile
              </option>
            )}
            {researchModelProfiles(capabilities).map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="text-xs muted mt-3">
        Defaults do not launch runs. Non-synthetic profiles may incur charges
        when a new run is submitted. No historical configuration or hash is
        rewritten.
      </p>
      <Actions
        pending={action.pending}
        save={() => void action.run()}
        cancel={() => {
          setDraft(initial);
          action.resetError();
          setSaved("Changes discarded.");
        }}
        name="Save defaults"
      />
      {!!action.error && (
        <ErrorState error={action.error} retry={() => void action.run()} />
      )}
      <Saved text={saved} />
    </Panel>
  );
}
function QuotaEditor({ id, initial }: { id: string; initial: QuotaPublic }) {
  const [runs, setRuns] = useState(initial.max_active_runs?.toString() || "");
  const [imports, setImports] = useState(
    initial.max_active_imports?.toString() || "",
  );
  const [saved, setSaved] = useState("");
  const action = useDataAction(async () => {
    await saveManagedQuota(id, {
      max_active_runs: runs === "" ? null : Number(runs),
      max_active_imports: imports === "" ? null : Number(imports),
    });
    await invalidateSettings();
    setSaved(
      "Quotas saved. Existing jobs are not cancelled; new admissions use these limits.",
    );
  });
  return (
    <div className="grid gap-3">
      <label>
        Active run quota
        <Input
          type="number"
          min={0}
          max={initial.deployment_active_runs}
          value={runs}
          onChange={(e) => setRuns(e.target.value)}
        />
      </label>
      <label>
        Active import quota
        <Input
          type="number"
          min={0}
          max={initial.deployment_active_imports}
          value={imports}
          onChange={(e) => setImports(e.target.value)}
        />
      </label>
      <p className="text-xs muted">
        Blank inherits deployment limits ({initial.deployment_active_runs} runs,{" "}
        {initial.deployment_active_imports} imports). Zero blocks new work. All
        native, portable and final runs share one run quota. Currently{" "}
        {initial.active_runs} active runs and {initial.active_imports} imports.
      </p>
      <Actions
        pending={action.pending}
        save={() => void action.run()}
        cancel={() => {
          setRuns(initial.max_active_runs?.toString() || "");
          setImports(initial.max_active_imports?.toString() || "");
          action.resetError();
          setSaved("Changes discarded.");
        }}
        name="Save quotas"
      />
      {!!action.error && (
        <ErrorState error={action.error} retry={() => void action.run()} />
      )}
      <Saved text={saved} />
    </div>
  );
}
function ManagedEditor({ target }: { target: UserPublic }) {
  const [name, setName] = useState(target.full_name || "");
  const [active, setActive] = useState(target.is_active);
  const [admin, setAdmin] = useState(target.is_superuser);
  const [saved, setSaved] = useState("");
  const quota = useQuery(managedQuotaOptions(target.id));
  const references = useQuery(managedReferencesOptions(target.id));
  const action = useDataAction(async () => {
    await saveManagedUser(target.id, {
      full_name: name,
      is_active: active,
      is_superuser: admin,
    });
    await invalidateSettings();
    setSaved("Account updated.");
  });
  const referenceAction = useDataAction(
    async (profileId: string, enabled: boolean) => {
      await saveManagedReference(target.id, profileId, enabled);
      await invalidateSettings();
      setSaved(
        "Reference policy saved. Already admitted calls cannot be undone.",
      );
    },
  );
  return (
    <div className="space-y-5" data-testid="managed-account">
      <p>{target.email}</p>
      <label>
        Account display name
        <Input
          value={name}
          maxLength={255}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="flex gap-2">
        <input
          type="checkbox"
          checked={active}
          onChange={(e) => setActive(e.target.checked)}
        />
        Account active
      </label>
      <label className="flex gap-2">
        <input
          type="checkbox"
          checked={admin}
          onChange={(e) => setAdmin(e.target.checked)}
        />
        Administrator
      </label>
      <p className="text-xs muted">
        At least one active administrator is required. Deactivation blocks
        future authenticated requests and model admissions, but cannot undo an
        already admitted external call.
      </p>
      <Actions
        pending={action.pending}
        save={() => void action.run()}
        cancel={() => {
          setName(target.full_name || "");
          setActive(target.is_active);
          setAdmin(target.is_superuser);
          action.resetError();
          setSaved("Changes discarded.");
        }}
        name="Save account"
      />
      {!!action.error && (
        <ErrorState error={action.error} retry={() => void action.run()} />
      )}
      <Saved text={saved} />
      {quota.isPending ? (
        <Loading />
      ) : quota.error ? (
        <ErrorState error={quota.error} retry={() => void quota.refetch()} />
      ) : (
        <QuotaEditor key={target.id} id={target.id} initial={quota.data} />
      )}
      <h3>Existing secret references</h3>
      <p className="text-xs muted">
        Only existing operator-allowlisted references can be enabled or
        restricted. No raw key, endpoint, environment name, credential rotation
        or new access grant is supported.
      </p>
      {references.isPending ? (
        <Loading />
      ) : references.error ? (
        <ErrorState
          error={references.error}
          retry={() => void references.refetch()}
        />
      ) : references.data.length === 0 ? (
        <p>No operator-allowlisted references for this account.</p>
      ) : (
        references.data.map((reference) => (
          <div
            key={reference.id}
            className="flex items-center justify-between gap-3"
          >
            <span>
              {reference.label} · {reference.provider} / {reference.model} ·{" "}
              {reference.enabled ? "Enabled" : "Disabled"}
              {!reference.deployment_enabled && " (deployment off)"}
            </span>
            <Button
              variant="outline"
              disabled={referenceAction.pending}
              onClick={() =>
                void referenceAction.run(reference.id, !reference.enabled)
              }
            >
              {reference.enabled ? "Disable" : "Enable"} {reference.label}
            </Button>
          </div>
        ))
      )}
      {!!referenceAction.error && (
        <ErrorState
          error={referenceAction.error}
          retry={() => void references.refetch()}
        />
      )}
    </div>
  );
}
function CreateUser({ done }: { done: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const action = useDataAction(async () => {
    await createManagedUser({
      email,
      password,
      full_name: name,
      is_active: true,
      is_superuser: false,
    });
    setPassword("");
    await invalidateSettings();
    done();
  });
  return (
    <div className="space-y-3">
      <label>
        New account email
        <Input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label>
        New account display name
        <Input
          maxLength={255}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label>
        Initial account password
        <Input
          type="password"
          autoComplete="new-password"
          minLength={12}
          maxLength={1024}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <p className="text-xs muted">
        Creates an ordinary active account. Password is submitted only to this
        service and is never returned or saved in browser storage. After an
        uncertain result, reload the account list before retrying creation.
      </p>
      <Actions
        pending={action.pending}
        save={() => void action.run()}
        cancel={() => {
          setPassword("");
          done();
        }}
        name="Create account"
      />
      {!!action.error && (
        <ErrorState
          error={action.error}
          retry={() => void invalidateSettings()}
        />
      )}
    </div>
  );
}
function UserManagement() {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState("");
  const [creating, setCreating] = useState(false);
  const users = useQuery(managedUsersOptions(offset));
  const target = users.data?.data.find((u) => u.id === selected);
  return (
    <Panel title="User management">
      <Button variant="outline" onClick={() => setCreating(true)}>
        New account
      </Button>
      {creating && <CreateUser done={() => setCreating(false)} />}
      {users.isPending ? (
        <Loading />
      ) : users.error ? (
        <ErrorState error={users.error} retry={() => void users.refetch()} />
      ) : (
        <>
          <label>
            Manage account
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              <option value="">Choose an account</option>
              {users.data.data.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.email} {item.is_superuser ? "(administrator)" : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="flex gap-3 mt-3">
            <Button
              variant="outline"
              disabled={offset === 0}
              onClick={() => {
                setOffset(offset - 50);
                setSelected("");
              }}
            >
              Previous accounts
            </Button>
            <Button
              variant="outline"
              disabled={offset + 50 >= users.data.count}
              onClick={() => {
                setOffset(offset + 50);
                setSelected("");
              }}
            >
              Next accounts
            </Button>
            <Button variant="outline" onClick={() => void users.refetch()}>
              Reload accounts
            </Button>
          </div>
          {target && <ManagedEditor key={target.id} target={target} />}
        </>
      )}
    </Panel>
  );
}
export function SettingsManagement({
  capabilities,
}: {
  capabilities?: ResearchCapabilities;
}) {
  const user = useQuery(currentUserOptions);
  const defaults = useQuery(settingsDefaultsOptions);
  const quota = useQuery(settingsQuotaOptions);
  const status = useQuery(settingsStatusOptions);
  return (
    <div className="grid gap-5 lg:grid-cols-2 mt-5">
      {user.data && <ProfileEditor user={user.data} />}
      {defaults.isPending ? (
        <Loading />
      ) : defaults.error ? (
        <ErrorState
          error={defaults.error}
          retry={() => void defaults.refetch()}
        />
      ) : (
        capabilities && (
          <DefaultsEditor initial={defaults.data} capabilities={capabilities} />
        )
      )}
      <Panel title="Effective permissions and quotas">
        {status.error ? (
          <ErrorState
            error={status.error}
            retry={() => void status.refetch()}
          />
        ) : (
          <p>{status.data?.permissions.join(", ")}</p>
        )}
        {quota.error ? (
          <ErrorState error={quota.error} retry={() => void quota.refetch()} />
        ) : (
          quota.data && (
            <p>
              {quota.data.active_runs} / {quota.data.effective_active_runs}{" "}
              active runs · {quota.data.active_imports} /{" "}
              {quota.data.effective_active_imports} active imports
            </p>
          )
        )}
        <p className="text-xs muted">
          Non-administrators cannot manage other accounts, quota policy or
          secret references.
        </p>
      </Panel>
      <Panel title="Dependencies and backup status">
        {status.isPending ? (
          <Loading />
        ) : status.error ? (
          <ErrorState
            error={status.error}
            retry={() => void status.refetch()}
          />
        ) : (
          <>
            <p>Database schema: {status.data.schema_revision}</p>
            {Object.entries(status.data.dependencies).map(([name, version]) => (
              <p key={name}>
                {name}: {version}
              </p>
            ))}
            <p className="mt-3">Backup status: not reported</p>
            <p className="text-xs muted">{status.data.backup_note}</p>
            <p className="text-xs muted mt-3">
              {status.data.secret_reference_boundary}
            </p>
          </>
        )}
      </Panel>
      {user.data?.is_superuser && <UserManagement />}
    </div>
  );
}
