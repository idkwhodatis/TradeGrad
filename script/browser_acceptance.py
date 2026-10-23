"""Bounded fixture Chromium suite for either a real Compose or process stack.

Requires explicit already-running loopback frontend/API origins, generated test
accounts and optional Settings admin credentials. No DB/model settings reach
Node. The existing Vite server must be started/owned by the ordinary stack when
not using Compose's frontend; this launcher never silently starts a second app.
"""

import os
import shutil
from pathlib import Path

from script.compose_acceptance import run_browser


def main():
    if not (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("TRADEGRAD_REQUIRE_BROWSER") == "1"
    ):
        raise SystemExit("Explicit disposable hosted browser acceptance is required")
    try:
        node = shutil.which("node")
        if not node:
            raise ValueError("Matched Node runtime unavailable")
        run_browser(node, Path(__file__).resolve().parents[1], os.environ)
        print("Hosted Chromium suite and owned-descendant cleanup passed")
    except Exception:  # noqa: BLE001 - no private child output or credentials
        raise SystemExit(
            "Hosted browser acceptance failed or containment is unconfirmed"
        ) from None


if __name__ == "__main__":
    main()
