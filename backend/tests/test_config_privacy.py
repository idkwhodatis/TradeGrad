"""Configuration diagnostics must not expose generated credential material."""

import secrets

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from backend.app.core.config import Settings


def assert_private(rendered, *credentials):
    # Do not include generated credentials in pytest assertion introspection.
    if any(value in rendered for value in credentials) or any(
        marker in rendered for marker in ("input_value=", "input_type=")
    ):
        pytest.fail(
            "Configuration diagnostics exposed generated credentials or input values",
            pytrace=False,
        )


def test_settings_repr_hides_credentials_without_changing_runtime_values():
    password, secret = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    settings = Settings(
        database_url=f"postgresql://test_user:{password}@localhost/not_used",
        secret_key=secret,
        access_token_minutes=30,
    )
    for rendered in (repr(settings), str(settings)):
        assert_private(rendered, password, secret)
        assert "database_url=" not in rendered
        assert "secret_key=" not in rendered
        assert "access_token_minutes=30" in rendered
    if (
        make_url(settings.database_url).password != password
        or make_url(settings.psycopg_url).password != password
        or settings.secret_key != secret
    ):
        pytest.fail("Diagnostic privacy changed runtime credentials", pytrace=False)


@pytest.mark.parametrize(
    "invalid_field",
    ["database_url", "secret_key", "access_token_minutes"],
)
def test_settings_validation_diagnostics_hide_invalid_inputs(invalid_field):
    password, secret = secrets.token_urlsafe(32), secrets.token_urlsafe(12)
    values = {
        "database_url": f"postgresql://test_user:{password}@localhost/not_used",
        "secret_key": secrets.token_urlsafe(48),
    }
    if invalid_field == "database_url":
        values[invalid_field] = f"mysql://test_user:{password}@localhost/not_used"
    else:
        values[invalid_field] = secret
    with pytest.raises(ValidationError) as error:
        Settings(**values)
    for rendered in (str(error.value), repr(error.value)):
        assert_private(rendered, password, secret, values["secret_key"])
        assert invalid_field in rendered


@pytest.mark.parametrize("missing_field", ["database_url", "secret_key"])
def test_settings_missing_field_diagnostics_hide_other_credentials(
    monkeypatch, missing_field
):
    monkeypatch.delenv(missing_field.upper(), raising=False)
    password, secret = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    values = {
        "database_url": f"postgresql://test_user:{password}@localhost/not_used",
        "secret_key": secret,
    }
    del values[missing_field]
    with pytest.raises(ValidationError) as error:
        Settings(**values)
    for rendered in (str(error.value), repr(error.value)):
        assert_private(rendered, password, secret)
        assert missing_field in rendered and "Field required" in rendered
