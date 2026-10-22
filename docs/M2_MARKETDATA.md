# M2 market-data contracts and runtime evidence

This module is the data boundary, not a statement that any public provider has
passed its live release gate. API-safe models/catalogs are in
`packages/marketdata`; heavy SDKs load only inside the independent data runtime.

## Frozen interface

- `get_provider(name, mode="live")`: `akshare`, `yfinance`, or `openbb`.
- `capabilities()`, `resolve_instruments(symbols)`, `fetch_bars(BarsRequest)`.
- `BarsRequest` uses resolved `Listing` objects, exchange-local daily dates and
  `[start,end)`. SDK end-date conversion occurs independently in each adapter.
- Results contain `data`, `raw`, `provenance`, `coverage`, `warnings`, optional
  actions, and structured `price_semantics`. Missing data is never filled or
  fetched from another provider.
- A capability can be `verified` only with **live** evidence, exact package
  versions, explicit symbols, checked time and date scope for bar capabilities.
  SDK parser/fixture tests remain documented, never upstream verification.

`Listing` uses validated internal UUIDs and exchange MIC, currency, IANA timezone,
calendar, optional known listing lifetime and validity-dated vendor mappings.
Initial provider identities are venue-scoped provisional registry identities;
they do not pretend a ticker globally identifies a security, prove a reused
symbol's historical identity, or unify cross-listed shares. The database keeps
persisted identities across mapping changes. User-upload identities are explicitly
`user_supplied` and owner-scoped, not allowed to overwrite provider identities.

## Upstream semantics

### AKShare 1.19.1 / Eastmoney

`stock_zh_a_hist` is called independently for each listing. The `adjust` parameter
is retained as raw (`""`), qfq or hfq. The upstream inclusive end is `end - 1 day`;
normalized results are filtered again. The documented volume unit is lots and the
A-share adapter converts it to shares by multiplying by 100. This does not alter
execution lot-size rules. Prices and metadata are preserved before validation.
A separate action feed is not implemented or inferred from adjusted prices;
strict corporate-action research is unavailable.

Resolution uses `stock_info_a_code_name` and only enables Shanghai/Shenzhen A-share
symbols. Other instruments remain unresolved. Resolution provenance identifies
its exchange-directory sources separately from Eastmoney bars.

### yfinance 1.7.0 / Yahoo

`Ticker.history` always receives `auto_adjust=False`, `back_adjust=False`,
`repair=False`, `actions=True`, `keepna=True`, `rounding=False`, a timeout and
explicit error behavior. Original OHLC, Adj Close, dividends and splits are kept.
Yahoo OHLC is already split-adjusted, **not truly unadjusted**; Adj Close also
incorporates dividends. Events have their own provenance and an actions snapshot
hash. Missing or null required dividend/split fields fail explicitly; they never
become an authoritative empty event feed. The adapter never reconstructs original
prices or performs repair.
Resolution exact-matches Yahoo Search results and validates history metadata
exchange, USD currency, asset type and IANA timezone; unknown venues do not become
US listings by ticker shape alone.

### OpenBB V5 / Nasdaq

See [OpenBB evidence](M2_OPENBB.md). Lean `openbb-core==2.0.1` and
`openbb-nasdaq==2.0.0` use the provider namespace. No `openbb-yfinance` fallback or
legacy shared-equity router is installed. Nasdaq native split/dividend basis is
unknown and remains visible, regardless of whether the SDK adds adjustments.

## Calendar and quality behavior

`exchange-calendars==4.13.2` supplies explicit session schedules. Shanghai uses
XSHG; Shenzhen uses the explicitly disclosed mainland XSHG schedule proxy, not a
fictitious native XSHE implementation. Both US adapters persist the same canonical
XNYS calendar while retaining the actual listing MIC (for example XNAS).
In the locked library, `get_calendar("XNAS") is get_calendar("XNYS")` is true:
XNAS is an alias to the XNYS implementation, not an independent Nasdaq schedule.
This regular-equity schedule proxy is explicitly recorded in listing metadata,
and cross-provider resolution tests prevent registry calendar conflicts. An absent
calendar or a date outside its supported bounds yields unknown coverage, never weekday synthesis. In this
pin XSHG's precomputed limit is 2026-12-31 and cannot establish later sessions.

Coverage records requested/actual sessions, missing, non-session, provisional,
before-listing and after-delisting dates. A missing session is not guessed to be
a suspension; absent authoritative suspension evidence it stays
`upstream_missing_or_suspension_unknown`. Holiday-only intervals have no expected
bars. Session-close instants, including early closes and DST, determine
provisional status. Provisional/partial/failed/unknown coverage cannot publish a
ready snapshot. Duplicate sessions, nonfinite/impossible OHLC and negative volume
fail validation while raw tables remain available. Invalid rows are never
forward-filled into tradable observations.

## Artifacts and price identity

`write_bundle` only writes an empty staging directory; the data worker performs
atomic publication. Artifacts contain normalized Parquet, original SDK tables,
preview rows, actions, provenance, coverage and quality JSON. Original tables
include Arrow IPC bytes preserving float bits, index types, timezones and nulls;
the accompanying JSON preview is not the authoritative precision representation.
This preserves SDK returns, not a claim to preserve upstream HTTP wire bytes.

Each file has SHA-256. The manifest fixes request, listing mappings, calendars,
versions, price semantics, actions identity, warnings and exact artifact hashes.
`bars_sha256` separately identifies canonical normalized rows. Refresh writes a
new version and cannot replace old files. `adjustment_as_of` remains unknown
(`None`) for AKShare/Yahoo: neither SDK supports an independently evidenced
historical adjustment-state request. `request.as_of` controls provisional-session
coverage only; it is not adjustment-state or PIT evidence. Provenance `fetched_at`
records actual acquisition, never an inferred adjustment-state timestamp.
Uploaded CSV/Parquet units/currency/price semantics are retained as
unknown when supplied that way, never guessed. File size, Parquet expanded size,
row/column counts and nested types are bounded before materialization. Uploads
are parsed once across their whole requested window, never per SDK date block.
The exact original file is preserved once as a hashed immutable artifact;
checkpoints contain only that verified reference, not repeated expanded Arrow
copies. Standalone normalization embeds the exact original bytes instead.
Limits are 10 MB source, 100,000 daily rows, 50 input/eight mapped columns,
100 MB Parquet expansion and 100 MB deep parsed-frame size. Every nonblank CSV
record must match its header width before pandas parses it. US/CN MIC, currency,
timezone and calendar/proxy compatibility use the same SDK-free validator in
API and worker; foreign exchange calendars are rejected.

Evaluator CSVs are explicitly derived, with a manifest mapping for every symbol.
A one-listing time-series export uses the engine's required `INDEX.csv` alias;
it does not claim the stock is an index. A requested basket defaults to user-fixed
non-PIT membership. The orchestration layer replaces that export with the actual
pinned universe timeline when one was supplied. Current memberships cannot be
backfilled into historical PIT universes. Unknown/mismatched units must be
rejected by research preflight rather than treated as shares silently.

## Resource and secret boundary

SDK calls are serial and individually bounded. Transient network/429/server
failures retry a finite number of times; schema/access errors do not retry.
`budget_scope` reserves each attempt against the worker's PostgreSQL-shared
upstream/endpoint budget. A local process limiter supports standalone tests.

Every real SDK call runs in an allowlisted child module with a private temporary
HOME/cache, no inherited database/model/API secrets, an explicit environment,
bounded file output and a hard process-group deadline. Normal existing CA/proxy
variables are retained; TLS verification is never disabled and trust settings
are never modified. Standard output/error are discarded, not reflected into API
messages. Process groups are cleaned on exit/timeout; parent-death cleanup handles
worker crashes, including death before Python/SDK initialization through a
creator PID fixed before launch. Provider exceptions become sanitized codes. No shared SDK global
credentials are mutated.

## Dependency and verification commands

Python 3.12; `runtimes/data/requirements.lock` is a complete 105-package hash lock.
It includes orchestration requirements constrained to the backend's exact pins,
not system-site-packages or an implicit API environment overlay.

```sh
python -m venv .venv-data
.venv-data/bin/python -m pip install --require-hashes -r runtimes/data/requirements.lock
HOME=/tmp/tradegrad-build-home .venv-data/bin/openbb-build
.venv-data/bin/python -m pip check
.venv-data/bin/python -m workers.data --help
.venv-data/bin/python -m pytest tests/marketdata -q
```

`runtimes/data/Dockerfile` builds the same separate runtime. Local Docker execution
was not attempted because this executor forbids it; the parent owns hosted gates.

Deterministic tests exercise real installed AKShare and yfinance parsers against
synthetic transport responses, the real generated OpenBB client, independent
adapter flows, raw/Parquet fidelity, calendars/early closes, failures/retries,
manifest checksums, changed-history refresh and process-boundary cleanup.
The generated fixture window is `[2024-01-02,2024-07-01)` and uses real exchange
sessions with explicitly artificial prices. These are neither captured market
responses nor live evidence. Small request slices remain deterministic.

**Live evidence from the shared-contract/AKShare/yfinance implementation task:
none.** Network opt-ins are skipped by default. On an authorized network runner:

```sh
TRADEGRAD_MARKETDATA_LIVE=1 .venv-data/bin/python -m pytest tests/marketdata/test_live_providers.py -q
TRADEGRAD_OPENBB_LIVE=1 .venv-data/bin/python -m pytest tests/marketdata/test_openbb.py -k optional_live -q
```

An upstream failure must remain a failed gate, never a substituted fixture pass.
Current offline warnings include exchange-calendars/NumPy deprecations, yfinance's
`raise_errors` deprecation and OpenBB's aiohttp subclass warning. They are visible
and do not change price semantics; no warning is asserted to prove upstream access.

## Primary sources checked 2026-10-22

- [AKShare stock contracts and volume units](https://akshare.akfamily.xyz/data/stock/stock.html)
- [AKShare 1.19.1 package](https://pypi.org/project/akshare/1.19.1/)
- [Yahoo price adjustment semantics](https://ranaroussi.github.io/yfinance/advanced/price_adjustment.html)
- [yfinance history arguments](https://ranaroussi.github.io/yfinance/reference/yfinance.price_history.html)
- [yfinance 1.7.0 package](https://pypi.org/project/yfinance/1.7.0/)
- [exchange_calendars source and supported exchanges](https://github.com/gerrymanoim/exchange_calendars)
- [OpenBB Nasdaq historical endpoint](https://docs.openbb.co/odp/python/reference/nasdaq/equity/historical)

SDK distributions, signatures and installed implementation code were also inspected
locally. Data-provider terms and usage rights remain distinct from SDK licenses.
