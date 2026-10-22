"""Offline M3 contract checks. No database mock or SQLite acceptance substitution."""

import hashlib
import secrets
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.models import ResearchRun
from backend.app.research_services import _validated_window, bounded_json
from backend.app.services import DomainError
from packages.tradegrad_adapter.config import MarketConfig, RunConfig


def sample_config():
    code = "# offline fixture, not executed\n"
    return RunConfig.model_validate(
        {
            "execution_mode": "optimization",
            "task": "cn_time",
            "dataset": {"id": str(uuid4()), "sha256": "a" * 64},
            "seed_strategy": {
                "id": str(uuid4()),
                "sha256": hashlib.sha256(code.encode()).hexdigest(),
            },
            "universe": {"id": str(uuid4()), "sha256": "b" * 64},
            "windows": {
                "train": {"start": "2024-02-01", "end": "2024-02-10"},
                "validation": {"start": "2024-02-15", "end": "2024-02-20"},
                "holdout": {"start": "2024-03-01", "end": "2024-03-10"},
            },
            "warmup": {"sessions": 20},
            "market_profile": MarketConfig.for_market("CN").model_dump(mode="json"),
            "environment_digest": "c" * 64,
        }
    )


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        2**64,
        {"x": float("-inf")},
        {1: "wrong key"},
        {"v": object()},
        [0] * 100001,
        {"deep": [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]},
    ],
)
def test_provenance_rejects_nonfinite_unbounded_and_nonjson_values(value):
    with pytest.raises(DomainError):
        bounded_json(value)


def test_json_payload_size_limit_and_deterministic_hash_bytes():
    assert bounded_json({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    with pytest.raises(DomainError):
        bounded_json({"value": "x" * 100}, 10)


def test_research_config_is_frozen_and_no_unapproved_fields():
    config = sample_config()
    with pytest.raises(ValidationError):
        config.task = "us_time"
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {**config.model_dump(mode="json"), "data_path": "/tmp/other"}
        )


@pytest.mark.parametrize(
    "window",
    [
        {"split": "holdout", "start": "2024-03-01", "end": "2024-03-10"},
        {"split": "train", "start": "2024-02-01", "end": "2024-02-09"},
        {
            "split": "train",
            "start": "2024-02-01",
            "end": "2024-02-10",
            "data_path": "/tmp",
        },
    ],
)
def test_evaluation_registration_rejects_holdout_and_arbitrary_subwindows(window):
    run = ResearchRun(config=sample_config().model_dump(mode="json"))
    with pytest.raises(DomainError):
        _validated_window(run, window)


def test_valid_evaluation_window_is_exact_frozen_plan():
    config = sample_config()
    run = ResearchRun(config=config.model_dump(mode="json"))
    window = {"split": "train", **config.windows.train.model_dump(mode="json")}
    assert _validated_window(run, window) == window
    assert run.execution_mode == "optimization"


def test_new_api_requires_auth_and_documents_bounded_owner_scoped_queries():
    from fastapi.testclient import TestClient

    settings = Settings(
        database_url="postgresql://localhost/not_used",
        secret_key=secrets.token_urlsafe(48),
    )
    app = create_app(settings)
    schema = app.openapi()
    for path in ["/api/v1/research-runs", "/api/v1/research-capabilities"]:
        with TestClient(app) as client:
            assert client.get(path).status_code == 401
    assert "/api/v1/runs" in schema["paths"]
    assert "/api/v1/research-runs/{run_id}/pause" in schema["paths"]
    assert "/api/v1/research-runs/{run_id}/resume" in schema["paths"]
    assert all("holdout" not in key for key in schema["paths"])
    assert schema["paths"]["/api/v1/research-runs"]["post"]["responses"]["202"]
    for resource in ("candidates", "evaluations", "model-calls", "checkpoints"):
        operation = schema["paths"][f"/api/v1/research-runs/{{run_id}}/{resource}"][
            "get"
        ]
        assert (
            next(
                p["schema"]["maximum"]
                for p in operation["parameters"]
                if p["name"] == "limit"
            )
            == 100
        )


def test_m3_migration_has_explicit_pointers_and_provenance_guards():
    root = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    migration = (root / "003_m3.py").read_text()
    ddl = (root / "003_m3.sql").read_text()
    for field in (
        "latest_checkpoint_id",
        "resume_checkpoint_id",
        "resumed_checkpoint_id",
    ):
        assert field in migration
    for table in (
        "research_candidates",
        "research_evaluations",
        "research_model_calls",
        "research_checkpoints",
    ):
        assert f"CREATE TABLE {table}" in ddl
    assert "protect_research_inputs" in migration
    assert "protect_evaluation_payload" in migration
    assert "sqlite" not in migration.lower()
