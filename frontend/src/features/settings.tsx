import { OperationsPanel } from "./operations-panel";
import { SettingsManagement } from "./settings-management";
import { useQuery } from "@tanstack/react-query";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import { currentUserOptions, statusOptions } from "@/lib/api";
import { researchCapabilitiesOptions } from "@/lib/research-submit";
import { label } from "@/lib/format";
import { researchModelProfiles } from "@/lib/research-form";

export function SettingsPage() {
  const user = useQuery(currentUserOptions);
  const status = useQuery(statusOptions);
  const capabilities = useQuery(researchCapabilitiesOptions);
  return (
    <>
      <PageHeader
        title="Settings & runtime status"
        subtitle="Observed service state and effective capabilities. Provider credentials are never displayed or accepted by this page."
      />
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Your workspace">
          {user.isPending ? (
            <Loading />
          ) : user.error ? (
            <ErrorState error={user.error} retry={() => void user.refetch()} />
          ) : (
            <dl className="details">
              <div>
                <dt>Signed in as</dt>
                <dd>{user.data?.email}</dd>
              </div>
              <div>
                <dt>Sharing</dt>
                <dd>Owner-scoped private resources</dd>
              </div>
              <div>
                <dt>Account and key management</dt>
                <dd>
                  Administrators manage accounts and restrict existing
                  operator-owned references below. Raw provider keys and new
                  allowlist grants are not accepted.
                </dd>
              </div>
            </dl>
          )}
        </Panel>
        <Panel title="Observed service health">
          {status.isPending ? (
            <Loading />
          ) : status.error ? (
            <ErrorState
              error={status.error}
              retry={() => void status.refetch()}
            />
          ) : (
            <dl className="details">
              <div>
                <dt>Database</dt>
                <dd>{status.data?.database ? "Reachable" : "Unavailable"}</dd>
              </div>
              <div>
                <dt>Your pending outbox deliveries</dt>
                <dd>{status.data?.queue_pending ?? "Unknown"}</dd>
              </div>
              {Object.entries(status.data?.workers ?? {}).map(
                ([role, state]) => (
                  <div key={role}>
                    <dt>{label(role)}</dt>
                    <dd>{label(state)}</dd>
                  </div>
                ),
              )}
            </dl>
          )}
          <p className="text-xs muted mt-3">
            A worker heartbeat is not proof of evaluator containment or
            successful recovery. This page does not certify backups or restore
            readiness.
          </p>
        </Panel>
        <Panel title="Research capabilities">
          {capabilities.isPending ? (
            <Loading />
          ) : capabilities.error ? (
            <ErrorState
              error={capabilities.error}
              retry={() => void capabilities.refetch()}
            />
          ) : (
            <dl className="details">
              <div>
                <dt>Models</dt>
                <dd>{capabilities.data?.models?.join(", ")}</dd>
              </div>
              <div>
                <dt>Paid model calls</dt>
                <dd>
                  {capabilities.data?.live_model_calls
                    ? "Enabled for your available non-synthetic operator profiles"
                    : "Disabled; available profiles are synthetic only"}
                </dd>
              </div>
              <div>
                <dt>Your model profiles</dt>
                <dd>
                  {capabilities.data &&
                    researchModelProfiles(capabilities.data).map((profile) => (
                      <p key={profile.id}>
                        {profile.label} · {profile.config.provider} /{" "}
                        {profile.config.model} ·{" "}
                        {profile.synthetic
                          ? "Synthetic"
                          : "Operator-managed; may incur charges"}
                      </p>
                    ))}
                </dd>
              </div>
              <div>
                <dt>Holdout inside native optimization</dt>
                <dd>
                  {capabilities.data?.holdout_execution
                    ? "Enabled"
                    : "Excluded from optimization; available through a separately frozen final selection"}
                </dd>
              </div>
              <div>
                <dt>Environment definition SHA-256</dt>
                <dd className="hash">
                  {capabilities.data?.environment_digest}
                </dd>
              </div>
              <div>
                <dt>Evaluator image</dt>
                <dd>
                  The actual image ID is pinned and recorded separately on each
                  configured run.
                </dd>
              </div>
            </dl>
          )}
        </Panel>
        <Panel title="Limits and release boundaries">
          {capabilities.data && (
            <dl className="details">
              {Object.entries(capabilities.data.limits).map(([name, value]) => (
                <div key={name}>
                  <dt>{label(name)}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          )}
          <p className="text-xs muted mt-3">
            Native engine output is untrusted strategy-process evidence, not an
            independent ledger. Live-data capabilities carry their own
            verification status. Per-account quotas can only narrow deployment
            limits. Deployment and restore rehearsal remain separate operator
            gates.
          </p>
        </Panel>
      </div>
      <OperationsPanel />
      <SettingsManagement capabilities={capabilities.data} />
    </>
  );
}
