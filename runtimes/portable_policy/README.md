# Portable sibling runtime (M5)

This is an implemented, deliberately narrow runtime, **not a runtime certification**.
Actual Docker/AKQuant acceptance is required; local fake/contract checks do not
prove isolation or native matching semantics. No live brokerage or external
framework equivalence is provided.

## Boundary

One physical Evaluation UUID owns two deterministic sibling names:

- `tradegrad-portable-<evaluation UUID>-engine`
- `tradegrad-portable-<evaluation UUID>-policy`

The parent worker registers this group durably before calling
`PortableDockerEvaluator.run`. Both containers have role, evaluation, attempt and
run labels. The host pins actual image IDs after checking both image labels:
`io.tradegrad.environment_digest` and `io.tradegrad.portable_role`.
Images are prebuilt; launch never pulls images or falls back to host Python.

Engine `/input` contains exactly `request.json` and `market.json`.
Policy `/input` contains exactly `policy.py`, `parameters.json` and `manifest.json`.
The latter is `safe_policy_manifest(full_manifest)`, not the full dataset-linked
manifest. Dataset, universe, actions and environment fingerprints are zeroed and
reference-result identity is removed from this policy-visible projection.

No shared writable mounts, output mounts, Docker sockets, credentials or network
are supplied. Both containers are non-root, read-only, no-capabilities,
no-new-privileges, with CPU/memory/swap/PID/tmpfs bounds. The supervisor verifies
those actual post-create settings independently of its command construction.

## ABI

Policy source exports `Policy(strategy_id: str, parameters: dict)`.
A portfolio policy implements `decide(observation) -> PortfolioReply`.
An event policy implements `on_event(observation) -> EventReply`.
Observation already contains the immutable bounded JSON state snapshot.

Transport is four-byte unsigned network-endian length followed by strict UTF-8
JSON. Protocol version is `tradegrad.portable-wire.v1`.

- Engine observation frame: `kind=observation`, physical `evaluation_id`, zero-based
  `sequence`, `request_id=<evaluation_id>:<sequence>`, `expected_state_sha256`,
  and SDK `observation`.
- Policy reply frame: identical identity/state fields, `kind=decision` and SDK
  `reply`. It cannot contain metrics, fills, trust flags or arbitrary fields.
- Engine terminal frame: `kind=result`, protocol/evaluation/sequence and one
  strict `tradegrad.portable-result.v1` result.

Exactly one policy request may be outstanding. The host parses and normalizes
all replies; the engine validates them again. Frames, state, JSON depth/nodes,
logs, observations, decisions and results are bounded. Nonblocking host reads and
writes have phase and wall deadlines. An independent watchdog contains both
siblings even if a lease/database callback blocks. Attachment CLI process groups
are killed too, including descendants of an already-exited leader.

Both roles are stopped even after partial creation, one failed stop, lost create
responses, cancellation or invalid output. Unknown containment returns
`interrupted`, never success. `stop(evaluation_id)`, `children(attempt_id)` and
`stop_attempt(attempt_id)` support recovery; deterministic names suffice when the
coordinator dies. Result files are exclusively created only after both siblings'
absence is confirmed. Native result validation remains separate and unchanged.

## Engine profile

AKQuant 0.3.27; daily CN/US regular equities, one market/currency, cash long-only.
CN uses 100-share lots and T+1; US one-share lots and T+0. Market and ordinary
limit GTC orders only. Unsupported expiry, shorts, leverage, stop/OCO, fractional
shares and other TIFs fail rather than being translated.

Portfolio orders use decision-close equity/prices, lot rounding and current
available cash, never next-session prices or anticipated sale proceeds. Pending
rebalance orders prevent duplicates. Explicit warmup replays state-only
observations with a flat account. Expected cross-sections are independently
validated; missing bars cannot shrink the declared universe. Every exported fill
must belong to a strictly later session than its decision. Native timestamp
integers are preserved separately from their economic next-open reference.

The bundled references are momentum portfolio targets and completed-bar event
trend orders. They are not claims of reproducing the paper or another framework.

## Validation

Offline trusted-only checks (no policy or native backtest execution):

    .venv-api/bin/python -m pytest tests/portable --ignore=tests/portable/test_isolation.py -q

Actual paired-container CI must build the engine using the existing evaluator
lock plus `runtimes/portable_engine/calendar-requirements.lock`, and the policy
using `runtimes/portable_policy/requirements.lock`. Build both images with the
same reviewed `TRADEGRAD_ENVIRONMENT_DIGEST` argument. Then set the matching
`PORTABLE_ENVIRONMENT_DIGEST` and explicitly enable:

    TRADEGRAD_REQUIRE_PORTABLE_DOCKER=1 python -m pytest tests/portable/test_isolation.py -q

Without this explicit flag, these tests do not even probe the Docker daemon.
They cover reference CN/US policies, native NextOpen/limit behavior, warmup,
missingness, symbol ordering, prefix and future perturbations, hostile filesystem/
network/result/flood/fork behavior, and unknown partial-create cleanup. Broader
PostgreSQL-to-worker-to-artifact and browser integration gates remain separate.
