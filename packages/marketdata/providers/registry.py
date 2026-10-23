"""Runtime-only lazy provider construction."""


def get_provider(name: str, *, mode: str = "live"):
    if name == "portable_fixture":
        from ..portable_fixture import PortableFixtureProvider

        return PortableFixtureProvider(mode=mode)
    if name == "akshare":
        from .akshare import AKShareProvider

        return AKShareProvider(mode=mode)
    if name == "yfinance":
        from .yfinance import YFinanceProvider

        return YFinanceProvider(mode=mode)
    if name == "openbb":
        from .openbb import OpenBBProvider

        return OpenBBProvider(mode=mode)
    raise ValueError("Unsupported provider")
