"""Market localization is a derived engine input, never a cache/CSV rewrite."""

import pandas as pd
import pytest

from src.data_cache import load_data, localize_market_dates


@pytest.mark.parametrize("zone", ["Asia/Shanghai", "America/New_York"])
def test_cold_and_warm_csv_cache_remain_raw_after_market_localization(
    tmp_path, monkeypatch, zone
):
    data = tmp_path / "data"
    data.mkdir()
    source = data / "A.csv"
    content = (
        b"date,open,high,low,close,volume\n"
        b"2024-03-08,10,11,9,10,1000\n"
        b"2024-03-11,11,12,10,11,1000\n"
        b"2024-11-01,12,13,11,12,1000\n"
        b"2024-11-04,13,14,12,13,1000\n"
    )
    source.write_bytes(content)
    monkeypatch.setenv("AKQUANT_DATA_CACHE", str(tmp_path / "cache"))
    cold = load_data(str(data))
    before = cold["A"].copy(deep=True)
    localized = localize_market_dates(cold, zone)
    pd.testing.assert_frame_equal(cold["A"], before, check_exact=True)
    assert cold["A"]["date"].dt.tz is None
    assert str(localized["A"]["date"].dt.tz) == zone
    assert localized["A"]["date"].dt.tz_localize(None).equals(before["date"])
    assert localized["A"].drop(columns="date").equals(before.drop(columns="date"))
    warm = load_data(str(data))
    pd.testing.assert_frame_equal(warm["A"], before, check_exact=True)
    pd.testing.assert_frame_equal(
        localize_market_dates(warm, zone)["A"], localized["A"], check_exact=True
    )
    assert source.read_bytes() == content
    offsets = [stamp.utcoffset().total_seconds() for stamp in localized["A"]["date"]]
    assert offsets == (
        [-18000, -14400, -14400, -18000] if zone == "America/New_York" else [28800] * 4
    )


def test_aware_input_keeps_its_instant_and_does_not_guess_a_session_close():
    dates = pd.to_datetime(["2024-03-08T21:00:00Z", "2024-03-11T20:00:00Z"])
    data = {"A": pd.DataFrame({"date": dates, "close": [10.0, 11.0]})}
    result = localize_market_dates(data, "America/New_York")["A"]
    assert result["date"].dt.tz_convert("UTC").tolist() == dates.tolist()
    assert result["date"].dt.hour.tolist() == [16, 16]
    assert str(data["A"]["date"].dt.tz) == "UTC"


def test_missing_dates_are_not_coerced_to_epoch():
    with pytest.raises(ValueError, match="Invalid native market date"):
        localize_market_dates(
            {"A": pd.DataFrame({"date": [pd.NaT]})}, "America/New_York"
        )
