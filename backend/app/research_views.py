"""Read-only M4 projections. Never import candidates or the numerical runtime.

A database registration, exact bytes checksum, native schema and immutable run
identity must all agree before any result is rendered. Queries are projections,
not replacement ResultBundles; downloadable JSON retains the original bytes.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from backend.app.benchmark_services import comparison_view
from backend.app.models import (
    Artifact,
    DatasetVersion,
    Evaluation,
    ModelCall,
    ResearchRun,
)
from backend.app.research_services import (
    bounded_json,
    owned_research_run,
    payload_hash,
    research_config,
    validate_research_submission,
)
from backend.app.research_view_schemas import (
    ComparisonDifference,
    DifferenceValue,
    ProfileDefault,
    ResearchCapabilities,
    ResearchPreflightPublic,
    ResearchUsagePublic,
    ResultComparePublic,
    ResultComparisonItem,
    ResultQueryPublic,
    ResultTable,
)
from backend.app.services import DomainError
from backend.app.storage import read_verified
from packages.tradegrad_adapter.config import (
    MarketConfig,
    WarmupConfig,
    canonical_bytes,
    evaluation_windows,
)
from packages.tradegrad_adapter.results import (
    MAX_WIRE_BYTES,
    canonical_json_bytes,
    validate_result_bundle,
)

SERIES = frozenset(("equity", "drawdown", "cash", "margin"))
SECTIONS = SERIES | {"positions", "orders", "fills", "closed_trades"}
TIME_COLUMNS = {
    "positions": "date",
    "orders": "created_at",
    "fills": "timestamp",
    "closed_trades": "exit_time",
}
SORT_COLUMNS = {
    "positions": {"date", "symbol", "long_shares", "short_shares", "equity", "close"},
    "orders": {
        "created_at",
        "updated_at",
        "symbol",
        "side",
        "quantity",
        "filled_quantity",
        "commission",
        "status",
    },
    "fills": {"timestamp", "symbol", "side", "quantity", "price", "commission"},
    "closed_trades": {
        "entry_time",
        "exit_time",
        "symbol",
        "quantity",
        "pnl",
        "net_pnl",
        "commission",
    },
}
ORDER_STATUSES = {
    "new",
    "pending",
    "submitted",
    "accepted",
    "partially_filled",
    "filled",
    "cancelled",
    "canceled",
    "rejected",
    "expired",
}
MAX_POINTS = 5000
MAX_TABLE_ROWS = 500
MAX_OFFSET = 100_000
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_USAGE_CALLS = 10_000
WARNINGS = [
    "Native strategy and engine share a process; these snapshots are not an independently trusted ledger or proof against look-ahead.",
    "Native optimization uses train and validation as selection data. Final holdout execution requires a separately frozen selection; portable execution uses its separate workflow.",
    "Metrics describe the full persisted evaluation window, not a filtered chart or table slice.",
    "Missing benchmark, internal decisions, and fee component breakdown are not inferred.",
]


def research_capability_metadata(settings, owner_id=None):
    from packages.tradegrad_adapter.environment import environment_digest
    from packages.tradegrad_adapter.model_profiles import available_profiles

    profiles = []
    for task in ("cn_time", "us_time", "cn_cross", "us_cross"):
        for profile in ("source_compat", "corrected_explicit_config"):
            cold = profile == "source_compat"
            profiles.append(
                ProfileDefault(
                    task=task,
                    profile=profile,
                    market_profile=MarketConfig.for_market(task[:2].upper(), profile),
                    warmup=WarmupConfig(
                        sessions=0 if cold else (20 if task.endswith("_time") else 3),
                        history_policy="source_cold_start" if cold else "require",
                    ),
                )
            )
    models = available_profiles(settings, owner_id)
    return ResearchCapabilities(
        model_profiles=models,
        model_providers=list(dict.fromkeys(item["config"].provider for item in models)),
        models=list(dict.fromkeys(item["config"].model for item in models)),
        live_model_calls=any(not item["synthetic"] for item in models),
        environment_digest=environment_digest(),
        profile_defaults=profiles,
        limits={
            key: getattr(settings, key)
            for key in (
                "max_active_runs_per_user",
                "max_active_imports_per_user",
                "max_upload_bytes",
                "max_artifact_bytes",
            )
        },
    )


def preflight_research(session, owner_id, body, settings):
    dataset, _strategy, _universe, reports = validate_research_submission(
        session, owner_id, body, settings
    )
    warnings = list(
        dict.fromkeys(warning for report in reports for warning in report.warnings)
    )
    warnings.append(
        "Preflight is read-only and reserves no capacity. Submission revalidates inputs and the current active-run quota."
    )
    if body.benchmark is not None:
        warnings.append(
            "Selected benchmark has exact train/validation session coverage and compatible raw-close semantics. Holdout benchmark coverage is checked only by separate final admission; no benchmark prices enter strategy or optimizer inputs."
        )
    if body.windows.holdout is not None:
        warnings.append(
            "Configured holdout is metadata only; no final test is executed or certified unexposed."
        )
    return ResearchPreflightPublic(
        config_sha256=body.sha256,
        evaluation_windows=list(evaluation_windows(body)),
        is_synthetic=dataset.is_synthetic,
        warnings=warnings,
    )


@dataclass(frozen=True)
class VerifiedResult:
    evaluation: Evaluation
    run: ResearchRun
    dataset: DatasetVersion
    artifact: Artifact
    bundle: dict
    content: bytes


def _invalid(message="Persisted result evidence does not match its immutable identity"):
    return DomainError("result_integrity", message, 409)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Nonfinite JSON number")


def validate_persisted_result(evaluation, run, dataset, artifact, content):
    """Pure/artifact test seam. This does not authorize access; loader owns that."""
    try:
        config = research_config(run)
    except DomainError as error:
        raise _invalid(error.message) from None
    return _validate_native_persisted_result(
        evaluation,
        run,
        dataset,
        artifact,
        content,
        config=config,
        config_sha256=config.sha256,
        strategy_version_id=UUID(config.seed_strategy.id),
        allowed_windows=evaluation_windows(config),
    )


def _validate_native_persisted_result(
    evaluation,
    run,
    dataset,
    artifact,
    content,
    *,
    config,
    config_sha256,
    strategy_version_id,
    allowed_windows,
    selected_code_sha256=None,
    independent_config=None,
):
    """Shared native bytes checks, after a schema-specific binding was validated.

    Only trusted research/final loaders call this private helper. In particular a
    caller cannot turn an optimizer result into a holdout by changing a label:
    the public research validator above always derives its train/validation plan.
    """
    try:
        bounded_json(dataset.manifest, MAX_WIRE_BYTES)
        payload = evaluation.payload
        if (
            not isinstance(payload, dict)
            or payload_hash(payload) != evaluation.payload_sha256
        ):
            raise ValueError("Evaluation payload checksum mismatch")
        reference = payload.get("result_bundle")
        if (
            evaluation.status != "succeeded"
            or not evaluation.sandbox_stopped
            or evaluation.finished_at is None
            or payload.get("status") != "succeeded"
            or payload.get("exit_code") != 0
            or evaluation.run_id != run.id
            or evaluation.split != evaluation.window.get("split")
            or evaluation.window not in allowed_windows
            or evaluation.window_sha256 != payload_hash(evaluation.window)
            or run.request_sha256 != config_sha256
            or evaluation.image_digest != run.research_image_digest
            or payload.get("image_digest") != evaluation.image_digest
            or dataset.id != run.dataset_version_id
            or dataset.id != UUID(config.dataset.id)
            or run.strategy_version_id != strategy_version_id
            or (
                selected_code_sha256 is not None
                and evaluation.code_sha256 != selected_code_sha256
            )
            or dataset.owner_id != run.owner_id
            or dataset.content_sha256 != config.dataset.sha256
            or hashlib.sha256(canonical_bytes(dataset.manifest)).hexdigest()
            != dataset.content_sha256
            or not isinstance(reference, dict)
            or artifact.id != UUID(str(reference.get("artifact_id")))
            or artifact.sha256 != reference.get("sha256")
            or artifact.size_bytes != reference.get("size_bytes")
            or artifact.run_id != evaluation.run_id
            or artifact.attempt_id != evaluation.attempt_id
            or artifact.name != f"evaluation-{evaluation.id}-result-bundle.json"
            or artifact.media_type != "application/json"
            or not 0 < artifact.size_bytes <= MAX_WIRE_BYTES
            or len(content) != artifact.size_bytes
            or hashlib.sha256(content).hexdigest() != artifact.sha256
        ):
            raise ValueError("Result registration identity mismatch")
        bundle = json.loads(
            content, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant
        )
        if independent_config is None:
            validate_result_bundle(bundle)
        else:
            from packages.tradegrad_adapter.independent_results import (
                validate_result_bundle as validate_independent_bundle,
            )

            validate_independent_bundle(bundle)
        from packages.tradegrad_adapter.benchmarks import require_comparison_binding

        require_comparison_binding(bundle, config)
        # The runner publishes canonical bytes; never silently normalize changed bytes.
        if canonical_json_bytes(bundle) != content:
            raise ValueError("Result bytes are not the canonical persisted bundle")
        window = evaluation.window
        warmup = {
            "sessions": config.warmup.sessions,
            "history_policy": config.warmup.history_policy,
        }
        eval_config = {
            "engine_overrides": config.market_profile.engine_kwargs(),
            "window": {
                "start": window["start"],
                "end": window["end"],
                "role": window["split"],
            },
            "warmup": warmup,
            "benchmark": None,
            "run_config_sha256": config_sha256,
        }
        if config.research_capture is not None:
            eval_config["research_capture"] = config.research_capture.model_dump(
                mode="json"
            )
        if config.strategy_contract is not None:
            eval_config["strategy_contract"] = config.strategy_contract.model_dump(
                mode="json"
            )
        request = {
            "task": config.task,
            "market": config.market_profile.market,
            "profile": config.profile,
            "start_date": window["start"],
            "end_date": window["end"],
            "evaluation_config": eval_config,
        }
        if independent_config is not None:
            eval_config["result_policy"] = "independent_observation.v1"
            request["runtime_observation"] = {
                "schema_version": "tradegrad.runtime-observation-request.v1",
                "rng_restore": independent_config.rng_restore,
            }
        identity = bundle["identity"]
        expected_identity = {
            "strategy_sha256": evaluation.code_sha256,
            "run_config_sha256": config_sha256,
            "evaluation_config_sha256": hashlib.sha256(
                canonical_json_bytes(eval_config)
            ).hexdigest(),
            "input_manifest_sha256": identity.get("input_manifest_sha256"),
            "inputs": identity.get("inputs"),
            "task": config.task,
            "market": config.market_profile.market,
            "profile": config.profile,
        }
        expected_window = {
            **eval_config["window"],
            "interval": "half_open",
            "warmup": warmup,
            "engine_start_time": window["start"],
            "engine_end_time": str(
                date.fromisoformat(window["end"]) - timedelta(days=1)
            ),
            "engine_boundary_adapter": "inclusive_daily_end_previous_calendar_date.v1",
        }
        inputs = {entry["path"]: entry for entry in identity.get("inputs", [])}
        request_bytes = canonical_bytes(request)
        if (
            identity != expected_identity
            or not inputs
            or inputs.get("strategy.py", {}).get("sha256") != evaluation.code_sha256
            or inputs.get("request.json", {}).get("sha256")
            != hashlib.sha256(request_bytes).hexdigest()
            or inputs.get("request.json", {}).get("bytes") != len(request_bytes)
            or bundle["window"] != expected_window
            or bundle["engine"]["currency"] != config.market_profile.currency
            or bundle["engine"]["requested_kwargs"]
            != {**config.market_profile.engine_kwargs(), "show_progress": False}
            or bundle["legacy_performance"]["value"] != payload.get("metrics")
        ):
            raise ValueError(
                "ResultBundle differs from pinned config, code, inputs or window"
            )
        return bundle
    except DomainError as error:
        raise _invalid(error.message) from None
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        raise _invalid() from None


def load_owned_result(session, evaluation_id, owner_id, settings):
    # Authorization happens before artifact lookup or filesystem access. No foreign
    # status/artifact existence is disclosed even for malformed or failed results.
    pair = session.execute(
        select(Evaluation, ResearchRun)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .where(Evaluation.id == evaluation_id, ResearchRun.owner_id == owner_id)
    ).one_or_none()
    if pair is None:
        raise DomainError("not_found", "Research evaluation not found", 404)
    evaluation, run = pair
    if evaluation.status != "succeeded" or evaluation.finished_at is None:
        raise DomainError(
            "result_unavailable",
            "Only completed successful evaluations have verified ResultBundle views",
            409,
        )
    try:
        artifact_id = UUID(evaluation.payload["result_bundle"]["artifact_id"])
    except (KeyError, ValueError, TypeError):
        raise _invalid() from None
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.id == artifact_id,
            Artifact.run_id == run.id,
            Artifact.attempt_id == evaluation.attempt_id,
        )
    )
    dataset = session.scalar(
        select(DatasetVersion).where(
            DatasetVersion.id == run.dataset_version_id,
            DatasetVersion.owner_id == owner_id,
        )
    )
    if artifact is None or dataset is None:
        raise _invalid()
    content = read_verified(
        settings.storage_root,
        artifact.storage_key,
        artifact.sha256,
        min(settings.max_artifact_bytes, MAX_WIRE_BYTES),
    )
    bundle = validate_persisted_result(evaluation, run, dataset, artifact, content)
    from backend.app.benchmark_services import verify_persisted_benchmark

    verify_persisted_benchmark(
        session, owner_id, research_config(run), dataset.manifest, bundle, settings
    )
    return VerifiedResult(evaluation, run, dataset, artifact, bundle, content)


def min_max_rows(rows, max_points):
    """Select original rows, preserving endpoints and each bucket's extrema.

    No averages, interpolation, reordering or numeric rounding. The returned
    count never exceeds max_points. All-null buckets retain their boundaries.
    """
    if type(max_points) is not int or not 4 <= max_points <= MAX_POINTS:
        raise DomainError(
            "invalid_result_query", "max_points must be between 4 and 5000", 422
        )
    if len(rows) <= max_points:
        return rows[:]
    buckets = (max_points - 2) // 2
    chosen = {0, len(rows) - 1}
    interior = len(rows) - 2
    for bucket in range(buckets):
        first = 1 + bucket * interior // buckets
        last = 1 + (bucket + 1) * interior // buckets
        indexes = [i for i in range(first, last) if rows[i][1] is not None]
        if indexes:
            chosen.add(min(indexes, key=lambda i: rows[i][1]))
            chosen.add(max(indexes, key=lambda i: rows[i][1]))
        else:
            chosen.update((first, last - 1))
    return [rows[index] for index in sorted(chosen)]


def query_result_table(
    table,
    section,
    *,
    start=None,
    end=None,
    symbol=None,
    max_points=1000,
    offset=0,
    limit=100,
    timezone="UTC",
    sort_by=None,
    sort_direction="asc",
    side=None,
    order_status=None,
):
    if (
        section not in SECTIONS
        or not 0 <= offset <= MAX_OFFSET
        or not 1 <= limit <= MAX_TABLE_ROWS
    ):
        raise DomainError(
            "invalid_result_query",
            "Result section or pagination bounds are invalid",
            422,
        )
    if start is not None and end is not None and start >= end:
        raise DomainError(
            "invalid_result_query", "start must precede exclusive end", 422
        )
    columns = [item["name"] for item in table["columns"]]
    allowed = SORT_COLUMNS.get(section, set()) & set(columns)
    if (
        (sort_by is not None and sort_by not in allowed)
        or sort_direction not in {"asc", "desc"}
        or (sort_by is None and sort_direction != "asc")
    ):
        raise DomainError(
            "invalid_result_query",
            "Unsupported sort column/direction for this result section",
            422,
        )
    if side is not None and (
        section not in {"orders", "fills"} or side not in {"buy", "sell"}
    ):
        raise DomainError(
            "invalid_result_query",
            "Side filtering supports only orders/fills and buy/sell",
            422,
        )
    if order_status is not None and (
        section != "orders" or order_status not in ORDER_STATUSES
    ):
        raise DomainError(
            "invalid_result_query", "Unsupported order status filter", 422
        )
    field = "timestamp" if section in SERIES else TIME_COLUMNS[section]
    rows = table["rows"]
    # Empty native positions are a supported no-observation case with no columns.
    time_index = columns.index(field) if rows else None
    symbol_index = (
        columns.index("symbol")
        if rows and section not in SERIES and symbol is not None
        else None
    )
    zone = ZoneInfo(timezone)
    filtered = []
    original_indices = {}
    for index, row in enumerate(rows):
        original_indices[id(row)] = index
        day = datetime.fromisoformat(row[time_index]).astimezone(zone).date()
        if start is not None and day < start or end is not None and day >= end:
            continue
        if symbol_index is not None and row[symbol_index] != symbol:
            continue
        if side is not None and row[columns.index("side")] != side:
            continue
        if order_status is not None and row[columns.index("status")] != order_status:
            continue
        filtered.append(row)
    if sort_by is not None:
        sort_index = columns.index(sort_by)
        nonnull = [row for row in filtered if row[sort_index] is not None]
        nulls = [row for row in filtered if row[sort_index] is None]

        def sort_key(row):
            value = row[sort_index]
            if sort_by in {
                "date",
                "created_at",
                "updated_at",
                "timestamp",
                "entry_time",
                "exit_time",
            }:
                return (0, datetime.fromisoformat(value))
            return (0, value) if type(value) in (int, float) else (1, str(value))

        filtered = (
            sorted(nonnull, key=sort_key, reverse=sort_direction == "desc") + nulls
        )
    if section in SERIES:
        # Charts use the entire requested date window; table offset/limit do not
        # silently cut the chart before downsampling and lose extrema.
        selected = min_max_rows(filtered, max_points)
        output_offset, output_limit = 0, max_points
    else:
        selected = filtered[offset : offset + limit]
        output_offset, output_limit = offset, limit
    sampled = section in SERIES and len(selected) < len(filtered)
    return ResultTable(
        columns=table["columns"],
        row_indices=[original_indices[id(row)] for row in selected],
        sortable_columns=sorted(allowed),
        sort_by=sort_by,
        sort_direction=sort_direction,
        side=side,
        order_status=order_status,
        rows=selected,
        total_rows=len(rows),
        filtered_rows=len(filtered),
        returned_rows=len(selected),
        offset=output_offset,
        limit=output_limit,
        downsampled=sampled,
        sampling="min_max" if sampled else "none",
        filter_status="not_requested"
        if symbol is None
        else (
            "portfolio_series_unfiltered_by_listing" if section in SERIES else "applied"
        ),
    )


def comparison_item(result):
    bundle, item = result.bundle, result.evaluation
    semantics = result.dataset.manifest.get("price_semantics")
    declared = (
        isinstance(semantics, dict)
        and semantics.get("native_mode") not in (None, "", "unknown")
        and semantics.get("split_basis") in ("adjusted", "unadjusted")
        and semantics.get("dividend_basis") in ("adjusted", "unadjusted")
    )
    return ResultComparisonItem(
        evaluation_id=item.id,
        run_id=item.run_id,
        candidate_key=item.candidate_key,
        code_sha256=item.code_sha256,
        config_sha256=result.run.request_sha256,
        artifact_sha256=result.artifact.sha256,
        split=item.split,
        window=item.window,
        config=research_config(result.run),
        currency=bundle["engine"]["currency"],
        price_definition={
            "execution_policy": bundle["engine"]["resolved_execution_policy"],
            "dataset_price_semantics": semantics,
            "definition_status": "declared"
            if declared
            else "dataset_semantics_unavailable",
        },
        result_trust=bundle["result_trust"],
        is_synthetic=result.dataset.is_synthetic,
        metrics=bundle["metrics"],
    )


def result_view(
    result,
    *,
    sections=None,
    start=None,
    end=None,
    listing_id=None,
    max_points=1000,
    offset=0,
    limit=100,
    sort_by=None,
    sort_direction="asc",
    side=None,
    order_status=None,
):
    sections = sections or ["equity", "drawdown"]
    if (
        len(sections) > len(SECTIONS)
        or len(set(sections)) != len(sections)
        or any(section not in SECTIONS for section in sections)
    ):
        raise DomainError(
            "invalid_result_query", "Select distinct supported result sections", 422
        )
    if start is not None and end is not None and start >= end:
        raise DomainError(
            "invalid_result_query", "start must precede exclusive end", 422
        )
    symbol = None
    if listing_id is not None:
        mapping = result.dataset.manifest.get("evaluator_symbol_mapping", {})
        symbol = mapping.get(str(listing_id))
        if not isinstance(symbol, str) or not symbol:
            raise DomainError(
                "invalid_result_listing",
                "Listing is not part of this evaluation's immutable dataset",
                422,
            )
        if research_config(result.run).task.endswith("_time"):
            symbol = "target_symbol"
    return ResultQueryPublic(
        **comparison_item(result).model_dump(),
        engine_timezone=result.bundle["engine"]["resolved_config"]["timezone"],
        tables={
            section: query_result_table(
                result.bundle[section],
                section,
                start=start,
                end=end,
                symbol=symbol,
                max_points=max_points,
                offset=offset,
                limit=limit,
                timezone=result.bundle["engine"]["resolved_config"]["timezone"],
                sort_by=sort_by,
                sort_direction=sort_direction,
                side=side,
                order_status=order_status,
            )
            for section in sections
        },
        capabilities=result.bundle["capabilities"],
        fees=result.bundle["fees"],
        benchmark=comparison_view(
            result.bundle["benchmark"], start=start, end=end, max_points=max_points
        ),
        warnings=WARNINGS,
        listing_id=listing_id,
        start=start,
        end=end,
    )


def compare_results(results):
    if not 2 <= len(results) <= 4 or len({r.evaluation.id for r in results}) != len(
        results
    ):
        raise DomainError(
            "invalid_comparison", "Compare two to four distinct owned evaluations", 422
        )
    items = [comparison_item(result) for result in results]
    differences = []
    comparable = True

    def check(field, values, affects=True):
        nonlocal comparable
        if any(value != values[0] for value in values[1:]):
            differences.append(
                ComparisonDifference(
                    field=field,
                    affects_comparability=affects,
                    values=[
                        DifferenceValue(evaluation_id=item.evaluation_id, value=value)
                        for item, value in zip(items, values)
                    ],
                )
            )
            if affects:
                comparable = False

    for name in (
        "window",
        "currency",
        "price_definition",
        "result_trust",
        "is_synthetic",
    ):
        check(
            name,
            [
                getattr(item, name).model_dump(mode="json")
                if name == "window"
                else getattr(item, name)
                for item in items
            ],
        )
    definitions = [
        [
            {
                key: value
                for key, value in metric.model_dump().items()
                if key not in ("value", "unavailable_reason")
            }
            for metric in item.metrics
        ]
        for item in items
    ]
    check("metric_definitions", definitions)
    check(
        "engine",
        [
            {
                "image_digest": result.evaluation.image_digest,
                "name": result.bundle["engine"]["name"],
                "version": result.bundle["engine"]["version"],
                "dependencies": result.bundle["engine"].get("dependencies"),
                "resolved_config": result.bundle["engine"]["resolved_config"],
            }
            for result in results
        ],
    )
    configs = [item.config.model_dump(mode="json") for item in items]
    for field in configs[0]:
        check(
            "config." + field,
            [config[field] for config in configs],
            field
            not in {"seed_strategy", "optimizer", "budgets", "model", "prompt_version"},
        )
    check("code_sha256", [item.code_sha256 for item in items], False)
    warnings = [
        "Order follows the request, never a score ranking.",
        "A matching configuration is not proof of statistical significance or a trusted ledger.",
    ]
    if any(item.price_definition["definition_status"] != "declared" for item in items):
        comparable = False
        warnings.append(
            "Dataset price semantics are unavailable for at least one evaluation; comparability cannot be certified."
        )
    if any(metric.value is None for item in items for metric in item.metrics):
        comparable = False
        warnings.append(
            "At least one comparison metric is unavailable; no ranking is produced."
        )
    return ResultComparePublic(
        comparable=comparable,
        differences=differences,
        evaluations=items,
        warnings=warnings,
    )


def render_result_report(result):
    """Escaped, script-free report made only from verified persisted evidence."""
    item = comparison_item(result)
    escape = lambda value: html.escape(str(value), quote=True)
    metrics = "".join(
        "<tr>"
        + "".join(
            f"<td>{escape(value)}</td>"
            for value in (
                metric.name,
                "Unavailable" if metric.value is None else metric.value,
                metric.unit,
                metric.definition_version,
                metric.calculation_basis,
            )
        )
        + "</tr>"
        for metric in item.metrics
    )
    summary = {
        "evaluation_id": str(item.evaluation_id),
        "run_id": str(item.run_id),
        "candidate_key": item.candidate_key,
        "code_sha256": item.code_sha256,
        "config_sha256": item.config_sha256,
        "artifact_sha256": item.artifact_sha256,
        "window": item.window.model_dump(mode="json"),
        "currency": item.currency,
        "price_definition": item.price_definition,
        "is_synthetic": item.is_synthetic,
        "result_trust": item.result_trust,
        "rows": {name: len(result.bundle[name]["rows"]) for name in sorted(SECTIONS)},
        "fees": result.bundle["fees"],
        "benchmark": result.bundle["benchmark"],
        "capabilities": result.bundle["capabilities"],
    }
    content = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; base-uri &#39;none&#39;; form-action &#39;none&#39;">'
        "<title>TradeGrad persisted research report</title></head><body>"
        "<h1>TradeGrad persisted research report</h1>"
        + "<p>"
        + escape(
            "Synthetic demonstration data"
            if item.is_synthetic
            else "Historical research evidence, not investment advice"
        )
        + "</p>"
        + "<ul>"
        + "".join("<li>" + escape(warning) + "</li>" for warning in WARNINGS)
        + "</ul>"
        + "<h2>Provenance and assumptions</h2><pre>"
        + escape(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False))
        + "</pre>"
        + "<h2>Full-window metrics, original precision</h2><table><thead><tr><th>Name</th><th>Value</th><th>Unit</th><th>Definition</th><th>Basis</th></tr></thead><tbody>"
        + metrics
        + "</tbody></table>"
        + "<h2>Immutable RunConfig</h2><pre>"
        + escape(
            json.dumps(
                item.config.model_dump(mode="json"),
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        + "</pre>"
        + "<p>Full raw series and tables remain in the original verified JSON download. This report executes no candidate code, scripts, or remote resources.</p></body></html>"
    ).encode("utf-8")
    if len(content) > MAX_REPORT_BYTES:
        raise DomainError(
            "report_too_large",
            "Report exceeds its bounded download size; use the verified JSON download",
            409,
        )
    return content


def research_usage(session, run_id, owner_id):
    owned_research_run(session, run_id, owner_id)
    rows = session.execute(
        select(ModelCall.usage, ModelCall.adopted)
        .where(ModelCall.run_id == run_id)
        .limit(MAX_USAGE_CALLS + 1)
    ).all()
    if len(rows) > MAX_USAGE_CALLS:
        raise DomainError(
            "usage_too_large", "Persisted model usage exceeds the supported budget", 409
        )
    totals = {key: 0 for key in ("input_tokens", "output_tokens", "charged_tokens")}
    known = adopted = 0
    sources = set()
    for usage, is_adopted in rows:
        if not isinstance(usage, dict):
            raise _invalid("Invalid persisted model usage")
        present = True
        for key in totals:
            value = usage.get(key)
            if value is None:
                if key == "charged_tokens":
                    raise _invalid(
                        "Persisted model call lacks reserved or charged token accounting"
                    )
                present = False
            elif type(value) is not int or not 0 <= value <= 10**9:
                raise _invalid("Invalid persisted model token count")
            else:
                totals[key] += value
        known += int(present)
        adopted += int(is_adopted)
        source = usage.get("usage_source")
        if isinstance(source, str):
            sources.add(source[:160])
    return ResearchUsagePublic(
        run_id=run_id,
        calls=len(rows),
        adopted_calls=adopted,
        input_tokens=totals["input_tokens"] if known == len(rows) else None,
        output_tokens=totals["output_tokens"] if known == len(rows) else None,
        charged_tokens=totals["charged_tokens"],
        known_usage_calls=known,
        unknown_usage_calls=len(rows) - known,
        usage_sources=sorted(sources),
    )
