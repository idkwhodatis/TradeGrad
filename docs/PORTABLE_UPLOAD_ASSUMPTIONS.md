# Portable upload research assumptions

This optional route is not independently verified raw-price or corporate-action evidence. It does not close the stronger B9 requirement for proven market-data action coverage. Provider-native research and existing synthetic portable fixtures retain their separate semantics. No Yahoo-adjusted or OpenBB-unknown snapshot becomes portable-compatible through this route.

## Explicit admission

The upload form defaults to no declaration. The user can opt into `user_declared_raw_no_actions` only for one exact symbol, MIC and complete `[start,end)` upload range, including all later warm-up sessions. The form retains the existing unknown defaults and does not automatically relabel prices or volume.

A typed declaration requires explicit acknowledgement, a bounded plain-text source reference (512 characters) and basis note (2000 characters). References are retained but never fetched or independently authenticated. The user must select `raw`/`user_declared_raw`, unadjusted split and dividend OHLC, known currency and share volume. Adjustment metadata, existing action snapshot claims and unresolved flags reject admission. Source references must not contain passwords or private access tokens.

The table parser does not discard recognized dividend/split/capital-gain columns for an opted-in upload: nonzero, missing or unparseable values within the declared interval cause failed quality/coverage publication. Absence of those columns or all-zero values is not proof of no corporate actions. Unmapped proprietary action formats cannot be inferred, and the declaration remains the user's claim.

## Immutable evidence and fail-closed staging

The existing worker retains the exact uploaded CSV/Parquet bytes. `portable-upload-declaration.json` binds their SHA-256, byte count and safe artifact name to the exact normalized listing, full date range, source provenance, price semantics and user declaration. The dataset manifest pins that artifact and labels `data_evidence=user_declaration_unverified`.

A distinct `marketdata.corporate-action-coverage.v2` sidecar uses `status=declared_none`, `evidence=user_declaration_unverified`, and `is_synthetic=false`. It never uses `known_none`, `live`, `verified` or synthetic construction proof. Existing fixture v1 sidecars are unchanged.

Portable staging independently validates the retained hashes, original bytes, raw reference, declaration acknowledgement and bounds, listing/venue/calendar mappings, raw semantics and provenance. The permitted upload route is single-listing; both event and one-listing portfolio research can use it. Every warm-up and active session still needs a clean bar and reviewed exchange-clock sidecar. Missing declarations, unknown action evidence, mixed/foreign sources, adjusted or unknown prices, unidentified/undated actions, and any action in a declared no-actions snapshot reject. Ordinary uploads with an empty actions array remain unknown and rejected.

The staging report labels `data_evidence=user_declaration_unverified`, `is_synthetic=false`, `point_in_time_proven=false` and carries an explicit no-provider/runtime/cross-framework-certification warning. Persisted dataset references preserve the declaration after staging. No SDK contract, trading engine, broker, live-data source or runtime certification is added.

## Verification boundary

`tests/portable/test_upload_admission.py` uses fabricated CSV/Parquet inputs and inert data normalization/staging only. `frontend/tests/unit/portable-upload.test.ts` checks opt-in, exact scope, semantic rejection and removal of consent. The separate `frontend/tests/e2e/portable-upload.spec.ts` requires a real API, PostgreSQL, data/research workers, both portable images and browser. It carries declared upload through actual preflight, seed evaluation, trusted-result evidence and reload, plus contradictory-action rejection. It is prepared, not run in the constrained local environment. No candidate policy, wrapper, SDK subprocess or live provider is executed by the local admission checks.
