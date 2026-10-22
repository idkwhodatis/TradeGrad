"""Optional no-key public-data probe. Writes scoped evidence, never a blanket badge.

Run from the repository root with the locked data-runtime Python. The report is
an observation, not an input to the production symbol registry or fixture mode.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from packages.marketdata.contracts import BarsRequest
from packages.marketdata.providers.openbb import OpenBBProvider, _versions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    provider = OpenBBProvider(max_attempts=1)
    resolved = provider.resolve_instruments(["AAPL"])
    report = {"checked_at": datetime.now(UTC).isoformat(), "package_versions": _versions(),
              "symbols": ["AAPL"], "start": "2024-01-02", "end": "2024-01-06",
              "boundary": "[start,end)", "evidence": "live", "status": "failed",
              "resolution": resolved.model_dump(mode="json"), "bars": None}
    if not resolved.unresolved and len(resolved.data) == 1:
        result = provider.fetch_bars(BarsRequest(listings=resolved.data,
                                                start="2024-01-02", end="2024-01-06"))
        report["bars"] = result.model_dump(mode="json")
        if result.data and len(result.coverage) == 1 and all(c.status == "complete" for c in result.coverage):
            report["status"] = "verified_within_scope"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "symbols", "start", "end", "package_versions")}))
    return 0 if report["status"] == "verified_within_scope" else 1


if __name__ == "__main__":
    raise SystemExit(main())
