# Trusted research runtime

This is a separate Python environment for durable TradeTGD orchestration. It has
API/queue/storage libraries plus pinned NumPy and OpenAI SDK dependencies. It
must not contain AKQuant, pandas, provider SDKs or a candidate execution fallback.
The API environment remains free of those research/data dependencies.

```sh
python3.12 -m venv .venv-research
.venv-research/bin/python -m pip install --require-hashes -r runtimes/research/requirements.lock
.venv-research/bin/python -m script.validate_runtime_split --runtime research
.venv-research/bin/python -m workers.research worker
```

Keep the API, dispatcher and reconciler in the separately locked API environment,
and the data worker in its data environment. The trusted research worker runs on
the same Linux host as the Docker daemon and storage root, as documented in
`infra/compose.yml`. Do not mount a daemon socket or any credentials into an
untrusted evaluator. The worker is not itself the evaluator sandbox.

Build the local-only evaluator with the reviewed semantic-input digest:

```sh
digest=$(.venv-api/bin/python -m packages.tradegrad_adapter.environment)
docker build --build-arg "TRADEGRAD_ENVIRONMENT_DIGEST=$digest" \
  -t tradegrad-evaluator:v1 -f runtimes/evaluator/Dockerfile .
```

The API advertises this digest. It is frozen into RunConfig and checked again by
the worker. The image label must match it; the worker pins the resolved Docker
image ID, not the mutable tag. Resume refuses changed build inputs or image IDs.
A matching label is provenance, not a guarantee of independently reproducible or
trusted native engine results.

## Model boundary

The configured application currently accepts only `fixture` /
`deterministic-fixture-v1`. Responses intentionally preserve the original seed;
this tests the actual source algorithm, lineage, state and recovery, not model
quality or strategy improvement. Usage is tagged `synthetic_fixture`; zero
external tokens are not presented as actual live-provider usage.

An OpenAI-compatible adapter exists for future explicitly authorized setup, but
live application requests are rejected, no credentials are provisioned, and no
paid request is part of validation. Do not bypass this boundary by changing a
request's provider name or passing arbitrary credential environment references.

## Recovery boundary

Each physical evaluation is registered in PostgreSQL before container creation.
The child container has its own UUID, never an already-stopped parent UUID. A
candidate's code and every repair are immutable, including failed versions.
Only complete, hash-verified JSON checkpoint manifests published through the
current generation/lease transaction are resumable. There is no directory scan,
pickle recovery, automatic generation restart, or silent host execution.

Model intent is written before the request; completed responses are durably
recorded before use. Unknown requests stop rather than repeat potential billing.
A new attempt publishes its own checkpoint before new work, even when recovering
a completed state whose final database transaction was interrupted. Persistence
and event-boundary errors bypass upstream retry logic and abort safely.

Pause is cooperative at a newly completed checkpoint; cancellation requests
immediate evaluator termination. Neither an unknown stop nor a stale lease can
publish a successful/cancelled/paused run. Teardown attempts every remaining
owned child, stops lease renewal even on unexpected errors, and lets the durable
reconciler confirm the final state. Runtime budgets include staging/startup;
mandatory bounded containment cleanup can take additional time after a budget.
