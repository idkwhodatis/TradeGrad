"""Pure API-runtime config/staging evidence; no engine, database, or live claims."""

import csv
import hashlib
import io
import secrets
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.tradegrad_adapter.config import (
    DateWindow,
    MarketConfig,
    ModelConfig,
    RunConfig,
    VersionRef,
    WarmupConfig,
    WindowSet,
    canonical_bytes,
)
from packages.tradegrad_adapter.windows import (
    COLD_START_WARNING,
    CsvLimits,
    PreflightError,
    preflight_csv_window,
    preflight_dataset,
    stage_csv_window,
    stage_dataset_window,
)

REF = VersionRef(id="version-1", sha256="a" * 64)


def _config(**changes):
    value = {
        "task": "cn_cross",
        "dataset": REF,
        "seed_strategy": REF,
        "universe": REF,
        "windows": WindowSet(
            train=DateWindow(start="2024-01-05", end="2024-01-08"),
            validation=DateWindow(start="2024-01-08", end="2024-01-10"),
            holdout=DateWindow(start="2024-01-10", end="2024-01-12"),
        ),
        "warmup": WarmupConfig(sessions=3),
        "market_profile": MarketConfig.for_market("CN"),
        "environment_digest": "b" * 64,
    }
    value.update(changes)
    return RunConfig(**value)


def _csv(days, *, fieldnames=("date", "open", "high", "low", "close", "volume")):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(fieldnames)
    for day in days:
        writer.writerow([str(day), "10", "11", "9", "10", "100"])
    return output.getvalue().encode()


def _days(start=1, end=13):
    return [date(2024, 1, 1) + timedelta(days=i - 1) for i in range(start, end)]


def _sessions(content):
    return [row["date"] for row in csv.DictReader(io.StringIO(content.decode()))]


def _universe(day, members):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["updateDate", "code", "code_name"])
    writer.writerows([[day, member, member] for member in members])
    return output.getvalue().encode()


def _bundle(*, second=True):
    files = {
        "data/A.csv": _csv(_days()),
        "universe/fixed_list_2024-01.csv": _universe("2024-01-01", ["A"]),
    }
    mapping = {"listing-A": "A"}
    if second:
        files["data/B.csv"] = _csv(_days())
        files["universe/members_list_2024-01-10.csv"] = _universe("2024-01-10", ["B"])
        mapping["listing-B"] = "B"
    manifest = {
        "files": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        },
        "universe": {
            "id": REF.id,
            "content_sha256": REF.sha256,
            "state": "historical_membership" if second else "user_fixed",
        },
        "evaluator_symbol_mapping": mapping,
        "listings": [
            {
                "listing_id": identity,
                "calendar_note": "Declared XSHG schedule proxy, not authoritative exchange equivalence.",
            }
            for identity in mapping
        ],
        "coverage": [
            {"listing_id": identity, "expected_sessions": [str(d) for d in _days()]}
            for identity in mapping
        ],
    }
    config = _config(
        dataset=VersionRef(
            id="dataset-1", sha256=hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        )
    )
    return config, manifest, files


def test_frozen_config_nested_and_canonical_digest():
    config = _config()
    with pytest.raises(ValidationError):
        config.task = "us_cross"
    with pytest.raises(ValidationError):
        config.market_profile.slippage.value = 0.1
    with pytest.raises(ValidationError):
        config.windows.train.start = date(2020, 1, 1)
    assert RunConfig.model_validate_json(config.canonical_bytes()) == config
    assert (
        RunConfig.from_verified_json(config.canonical_bytes(), config.sha256) == config
    )
    assert config.sha256 == hashlib.sha256(config.canonical_bytes()).hexdigest()
    with pytest.raises(ValueError, match="digest mismatch"):
        RunConfig.from_verified_json(config.canonical_bytes(), "f" * 64)


def test_unknown_settings_and_secret_model_configuration_rejected():
    config = _config().model_dump(mode="json")
    config["ignored_setting"] = True
    with pytest.raises(ValidationError):
        RunConfig.model_validate(config)
    for field, value in (
        ("api_key", secrets.token_urlsafe(32)),
        ("base_url", "https://service.invalid"),
        ("model", "https://service.invalid"),
        ("model", "sk-" + secrets.token_urlsafe(32)),
    ):
        with pytest.raises(ValidationError):
            ModelConfig(**{field: value})
    with pytest.raises(ValidationError):
        ModelConfig(provider="unsupported")
    with pytest.raises(ValidationError):
        ModelConfig(provider="openai", model="gpt-example")
    with pytest.raises(ValidationError):
        ModelConfig(
            provider="openai", model="gpt-example", credential_ref="server:openai"
        )
    assert (
        ModelConfig(
            provider="openai",
            model="gpt-example",
            profile_id="admitted",
            profile_sha256="a" * 64,
        ).credential_ref
        is None
    )
    config = _config().model_dump(mode="json")
    config["objective"]["ar_weight"] = 0.9
    with pytest.raises(ValidationError):
        RunConfig.model_validate(config)


@pytest.mark.parametrize(
    "windows",
    [
        {"train": {"start": "2024-01-01", "end": "2024-01-01"}},
        {
            "train": {"start": "2024-01-01", "end": "2024-02-01"},
            "validation": {"start": "2024-01-20", "end": "2024-03-01"},
        },
        {
            "train": {"start": "2024-02-01", "end": "2024-03-01"},
            "holdout": {"start": "2024-01-01", "end": "2024-02-01"},
        },
    ],
)
def test_overlap_empty_and_reversed_split_order_rejected(windows):
    with pytest.raises(ValidationError):
        WindowSet(**windows)


def test_touching_half_open_boundaries_allowed_and_optimizer_never_sees_holdout():
    windows = _config().windows
    assert not windows.train.overlaps(windows.validation)
    assert tuple(name for name, _ in windows.optimization_windows()) == (
        "train",
        "validation",
    )
    assert tuple(name for name, _ in windows.items()) == (
        "train",
        "validation",
        "holdout",
    )
    assert windows.get("holdout") == windows.holdout
    with pytest.raises(ValueError):
        windows.get("other")
    with pytest.raises(ValidationError, match="validation"):
        _config(execution_mode="optimization", windows=WindowSet(train=windows.train))


@pytest.mark.parametrize(
    "task,minimum", [("cn_time", 20), ("us_time", 20), ("cn_cross", 3), ("us_cross", 3)]
)
def test_seed_minimum_history_contract(task, minimum):
    market = task[:2].upper()
    assert (
        _config(
            task=task,
            warmup=WarmupConfig(sessions=minimum),
            market_profile=MarketConfig.for_market(market),
        ).warmup.sessions
        == minimum
    )
    with pytest.raises(ValidationError, match="previous available sessions"):
        _config(
            task=task,
            warmup=WarmupConfig(sessions=minimum - 1),
            market_profile=MarketConfig.for_market(market),
        )


def test_source_compat_explicitly_keeps_coldstart_and_implicit_market_defaults():
    config = _config(
        profile="source_compat",
        warmup=WarmupConfig(sessions=0, history_policy="source_cold_start"),
        market_profile=MarketConfig.for_market("CN", "source_compat"),
    )
    assert "timezone" not in config.market_profile.engine_kwargs()
    assert "lot_size" not in config.market_profile.engine_kwargs()
    staged = stage_csv_window(
        _csv(_days()),
        listing_id="A",
        window=config.windows.train,
        warmup_sessions=0,
        history_policy="source_cold_start",
    )
    assert _sessions(staged.content) == ["2024-01-05", "2024-01-06", "2024-01-07"]
    assert COLD_START_WARNING in staged.report.warnings
    source_warning = (
        (Path(__file__).parents[2] / "docs/M2_VALIDATION.md")
        .read_text()
        .split("The transitional fixed-seed runner", 1)[1]
        .split("\n\n", 1)[0]
    )
    assert COLD_START_WARNING == " ".join(
        ("The transitional fixed-seed runner" + source_warning).splitlines()
    )
    with pytest.raises(ValidationError, match="source market defaults"):
        _config(
            profile="source_compat",
            warmup=config.warmup,
            market_profile=MarketConfig.for_market("CN"),
        )


def test_market_kwargs_exactly_match_original_supported_defaults():
    from src.market import backtest_kwargs

    for market in ("CN", "US"):
        for profile in ("source_compat", "corrected_explicit_config"):
            source = backtest_kwargs(market, profile)
            source.pop("show_progress")
            assert MarketConfig.for_market(market, profile).engine_kwargs() == source


def test_exact_half_open_window_and_last_available_rows_no_future_exposure():
    staged = stage_csv_window(
        _csv(_days()), listing_id="A", window=_config().windows.train, warmup_sessions=3
    )
    assert _sessions(staged.content) == [str(day) for day in _days(2, 8)]
    assert staged.report.staged_warmup_sessions == 3
    assert staged.report.evaluation_rows == 3
    assert staged.report.staged_last == date(2024, 1, 7)
    assert staged.report.expected_session_check == "unavailable"
    assert b"2024-01-08" not in staged.content and b"2024-01-10" not in staged.content


def test_per_listing_gaps_count_available_rows_not_calendar_days():
    window = DateWindow(start="2024-01-08", end="2024-01-10")
    complete = stage_csv_window(
        _csv(_days()), listing_id="A", window=window, warmup_sessions=3
    )
    gaps = stage_csv_window(
        _csv([d for d in _days() if d.day not in (5, 7)]),
        listing_id="B",
        window=window,
        warmup_sessions=3,
    )
    assert _sessions(complete.content)[:3] == ["2024-01-05", "2024-01-06", "2024-01-07"]
    assert _sessions(gaps.content)[:3] == ["2024-01-03", "2024-01-04", "2024-01-06"]
    assert gaps.report.staged_warmup_sessions == 3


def test_short_history_actionable_failure_does_not_fill_or_silently_coldstart():
    with pytest.raises(PreflightError) as caught:
        stage_csv_window(
            _csv(_days(3, 9)),
            listing_id="listing-B",
            window=_config().windows.train,
            warmup_sessions=3,
        )
    assert caught.value.code == "insufficient_warmup"
    assert caught.value.listing_id == "listing-B"
    assert caught.value.details["available"] == 2
    assert "import earlier history" in str(caught.value)


def test_pinned_missing_evaluation_session_fail_closed_but_prior_gaps_not_filled():
    data = _csv([d for d in _days() if d.day != 6])
    with pytest.raises(PreflightError) as caught:
        preflight_csv_window(
            data,
            listing_id="A",
            window=_config().windows.train,
            warmup_sessions=3,
            expected_sessions=_days(),
        )
    assert caught.value.code == "session_coverage_gap"
    assert caught.value.details["missing_sessions"] == ["2024-01-06"]


@pytest.mark.parametrize(
    "data,code",
    [
        (b"date,date\n2024-01-01,2024-01-02\n", "invalid_csv_header"),
        (
            b"date,open,high,low,close,volume\n2024-01-01,1,2,1,1,10,extra\n",
            "invalid_csv_width",
        ),
        (_csv(["2024-01-04", "2024-01-04"]), "unordered_sessions"),
        (_csv(["2024-01-05", "2024-01-04"]), "unordered_sessions"),
        (_csv(["20240105"]), "invalid_session"),
        (b"date,open,high,low,close,volume\n2024-01-05,NaN,2,1,1,10\n", "invalid_bar"),
    ],
)
def test_invalid_csv_rejected(data, code):
    with pytest.raises(PreflightError) as caught:
        stage_csv_window(
            data, listing_id="A", window=_config().windows.train, warmup_sessions=0
        )
    assert caught.value.code == code


def test_bounded_csv_and_tampering():
    data = _csv(_days())
    for limits, code in [
        (CsvLimits(max_file_bytes=10), "csv_byte_budget"),
        (CsvLimits(max_rows=2), "csv_row_budget"),
    ]:
        with pytest.raises(PreflightError) as caught:
            preflight_csv_window(
                data,
                listing_id="A",
                window=_config().windows.train,
                warmup_sessions=3,
                limits=limits,
            )
        assert caught.value.code == code
    with pytest.raises(PreflightError) as caught:
        preflight_csv_window(
            data,
            listing_id="A",
            window=_config().windows.train,
            warmup_sessions=3,
            expected_sha256="0" * 64,
        )
    assert caught.value.code == "input_hash_mismatch"


def test_full_staging_excludes_future_universe_and_holdout_only_files():
    config, manifest, files = _bundle()
    staged = stage_dataset_window(config, "train", manifest, files.__getitem__)
    assert set(staged.files) == {"data/A.csv", "universe/members_list_2024-01-01.csv"}
    assert "data/B.csv" not in staged.files
    assert _sessions(staged.files["data/A.csv"]) == [str(d) for d in _days(2, 8)]
    assert staged.report.point_in_time_proven is False
    assert any("proxy" in warning for warning in staged.report.warnings)
    assert (
        preflight_dataset(config, "train", manifest, files.__getitem__) == staged.report
    )
    holdout = stage_dataset_window(config, "holdout", manifest, files.__getitem__)
    assert "data/A.csv" not in holdout.files
    assert "data/B.csv" in holdout.files
    assert all(day < "2024-01-12" for day in _sessions(holdout.files["data/B.csv"]))


def test_manifest_file_and_universe_tampering_are_independently_rejected():
    config, manifest, files = _bundle()
    bad_manifest = dict(manifest, unknown="changed")
    with pytest.raises(PreflightError, match="manifest changed"):
        stage_dataset_window(config, "train", bad_manifest, files.__getitem__)
    files["data/A.csv"] += b"tampered"
    with pytest.raises(PreflightError, match="input bytes changed"):
        stage_dataset_window(config, "train", manifest, files.__getitem__)
    config, manifest, files = _bundle()
    bad_ref = config.model_dump(mode="json")
    bad_ref["universe"]["sha256"] = "0" * 64
    with pytest.raises(PreflightError, match="universe differs"):
        stage_dataset_window(
            RunConfig.model_validate(bad_ref), "train", manifest, files.__getitem__
        )


def test_current_snapshot_not_back_applied_and_missing_mapping_fail_closed():
    config, manifest, files = _bundle()
    manifest["universe"].update(
        state="current_snapshot", as_of="2024-01-07T12:00:00+00:00"
    )
    config = _config(
        dataset=VersionRef(
            id="dataset-1", sha256=hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        )
    )
    with pytest.raises(PreflightError) as caught:
        stage_dataset_window(config, "train", manifest, files.__getitem__)
    assert caught.value.code == "universe_not_historical"
    manifest["universe"]["state"] = "user_fixed"
    del manifest["evaluator_symbol_mapping"]
    config = _config(
        dataset=VersionRef(
            id="dataset-1", sha256=hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        )
    )
    with pytest.raises(PreflightError) as caught:
        stage_dataset_window(config, "train", manifest, files.__getitem__)
    assert caught.value.code == "missing_listing_mapping"


def test_unchecked_pydantic_copy_cannot_bypass_staging_validation():
    config, manifest, files = _bundle()
    forged = config.model_copy(update={"warmup": WarmupConfig(sessions=0)})
    with pytest.raises(ValidationError):
        stage_dataset_window(forged, "train", manifest, files.__getitem__)


def test_api_import_does_not_load_provider_array_or_engine_runtime():
    command = "import sys; import packages.tradegrad_adapter.config; import packages.tradegrad_adapter.windows; forbidden={'pandas','numpy','pyarrow','akquant','akshare','yfinance','openbb'}; assert not forbidden.intersection(sys.modules)"
    subprocess.run(
        [sys.executable, "-c", command], check=True, cwd=Path(__file__).parents[2]
    )


def test_unsupported_fixture_model_prompt_and_benchmark_fail_instead_of_ignored():
    for change in ({"model": "another-fixture"}, {"temperature": 0.5}):
        with pytest.raises(ValidationError, match="Fixture model requires"):
            ModelConfig(**change)
    for change in ({"prompt_version": "unknown"}, {"benchmark": "SP500"}):
        with pytest.raises(ValidationError):
            _config(**change)


@pytest.mark.parametrize(
    "path",
    ["data/../A.csv", "/data/A.csv", "data//A.csv", "data/./A.csv", "data\\A.csv"],
)
def test_manifest_paths_must_be_canonical_and_contained(path):
    config, manifest, files = _bundle(second=False)
    original = files.pop("data/A.csv")
    files[path] = original
    manifest["files"][path] = manifest["files"].pop("data/A.csv")
    config = _config(
        dataset=VersionRef(
            id="dataset-1", sha256=hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        )
    )
    seen = []
    with pytest.raises(PreflightError) as caught:
        stage_dataset_window(
            config, "train", manifest, lambda name: (seen.append(name), files[name])[1]
        )
    assert caught.value.code == "unsupported_input_path"
    assert path not in seen


def test_no_bars_in_evaluation_interval_is_actionable_failure():
    with pytest.raises(PreflightError) as caught:
        stage_csv_window(
            _csv(_days(1, 5)),
            listing_id="A",
            window=_config().windows.train,
            warmup_sessions=3,
        )
    assert caught.value.code == "empty_evaluation_window"


def test_fixed_universe_cannot_silently_become_historical_membership():
    config, manifest, files = _bundle()
    manifest["universe"]["state"] = "user_fixed"
    config = _config(
        dataset=VersionRef(
            id="dataset-1", sha256=hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        )
    )
    with pytest.raises(PreflightError) as caught:
        stage_dataset_window(config, "holdout", manifest, files.__getitem__)
    assert caught.value.code == "inconsistent_fixed_universe"


def test_file_and_total_byte_budgets_apply_before_staging():
    config, manifest, files = _bundle()
    for limits, code in [
        (CsvLimits(max_files=1), "file_budget"),
        (CsvLimits(max_total_bytes=100), "dataset_byte_budget"),
    ]:
        with pytest.raises(PreflightError) as caught:
            stage_dataset_window(
                config, "train", manifest, files.__getitem__, limits=limits
            )
        assert caught.value.code == code


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (
            "2022-11-15",
            "2025-01-01",
            [
                ("2022-11-15", "2023-01-01"),
                ("2023-01-01", "2024-01-01"),
                ("2024-01-01", "2025-01-01"),
            ],
        ),
        ("2024-02-28", "2024-03-01", [("2024-02-28", "2024-03-01")]),
        (
            "2023-12-31",
            "2024-03-01",
            [("2023-12-31", "2024-01-01"), ("2024-01-01", "2024-03-01")],
        ),
        ("2023-01-01", "2024-01-01", [("2023-01-01", "2024-01-01")]),
    ],
)
def test_registered_calendar_year_windows_handle_leap_days_and_exclusive_january(
    start, end, expected
):
    from packages.tradegrad_adapter.config import evaluation_windows

    config = _config(windows=WindowSet(train=DateWindow(start=start, end=end)))
    assert evaluation_windows(config) == tuple(
        {"split": "train", "start": first, "end": last} for first, last in expected
    )
    assert (
        config.objective.windowing_definition
        == "calendar_year_train_full_validation.v1"
    )
    total_days = sum(
        (date.fromisoformat(window["end"]) - date.fromisoformat(window["start"])).days
        for window in evaluation_windows(config)
    )
    assert total_days == (date.fromisoformat(end) - date.fromisoformat(start)).days


@pytest.mark.parametrize("profile", ["source_compat", "corrected_explicit_config"])
def test_calendar_year_partition_matches_original_inclusive_source_loss(profile):
    from packages.tradegrad_adapter.config import evaluation_windows
    from src.loss import split_time_windows_by_year

    windows = WindowSet(
        train=DateWindow(start="2018-03-15", end="2024-01-01"),
        validation=DateWindow(start="2024-01-01", end="2026-01-01"),
        holdout=DateWindow(start="2026-01-01", end="2027-01-01"),
    )
    config = _config(
        windows=windows,
        profile=profile,
        market_profile=MarketConfig.for_market("CN", profile),
        warmup=WarmupConfig(sessions=0, history_policy="source_cold_start")
        if profile == "source_compat"
        else WarmupConfig(sessions=3),
    )
    registered = evaluation_windows(config)
    inclusive = [
        (row["start"], str(date.fromisoformat(row["end"]) - timedelta(days=1)))
        for row in registered
        if row["split"] == "train"
    ]
    assert inclusive == split_time_windows_by_year("2018-03-15", "2023-12-31")
    assert registered[-1] == {
        "split": "validation",
        "start": "2024-01-01",
        "end": "2026-01-01",
    }
    assert len(registered) == 7  # Six independent train accounts plus whole validation.
    assert all(row["split"] != "holdout" for row in registered)


def test_exact_registered_subwindow_staging_preserves_original_config_identity():
    from packages.tradegrad_adapter.config import evaluation_windows

    config, manifest, files = _bundle(second=False)
    days = [date(2023, 12, 20) + timedelta(days=i) for i in range(25)]
    files["data/A.csv"] = _csv(days)
    files["universe/fixed_list_2024-01.csv"] = _universe("2023-12-20", ["A"])
    manifest["files"] = {
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()
    }
    manifest["coverage"][0]["expected_sessions"] = [str(d) for d in days]
    config = _config(
        dataset=VersionRef(
            id="dataset-1", sha256=hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        ),
        windows=WindowSet(train=DateWindow(start="2023-12-30", end="2024-01-08")),
    )
    original_hash = config.sha256
    registered = evaluation_windows(config)
    assert len(registered) == 2
    staged = stage_dataset_window(
        config, "train", manifest, files.__getitem__, evaluation_window=registered[1]
    )
    assert _sessions(staged.files["data/A.csv"]) == [
        str(date(2023, 12, 29) + timedelta(days=i)) for i in range(10)
    ]
    assert staged.report.window == DateWindow(start="2024-01-01", end="2024-01-08")
    assert (
        preflight_dataset(
            config,
            "train",
            manifest,
            files.__getitem__,
            evaluation_window=DateWindow(start="2024-01-01", end="2024-01-08"),
        )
        == staged.report
    )
    assert config.sha256 == original_hash


@pytest.mark.parametrize(
    "split,window",
    [
        ("train", {"start": "2024-01-06", "end": "2024-01-08"}),
        ("train", {"split": "validation", "start": "2024-01-05", "end": "2024-01-08"}),
        ("holdout", {"start": "2024-01-10", "end": "2024-01-12"}),
    ],
)
def test_optimizer_staging_rejects_noncanonical_subsets_and_holdout(split, window):
    config, manifest, files = _bundle()
    reads = []
    with pytest.raises(PreflightError) as caught:
        stage_dataset_window(
            config,
            split,
            manifest,
            lambda name: (reads.append(name), files[name])[1],
            evaluation_window=window,
        )
    assert caught.value.code == "unregistered_evaluation_window"
    assert reads == []


def test_timing_symbol_alias_is_index_but_source_identity_and_hash_are_retained():
    config, manifest, files = _bundle(second=False)
    days = [date(2023, 12, 1) + timedelta(days=i) for i in range(45)]
    files["data/A.csv"] = _csv(days)
    source_digest = hashlib.sha256(files["data/A.csv"]).hexdigest()
    manifest["files"]["data/A.csv"] = source_digest
    manifest["coverage"][0]["expected_sessions"] = [str(d) for d in days]
    config = _config(
        task="cn_time",
        warmup=WarmupConfig(sessions=20),
        dataset=VersionRef(
            id="dataset-1", sha256=hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        ),
    )
    staged = stage_dataset_window(config, "train", manifest, files.__getitem__)
    assert "data/INDEX.csv" in staged.files and "data/A.csv" not in staged.files
    assert staged.report.listings[0].listing_id == "listing-A"
    assert staged.report.listings[0].source_sha256 == source_digest
    assert manifest["evaluator_symbol_mapping"] == {"listing-A": "A"}
    assert any(
        "not an index or benchmark classification" in warning
        for warning in staged.report.warnings
    )
    assert len(_sessions(staged.files["data/INDEX.csv"])) == 23


def test_optimization_config_cannot_stage_holdout_without_subwindow_argument():
    config, manifest, files = _bundle()
    config = RunConfig.model_validate(
        {**config.model_dump(mode="json"), "execution_mode": "optimization"}
    )
    with pytest.raises(PreflightError) as caught:
        stage_dataset_window(config, "holdout", manifest, files.__getitem__)
    assert caught.value.code == "holdout_not_authorized"
