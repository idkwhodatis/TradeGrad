"""Offline authoring contract checks, not PostgreSQL or runtime isolation evidence."""

import hashlib
import secrets
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.models import StrategyVersion
from backend.app.services import DomainError
from backend.app.strategy_schemas import MAX_STRATEGY_BYTES, StrategyCreate
from backend.app.strategy_services import (
    same_submission,
    submission_id,
    validate_syntax,
)


def body(**changes):
    return StrategyCreate(
        **{
            "name": "Native version",
            "task": "cn_time",
            "code": "value = 1\n",
            **changes,
        }
    )


def test_source_preserves_exact_utf8_and_line_endings():
    code = '# 中文 source\r\nvalue = "é"\r\n\r\n'
    source = body(name=" Keep name whitespace ", code=code)
    assert source.code.encode("utf-8") == code.encode("utf-8")
    assert source.name == " Keep name whitespace "
    validate_syntax(source.code)
    with pytest.raises(ValidationError):
        source.code = "changed = True\n"


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"name": " \t"},
        {"name": "x" * 256},
        {"name": "line\nbreak"},
        {"name": "\ud800"},
        {"task": "mixed"},
        {"code": ""},
        {"code": " \n"},
        {"code": "\x00"},
        {"code": "#" + "x" * MAX_STRATEGY_BYTES},
        {"code": "#" + "中" * (MAX_STRATEGY_BYTES // 3 + 1)},
        {"code": "#\ud800"},
        {"mode": "portable"},
        {"owner_id": str(uuid4())},
        {"parent_id": "not-a-uuid"},
        {"content_sha256": "f" * 64},
    ],
)
def test_contract_rejects_unbounded_invalid_and_unowned_input_fields(changes):
    with pytest.raises(ValidationError):
        body(**changes)


def test_source_byte_boundary_is_not_character_count():
    assert (
        len(body(code="#" + "x" * (MAX_STRATEGY_BYTES - 1)).code) == MAX_STRATEGY_BYTES
    )
    with pytest.raises(ValidationError):
        body(code="#" + "😀" * (MAX_STRATEGY_BYTES // 4))


def test_utf8_bom_is_parsed_without_changing_saved_source():
    code = "\ufeff# UTF-8 BOM\r\nvalue = 1\r\n"
    source = body(code=code)
    validate_syntax(source.code)
    assert source.code == code


def test_parser_never_imports_or_executes_source(tmp_path):
    marker = tmp_path / "must-not-be-created"
    source = f"import a_module_that_does_not_exist\nopen({str(marker)!r}, 'w').write('executed')\nraise RuntimeError('must not execute')\n"
    validate_syntax(body(code=source).code)
    assert not marker.exists()


@pytest.mark.parametrize(
    "source", ["def invalid(\n", "if True\n pass\n", "x = " + "[" * 1000 + "]" * 1000]
)
def test_syntax_failures_are_bounded_user_errors(source):
    with pytest.raises(DomainError) as failure:
        validate_syntax(source)
    assert failure.value.code == "strategy_syntax_error"
    assert failure.value.status_code == 422
    assert "code" not in (failure.value.details or {})


@pytest.mark.parametrize("key", ["", "x" * 129, "with space", "\n", "é", "abc\x7f"])
def test_submission_key_rejects_invalid_ascii(key):
    with pytest.raises(DomainError) as failure:
        submission_id(uuid4(), key)
    assert failure.value.status_code == 422


def test_idempotency_namespace_separates_owners_and_exact_keys():
    owner, other = uuid4(), uuid4()
    assert submission_id(owner, "same-key") == submission_id(owner, "same-key")
    assert submission_id(owner, "same-key") != submission_id(other, "same-key")
    assert submission_id(owner, "same-key") != submission_id(owner, "Same-key")


def test_replay_requires_all_exact_fields_not_only_code_hash():
    source, owner = body(), uuid4()
    item = StrategyVersion(
        owner_id=owner,
        mode="native_akquant",
        **source.model_dump(),
        content_sha256=hashlib.sha256(source.code.encode()).hexdigest(),
    )
    assert same_submission(item, owner, body())
    assert not same_submission(item, uuid4(), source)
    for changes in [
        {"name": "other"},
        {"task": "us_time"},
        {"code": "value = 1\r\n"},
        {"parent_id": uuid4()},
    ]:
        assert not same_submission(item, owner, body(**changes))
    item.content_sha256 = "0" * 64
    assert not same_submission(item, owner, source)


def test_create_contract_requires_auth_key_and_has_no_mutation_routes():
    settings = Settings(
        database_url="postgresql://localhost/not_used",
        secret_key=secrets.token_urlsafe(48),
    )
    app = create_app(settings)
    schema = app.openapi()
    path = schema["paths"]["/api/v1/strategy-versions"]
    post = path["post"]
    assert post["operationId"] == "create_strategy_version"
    assert post["security"]
    key = next(p for p in post["parameters"] if p["name"] == "Idempotency-Key")
    assert key["required"] and key["schema"]["maxLength"] == 128
    assert {"200", "201"}.issubset(post["responses"])
    contract = schema["components"]["schemas"]["StrategyCreate"]
    assert contract["additionalProperties"] is False
    assert contract["properties"]["code"]["maxLength"] == MAX_STRATEGY_BYTES
    assert set(schema["paths"]["/api/v1/strategy-versions/{strategy_id}"]) == {"get"}
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/strategy-versions",
            json=body().model_dump(mode="json"),
            headers={"Idempotency-Key": "test"},
        )
        assert response.status_code == 401
