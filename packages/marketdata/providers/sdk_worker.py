"""Private SDK boundary. Not an API, and accepts no credentials or arbitrary code."""

import json
import sys
from pathlib import Path

from .common import classify_error


def frame_json(frame):
    from ..normalize import frame_encoding

    return frame_encoding(frame)


def execute(operation, params):
    if operation == "akshare_bars":
        import akshare as ak

        return frame_json(ak.stock_zh_a_hist(**params))
    if operation == "akshare_resolve":
        import akshare as ak

        return frame_json(ak.stock_info_a_code_name())
    if operation == "yfinance_bars":
        import yfinance as yf

        symbol = params.pop("symbol")
        ticker = yf.Ticker(symbol)
        frame = ticker.history(**params)
        metadata = ticker.get_history_metadata()
        allowed = ["symbol", "currency", "exchangeName", "exchangeTimezoneName", "instrumentType", "priceHint"]
        frame.attrs["history_metadata"] = {key: metadata[key] for key in allowed if key in metadata}
        return frame_json(frame)
    if operation == "yfinance_resolve":
        import yfinance as yf

        symbol, timeout = params["symbol"], params["timeout"]
        result = yf.Search(
            symbol, max_results=10, news_count=0, timeout=timeout, raise_errors=True
        )
        quotes = [q for q in result.quotes if q.get("symbol", "").upper() == symbol]
        if not quotes:
            return {"quotes": result.quotes, "metadata": {}}
        ticker = yf.Ticker(symbol)
        ticker.history(
            period="5d",
            auto_adjust=False,
            back_adjust=False,
            repair=False,
            actions=True,
            timeout=timeout,
            raise_errors=True,
        )
        metadata = ticker.get_history_metadata()
        allowed = [
            "symbol",
            "currency",
            "exchangeName",
            "fullExchangeName",
            "exchangeTimezoneName",
            "instrumentType",
            "firstTradeDate",
        ]
        return {
            "quotes": quotes,
            "metadata": {key: metadata[key] for key in allowed if key in metadata},
        }
    raise ValueError("Unsupported SDK operation")


def main():
    destination = Path(sys.argv[1])
    try:
        request = json.load(sys.stdin)
        result = {"result": execute(request["operation"], request["params"])}
    except Exception as exc:  # noqa: BLE001 - redact SDK failures at the trust boundary
        error = classify_error(exc)
        result = {"error": error.code, "retryable": error.retryable}
    destination.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
