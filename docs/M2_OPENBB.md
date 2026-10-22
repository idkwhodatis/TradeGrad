# M2 OpenBB Nasdaq adapter

## Runtime and provenance

This connector is independent of the AKShare and yfinance connectors. Its upstream
is Nasdaq, not Yahoo. Product **OpenBB V5** is not an SDK package-major requirement:
this implementation locks `openbb-core==2.0.1` and `openbb-nasdaq==2.0.0` in the
separate data runtime. No `openbb-yfinance`, metapackage, legacy asset-class router,
API key or account is required.

Official PyPI wheels were inspected and installed on 2026-10-22. Nasdaq declares
`openbb-core[pandas]>=2.0.0`; both packages declare Python `>=3.10,<4`. The compatible
full transitive combination is recorded in `runtimes/data/requirements.lock`, not
assumed from the marketing version. After installation run `openbb-build`; then
assert both `obb.nasdaq.equity.search` and `obb.nasdaq.equity.historical` exist. The
lean install matters: the provider package steps aside when a corresponding legacy
router extension is installed. Namespace presence is an executable contract test.

Sources: [Nasdaq package](https://pypi.org/project/openbb-nasdaq/2.0.0/),
[core package](https://pypi.org/project/openbb-core/2.0.1/),
[V5 provider FAQ](https://docs.openbb.co/odp/python/faqs/data_providers).

Wheel SHA-256 values checked against downloaded official PyPI files:

- core 2.0.1: `4f5aa21ba00cbf194423f154a58f60ee413599531cf1c4abfe5ef3275a516f64`
- Nasdaq 2.0.0: `e1a990b90509d69b92ad64adf7db62284f41455c7632f2b69e6b827d9669ea76`

## Search, identity and historical semantics

`obb.nasdaq.equity.search(query=..., is_symbol=True, is_etf=False, provider="nasdaq")`
reads the current Nasdaq-traded directory. In this exact package, `is_symbol` does
not enforce equality and the query is interpreted as a regex. The adapter escapes
and anchors symbols, then independently requires a unique exact symbol/alias match.
No static ticker list is used in live mode. Known alias defect: the returned
`exchange` can be null while the extra `Listing Exchange` field holds the code.
Both forms are handled and tested through the actual generated SDK.

Directory exchange codes map Q/N/A/P/Z/V to XNAS/XNYS/XASE/ARCX/BATS/IEXG respectively.
Unmapped venues, invalid symbols, ETFs, test issues and explicitly named
warrants/rights/units/debt instruments are not guessed into stock
listings. Stable provisional identities are scoped to venue and asset type. USD,
America/New_York and the XNYS regular-equity-session calendar proxy are explicit
adapter mappings, not fields supplied by this directory. IPO/delisting dates and
historical symbol validity remain unknown. Round lot size is not a volume multiplier.

Sources: [search contract](https://docs.openbb.co/odp/python/reference/nasdaq/equity/search),
[Nasdaq directory definitions](https://www.nasdaqtrader.com/Trader.aspx?id=SymbolDirDefs).

The historical route takes symbol/start_date/end_date and returns an OBBject with
results, provider, warnings and extra metadata. Each row contains date/OHLC, volume
and optional VWAP; symbol is populated for multi-symbol requests. The Nasdaq helper
sends inclusive `fromdate`/`todate`. TradeGrad subtracts one calendar day from its
exclusive end, then filters independently to `[start,end)`.

Nasdaq batch helpers can return only successful symbols and a warning. This adapter
uses serial singleton requests in at most 366-calendar-day blocks, preserves the
uncleaned serialized OBBject including warnings, and computes coverage independently
for every listing. A successful SDK envelope is not proof of complete coverage.
Malformed/missing volume is not replaced with zero, and missing sessions are never
forward-filled. The provider-native price basis retains unknown split and dividend
basis; no strict corporate-action capability or synthesized actions are claimed.

Sources: [historical contract](https://docs.openbb.co/odp/python/reference/nasdaq/equity/historical),
[versioned Nasdaq helper source](https://github.com/OpenBB-finance/OpenBB/blob/46c3f8e77b6297d881dd1d8966f8a2b3f115bea2/openbb_platform/providers/nasdaq/openbb_nasdaq/utils/helpers.py).
The installed official 2.0.0 wheel, rather than a moving branch, was inspected for
endpoint construction and batch error behavior.

## Bounds and security

The actual SDK call uses the shared bounded SDK-process helper with a 45-second
deadline, an explicit minimal environment and fresh private HOME/cache. Unrelated
app/database/model credentials are not inherited. Existing permitted CA/proxy
configuration is preserved unchanged. SDK stdout/stderr are discarded, the JSON
output file is size-limited, and the owned process group is cleaned up. The adapter
uses two attempts by default (a hard maximum of three), retries only classified
transient failures, and
reserves a shared `nasdaq:equity.<endpoint>` budget before each attempt. Default
maximum request work is 100 singleton date blocks. Both interval and attempt
limits are validated. The data worker separately owns durable chunking, cancellation,
lease fencing, publication and cross-process budget coordination.

No global credentials/preferences, account access, host trust store or TLS settings
are changed. The runner refuses explicit `verify_ssl=False`. Runtime images must
provide a writable HOME/cache location and build the generated client in advance.

## Evidence and remaining live gate

- Actual package installation and `openbb-build`: passed locally with core 2.0.1 /
  Nasdaq 2.0.0, Python 3.12.14.
- Local synthetic adapter → 124-session immutable-format bundle → hash verification
  → original `us_time` seed with actual AKQuant 0.3.27: passed; nonzero trading. This
  is real engine execution on synthetic prices, not live market-data verification.
- Actual generated SDK search and historical transformation using synthetic upstream
  responses: deterministic tests, **not live verification**. Covers the alias defect,
  numerical conversion, missing-symbol batch warning, and partial coverage.
- One direct live SDK `search(query="^AAPL$", provider="nasdaq")` on 2026-10-22
  returned Apple Inc. / directory code Q. This verifies only that one directory
  lookup, not the full adapter or historical import path.
- That search emitted a warning: core tried opening
  `/usr/local/share/ca-certificates/nebula-dns_combined.crt` with `wb`; the filesystem
  rejected it as read-only. Source inspection confirmed the exception handler
  returned the existing configured CA file and requests retained certificate
  verification. No workaround or trust change was made. Further local live calls
  are intentionally not run in this environment.
- Actual live AAPL history for inclusive 2024-01-02..2024-01-05: **failed**.
  OpenBB raised `EmptyDataError`; one provider-helper diagnostic showed
  `ClientConnectorDNSError` resolving `api.nasdaq.com`. No alternate source, insecure
  transport, proxy change or network-permission bypass was attempted.
- Therefore published capabilities remain **documented**, not globally verified.
  Cboe/TMX, actions, PIT membership and non-equity coverage are not enabled here.

Optional hosted no-key probe (must have normal permitted public internet access):

```bash
python -m script.openbb_live_probe --output /tmp/openbb-live-evidence.json
# Or: TRADEGRAD_OPENBB_LIVE=1 python -m pytest tests/marketdata/test_openbb.py -k optional_live
```

The JSON report preserves scoped parameters, package versions, raw results, warnings,
coverage and failure status. A passing probe alone still does not replace the
application-level resolve → import → immutable snapshot → chart → TradeGrad gate.

Fixture mode keeps small hand-authored Jan2–5 SDK-schema samples separate from a
generated synthetic AAPL/MSFT fixture over the fixed window
`[2024-01-02,2024-07-01)`. The longer fixture has more than 84 actual XNYS exchange
sessions for the real-seed evaluation path, but its prices are deterministic
synthetic waves, not market observations. Raw fixtures explicitly say
`synthetic_fixture_not_live`. Outside that fixed window coverage is partial rather
than inventing market history.

## Local validation

The local OpenBB offline gate passed 25 tests, with one opt-in live probe skipped.
This includes response-completion acquisition timestamps, installed SDK
parser/namespace contracts and an actual subprocess boundary check. Shared process
tests separately cover environment secret exclusion, output-size enforcement and
descendant cleanup. Ruff and `git diff --check` passed. These focused results do
not stand in for the combined market-data suite, hosted service/Playwright or live
historical-data gates.
