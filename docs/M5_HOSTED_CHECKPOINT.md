# M5 hosted validation checkpoint

Recorded 2026-10-23. **The subsequent normal full M5 gate passed; see the final
section.** Targeted diagnostics are not a substitute for cumulative acceptance.

## Last complete full-gate attempt

[Run 39899375443](https://github.com/idkwhodatis/TradeGrad/actions/runs/39899375443)
tested commit `67a1bc13ff0de50faed5d57920ad5854590c9bae`.

- The four-task upstream baseline, locked component tests, real PostgreSQL,
  three image builds, 52 paired-container cases and kernel-isolation checks passed.
- Both real worker crash/recovery gates, configured native optimization and
  checkpoint flow, native final holdout, both portable research modes and their
  checkpoint flow, and portable final holdout/cancel/retry/export checks passed.
- Required teardown barriers passed before independent acceptance groups ran.
  A failure remains an overall workflow failure; continuation does not waive it.
- Chromium completed 28 cases: 27 passed and one native-final browser case
  failed. This run therefore **failed**, despite all non-browser gates passing.

The prior research-recovery timing failure did not recur on this run. Its 18-second
wait and independent before-natural-completion proof were not relaxed. No specific
production deadlock or repaired timing root cause has been established.

## Bounded chart diagnosis

Static inspection of that case found an accessible-name assertion expecting a
prefix ending in a semicolon. The
installed ECharts renderer instead settles the chart name to its configured
description, beginning `Holdout Equity.`. The corrected assertion checks that
exact configured description **and a visible rendered SVG**, without loosening
result identity, timeout, cancellation or cleanup assertions.

[Diagnostic run 39902721107](https://github.com/idkwhodatis/TradeGrad/actions/runs/39902721107)
tested commit `64fa3f8bc9f83b09c1ed9cfb5436e21da1155a22`, tree
`4de03906fecc67318d9f11384c7347900bf3ca62`.

- Separate locked API/data/research runtimes, actual PostgreSQL migration and
  bootstrap, the native Docker image and real Chromium were used.
- Exactly one native-final browser case passed in 25.474 seconds, with no skipped
  cases, errors or failures. The sanitized source-failure projection was empty.
- The independent owned-browser cleanup marker was `confirmed`.
- Sanitized outcome artifact: `12479263978`, SHA-256
  `223031c532fa59bdbdec174f980b2be51dbb7827711fa6c9a5c6c74a909f2832`.
- The baseline and full application jobs were deliberately skipped in this
  explicitly labelled diagnostic profile. Its success **does not pass M5**.

## Gate rule retained after diagnosis

A normal feature-branch push without the diagnostic marker must pass the entire
M5 workflow on its exact head before M6, native-operator admission and cumulative
completion gates advance. Separate bounded live-source checks remain distinct
from frozen-data/model tests. No paid/live model calls, trades, merge or deployment
are part of this checkpoint. Git commit metadata does not alter actual GitHub
push or workflow timestamps.

## Subsequent complete M5 acceptance

[Run 39903672696](https://github.com/idkwhodatis/TradeGrad/actions/runs/39903672696)
passed on exact head `5bf831f63fa0daf406a355ae99f09d3b9b471be1`, tree
`6e3daf769f6415c19cdc30d8fa33b2e3e21a36f5`. The sanitized artifact verified
297/297 PostgreSQL, 52/52 paired-container and 28/28 Chromium cases, all required
native/portable application groups and explicit owned process/container cleanup.
Both required real worker recovery cases passed. Seven unrelated opt-in cases
in that recovery test file were intentionally skipped, not counted as passed.

This is the complete normal M5 workflow, not a combination of the earlier failed
run and one-case diagnostic. Only the diagnostic-only job was intentionally
skipped. Later M6/operator/cumulative/live-source gates retain their own evidence.
