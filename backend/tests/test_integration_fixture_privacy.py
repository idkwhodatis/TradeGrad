"""Pure regression for generated test account diagnostics, without database I/O."""

import secrets
from types import SimpleNamespace

import pytest

from tests.integration.conftest import SyntheticAccount


@pytest.mark.parametrize("renderer", [repr, str], ids=["repr", "str"])
def test_generated_account_diagnostics_are_opaque(renderer):
    password = secrets.token_urlsafe(32)
    nested = SimpleNamespace(private=secrets.token_urlsafe(32))
    account = SyntheticAccount(nested, password, nested, nested)
    rendered = renderer(account)
    if password in rendered or nested.private in rendered:
        pytest.fail("Generated fixture credentials escaped diagnostics", pytrace=False)
    assert rendered == "SyntheticAccount(<private fixture>)"
    if account.password != password or account.user is not nested:
        pytest.fail("Opaque diagnostics altered fixture values", pytrace=False)
