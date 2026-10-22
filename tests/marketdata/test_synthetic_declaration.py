"""Explicit uploaded synthetic data must remain labelled through publication."""

from packages.marketdata.contracts import PriceSemantics, ProviderResult
from workers.data.executor import is_synthetic_result


def test_declared_synthetic_upload_remains_synthetic_without_sdk_fixture_evidence():
    result = ProviderResult(
        price_semantics=PriceSemantics(native_mode="user_declared_synthetic")
    )
    assert is_synthetic_result(result)


def test_ordinary_unknown_upload_does_not_invent_synthetic_evidence():
    assert not is_synthetic_result(ProviderResult())


def test_declaration_is_exact_not_a_name_substring_heuristic():
    assert not is_synthetic_result(
        ProviderResult(price_semantics=PriceSemantics(native_mode="not_synthetic"))
    )
