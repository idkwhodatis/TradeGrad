# M1 PostgreSQL integration gate

These tests require an actual PostgreSQL server. They exercise HTTP requests via
FastAPI's ASGI test client and use independent PostgreSQL connections for race
conditions. They do not substitute SQLite or mocked persistence for database
behavior.

Install `backend/requirements.lock`, then point `TEST_DATABASE_URL` at a dedicated
throwaway test database and run:

```sh
: "${TEST_DATABASE_URL:?Set the URL for a dedicated ephemeral PostgreSQL database}"
python -m pytest tests/integration -q -ra --junitxml=m1-postgres-junit.xml
```

Supply the connection URL through the environment without committing or logging
its credentials. Do not use a production database. The supplied role needs
permission to create/drop schemas and create the functions/types used by the
application and Procrastinate. Test account passwords and JWT signing keys are
generated in memory for each run; no reusable authentication material is stored
in the test sources or test IDs.

Each test creates its own randomly named `tradegrad_test_*` schema, applies the
real migration chain, and drops only that schema at teardown. Tests can therefore
run concurrently without truncating existing tables. Schema-level isolation does
not make an otherwise production database an appropriate test target.

An absent `TEST_DATABASE_URL` produces an explicit skip saying PostgreSQL was not
run. A supplied non-PostgreSQL URL is rejected; connection failures and migration
failures are errors, not skips. A green invocation containing skipped database
tests is not evidence that M1 passed.

In the initial restricted executor, PostgreSQL socket creation is denied. These
tests must run on a permitted host/CI PostgreSQL service before the M1 release
gate is marked complete. No socket restriction is bypassed and no database pass
is inferred from collection, syntax checks, or fixture-only tests.

## Covered contracts

- Real Alembic upgrade and downgrade/upgrade round trip, including Procrastinate.
- Actual password/token authentication, token expiry/signature validation,
  deactivated users, and owner-only dataset, strategy, run, event and artifact access.
- Atomic run/event/outbox creation, full rollback, repeated and concurrent
  idempotency keys, conflict responses, per-user scoping and active-run quota races.
- Concurrent attempt claiming, queued/running cancellation, lease expiry,
  stop-confirmed recovery, duplicate resume, and stale generation/token rejection.
- Resume/submission quota contention and malformed metric envelopes becoming
  durable failed attempts instead of losing the terminal transaction.
- Transactional Procrastinate handoff, rollback after deferral, concurrent
  dispatchers, and a stale queued delivery after cancellation/resume.
- Stalled queue-worker redelivery before business claim, orphaned worker rows,
  healthy/unrelated job exclusions, concurrent recovery and recovery rollback.
- Bounded SSE replay and `Last-Event-ID` continuity; artifact exact bytes,
  immutable checksum validation, and fail-closed storage traversal handling.

The per-test fixtures provision synthetic metadata directly in PostgreSQL and
exercise public authentication and run endpoints over ASGI HTTP. They do not
claim to evaluate strategies or download market data. Evaluator sandbox tests
and full browser/worker E2E are separate gates.

## Initial executor evidence

On 2026-10-22, 77 tests collected successfully. Ruff lint/format and Python syntax
checks passed. With `TEST_DATABASE_URL` unset, all 77 tests were explicitly
skipped, not passed. Setting it to `sqlite://` caused the expected setup error
before any database access: "SQLite is not integration evidence."

No PostgreSQL integration pass is claimed until the suite runs against a
permitted PostgreSQL service. The initial skip-only JUnit report is
`/tmp/m1-postgres-unrun-junit.xml` in the implementation executor, not a reusable
repository artifact or proof of runtime correctness.
