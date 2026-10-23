# V1 Settings management

## Accepted cumulative fixture evidence, 2026-10-23

The complete fixture gate passed in [run 39942518018](https://github.com/idkwhodatis/TradeGrad/actions/runs/39942518018)
at head `42f6cc25f2e4525dcbaad4e58ee95cf733e286cd`, tree `a99216d3fe10b5c5015627e78cabad1e5de66577`.
See [PC and exact suite counts](V1_IMPLEMENTATION.md#original-v1-cumulative-fixture-evidence-accepted-pc).
This evidence is for that exact source, not live upstreams or production deployment.

Actual PostgreSQL and browser management/default/quota/permission checks passed,
including last-active-admin protection, existing operator-reference enablement,
versions and backup receipts. The 16-case backup/upgrade group passed genuine
historical-schema restore and upgrade through ProviderConnection migration.
Earlier collected-only checks remain historical. No raw secrets, new grants,
arbitrary endpoints or rewriting of frozen runs is authorized.

This slice implements the original V1 Settings page. It does not enable live
trading, arbitrary model providers, paid calls by default, or V2 capabilities.

## Permissions and accounts

Every route requires an active authenticated user. Ordinary users can edit only
their display name and new-experiment defaults and inspect their effective
quotas, permissions, dependency versions and observed worker status. They cannot
submit privilege flags, edit another user, or change quota/reference policy.

Active administrators can list/create accounts, change display name/active/admin
flags, and narrow each user's quotas and existing model-reference admission.
Passwords for new accounts are hashed and never returned; there is no password
reset, raw provider-key entry, automatic credential storage or account deletion
endpoint in this slice. Deactivation is reversible and preserves owned records.
The last active administrator cannot be demoted/deactivated. A transaction-wide
administrator lock serializes this invariant and rechecks the acting admin after
waiting, including concurrent demotions. Resources are still owner-scoped;
administrator account management does not grant another owner's research data.

Initial trusted provisioning is explicit, never application startup:

```sh
python -m backend.app.bootstrap --email "$ADMIN_EMAIL" \
  --password-env ADMIN_PASSWORD --full-name 'Workspace administrator' --superuser
```

The password variable must already be securely supplied. `--superuser` applies
only to a NEW account. An existing ordinary account is never silently promoted;
use an existing authorized administrator instead. No provisioning command was
executed as part of this implementation.

## Restricted existing secret references

Deployment `MODEL_PROFILES_ENABLED` remains false by default. Operator profiles
still pin endpoint, secret environment reference, model, owner allowlist and
request bounds. HTTP accepts only an existing profile ID plus `enabled` for an
owner already present in that trusted operator profile. Admins can disable or
re-enable that existing admission, never add an owner or profile, enter a raw
key, change an endpoint/environment name, rotate credentials, or expand the
operator allowlist. Unknown/unlisted references return the same not-found error.
The API never resolves provider secrets or returns endpoint/environment names.

Capabilities filter disabled profiles. New submissions, preflight, resume,
worker preparation, and each trusted worker model-call admission recheck persisted
restrictions. Reference changes use the same owner transaction lock as model
admission, so a committed restriction precedes all subsequent admissions. An
already committed call admission may still launch or finish; a running external
request cannot be undone instantly. Account deactivation and quota reductions
do not kill or cancel already accepted jobs. Operators retain responsibility for
credential rotation/revocation at the provider itself.

Profile hashes, saved run configurations and recorded usage are never rewritten
when policy changes. Re-enabling cannot make an unlisted owner eligible and cannot
override the deployment-wide off switch. Portable profiles remain fixture-only.

## Quotas and transaction boundaries

`user_workspace_settings` stores optional per-account active-run and active-import
limits. Null inherits the deployment cap, zero blocks new work, and configured
limits cannot exceed that cap. Effective values also use `min` at read/admission
time so lowering a deployment cap safely narrows any older saved override.

The active-run count is owner-wide across seed, configured native, portable,
native-final and portable-final jobs; paused/cancellation-pending work follows
the existing nonterminal accounting. All new submissions and resume/retry paths
check the persisted cap under the existing per-owner `submit` transaction lock.
Data admission/retry uses its existing `data-submit` lock. Administrative quota
writes take both in the same order, preventing a policy update from racing a
reservation. Counts and outbox insertion remain in the admission transaction.
Idempotent replay of an already accepted request does not reserve another slot.
A lowered limit can be below current use; it blocks further admission rather
than cancelling jobs. Existing evaluator CPU/RAM/process/time/model budgets
remain separate containment limits, not currency spending caps.

## Defaults, UI and observability

Defaults persist initial cash, iteration/model-call/token/runtime budgets and an
admitted profile ID. The exported `settingsDefaultsOptions` and
`applyWorkspaceDefaults` helper initialize only a fresh native experiment wizard;
the companion wizard integration belongs to the benchmark slice. Recovered
drafts/pending idempotent submissions take precedence. An unavailable saved model
requires updating Settings, not silent substitution. Source/run/config/hash bytes
and immutable dataset/universe/strategy references are untouched.

The UI includes account creation/editing, paginated account selection, quota
editing, restricted reference toggles, self profile/default editing, save/reload,
error retry and cancel/discard controls. Writes have an immediate single-flight
guard, and cancellation discards unsaved edits without a server request. Creation
errors advise reloading accounts before retrying an uncertain result; ordinary
updates are replacement/idempotent operations.

The page retains the existing owner-scoped model-profile and worker heartbeat
presentation. API dependency versions are observed only in the API environment,
not a claim about evaluator/container versions. Backup status is explicitly
`not_reported`: there is no trusted durable backup receipt registry in V1, and a
heartbeat, reachable database or migration head does not prove a valid backup.
Use the separate operator cold-backup and restore rehearsal gates.

## Migration and backup coordination

Migration `005_settings` follows `004_m5` and adds only mutable settings. It does
not update historical research configuration/hash or published evidence rows.
Current readiness requires `006_provider_connections`; see [provider connections](V1_PROVIDER_CONNECTIONS.md). Downgrade removes only these mutable
preferences, losing them; do not use it as a replacement for backup. The parent
integration owns adding `005_settings` to supported cold-backup revisions and
running the real old-revision restore/upgrade drill before release. Existing
003/004 archive support must remain intact.

## Historical preparation and verification contract

Pure checks cover strict contracts, self-escalation input rejection, effective
quota caps, reference narrowing/privacy/hash stability, every admission/retry
callsite, worker policy checkpoints, new-default copy semantics and generated
contracts. Real PostgreSQL tests use fresh schemas and generated disposable
accounts for authorization, last-admin races, account/deactivation behavior,
concurrent native/seed admission, zero data quota, retry refusal, persisted
reference policy and 004-to-005 history preservation. The existing final quota
race test now proves the persisted per-user override instead of changing the
deployment cap.

Playwright suite: `frontend/tests/e2e/settings-management.spec.ts`. It has no route
mocks and requires an explicitly disposable stack:

- `TRADEGRAD_REQUIRE_SETTINGS_E2E=1`
- Existing ordinary `E2E_EMAIL`/`E2E_PASSWORD` and `E2E_OTHER_EMAIL`/`E2E_OTHER_PASSWORD`
- Separate `E2E_ADMIN_EMAIL`/`E2E_ADMIN_PASSWORD`, provisioned with the NEW-account
  `--superuser` command above (use `--password-env E2E_ADMIN_PASSWORD`)

When required, missing admin inputs fail rather than silently skipping. Do not
point this mutation suite at real accounts. It tests account/quota persistence,
nonadmin denial, profile/default save/reload, offline error/retry, cancelled
account creation and unknown-reference rejection. Traces/screenshots/video stay
off under the existing credential-safe Playwright configuration. Actual reference
toggle behavior is additionally covered by the prepared PostgreSQL fixture using
an operator profile with an unset secret; it makes no model call.

No local PostgreSQL, sockets, Docker, browser, candidate execution, provider calls
or SDK execution was used. PostgreSQL and browser cases were collected only.
They subsequently passed in PC; the local collection and pure tests are not
the hosted acceptance evidence.

## Combined recovery gate

The additive `005_settings` revision is admitted by the private cold-backup tool.
The prepared real PostgreSQL drills restore genuine `003_m3` archives and then
upgrade through `004_m5` to `005_settings`, and separately back up and restore
`005_settings` management rows together with immutable RunConfig and artifact
bytes. These real restore/upgrade checks subsequently passed in PC, including
the later `006_provider_connections` migration and its immutable revision rows.

The hosted Settings browser opt-in provisions a separate administrator with a
job-local random password. No existing account is promoted, and no deployment
credential or user account is created by local preparation.
