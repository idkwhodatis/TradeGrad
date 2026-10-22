"""Export deterministic contracts without a database connection or runtime secrets."""

import json
import secrets
from pathlib import Path

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.schemas import EventPublic


def export():
    settings = Settings(
        database_url="postgresql+psycopg://127.0.0.1/contract",
        secret_key=secrets.token_urlsafe(48),
    )
    schema = create_app(settings).openapi()
    schema["components"]["schemas"]["EventPublic"] = EventPublic.model_json_schema()
    target = (
        Path(__file__).resolve().parents[2] / "packages" / "contracts" / "openapi.json"
    )
    target.resolve().parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return target.resolve()


if __name__ == "__main__":
    print(export())
