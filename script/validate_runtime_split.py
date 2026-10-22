"""Check frozen installed distributions without contacting providers or databases."""

from __future__ import annotations

import argparse
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_PACKAGES = ("numpy", "openai")
DATA_PACKAGES = (
    "akshare",
    "yfinance",
    "openbb-core",
    "openbb-nasdaq",
    "pandas",
    "pyarrow",
    "exchange-calendars",
)


def locked_versions(lock: Path) -> dict[str, str]:
    result = {}
    for line in lock.read_text().splitlines():
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^\s;]+)(?:\s+\\)?", line
        )
        if match:
            result[match[1]] = match[2]
    if not result:
        raise ValueError("The selected runtime lock contains no exact versions")
    return result


def validate(runtime: str, installed=version) -> int:
    locks = {
        "api": "backend/requirements.lock",
        "data": "runtimes/data/requirements.lock",
        "research": "runtimes/research/requirements.lock",
    }
    if runtime not in locks:
        raise ValueError("Unknown runtime")
    lock = ROOT / locks[runtime]
    expected = locked_versions(lock)
    for name, wanted in expected.items():
        try:
            actual = installed(name)
        except PackageNotFoundError:
            raise ValueError(f"Locked runtime package is missing: {name}") from None
        if actual != wanted:
            raise ValueError(f"Installed runtime version differs from lock: {name}")
    if runtime in {"api", "research"}:
        forbidden = DATA_PACKAGES + (
            ("akquant",) if runtime == "research" else RESEARCH_PACKAGES
        )
        for name in forbidden:
            try:
                installed(name)
            except PackageNotFoundError:
                continue
            raise ValueError(
                f"Dependency must not be installed in the {runtime} runtime: {name}"
            )
    if runtime in {"data", "research"}:
        for name in DATA_PACKAGES if runtime == "data" else RESEARCH_PACKAGES:
            if name not in expected:
                raise ValueError(f"Data dependency must be explicitly locked: {name}")
    return len(expected)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", choices=("api", "data", "research"), required=True)
    args = parser.parse_args()
    count = validate(args.runtime)
    print(
        f"{args.runtime}: {count} exact package versions verified; runtime separation passed"
    )


if __name__ == "__main__":
    main()
