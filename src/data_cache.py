"""Content-addressed, non-executable CSV cache with exact numeric round trips.

The bytes hashed for a key are the bytes parsed. Source paths and mtimes are not identities.
The isolated runner, not this cache, supplies filesystem and memory security boundaries.
"""
import hashlib
import io
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

try:
    from .resources import bounded_workers
except ImportError:
    from resources import bounded_workers

TRANSFORM_VERSION = "tradegrad-csv-v3"
CACHE_DTYPES = {"object", "float64", "float32", "int64", "int32", "uint64", "bool", "datetime64[ns]"}


def localize_market_dates(data: dict[str, pd.DataFrame], timezone: str) -> dict[str, pd.DataFrame]:
    """Give native daily session labels an explicit market timezone.

    AKQuant 0.3.27's naive input default is Shanghai, independently of its
    configured strategy timezone. Passing naive US dates therefore shifts bars,
    window membership and cross-sectional callbacks to the previous session.
    Localize before the engine derives *either* its feed or trading-day metadata.

    A date-only value remains a local-midnight session label, NOT an observed
    exchange close or a claim that the completed bar was available at midnight.
    Already-aware inputs retain their actual instant. No bars are filled, moved
    to a guessed closing hour, or rewritten in the immutable CSV/content cache.
    Keep this outside load_data so the source_compat path and raw cache stay exact.
    """
    localized = {}
    for symbol, frame in data.items():
        dates = frame["date"]
        if dates.isna().any():
            raise ValueError("Invalid native market date")
        copy = frame.copy(deep=False)
        copy["date"] = (
            dates.dt.tz_localize(timezone, ambiguous="raise", nonexistent="raise")
            if dates.dt.tz is None
            else dates.dt.tz_convert(timezone)
        )
        localized[symbol] = copy
    return localized


def _paths(data_dir: str, symbols: list[str] | None):
    root = Path(data_dir)
    if symbols is None:
        paths = sorted(root.glob("*.csv"))
    else:
        for symbol in symbols:
            if not isinstance(symbol, str) or not symbol or "/" in symbol or "\\" in symbol:
                raise ValueError("Invalid symbol path")
        paths = [root / f"{symbol}.csv" for symbol in sorted(set(symbols))]
    if not paths:
        raise ValueError("No market data CSVs were selected")
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"Missing symbol data: {path.name}")
    return paths


def _snapshot(paths: list[Path]) -> list[tuple[str, bytes]]:
    # Holding byte snapshots avoids hashing one version and parsing a changed file later.
    return [(path.name, path.read_bytes()) for path in paths]


def _snapshot_cache_path(sources: list[tuple[str, bytes]]) -> Path:
    digest = hashlib.sha256(f"{TRANSFORM_VERSION}|pandas={pd.__version__}".encode())
    for name, data in sources:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    cache_dir = Path(os.environ.get("AKQUANT_DATA_CACHE", "/tmp/tradegrad-data-cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{digest.hexdigest()}.json"


def _cache_path(data_dir: str, symbols: list[str] | None) -> Path:
    return _snapshot_cache_path(_snapshot(_paths(data_dir, symbols)))


def _read_one_csv(source: tuple[str, bytes]):
    name, data = source
    frame = pd.read_csv(io.BytesIO(data), parse_dates=["date"], float_precision="round_trip")
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns) or frame.empty:
        raise ValueError(f"Invalid OHLCV schema in {name}")
    if frame["date"].isna().any() or frame["date"].duplicated().any():
        raise ValueError(f"Invalid or duplicate dates in {name}")
    frame["symbol"] = Path(name).stem
    return Path(name).stem, frame


def _encode_frame(frame: pd.DataFrame) -> dict:
    # Python's JSON float repr round-trips IEEE-754 doubles, unlike DataFrame.to_json's
    # limited decimal precision. Dates have a separate explicit dtype.
    data = {}
    for column in frame:
        data[column] = [value.isoformat() if isinstance(value, pd.Timestamp) else
                        None if pd.isna(value) else value for value in frame[column].tolist()]
    return {"columns": list(frame.columns), "dtypes": {key: str(value) for key, value in frame.dtypes.items()}, "data": data}


def _decode_frame(encoded: dict) -> pd.DataFrame:
    dtypes = encoded["dtypes"]
    if not isinstance(dtypes, dict) or not set(dtypes.values()).issubset(CACHE_DTYPES):
        raise ValueError("unsupported cache dtype")
    frame = pd.DataFrame(encoded["data"], columns=encoded["columns"])
    for column, dtype in dtypes.items():
        if dtype == "datetime64[ns]":
            frame[column] = pd.to_datetime(frame[column])
        frame[column] = frame[column].astype(dtype)
    return frame


def load_data(data_dir: str, max_workers: int | None = None, symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
    started = time.monotonic()
    paths = _paths(data_dir, symbols)
    workers = bounded_workers(max_workers, limit_var="TRADEGRAD_DATA_WORKERS", jobs=len(paths))
    sources = _snapshot(paths)
    cache = _snapshot_cache_path(sources)
    try:
        payload = json.loads(cache.read_text())
        if payload["schema_version"] != TRANSFORM_VERSION or payload["key"] != cache.stem:
            raise ValueError("cache version mismatch")
        content = payload["frames"]
        if hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest() != payload["sha256"]:
            raise ValueError("cache checksum mismatch")
        if set(content) != {p.stem for p in paths}:
            raise ValueError("cache symbols mismatch")
        frames = {symbol: _decode_frame(raw) for symbol, raw in content.items()}
        print(f"Loaded {len(frames)} symbols from content cache")
        return frames
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        pass
    if workers == 1:
        frames = dict(_read_one_csv(source) for source in sources)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            frames = dict(executor.map(_read_one_csv, sources))
    content = {symbol: _encode_frame(frame) for symbol, frame in frames.items()}
    payload = {"schema_version": TRANSFORM_VERSION, "key": cache.stem, "frames": content,
               "sha256": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=cache.parent, suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(payload, output, allow_nan=False)
        os.replace(temporary, cache)
    except OSError:
        pass  # A cache is an optimization, never the only copy of input data.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Loaded {len(frames)} symbols in {time.monotonic() - started:.2f}s (workers={workers})")
    return frames
