"""One bounded SDK call. Internal data-runtime subprocess, not a web server."""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path


def main() -> int:
    destination = Path(sys.argv[1])
    try:
        request = json.load(sys.stdin)
        if request.get("command") not in {"search", "historical"}:
            raise ValueError("Unsupported SDK command")
        # Never mutate credentials/preferences. Client generation belongs to
        # image build. SDK diagnostic output must not corrupt the JSON boundary.
        with contextlib.redirect_stdout(sys.stderr):
            from openbb import obb
            from openbb_core.provider.utils.helpers import get_python_request_settings
            if get_python_request_settings().get("verify_ssl") is False:
                raise ValueError("OpenBB TLS verification must remain enabled")
            output = getattr(obb.nasdaq.equity, request["command"])(**request["params"])
        result = output.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 - SDK process must return structured failures
        # EmptyDataError may mask an upstream failure; never interpret it as a
        # complete empty dataset, and don't guess that retrying would help.
        kind = type(exc).__name__
        retryable = kind in {"TimeoutError", "ConnectionError", "ConnectTimeout", "ReadTimeout",
                             "ClientConnectorError", "ClientConnectorDNSError"}
        status = getattr(exc, "status", None) or getattr(getattr(exc, "response", None), "status_code", None)
        retryable = retryable or status == 429 or (isinstance(status, int) and 500 <= status < 600)
        result = {"error": {"code": kind, "message": f"OpenBB SDK request failed ({kind})", "retryable": retryable}}
    destination.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
