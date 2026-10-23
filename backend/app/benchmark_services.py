"""Owner-scoped immutable benchmark admission and durable result verification."""

from backend.app.models import DatasetVersion
from backend.app.research_services import _reference
from backend.app.services import DomainError
from backend.app.storage import read_verified
from packages.tradegrad_adapter.benchmarks import (
    account_observations,
    build_comparison,
    prepare_benchmark,
    require_comparison_binding,
)


def prepare_for_window(session, owner_id, config, strategy_manifest, window, settings):
    source = getattr(config, "source_config", config)
    if source.benchmark is None:
        return None
    dataset = _reference(session, DatasetVersion, source.benchmark.dataset, owner_id)
    market = source.task[:2].upper()
    if (
        dataset.market != market
        or str(dataset.start_date) > window["start"]
        or str(dataset.end_date) < window["end"]
    ):
        raise DomainError(
            "benchmark_incompatible",
            "Benchmark market or immutable dataset coverage differs from the scored window",
            422,
        )
    try:
        return prepare_benchmark(
            source.benchmark,
            dataset.manifest,
            strategy_manifest,
            lambda name: read_verified(
                settings.storage_root,
                f"{dataset.storage_key}/{name}",
                dataset.manifest["files"][name],
                settings.max_artifact_bytes,
            ),
            window,
            market,
            is_synthetic=dataset.is_synthetic,
        )
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise DomainError(
            "benchmark_preflight_failed", str(error)[:1000], 422
        ) from None


def preflight_benchmarks(
    session, owner_id, config, strategy_manifest, windows, settings
):
    for window in windows:
        prepare_for_window(
            session, owner_id, config, strategy_manifest, window, settings
        )


def verify_persisted_benchmark(
    session, owner_id, config, strategy_manifest, bundle, settings
):
    require_comparison_binding(bundle, config)
    source = getattr(config, "source_config", config)
    if source.benchmark is None:
        return
    prepared = prepare_for_window(
        session, owner_id, config, strategy_manifest, bundle["window"], settings
    )
    if bundle["benchmark"] != build_comparison(prepared, account_observations(bundle)):
        raise DomainError(
            "benchmark_result_invalid",
            "Benchmark result differs from exact immutable owned snapshot observations",
            409,
        )


def comparison_view(value, *, start=None, end=None, max_points=1000):
    """Bounded display only. Full-window metrics/anchor never change with filters."""
    if not value or value.get("status") != "available":
        return value
    if type(max_points) is not int or not 2 <= max_points <= 5000:
        raise DomainError(
            "invalid_result_query", "Benchmark point budget must be 2 to 5000", 422
        )
    rows = value["series"]["rows"]
    filtered = [
        row
        for row in rows
        if (start is None or row[0] >= str(start))
        and (end is None or row[0] < str(end))
    ]
    shown = (
        filtered
        if len(filtered) <= max_points
        else [
            filtered[round(i * (len(filtered) - 1) / (max_points - 1))]
            for i in range(max_points)
        ]
    )
    return {
        **value,
        "input": {k: v for k, v in value["input"].items() if k != "observations"},
        "series": {**value["series"], "rows": shown},
        "series_total_rows": len(rows),
        "series_filtered_rows": len(filtered),
        "series_returned_rows": len(shown),
        "downsampled": len(filtered) > len(shown),
        "sampling": "uniform" if len(filtered) > len(shown) else "none",
        "filter_scope": "session_window_only_not_listing; metrics_and_anchor_use_full_scored_window",
    }


def record_benchmark_admission(session, run, config):
    """Conservative listing coverage, exact scored dates; no benchmark warmup."""
    from backend.app.experiment_services import record_exposure
    from packages.tradegrad_adapter.config import DateWindow

    source = getattr(config, "source_config", config)
    if source.benchmark is None:
        return
    dataset = _reference(
        session, DatasetVersion, source.benchmark.dataset, run.owner_id
    )
    windows = (
        [("holdout", config.holdout)]
        if hasattr(config, "holdout")
        else source.windows.optimization_windows()
    )
    for split, window in windows:
        record_exposure(
            session,
            owner_id=run.owner_id,
            dataset=dataset,
            run=run,
            window=DateWindow.model_validate(window.model_dump(mode="json")),
            purpose=f"benchmark_{split}_admission",
            source_key=f"benchmark:{run.id}:{run.generation}:{split}",
            provenance={
                "config_sha256": config.sha256,
                "reference": source.benchmark.model_dump(mode="json"),
                "admission_before_dispatch": True,
                "coverage": "exact_scored_dates_conservative_dataset_listings",
                "optimizer_feedback": False,
            },
        )
