"""Explicit synthetic ResultBundle evidence for pure/API-PostgreSQL boundary tests.

This never invokes an engine and must not be described as native-engine evidence.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from backend.app.models import Artifact, DatasetVersion, Evaluation, ResearchRun
from backend.app.research_services import payload_hash
from backend.app.research_views import VerifiedResult
from packages.tradegrad_adapter.config import (
    MarketConfig,
    RunConfig,
    canonical_bytes,
    evaluation_windows,
)
from packages.tradegrad_adapter.results import (
    BUNDLE_VERSION,
    SECTIONS,
    TRUST,
    UNITS,
    canonical_json_bytes,
    validate_result_bundle,
)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def rehash_bundle(bundle):
    bundle["artifacts"] = [
        {
            "section": name,
            "media_type": "application/json",
            "sha256": digest(canonical_json_bytes(bundle[name])),
            "bytes": len(canonical_json_bytes(bundle[name])),
        }
        for name in SECTIONS
    ]
    return bundle


def synthetic_result(
    config=None,
    *,
    owner_id=None,
    run_id=None,
    attempt_id=None,
    evaluation_id=None,
    dataset_manifest=None,
    evaluation_window=None,
    binding_config_sha256=None,
    binding_strategy=None,
):
    owner_id, run_id, attempt_id, evaluation_id = (
        owner_id or uuid4(),
        run_id or uuid4(),
        attempt_id or uuid4(),
        evaluation_id or uuid4(),
    )
    listing_id = uuid4()
    dataset_manifest = dataset_manifest or {
        "evaluator_symbol_mapping": {str(listing_id): "INDEX"},
        "price_semantics": {
            "native_mode": "unadjusted",
            "split_basis": "unadjusted",
            "dividend_basis": "unadjusted",
        },
    }
    if config is None:
        config = RunConfig.model_validate(
            {
                "execution_mode": "optimization",
                "task": "cn_time",
                "dataset": {
                    "id": str(uuid4()),
                    "sha256": digest(canonical_bytes(dataset_manifest)),
                },
                "seed_strategy": {"id": str(uuid4()), "sha256": "b" * 64},
                "universe": {"id": str(uuid4()), "sha256": "c" * 64},
                "windows": {
                    "train": {"start": "2024-02-01", "end": "2024-02-10"},
                    "validation": {"start": "2024-02-15", "end": "2024-02-20"},
                },
                "warmup": {"sessions": 20},
                "market_profile": MarketConfig.for_market("CN").model_dump(mode="json"),
                "environment_digest": "d" * 64,
            }
        )
    window = evaluation_window or evaluation_windows(config)[0]
    binding_hash = binding_config_sha256 or config.sha256
    selected = binding_strategy or config.seed_strategy
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
        "run_config_sha256": binding_hash,
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
    request_bytes = canonical_bytes(request)
    inputs = [
        {"path": "strategy.py", "sha256": selected.sha256, "bytes": 12},
        {
            "path": "request.json",
            "sha256": digest(request_bytes),
            "bytes": len(request_bytes),
        },
    ]
    identity = {
        "strategy_sha256": selected.sha256,
        "run_config_sha256": binding_hash,
        "evaluation_config_sha256": digest(canonical_json_bytes(eval_config)),
        "input_manifest_sha256": digest(canonical_json_bytes(inputs)),
        "inputs": inputs,
        "task": config.task,
        "market": config.market_profile.market,
        "profile": config.profile,
    }
    zone = (
        "Asia/Shanghai" if config.market_profile.market == "CN" else "America/New_York"
    )
    offset = "+08:00" if config.market_profile.market == "CN" else "-05:00"
    times = [
        (datetime.fromisoformat(window["start"]) + timedelta(days=i)).strftime(
            "%Y-%m-%dT10:00:00"
        )
        + offset
        for i in range(
            min(
                8,
                (
                    datetime.fromisoformat(window["end"])
                    - datetime.fromisoformat(window["start"])
                ).days,
            )
        )
    ]
    stamp = times[0]

    def table(names, rows):
        return {
            "columns": [
                {
                    "name": name,
                    "dtype": "object"
                    if UNITS.get(name, "label") in ("label", "timestamp_iso8601")
                    else "float64",
                    "unit": UNITS.get(name, "label"),
                }
                for name in names
            ],
            "rows": rows,
        }

    def metric(name, value, unit, definition="tradegrad.metrics.v1"):
        return {
            "name": name,
            "value": value,
            "unit": unit,
            "definition_version": definition,
            "calculation_basis": "Explicit synthetic boundary fixture, not engine evidence",
            "unavailable_reason": None,
        }

    units = {
        "sharpe_ratio": "dimensionless",
        "annualized_return": "percent",
        "max_drawdown": "percent",
        "trade_per_day": "closed_trades_per_estimated_trading_day",
        "win_rate": "percent",
        "return_per_trade": "percent",
    }
    metrics = [metric(name, 1.123456789123, unit) for name, unit in units.items()] + [
        metric("combined_score", 50.0, "score_0_100")
    ]
    legacy = {
        "combined_score": 50.0,
        "metrics": {name: 1.12 for name in units},
        "market": config.market_profile.market,
        "profile": config.profile,
    }
    equity = [
        100.123456789,
        103.987654321,
        92.123456789,
        101.2,
        110.4,
        105.6,
        104.7,
        108.8,
    ][: len(times)]
    peak = equity[0]
    drawdown = []
    for value in equity:
        peak = max(peak, value)
        drawdown.append(1 - value / peak)
    bundle = {
        "schema_version": BUNDLE_VERSION,
        "evaluation_mode": "native_akquant",
        "result_trust": TRUST,
        "identity": identity,
        "window": {
            **eval_config["window"],
            "interval": "half_open",
            "warmup": warmup,
            "engine_start_time": window["start"],
            "engine_end_time": str(
                datetime.fromisoformat(window["end"]).date() - timedelta(days=1)
            ),
            "engine_boundary_adapter": "inclusive_daily_end_previous_calendar_date.v1",
        },
        "engine": {
            "name": "akquant",
            "version": "0.3.27",
            "requested_kwargs": {
                **config.market_profile.engine_kwargs(),
                "show_progress": False,
            },
            "resolved_execution_policy": {
                "price_basis": "open",
                "bar_offset": 1,
                "temporal": "same_cycle",
                "source": "legacy",
            },
            "resolved_config": {"timezone": zone},
            "currency": config.market_profile.currency,
        },
        "metrics": metrics,
        "native_metrics": [
            metric("closed_trade_count", 1, "count", "akquant.0.3.27.metrics_df"),
            metric("execution_count", 1, "count", "akquant.0.3.27.metrics_df"),
            metric(
                "total_commission", 1, "account_currency", "akquant.0.3.27.metrics_df"
            ),
        ],
        "legacy_performance": {"value": legacy},
        "equity": table(
            ["timestamp", "equity"], [[t, v] for t, v in zip(times, equity)]
        ),
        "drawdown": table(
            ["timestamp", "drawdown"], [[t, v] for t, v in zip(times, drawdown)]
        ),
        "cash": table(["timestamp", "cash"], [[t, 90.123456789] for t in times]),
        "margin": table(["timestamp", "margin"], [[t, 0.0] for t in times]),
        "positions": table(
            ["date", "symbol", "long_shares", "short_shares", "equity", "close"],
            [[stamp, "target_symbol", 1, 0, 100.0, 10.0]],
        ),
        "orders": table(
            [
                "id",
                "symbol",
                "side",
                "quantity",
                "filled_quantity",
                "created_at",
                "updated_at",
                "commission",
                "status",
                "reject_reason",
            ],
            [
                [
                    "order-1",
                    "target_symbol",
                    "buy",
                    1,
                    1,
                    stamp,
                    stamp,
                    1,
                    "filled",
                    None,
                ]
            ],
        ),
        "fills": table(
            [
                "id",
                "order_id",
                "symbol",
                "side",
                "quantity",
                "price",
                "commission",
                "timestamp",
            ],
            [["fill-1", "order-1", "target_symbol", "buy", 1, 10.123456789, 1, stamp]],
        ),
        "closed_trades": table(
            [
                "symbol",
                "entry_time",
                "exit_time",
                "quantity",
                "pnl",
                "net_pnl",
                "commission",
            ],
            [["target_symbol", stamp, stamp, 1, 10, 9, 1]],
        ),
        "fees": {
            "unit": config.market_profile.currency,
            "definition_version": "akquant.0.3.27.execution_commission",
            "total_from_fills": 1,
            "native_total_commission": 1,
            "calculation_basis": "Synthetic test fixture",
            "commission_stamp_transfer_breakdown": None,
            "breakdown_status": "unsupported_by_execution_report",
            "slippage_cost": None,
            "slippage_cost_status": "not_separately_reported_by_native_engine",
        },
        "benchmark": {
            "status": "not_configured",
            "series": None,
            "reason": "Not configured",
        },
        "decision_trace": {
            "scope": "observable_native_orders_only",
            "portable_contract": False,
            "input_snapshot_sha256": identity["input_manifest_sha256"],
            "input_snapshot_scope": "staged_input_artifacts_not_asof_strategy_view",
            "strategy_sha256": identity["strategy_sha256"],
            "decisions": [
                {
                    "observed_at": stamp,
                    "order_id": "order-1",
                    "orders_row": 0,
                    "reject_reason": None,
                }
            ],
            "scores": None,
            "signals": None,
            "target_portfolio": None,
            "state_digest": None,
            "unobservable_reason": "Not observed",
        },
        "model_usage": {
            "status": "not_applicable_to_evaluator",
            "calls": [],
            "cost": None,
        },
        "capabilities": {
            **{
                name: True
                for name in (
                    "equity",
                    "drawdown",
                    "positions",
                    "orders",
                    "fills",
                    "closed_trades",
                    "fees_aggregate",
                    "observable_order_trace",
                )
            },
            **{
                name: False
                for name in (
                    "fee_component_breakdown",
                    "benchmark",
                    "internal_decisions",
                    "trusted_ledger",
                )
            },
        },
    }
    validate_result_bundle(rehash_bundle(bundle))
    content = canonical_json_bytes(bundle)
    artifact = Artifact(
        id=uuid4(),
        run_id=run_id,
        attempt_id=attempt_id,
        name=f"evaluation-{evaluation_id}-result-bundle.json",
        media_type="application/json",
        storage_key=f"results/{evaluation_id}.json",
        sha256=digest(content),
        size_bytes=len(content),
    )
    payload = {
        "status": "succeeded",
        "exit_code": 0,
        "image_digest": "sha256:" + "a" * 64,
        "metrics": legacy,
        "result_bundle": {
            "artifact_id": str(artifact.id),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        },
    }
    run = ResearchRun(
        id=run_id,
        owner_id=owner_id,
        request_sha256=binding_hash,
        config=config.model_dump(mode="json"),
        dataset_version_id=UUID(config.dataset.id),
        strategy_version_id=UUID(selected.id),
        research_image_digest=payload["image_digest"],
    )
    evaluation = Evaluation(
        id=evaluation_id,
        run_id=run_id,
        attempt_id=attempt_id,
        generation=1,
        candidate_key="candidate-0000",
        code_sha256=selected.sha256,
        split=window["split"],
        window=window,
        window_sha256=payload_hash(window),
        image_digest=payload["image_digest"],
        sandbox_id=f"synthetic-{evaluation_id}",
        sandbox_stopped=True,
        status="succeeded",
        payload=payload,
        payload_sha256=payload_hash(payload),
        finished_at=datetime.now(UTC),
    )
    dataset = DatasetVersion(
        id=UUID(config.dataset.id),
        owner_id=owner_id,
        content_sha256=config.dataset.sha256,
        is_synthetic=True,
        manifest=dataset_manifest,
    )
    return VerifiedResult(evaluation, run, dataset, artifact, bundle, content)
