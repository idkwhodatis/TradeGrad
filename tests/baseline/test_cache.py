import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from src import data_cache
from src.data_cache import _cache_path, load_data
from src.resources import bounded_workers


@pytest.fixture
def csv_data(tmp_path,monkeypatch):
    root=tmp_path / "data"
    root.mkdir()
    path=root / "ABC.csv"
    path.write_text("date,open,high,low,close,volume\n2024-01-02,1.234567891234567,1.34567891234567,1.12345678912345,1.234567891234566,1001\n2024-01-03,2.234567891234567,2.34567891234567,2.12345678912345,2.234567891234566,2002\n")
    monkeypatch.setenv("AKQUANT_DATA_CACHE",str(tmp_path / "cache"))
    return root,path


def test_cache_round_trips_exact_numeric_data(csv_data):
    root,_=csv_data
    first=load_data(str(root),symbols=["ABC"])["ABC"]
    second=load_data(str(root),symbols=["ABC"])["ABC"]
    pd.testing.assert_frame_equal(first,second,check_exact=True)
    assert first.loc[0,"close"] == 1.234567891234566
    assert _cache_path(str(root),["ABC"]).suffix == ".json"


def test_content_not_path_or_mtime_is_cache_identity(csv_data,tmp_path):
    root,path=csv_data
    old=_cache_path(str(root),["ABC"])
    before=path.stat()
    path.write_text(path.read_text().replace("1001","1002"))
    os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    assert _cache_path(str(root),["ABC"]) != old
    other=tmp_path / "same-content-elsewhere"
    other.mkdir()
    (other/path.name).write_bytes(path.read_bytes())
    assert _cache_path(str(other),["ABC"]) == _cache_path(str(root),["ABC"])


def test_hash_and_parse_use_same_bytes_if_source_changes(csv_data,monkeypatch):
    root,path=csv_data
    key=_cache_path(str(root),["ABC"])
    original=data_cache._read_one_csv
    def changing_source(snapshot):
        path.write_text(path.read_text().replace("1001","9999"))
        return original(snapshot)
    monkeypatch.setattr(data_cache,"_read_one_csv",changing_source)
    first=load_data(str(root),symbols=["ABC"])["ABC"]
    assert first.loc[0,"volume"] == 1001
    assert key.exists()
    monkeypatch.setattr(data_cache,"_read_one_csv",original)
    next_frame=load_data(str(root),symbols=["ABC"])["ABC"]
    assert next_frame.loc[0,"volume"] == 9999
    assert _cache_path(str(root),["ABC"]) != key


def test_corrupt_cache_is_rebuilt_without_executing_pickle(csv_data):
    root,_=csv_data
    key=_cache_path(str(root),["ABC"])
    key.write_bytes(b"\x80\x04cpickle\nunsafe")
    frame=load_data(str(root),symbols=["ABC"])["ABC"]
    assert len(frame) == 2
    assert key.read_text().startswith("{")


def test_parallel_cache_publication_is_complete(csv_data):
    root,_=csv_data
    with ThreadPoolExecutor(max_workers=4) as pool:
        values=list(pool.map(lambda _:load_data(str(root),symbols=["ABC"]),range(8)))
    for value in values:
        pd.testing.assert_frame_equal(values[0]["ABC"],value["ABC"],check_exact=True)
    assert not list(_cache_path(str(root),["ABC"]).parent.glob("*.tmp"))


def test_missing_and_invalid_symbols_fail_loudly(csv_data):
    root,_=csv_data
    with pytest.raises(FileNotFoundError,match="MISSING"):
        load_data(str(root),symbols=["ABC","MISSING"])
    with pytest.raises(ValueError,match="symbol"):
        load_data(str(root),symbols=["../ABC"])
    with pytest.raises(ValueError,match="No market data"):
        load_data(str(root),symbols=[])


def test_worker_limits_are_conservative_and_bounded(monkeypatch):
    monkeypatch.delenv("TRADEGRAD_WORKERS",raising=False)
    assert bounded_workers(999) == 1
    monkeypatch.setenv("TRADEGRAD_WORKERS","2")
    assert bounded_workers(999) == 2
    assert bounded_workers(2,jobs=1) == 1
    with pytest.raises(ValueError):
        bounded_workers(0)
    monkeypatch.setenv("TRADEGRAD_WORKERS","0")
    with pytest.raises(ValueError):
        bounded_workers(2)
