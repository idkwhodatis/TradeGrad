# V1 owner-local public provider connections

## Accepted cumulative fixture evidence, 2026-10-23

The complete fixture gate passed in [run 39942518018](https://github.com/idkwhodatis/TradeGrad/actions/runs/39942518018)
at head `42f6cc25f2e4525dcbaad4e58ee95cf733e286cd`, tree `a99216d3fe10b5c5015627e78cabad1e5de66577`.
See [PC and exact suite counts](V1_IMPLEMENTATION.md#original-v1-cumulative-fixture-evidence-accepted-pc).
This evidence is for that exact source, not live upstreams or production deployment.

Actual PostgreSQL revision concurrency/immutability, owner isolation,
import/probe frozen defaults, delayed-worker execution after disablement,
corrupt-binding rejection, browser save/conflict/reload and backup/migration
coverage passed within PC. These public/no-auth preferences do not create accounts,
store secrets or grant new endpoints. Receipt integrity and `fixture_passed` are
not live connectivity or data-use rights; separate live outcomes remain pending.

Data Sources now manages the three public/no-auth acquisition adapters with
persisted owner-local configuration. This is not an account connection, a secret
store, a new endpoint selector, or authorization to trade. AKShare/Eastmoney,
yfinance/Yahoo and OpenBB/Nasdaq keep the existing fixed adapter endpoints.
`public_no_auth` is explicit, and no credential or password field is accepted.
The existing operator model secret references remain separate in Settings.
The portable synthetic fixture is not an editable ProviderConnection.

## Preferences and immutable use

- Each owner/provider has a stable connection ID and append-only revisions.
  PostgreSQL rejects updates/deletes of existing revision rows. Configuration
  hashes cover the ID, revision, provider, enabled/default flags, price mode and
  auth mode. They establish integrity, not live connectivity or data-use rights.
- Read-only catalog access reports revision 0 as unsaved defaults without writing
  rows. The first save or successful admission persists revision 1 transactionally.
  Existing users need no backfill, and no old import or published manifest is
  rewritten. All three public adapters initially remain enabled; AKShare is the
  CN default and yfinance is the US default.
- A default is per market. Promoting OpenBB or yfinance atomically creates a new
  demotion revision for the previous US default. Disabled providers cannot be
  defaults. No configured market default means an omitted-provider request fails,
  even when a different provider is enabled: there is no silent fallback.
- AKShare defaults allow provider_native/raw/qfq/hfq. Yahoo and Nasdaq defaults
  allow only provider_native. Yahoo repair remains an explicit parent-snapshot
  action, never an implicit default. Explicit supported request price modes
  continue to override the saved default.
- New resolve/import/probe requests freeze `_provider_connection` with revision
  and hash, after checking owner scope, enablement and optional expected revision.
  The worker validates that immutable persisted revision before any adapter or
  checkpoint access. Dataset manifests and connection-test receipts carry the
  same snapshot separately from unchanged upstream provenance IDs and hashes.
- New bound jobs use `data-import.v3`, which older workers reject. Current workers
  still accept genuine unbound v1/v2 jobs without altering their requests or
  hashes. Run API/data workers from the same reviewed revision after migration.
- Configuration changes apply only to new jobs. Disabling does not cancel an
  accepted job or revoke an existing frozen retry. Use the import Cancel action
  to stop one. Idempotent replay with the same key/input returns the original job,
  including after a default switch or disable; changed input conflicts as before.
- Refresh creates a new import bound to today's configuration. The old snapshot
  remains immutable. Historical probe receipts are clearly labeled when they
  predate the current connection revision; fixture tests never establish live
  verification.

## API and UI

The authenticated catalog at `GET /api/v1/data/providers` embeds the owner's
current connection. `POST /api/v1/data/provider-connections/{provider}` accepts
only `expected_revision`, `enabled`, `is_default`, and `default_price_mode`.
It uses the existing allowed POST method, without expanding CORS or any access
permission. A stale revision returns 409; an unchanged save at the current
revision creates no extra row. Historical saved configurations are readable at
`GET /api/v1/data/provider-connections/{provider}/revisions/{revision}` only by
their owner. There is no project-sharing path in this slice.

Resolve and import accept an omitted/null provider to use the saved market
default. Import and probe use the saved default price mode when omitted; an
optional `expected_connection_revision` rejects stale forms before admission.
Server-only snapshots, arbitrary endpoints, secrets and caller-supplied owner
IDs are rejected by strict schemas. Owner locks serialize mutation and admission;
quota or validation rejection rolls back first-use configuration and queue writes.

Data Sources offers guarded save/discard controls, reload-safe preferences,
revision/hash inspection and explicit default-mode selection. Uncertain submissions retain their exact expected revision and idempotency key across reloads and catalog changes; matching pending requests offer recovery even if the provider is now disabled. It preserves a
previous disabled source selection instead of silently repointing already
resolved listings. A stale settings form must discard/reload before saving.

## Migration, cold restore and validation

`006_provider_connections` follows `005_settings`. Readiness requires this head.
The cold-backup format still accepts genuine 003/004/005 archives, adds 006, and
continues to inventory strategy packages at the new head. The actual restore drill
now upgrades genuine M3 data through 004, 005 and 006 without changing historical
rows/files/hashes; a separate parametrized 005/006 backup restores management
rows, including multiple immutable connection revisions in 006. Downgrade removes
only the new table, so never downgrade an active deployment with queued v3 jobs.

The following real-stack contracts executed in PC; their results are not inferred
from collection or synthetic unit results:

```sh
.venv-api/bin/python -m pytest -q tests/integration/test_provider_connections.py tests/integration/test_provider_completion_http.py tests/integration/test_migrations.py tests/integration/test_workspace_settings.py
RUN_BACKUP_RESTORE_DRILL=1 .venv-api/bin/python -m pytest -q tests/integration/test_backup_restore.py
TRADEGRAD_REQUIRE_PROVIDER_RESEARCH=1 .venv-api/bin/python -m pytest -q tests/system/test_provider_research_pipeline.py
npm run test:e2e --prefix frontend -- tests/e2e/provider-connections.spec.ts
```

The integration suite includes concurrent edits/default changes, owner isolation,
SQL immutability, rollback, strict fields, frozen retry and old request-hash
compatibility. Playwright uses actual login/API/PG/data workers to check save,
discard, stale-conflict recovery, reload, disable, immutable queued jobs, published
manifest bindings and scoped probe receipts. It restores preferences afterward
without erasing revision history. Existing two-account fixture and data-worker
setup is required; no live upstream, paid model or trading operation is used.
The provider system gate also defers all data-worker startup until after a connection is disabled, proving an already accepted job still executes its frozen revision. Five corrupt/missing-binding variants must fail before any listing resolution or data-block creation. Local pure tests, build and collection do not replace these hosted gates.
