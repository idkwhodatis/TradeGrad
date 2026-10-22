"""Create job-local disposable credentials, never repository or account secrets.

Only the hosted CI job invokes main. Values live in that job's environment file,
are masked before use, and disappear with the runner and its disposable database.
"""

import os
import secrets
from pathlib import Path

SECRET_FIELDS = (
    "POSTGRES_PASSWORD",
    "SECRET_KEY",
    "E2E_PASSWORD",
    "E2E_OTHER_PASSWORD",
)


def create_environment() -> dict[str, str]:
    values = {name: secrets.token_hex(32) for name in SECRET_FIELDS}
    url = (
        "postgresql+psycopg://tradegrad_ci:"
        + values["POSTGRES_PASSWORD"]
        + "@127.0.0.1:5432/tradegrad_ci"
    )
    values.update(DATABASE_URL=url, TEST_DATABASE_URL=url)
    return values


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("GITHUB_ENV"):
        raise SystemExit("This helper is only for an ephemeral GitHub Actions job")
    values = create_environment()
    # Register masks before making these values available to any later step.
    # https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands
    for value in dict.fromkeys(values.values()):
        print(f"::add-mask::{value}", flush=True)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as destination:
        for name, value in values.items():
            destination.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
