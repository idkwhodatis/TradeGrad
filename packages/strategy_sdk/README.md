# Portable policy SDK, interface version 1

This is a new, bounded research contract, not the upstream paper protocol.
`interface_only` and `external_framework_equivalence=not_certified` are fixed
manifest values. Shape compliance is neither trusted execution nor a sandbox,
live-trading certification, profitability evidence, or cross-framework parity.

## ABI

Import first-party SDK types from `packages.strategy_sdk`. Importing the SDK
never imports the source templates. A policy source exports
`Policy(strategy_id: str, parameters: dict)` and one method:

- Portfolio: `decide(observation: Observation) -> PortfolioReply`.
- Event: `on_event(observation: Observation) -> EventReply`.

Both replies have exactly two fields: `decision` and `next_state`. There is no
policy-supplied ledger, fills, metrics, or result bundle. Observations already
contain `state`. Restore through `StateCodec(manifest.state_schema)`, not pickle,
object hooks, dynamic evaluation, or inferred migrations. State snapshots are
immutable canonical JSON strings with schema/version and payload checksums;
restored JSON objects are detached copies. Nested array/object state fields are
bounded finite JSON, not a claim to implement all of JSON Schema.

Only the restricted policy container may load and invoke a policy source,
including these first-party reference templates. Host tests parse the two source
files with AST and validate frozen replies, never import or execute templates.
The runtime owns bootstrap configuration and length-prefixed frame transport.
SDK `parse_json`, `canonical_json`, `parse_manifest`, `parse_observation`, and
`parse_reply` reject duplicate keys, extra fields, non-finite values, integer
precision overflow, malformed UTF-8, excessive bytes/nodes/depth/arrays/strings,
wrong state schema, and unsupported capabilities. Timestamp wire values are
UTC RFC3339 ending in `Z`; identities are canonical lowercase UUIDs, not tickers.
`portable-json.v1` uses sorted keys, compact UTF-8 and Python finite JSON numeric
encoding. It does not claim RFC 8785 equivalence across languages.

## Explicit economics and limits

- Daily, cash, long-only equities, US/CN metadata, native AKQuant 0.3.27.
- Market/limit, whole shares, GTC only. `expires_at: null` explicitly means no
  strategy expiry. Non-null expiry, DAY, stops, OCO, trailing orders, fractional
  shares, short/hedge, close-today and leverage are rejected, never erased.
- Decisions are after close with `effective_at` exactly the trusted calendar's
  NextOpen. Never infer NextOpen from the next data row. The runtime must reject
  unsupported calendar, settlement, lot-size, cash, fees, sellability, or venue
  semantics and enforce signal deduplication across invocations.
- Under controller profile `akquant.portable.cash-daily.controller-t1.v3`,
  `PositionView.available_quantity` explicitly means next-open unreserved
  holdings. It is not current native T+1 sellability. The native ledger uses T+0;
  the manifest separately names the trusted controller's CN daily-next-open T+1
  constraint. Flat start, after-close-only complete cross-sections, later-session
  fills and held-minus-pending-sells admission are mandatory. Pending buys never
  authorize a sale; cancellation releases reservations only after terminal
  receipts. Old profile hashes reject. See the [engine profile](../../runtimes/portable_engine/README.md).
- Limit support is the marketable-at-open or no-touch GTC subset. An actual
  intrabar-only or slippage-breaching native fill fails the whole evaluation,
  rather than being corrected or presented as a valid next-open trade.
- Target basis is explicit: weight of account equity, share quantity, or account-
  currency notional. `omitted_positions` is explicitly preserve or liquidate.
  Empty rebalance is invalid. `hold`, `liquidate`, and `no_decision` are distinct.
  Empty event intents mean no new order/cancel. No-decision still may advance
  state; warmup permits state updates only and must not repeat at the seam.
- The current price profile requires unadjusted prices and rejects windows
  containing corporate actions. This does not certify provider data quality.
  Full action/universe provenance hashes are manifest references, not policy-
  visible source-wide identifiers.

## Past-only projection

`build_observation` accepts normalized trusted listings/bars and current account
snapshots. It exposes only effective listings known at decision, bounded history
available by that time, sorted oldest-first by session/listing, and observed order
events. A missing complete current bar for any effective listing rejects the
whole cross-sectional snapshot. It never silently shrinks the universe.
Unannounced listing end dates are stripped: `effective_to_known_at` is required
before a future removal/delisting is visible. Account and open-order out-of-scope
positions reject rather than disappearing during a universe transition.

The builder's `snapshot_id` hashes precisely this normalized visible input,
excluding the ID itself. It contains no full data hash, future universe member,
window end, total bar count, or final-bar flag. Wire parsing checks its digest.
A caller may explicitly mark full-lookback observations as pre-active warmup;
insufficient history must always be warmup. Actual exchange schedule validation,
knowledge-time provenance, account correctness, and duplicate-event prevention
belong to the trusted engine, not to the policy or this projection helper.

Manifest hashes pin code, canonical parameters, state schema, engine profile,
environment, universe and action snapshots; the runtime/export boundary must
verify those bytes against the pins. The manifest is not embedded in policy
observations, since its full-dataset references could reveal future information.

## Offline validation

`.venv-api/bin/python -m pytest tests/portable/test_contracts.py tests/portable/test_state.py -q`

The evaluator, API and research locks already resolve Pydantic 2.14.1. No new
runtime dependency is required. Prefix/future perturbation tests here validate
only the trusted input builder. Real template replay, sibling-process isolation,
trusted engine economic comparison and cross-framework checks are separate gates.
