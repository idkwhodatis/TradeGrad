# Native daily source-session alignment (I7 correction)

## Defect and bounded correction

Native CSV `date` values are exchange-session labels (the existing
`packages/tradegrad_adapter/windows.py` contract), not UTC instants. AKQuant
0.3.27 interprets timezone-naive input timestamps with its separate
Asia/Shanghai input default, even when the strategy timezone is America/New_York.
Consequently a US source row labelled Monday can run on the previous Sunday;
start-window filtering can discard the first scored source row. Its actual
source timestamp then flows into callbacks, history, membership lookup, trades,
and trace records. Relabeling stored trace timestamps would not fix this defect.

For `corrected_explicit_config` only, both native timing and cross-sectional
entry paths now supply timezone-aware dates before AKQuant constructs its feed
and trading-day metadata. Date-only values are localized to midnight in the
resolved market timezone, including DST offsets. Already-aware direct inputs
preserve their instants. Immutable CSV bytes, staged window bounds, prior-row
warmup policy, cached raw frames, and engine execution policy are unchanged.
`source_compat` bypasses this transformation entirely.

Midnight is a **session label**, not an assertion that a completed daily close
was available then. Native `decision_timing` remains `native_akquant`; unlike the
portable protocol, it does not carry a pinned close/next-open clock schedule.
This patch neither guesses 15:00/16:00 closes nor claims exchange-calendar,
early-close, intraday availability, or arbitrary native-code no-lookahead proof.
Corrected result exports explicitly state this limitation in timestamp semantics.

## Actual engine evidence

The old production CSV loader with the frozen eight-session, four-symbol I0
sample and scored Jan 4–11 window emitted only five decisions. The first,
Jan 4 16:00 UTC, already consumed Jan 5 prices: B's trailing return was
`13 / 11 - 1`, and Monday Jan 8 was emitted on Sunday Jan 7.
The corrected path emits all six scored session labels, beginning Jan 4
05:00 UTC (midnight New York), with B's expected `12 / 10 - 1 = 0.20`,
C's `15 / 10 - 1 = 0.50`, and C's average dollar volume `370 / 3`.
The trace receives the genuine callback source timestamp; none is rewritten.

`tests/research_trace/test_native_session_alignment.py` exercises both unchanged,
SHA-verified maintained templates through actual installed AKQuant 0.3.27:

- CN and US, January plus US spring and fall DST boundaries. CN's preceding UTC
  date still resolves to the correct local source session.
- Production date-only CSV staging/loading, exactly two prior warmup rows,
  first and last scored sessions, and end-exclusive source-row removal.
- Source event ID, callback timestamp, information-cutoff label and actual native
  equity timestamp correspondence; one complete event for all four symbols.
- Frozen feature values and membership changes on the source session itself.
- Cold-start missing-history states; future-input mutation leaves earlier
  controlled observations unchanged (not a general native trust certification).
- Trace on/off exact economics parity and legacy raw-loader/engine equivalence.

Separate cache tests verify cold/warm equality, no source/cache/frame mutation,
DST localization, already-aware instants, and fail-closed missing dates.

Local validation on 2026-10-23: the required-native research-trace aggregate
passed **384 tests in 101.86 seconds**. The final baseline aggregate, including
four original-seed legacy economic-hash gates, passed **40 tests in 22.26
seconds**. The installed interpreter was the existing pinned native environment;
no dependency installation or runtime substitution was used. Ruff passed for
new/helper/export files; the two legacy evaluators retain their pre-existing
PLW1508/C413 lint findings (focused checks exclude only those rules).

## Historical and current four-seed expectations

`docs/evidence/native-session-alignment-20261023.json` retains the exact old I6
commit and evaluator hashes, all four unchanged original seed hashes, frozen
baseline manifest hash, and before/after measurements using AKQuant 0.3.27,
pandas 2.3.3, NumPy 2.3.4 and production `PYTHONHASHSEED=0`. It compares actual
orders, fills, positions, closed trades, equity and metrics, excluding only
random native IDs and pandas row labels from the economic hash.

All four `source_compat` economic hashes and full performance objects are equal
before and after. Both corrected CN economic hashes also remain equal. Corrected
US expectations intentionally change because session membership and execution
windows were wrong. The old corrected values remain in that evidence file;
`tests/baseline/expected.json` now contains the independently reproduced corrected
values from the normal `script/validate_baseline.py` subprocess path.

These are local real-engine component measurements, not PostgreSQL, Docker,
HTTP, Playwright, live-provider or market-validity certification. No model,
broker or paid-provider call was made. The existing build-input closure already
includes every changed runtime file; rebuild both dependent research/evaluator
images and rerun their exact-head gates. Never reuse an old image or relabel old
results/configuration evidence as corrected runs.
