# Reviewed portable engine runtime

The host must run this image separately from the untrusted policy image. The
engine image contains no candidate source, strategy loader or optimizer. Only
read-only `request.json` and `market.json` are staged at `/input`; the supervisor
relays bounded, length-prefixed JSON observations/replies through stdin/stdout.
Stdout is reserved for protocol frames before importing the native engine.

`request.json` has exactly `schema_version=portable-engine-request.v1`, the full
SDK `manifest`, `window={start,end}` (half-open), positive `initial_cash`,
`initial_state` (SDK StateSnapshot), and `input_identity={run_config_sha256,
market_data_sha256}`. Market digest is SHA-256 of exact `market.json` bytes.

`market.json` has exactly `listings`, `bars`, `sessions`, `corporate_actions`.
Listings and bars use the SDK JSON forms. Sessions contain `session_date`,
`open_time`, `close_time`, `next_open_time`, using explicit UTC RFC3339. The
engine independently verifies the complete exchange session schedule and next
opens with exchange-calendars. Every expected effective listing must have a
complete bar at each staged session; missing bars cannot reduce the universe.
`corporate_actions` must be empty; corporate-action windows are unsupported,
not silently adjusted. The manifest's universe hash covers canonical listings;
its corporate-action hash covers canonical `[]`. Its profile hash is produced by
`profile_sha256(market, initial_cash)`. The host verifies image/environment hashes.

## Explicit controller profile

`akquant.portable.cash-daily.controller-t1.v3` is a bounded application profile,
not native-T+1 parity or a certification of current broker/tax rules:

- Daily CN/US cash long-only equities; CN 100-share lots, US whole shares.
  The native engine settlement switch is explicitly **T+0** in both markets.
  CN T+1 is separately enforced by the trusted controller's daily next-open
  inventory rule. The original native research/baseline profile is unchanged.
- Flat initial positions, no external funding, intraday callbacks, corporate
  actions, leverage, short selling, fractional shares, expiry, stops, OCO or
  trailing orders. Unsupported inputs reject; there is no unsafe fallback.
- Both portfolio and event policies receive a complete daily cross-section after
  close. The native framework's cross-section timer provides the barrier, but
  the policy sees the source close timestamp, not an invented later market bar.
  Policies are never called from native order, generic timer or pre-open hooks.
- `aq.NextOpen()` applies globally and to every order; native GTC only. A native
  fill must belong to a strictly later exchange session than its decision.
  Exact native nanosecond timestamps remain unchanged; separate economic
  reference fields identify that later session's open.
- Limits support only the **marketable-at-open or no-touch pending subset**.
  The pinned native matcher can otherwise use intrabar high/low. Any observed
  non-open fill or slippage-adjusted limit breach fails the entire evaluation;
  the controller never rewrites a native fill to make it look supported.
- Targets use decision-close native equity/prices and deterministic lot flooring.
  Buy budgets reserve fees/slippage without anticipated sale proceeds. Pending
  rebalances are not duplicated. Event replies are preflighted as a whole before
  submitting any leg/cancellation; unsupported sizing rejects instead of clipping.
- Native cash, positions, executions and fees are authoritative. The controller
  maintains bounded intent/reservation records, not a second financial ledger.
  Random native order IDs do not enter policy observations or snapshot hashes.
  There is no terminal liquidation or invented final fill.

## T+1 inventory invariant and next-open sellability

The manifest records `engine_settlement_mode=T+0`,
`application_settlement_enforcement=trusted_controller_daily_next_open_inventory.v1`,
and `application_t_plus_one=true` for CN. `PositionView.available_quantity`
explicitly means **next-open unreserved holdings**, not native current-session
T+1 availability. Every already-held share was acquired no later than this
session; an after-close sell can first execute at a later session's open.

Across all legs, requested sells cannot exceed already-held quantity less every
pending sell remainder. Pending buys never increase this budget. Partial fills
reduce both held inventory and the reserved remainder. A cancel request does not
release inventory until every affected native leg is confirmed terminal. No-touch
GTC limits retain their reservations across sessions. Regressing/overfilled
receipts, same-session fills, unsupported native order states and incomplete
cross-sections fail closed. A buy filled at session T's open can therefore be
requested for sale after T's close for T+1's open, without adding a hidden T+2
holding period. This intended behavior still requires the real paired proof.

The former v2 containment used native T+1 and rejected those earliest exits:
AKQuant 0.3.27 checks sellability at submission before next-date settlement.
There is no supported policy hook between that settlement and NextOpen matching.
The v3 profile deliberately changes the native settlement mode and exposes the
separate controller constraint; it does not relabel T+0 as native T+1. Old v1/v2
profile/manifest hashes reject rather than being silently reinterpreted.

Read-only source evidence at official release commit
`765ee6b82066ae68e9fa7d5ce17ee0bb1840619f` (not execution evidence):
- [NextOpen timestamp guard](https://github.com/akfamily/akquant/blob/765ee6b82066ae68e9fa7d5ce17ee0bb1840619f/src/execution/simulated.rs#L328-L341).
- [Complete cross-section dispatch](https://github.com/akfamily/akquant/blob/765ee6b82066ae68e9fa7d5ce17ee0bb1840619f/python/akquant/strategy_framework_hooks.py#L629-L661).
- [Limit matching and slippage](https://github.com/akfamily/akquant/blob/765ee6b82066ae68e9fa7d5ce17ee0bb1840619f/src/execution/common.rs#L263-L347).
- [Native submission sellability](https://github.com/akfamily/akquant/blob/765ee6b82066ae68e9fa7d5ce17ee0bb1840619f/src/risk/stock.rs#L16-L44).

The paired-container gates require buy-at-T-open to immediate-next-session sale
for both policy modes, plus multisymbol barriers, pending buys/sells, partial
fills, cancel confirmation, no-touch GTC and the supported limit subset. They are
strict tests, not xfails. If a native interaction violates the reviewed profile,
the evaluation must fail and the profile remains unsupported; pure fake-broker
proofs cannot certify actual matching, settlement or container separation.

The result validator is separate from the existing native ResultBundle validator
and imports only the Python standard library. Both containers must terminate
and the supervisor must verify provenance before publishing. JSON schema/hash
validation alone is never isolation proof or execution certification.

`tests/portable/test_engine.py` uses inert broker/result fakes and does not import
or execute AKQuant. Actual paired-container acceptance tests belong to
`tests/portable/test_isolation.py` and are opt-in. Runtime certification remains
false until those tests run successfully in an authorized Docker environment.
The engine dependency closure combines the evaluator lock with the copied,
hash-pinned `calendar-requirements.lock`; it does not install the data-provider
or API environments.
