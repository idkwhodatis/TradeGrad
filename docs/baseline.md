# M0: frozen TradeGrad / AKQuant baseline

## What this gate proves

The four **unchanged** upstream seed programs (`cn_cross`, `cn_time`, `us_cross`,
`us_time`) run in separate Python subprocesses using **AKQuant 0.3.27**, with no
model client invocation, model key, paid request, network market-data download,
or simulated engine replacement. The fixture has 40 synthetic symbols plus one
synthetic index per market, each with 84 business-day OHLCV rows. These are **not
historical prices or exchange-calendar-validated sessions**. The intentionally
artificial returns are not investment performance evidence.

Pinned source: `baf73ee56353de8996731139f6721b62f4b26da7` (the original checkout).
The direct research requirements are fixed in `requirements.txt`; the complete
Python 3.12 validation environment is fixed in `tests/baseline/requirements.lock`.
The fixture manifest records every CSV's SHA-256. The fixture generator is local,
deterministic, and never calls an upstream provider.

## Reproduce

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r tests/baseline/requirements.lock
.venv/bin/python -m pytest tests/baseline -q --junitxml=m0-junit.xml
.venv/bin/python script/validate_baseline.py
```

The pytest integration fixture always performs all four actual evaluations twice,
once with an empty cache and once with a warm cache. It compares the repeats
exactly and compares rounded metrics with the committed numerical reference
(`tests/baseline/expected.json`, absolute tolerance 0.01 for two-decimal source
metrics). It is not optional, marked as a mock, or skipped when AKQuant is missing.
Other integration tests run simultaneous CN/US evaluator subprocesses, execute
warning/nonzero/missing/invalid-result cases, kill a sleeping process group, and
verify that coordinator credentials are absent from the child environment.

`script/validate_baseline.py --output <new-directory>` writes per-task `metrics.json`,
`job.json`, `log.out`, `log.err`, and a top-level `report.json`, including dependency
versions, the fixture digest, status and `llm_calls: 0`. Existing task output
subdirectories are rejected, not overwritten. `llm_calls: 0` describes this
non-optimizer smoke path, not a model-cost accounting feature.

## Explicit execution semantics

- Both evaluators require `--market CN|US`. Loss objects require a market, and
  `run_one.build_loss` and `eval_runs.evaluate_one` pass their task's market rather
  than mutating global environment variables. Ambient `AKQUANT_MARKET` has no effect.
- The default `corrected_explicit_config` profile explicitly sets CN time zone
  Asia/Shanghai, CN lot size 100, and US time zone America/New_York, US lot size 1.
  CN uses T+1 and the upstream example's stamp duty; US has no stamp duty, permits
  same-day sells and preserves the upstream margin/short-sell settings.
- `--profile source_compat` preserves the pinned source's **engine kwargs**,
  including implicit engine time zone/lot defaults. It does not reintroduce the
  US market-selection bug, unsafe subprocess behavior or pickle cache. Neither
  profile claims a full upstream research-protocol or paper reproduction.
- The historical source's commission, stamp duty, slippage and trade-frequency
  floors remain **example baseline parameters**, not current broker/tax advice.
- Legacy evaluator date windows remain **inclusive [start, end]**. The WebUI data
  adapters' half-open [start, end) contract must explicitly convert boundaries;
  changing this legacy behavior here would silently change historical scoring.
- Seed source code, initial history handling, scoring formulas, and rounding
  before reward evaluation are retained. In particular, the original cross
  seeds can score insufficient initial history as NaN; Python's unordered sets
  made early selection and later metrics vary. Evaluator subprocesses now fix
  `PYTHONHASHSEED=0`, so this baseline is repeatable on the pinned environment.
  This does **not** certify warm-up adequacy, absence of look-ahead, or arbitrary
  candidate determinism. The immutable run configuration must address warm-up
  and strategy semantics in the later execution-adapter milestone.
- Metrics are the source's annualized return (percent), maximum drawdown (percent),
  Sharpe, closed trades per estimated 243-trading-day year, win rate (percent),
  return per trade (percent), and the source combined reward. A complete
  versioned ResultBundle is a later gate, not inferred from these metrics.

## Process outcome and resource boundaries

Success requires exit code 0, no timeout, a newly produced JSON result of bounded
size, all required finite numeric metrics, and positive completed-trade frequency.
Stderr is retained as evidence: a warning is not failure. Nonzero exits fail even
if a valid result exists. A stale previous metrics file is removed before launch.
Launch failures and timeouts produce structured `job.json` failures.

The supervisor starts a new POSIX process group, terminates it on timeout and
collects the leader's exit status; lingering descendants in that group are killed
also after normal leader exit. This is **not** complete application cancellation,
a lease/fencing protocol, or a security sandbox. A process that deliberately
escapes its group needs the isolated runner/container boundary.

Only a small environment allowlist reaches evaluators; database/model credentials
are excluded. Native math libraries and nested CSV workers are limited to one.
`TRADEGRAD_WORKERS` bounds outer window/evaluation pools (default 1), and
`TRADEGRAD_LLM_WORKERS` bounds the existing optimizer's summary-call pool (default
1). `TRADEGRAD_DATA_WORKERS` can bound direct CSV-loader use, but supervised
candidate evaluations force it to one to avoid multiplying nested concurrency.
These are conservative local limits, not cross-run OS-enforced resource quotas.

## Content cache

The cache key includes SHA-256 of the selected source bytes, their symbol names,
the parsing transformation version and pandas version. Path and mtime are not
identities. Parsing uses the same byte snapshots that were hashed, eliminating
hash/read races. Missing selected symbols fail instead of silently shrinking the
universe. Cache writes use unique temporary files plus atomic replacement.

The format is JSON with a constrained dtype schema, not pickle. Python's exact
float serialization preserves parsed float values on cache round trips; tests
include high-precision numbers, identical mtimes with changed content, source
mutation during parsing, concurrent publication, and corrupt cache recovery.
A cache checksum detects corruption; it is not an authenticity guarantee against
an adversary who can write the cache. Filesystem access control and memory limits
belong to the runner. Raw byte snapshots are held in memory while parsing.

This is a **data-parsing cache**, not a complete evaluation-result cache. Future
result cache keys must additionally bind code, time windows, universe snapshot,
engine profile and dependencies. No result cache keyed only by CSV data is added.

## Deferred gates and known limits

- No claim of live AKShare, yfinance or OpenBB provider acceptance.
- No claim of secure untrusted execution, process-tree/container cancellation,
  persistent queue delivery, multi-user authorization, or worker fencing.
- No complete checkpoints, resumable optimizer state, immutable repair lineage,
  budget tracking, or final-test exposure protocol. The legacy optimizer remains
  unsuitable as a resumable WebUI service until the adapter replaces those paths.
- No claim that M0 alone implements the V1 product or validates browser behavior.
  Real PostgreSQL, isolated-runner, recovery, Playwright and provider tests remain
  the explicitly separate release gates in `docs/V1_IMPLEMENTATION.md`.
