"""Offline unit checks only. These never stand in for PostgreSQL/Docker gates."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.core.security import (
    create_access_token,
    decode_access_token,
    get_password_hash,
    verify_password,
)
from backend.app.models import DatasetVersion, ResearchRun, StrategyVersion
from backend.app.schemas import RunCreate
from backend.app.services import DomainError
from backend.app.storage import read_verified, redact, safe_path
from workers.research.executor import publish_artifacts, stage_input


def config(tmp_path):
    return Settings(
        database_url="postgresql://localhost/tests",
        secret_key=secrets.token_urlsafe(48),
        storage_root=tmp_path,
    )


def test_configuration_has_no_database_or_signing_key_defaults():
    assert Settings.model_fields["database_url"].is_required()
    assert Settings.model_fields["secret_key"].is_required()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://",
        "sqlite:///test.db",
        "mysql://localhost/test",
        "postgresql+psycopg2://localhost/test",
    ],
)
def test_configuration_rejects_non_postgres_psycopg(url):
    with pytest.raises(ValidationError):
        Settings(database_url=url, secret_key=secrets.token_urlsafe(48))


def test_password_hash_and_expiring_signed_token(tmp_path):
    settings = config(tmp_path)
    password = secrets.token_urlsafe(32)
    wrong_password = secrets.token_urlsafe(32)
    digest = get_password_hash(password)
    if not verify_password(password, digest):
        pytest.fail("Generated password did not verify", pytrace=False)
    if verify_password(wrong_password, digest):
        pytest.fail("Unrelated generated password authenticated", pytrace=False)
    if verify_password(password, secrets.token_urlsafe(48)):
        pytest.fail("Malformed password hash authenticated", pytrace=False)
    user_id = uuid4()
    if decode_access_token(create_access_token(user_id, settings), settings) != user_id:
        pytest.fail(
            "Generated signed token decoded to the wrong subject", pytrace=False
        )
    expired = jwt.encode(
        {
            "sub": str(user_id),
            "iat": datetime.now(UTC) - timedelta(minutes=2),
            "exp": datetime.now(UTC) - timedelta(minutes=1),
            "aud": "tradegrad-api",
            "iss": "tradegrad",
        },
        settings.secret_key,
        algorithm="HS256",
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(expired, settings)


def test_run_config_rejects_paths_and_inclusive_or_empty_windows():
    base = {
        "task": "cn_time",
        "dataset_version_id": str(uuid4()),
        "strategy_version_id": str(uuid4()),
        "start_date": "2024-01-02",
        "end_date": "2024-04-27",
    }
    assert RunCreate(**base).profile == "corrected_explicit_config"
    for extra in (
        {"data_path": "/etc/passwd"},
        {"end_date": "2024-01-02"},
        {"profile": "source_compat"},
    ):
        with pytest.raises(ValidationError):
            RunCreate(**{**base, **extra})


def test_storage_blocks_symlink_traversal_and_checksum_mismatch(tmp_path):
    content = b"private result"
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / "good.txt").write_bytes(content)
    assert read_verified(tmp_path, "good.txt", digest, 100) == content
    for key in ("../secret", "/etc/passwd"):
        with pytest.raises(DomainError):
            safe_path(tmp_path, key)
    (tmp_path / "link").symlink_to(tmp_path / "good.txt")
    with pytest.raises(DomainError):
        read_verified(tmp_path, "link", digest, 100)
    with pytest.raises(DomainError):
        read_verified(tmp_path, "good.txt", "0" * 64, 100)
    with pytest.raises(DomainError):
        read_verified(tmp_path, "good.txt", digest, 1)


def test_published_artifacts_redact_logs_and_include_trust_provenance(tmp_path):
    api_key, bearer_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    outcome = {
        "metrics": {"sharpe_ratio": 1.2},
        "logs": {"stderr": f"warning only\napi_key={api_key}\nBearer {bearer_token}"},
        "image_digest": "sha256:abc",
    }
    records = publish_artifacts(tmp_path, tmp_path / "artifacts", outcome, 1000)
    by_name = {item["name"]: item for item in records}
    assert set(by_name) == {"metrics.json", "logs.txt", "provenance.json"}
    for item in records:
        read_verified(tmp_path, item["storage_key"], item["sha256"], 1000)
    logs = (tmp_path / "artifacts/logs.txt").read_text()
    if api_key in logs or bearer_token in logs:
        pytest.fail(
            "Artifact log redaction exposed generated test material", pytrace=False
        )
    provenance = json.loads((tmp_path / "artifacts/provenance.json").read_text())
    assert provenance["result_trust"] == "native_strategy_and_engine_share_process"
    assert provenance["image_digest"] == "sha256:abc"
    with pytest.raises(FileExistsError):
        publish_artifacts(tmp_path, tmp_path / "artifacts", outcome, 1000)


def test_data_staging_verifies_hashes_filters_exclusive_dates_and_preserves_code(
    tmp_path,
):
    settings = config(tmp_path)
    data = b"date,close\n2024-01-01,1\n2024-01-02,2\n2024-01-03,3\n2024-01-04,4\n"
    source = tmp_path / "dataset/data"
    source.mkdir(parents=True)
    (source / "INDEX.csv").write_bytes(data)
    manifest = {"files": {"data/INDEX.csv": hashlib.sha256(data).hexdigest()}}
    dataset_hash = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    code = "# Original code bytes preserved; never executed in the orchestrator.\n"
    strategy_hash = hashlib.sha256(code.encode()).hexdigest()
    run = ResearchRun(
        id=uuid4(),
        config={
            "dataset_sha256": dataset_hash,
            "strategy_sha256": strategy_hash,
            "start_date": "2024-01-02",
            "end_date": "2024-01-04",
            "market": "CN",
            "task": "cn_time",
            "profile": "corrected_explicit_config",
        },
    )
    dataset = DatasetVersion(
        storage_key="dataset", manifest=manifest, content_sha256=dataset_hash
    )
    strategy = StrategyVersion(code=code)
    attempt_id = uuid4()
    input_dir, _, _ = stage_input(run, dataset, strategy, attempt_id, settings)
    staged = (input_dir / "data/INDEX.csv").read_text()
    assert "2024-01-02" in staged and "2024-01-03" in staged
    assert "2024-01-01" not in staged and "2024-01-04" not in staged
    assert (input_dir / "strategy.py").read_bytes() == code.encode()
    assert not (input_dir.stat().st_mode & 0o222)
    with pytest.raises(FileExistsError):
        stage_input(run, dataset, strategy, attempt_id, settings)
    dataset.manifest = {"files": {}}
    with pytest.raises(ValueError, match="manifest changed"):
        stage_input(run, dataset, strategy, uuid4(), settings)


def test_error_redaction_includes_database_credentials():
    password, api_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    result = redact(f"postgresql+psycopg://user:{password}@host/db api_key={api_key}")
    if password in result or api_key in result:
        pytest.fail("Error redaction exposed generated test material", pytrace=False)


def test_openapi_contract_is_current():
    from backend.app.main import create_app
    from backend.app.schemas import EventPublic

    expected = json.loads(
        (
            Path(__file__).resolve().parents[2] / "packages/contracts/openapi.json"
        ).read_text()
    )
    for _ in range(2):
        settings = Settings(
            database_url="postgresql://localhost/contract",
            secret_key=secrets.token_urlsafe(48),
        )
        actual = create_app(settings).openapi()
        actual["components"]["schemas"]["EventPublic"] = EventPublic.model_json_schema()
        if settings.secret_key in json.dumps(actual):
            pytest.fail("Ephemeral export settings leaked into OpenAPI", pytrace=False)
        if actual != expected:
            pytest.fail(
                "Ephemeral export settings changed the OpenAPI contract", pytrace=False
            )
        assert actual["paths"]["/api/v1/runs"]["post"]["operationId"] == "create_run"
