# V1 strategy authoring completion (B5)

Prepared implementation, not hosted acceptance. This patch closes the functional native declaration and portable whole-source authoring gaps behind the V28/V34 audit rows. It does not certify paired-container isolation, live providers/models, wrappers, paper/live trading, or all original V1 gates. The earlier M5 gate remains independently pending.

## Native immutable declarations

`POST /api/v1/strategy-versions` accepts optional `manifest: NativeStrategyContract` alongside exact source, task, name and parent. The existing immutable `StrategyVersion.manifest` and `parameters` columns store the normalized declaration and derived scalar values. No migration or in-place update is introduced. Owner-scoped idempotent replay compares declaration and parameter values as well as exact source. A changed parameter is a new version even when the code hash is identical.

`native-declarations.v1` contains:

- `required_fields`: unique OHLCV/amount field names. Engine-required OHLCV remains mandatory; declared amount is additionally required and must be finite and non-null. No synthetic amount or fill-forward is invented.
- `lookback_bars`, `warmup_bars`: 0–252 previous available sessions. Configured warm-up must cover the maximum of the two, bounded by the pinned engine history depth. Missing per-listing history fails preflight/staging with an actionable error.
- `requires_shorting`: an exact boolean. A true claim requires a supported margin/shorting market profile. CN short selling remains unsupported. Historical undeclared US seeds retain their existing explicit shorting requirement.
- Up to 32 typed named scalar parameters, with integer/number bounds, optional choices, finite values and bounded strings. The complete declaration fits 16 KiB, below the evaluator request limit. Tuple fields and scalar members are frozen; callers receive detached value maps.
- Explicit `user_declared_not_code_verified` evidence. A declaration is not proof that source consumes a parameter, needs only its declared history, is safe, or avoids lookahead.

Configured research pins the exact declaration in `RunConfig.strategy_contract`; API preflight, worker preparation and final-selection binding reject mismatches. The declared values are passed through the existing isolated evaluator request and supplied to AKQuant's strategy context as `self.tradegrad_parameters`. For example, source can read `self.tradegrad_parameters["allocation"]`. Neither the API nor research worker imports source to discover, verify or inject Python attributes.

A null/absent declaration preserves historical RunConfig serialization byte for byte, including sealed fixture hashes. It also preserves the old evaluator context and source-compatible behavior. `source_compat` rejects new declarations instead of changing the baseline silently. The old `/runs` seed API cannot bypass declaration preflight; declared versions use configured research, with zero optimizer iterations for seed-only evaluation.

## Portable whole-source versions

`POST /api/v1/portable-strategy-versions` accepts exact UTF-8 Python (128 KiB), name, task, parent, full strict `StrategyManifest`, and bounded parameter JSON. The optional manifest `authoring` block is required for this new endpoint and contains:

- `source_kind: user_authored`, with an explicit unverified-declaration label.
- `parameter_schema: StateSchema`, reusing strict field types, unknown-field rejection and bounded finite JSON validation.
- `initial_state: StateSnapshot`, validated against the declared state schema/checksums. This is the saved default initial state; each RunConfig still pins its actual initial state.

The server assigns the new version identity and recomputes source/parameter hashes; it clears a submitted reference-result hash. Other manifest fields remain strict immutable claims. Owner checks, distinct idempotency namespace and the existing database immutability trigger cover roots and derived versions. `GET /portable-strategies/{id}/source-draft` returns an owner-private editable copy, including valid declarations when deriving an old reference policy. Saving never imports or executes a candidate. AST parsing is syntax/interface-shape checking, never a safety proof.

A custom `Policy` source may differ completely from the reference templates. Zero-iteration evaluation accepts source without edit markers. Optimization uses one `### EDIT START` / `### EDIT END` region and freezes the bytes outside that region against the **user's own seed**, not a reference template. Existing reference policies retain their original template and parameter checks. A custom policy must satisfy the same state/decision contracts and isolated trusted-engine boundary at execution; runtime capability/preflight failures still fail closed.

The optional authoring block is omitted when absent, preserving historical portable manifest hashes. No new claim of runtime or external-framework certification is made.

## User interface and retry behavior

Strategy Library exposes native declaration editing, exact source upload, immutable parameter-only versions, parent comparison and readable saved declarations. Configured research carries the selected version's declaration into preflight with declared history defaults and field/shorting explanations.

The portable section exposes bounded Python upload, full source editing, strict manifest/parameter JSON, parent-linked new versions, diff and saved detail. Drafts are owner-scoped tab storage. An uncertain write freezes the exact request and idempotency key across reload; retry returns the same version. Closing retains a draft, while explicit discard is disabled for uncertain saves. Existing template creation in the portable experiment picker remains available.

## Acceptance and remaining gates

Pure contract coverage lives in `backend/tests/test_strategy_authoring_contracts.py` and the declared-export case in `tests/tradegrad/test_strategy_package.py`, so existing backend/research contract jobs discover it. Locally allowed checks are pure/inert tests, static/type/build checks, OpenAPI generation and test collection. No strategy/SDK subprocess, PostgreSQL, Docker, socket server or browser was executed here.

Prepared actual gates:

1. `tests/integration/test_strategy_declarations.py`: real migrated PostgreSQL HTTP replay/race/owner/immutability; declared amount preflight and omitted-contract rejection.
2. `tests/system/test_strategy_authoring_pipeline.py`: actual API, queue and native Docker evaluator; identical source with different parameter versions must produce different nonempty numeric fill quantities. Separate custom portable whole-source path verifies both physical images, actual decisions, trusted ledger and confirmed cleanup.
3. `frontend/tests/e2e/strategy-contracts.spec.ts`: actual API/PostgreSQL Chromium upload/edit/derive, draft close/reload, exact parameter pinning, lost-response replay and unchanged parent versions.

These gates are collected only, not passed. They must run on the integrated exact head with rebuilt native/portable images, existing explicit opt-in harness flags, three locked runtimes and disposable PostgreSQL. No workflow file is changed in this patch. The new system module must also be named in a hosted job with both existing harness opt-ins enabled; generic unconfigured collection is not an execution pass. Re-run the cumulative M5/M6/final/package gates after integration; stale images must fail the build-input digest check rather than be reused.

Combined-tree integration retains benchmark selection/derivation and all model,
Settings and native/portable execution fences. It passed 290 inert tests (three
nonmatching paths excluded), five pure checkpoint/legacy-serialization checks in
the locked research environment, and 423 frontend unit tests. Typecheck and
production build passed. Four PG/system and two Chromium cases collected only.
The hosted workflow explicitly opts into both real native and paired-container
custom-authoring harnesses; these remain unrun at this local revision.
