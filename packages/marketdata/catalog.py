"""API-safe documented catalog: installation/fixture tests do not verify a source."""

from .contracts import Capability

LOCKED_VERSIONS = {
    "akshare": "1.19.1",
    "yfinance": "1.7.0",
    "openbb-core": "2.0.1",
    "openbb-nasdaq": "2.0.0",
}


def documented_capabilities(connector: str) -> list[Capability]:
    if connector == "portable_fixture":
        from .portable_fixture import PortableFixtureProvider

        return PortableFixtureProvider(mode="fixture").capabilities()
    definitions = {
        "akshare": (
            "eastmoney",
            "stock_zh_a_hist",
            "https://akshare.akfamily.xyz/data/stock/stock.html",
            ["akshare"],
        ),
        "yfinance": (
            "yahoo",
            "Ticker.history",
            "https://ranaroussi.github.io/yfinance/advanced/price_adjustment.html",
            ["yfinance"],
        ),
        "openbb": (
            "nasdaq",
            "obb.nasdaq.equity.historical",
            "https://docs.openbb.co/odp/python/reference/nasdaq/equity/historical",
            ["openbb-core", "openbb-nasdaq"],
        ),
    }
    provider, endpoint, url, packages = definitions[connector]
    common = {
        "connector": connector,
        "provider": provider,
        "endpoint": endpoint,
        "package_versions": {p: LOCKED_VERSIONS[p] for p in packages},
        "source_url": url,
    }
    return [
        Capability(
            feature="daily_bars",
            status="documented",
            evidence="documentation",
            **common,
            note="Pinned SDK contract; no live symbol/date verification implied",
        ),
        Capability(
            feature="corporate_actions",
            status="documented" if connector == "yfinance" else "unsupported",
            evidence="documentation",
            **common,
            note="Yahoo events are preserved separately"
            if connector == "yfinance"
            else "No independently verified action feed in this adapter",
        ),
        Capability(
            feature="point_in_time_universe",
            status="unsupported",
            evidence="none",
            **common,
        ),
    ]


def provider_catalog() -> list[dict]:
    return [
        {
            "id": name,
            "name": "Portable synthetic fixture (not market data)"
            if name == "portable_fixture"
            else name,
            "provider": upstream,
            "markets": markets,
            "intervals": ["1d"],
            "price_modes": modes,
            "capabilities": [
                c.model_dump(mode="json") for c in documented_capabilities(name)
            ],
        }
        for name, upstream, markets, modes in [
            ("akshare", "eastmoney", ["CN"], ["raw", "qfq", "hfq"]),
            ("yfinance", "yahoo", ["US"], ["provider_native"]),
            ("openbb", "nasdaq", ["US"], ["provider_native"]),
            ("portable_fixture", "synthetic", ["CN", "US"], ["raw"]),
        ]
    ]
