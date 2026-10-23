"""Explicitly provision durable storage below an operator-chosen boundary."""

import argparse
from pathlib import Path

from backend.app.core.atomic_storage import provision_directory


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--durable-parent",
        type=Path,
        required=True,
        help="Existing durable directory on the persistent filesystem, containing root",
    )
    args = parser.parse_args(argv)
    provision_directory(args.root, durable_parent=args.durable_parent)


if __name__ == "__main__":
    main()
