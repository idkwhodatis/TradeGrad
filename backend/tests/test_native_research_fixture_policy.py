"""SDK-free control-plane checks for the separately versioned synthetic profile."""

import subprocess
import sys

import pytest

from backend.app.data_services import research_fixture_profile
from backend.app.services import DomainError
from packages.marketdata.native_research_fixture import PROFILE, research_listing
from workers.data.executor import validate_research_fixture_policy


def request(**changes):
    return {
        "provider": "portable_fixture",
        "market": "US",
        "symbols": ["A", "B", "C", "D"],
        "start_date": "2024-01-02",
        "end_date": "2024-03-01",
        "price_mode": "raw",
        "_execution": {"fixture_profile": PROFILE},
        **changes,
    }


@pytest.mark.parametrize("symbols", [["A"], ["B", "D"], ["A", "B", "C", "D"]])
def test_profile_symbol_and_owned_listing_requests_pin_same_distinct_identity(symbols):
    assert research_fixture_profile(request(symbols=symbols)) == PROFILE
    assert (
        research_fixture_profile(
            request(symbols=[]), listings=[research_listing(value) for value in symbols]
        )
        == PROFILE
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"symbols": ["A", "AAPL"]},
        {"market": "CN"},
        {"start_date": "2024-01-01"},
        {"end_date": "2024-03-02"},
        {"end_date": "2024-01-02"},
    ],
)
def test_profile_rejects_mixed_market_and_unfrozen_windows(changes):
    with pytest.raises(DomainError, match="Synthetic research fixture"):
        research_fixture_profile(request(**changes))


def test_legacy_fixture_and_real_provider_requests_do_not_acquire_new_profile():
    assert research_fixture_profile(request(symbols=["AAPL", "MSFT"])) is None
    assert research_fixture_profile(request(provider="yfinance", symbols=["A"])) is None


@pytest.mark.parametrize("kind", ["resolve", "import", "probe"])
def test_worker_rejects_unpinned_and_relabelled_profiles_before_fetch(kind):
    with pytest.raises(ValueError, match="Pinned synthetic fixture profile"):
        validate_research_fixture_policy(
            request(_execution={}), "portable_fixture", kind
        )
    with pytest.raises(ValueError, match="Pinned synthetic fixture profile"):
        validate_research_fixture_policy(
            request(symbols=["AAPL"]), "portable_fixture", kind
        )


def test_owned_listing_policy_check_is_deferred_but_not_omitted():
    config = request(symbols=[], listing_ids=[research_listing("A").listing_id])
    validate_research_fixture_policy(config, "portable_fixture", "import")
    validate_research_fixture_policy(
        config, "portable_fixture", "import", listings=[research_listing("A")]
    )
    with pytest.raises(ValueError, match="Pinned synthetic fixture profile"):
        validate_research_fixture_policy(
            {**config, "_execution": {}},
            "portable_fixture",
            "import",
            listings=[research_listing("A")],
        )


def test_full_request_rejects_wrong_listing_metadata_and_nonraw_prices():
    listing = research_listing("A")
    with pytest.raises(DomainError):
        research_fixture_profile(
            request(), listings=[listing.model_copy(update={"timezone": "UTC"})]
        )
    with pytest.raises(ValueError, match="requires raw prices"):
        validate_research_fixture_policy(
            request(price_mode="qfq"), "portable_fixture", "import", listings=[listing]
        )


def test_profile_admission_imports_no_data_sdk_or_engine():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from backend.app.data_services import research_fixture_profile; "
                "research_fixture_profile({'provider':'portable_fixture','market':'US','symbols':['A']}); "
                "assert not any(m.split('.')[0] in {'pandas','pyarrow','exchange_calendars','akquant','akshare','yfinance','openbb'} for m in sys.modules)"
            ),
        ],
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode()
