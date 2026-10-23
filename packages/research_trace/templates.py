"""Trusted-host, AST-only descriptions of the two controlled native templates.

Never import strategy source here. A RESEARCH_TEMPLATE literal is a description,
not executable rules or proof that arbitrary Python implements that description.
Only the exact bundled source receives controlled mapping and a read-only graph.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from packages.tradegrad_adapter.native_contract import NativeStrategyContract

from .contracts import Rule, SourceRef, StrategySpec

_ROOT = Path(__file__).resolve().parents[2]
_PATHS = {
    "daily_timing": "seed_strategies/research_time.py",
    "cross_sectional": "seed_strategies/research_cross.py",
}
_IDS = {"daily_timing.v1": "daily_timing", "daily_cross_section.v1": "cross_sectional"}
_CATEGORIES = {
    "universe": "universe",
    "features": "feature",
    "signal": "filter",
    "liquidity": "filter",
    "ranking": "ranking",
    "allocation": "allocation",
    "execution": "execution",
}
_UNITS = {
    "lookback": "sessions",
    "fast_window": "sessions",
    "slow_window": "sessions",
    "liquidity_enabled": "boolean",
    "min_average_dollar_volume": "quote_currency_per_session",
    "top_n": "instruments",
    "retention_rank_buffer": "rank_places",
    "max_positions": "instruments",
    "gross_target": "equity_fraction",
    "max_weight": "equity_fraction",
    "cash_reserve": "equity_fraction",
    "target_weight": "equity_fraction",
    "exit_buffer": "fraction",
}
_STAGE_IO = {
    "universe": (
        ["effective_universe_snapshot", "prior_holdings"],
        ["candidate_instruments"],
    ),
    "features": (
        ["completed_daily_close", "completed_daily_volume"],
        ["features", "feature_validity"],
    ),
    "signal": (["fast_mean", "slow_mean"], ["entry_signal"]),
    "liquidity": (
        ["average_dollar_volume", "liquidity_enabled"],
        ["liquidity_minimum"],
    ),
    "ranking": (
        ["trailing_return", "liquidity_minimum"],
        ["eligibility", "rank", "selected"],
    ),
    "allocation": (
        ["selected", "cash", "equity", "prior_holdings", "open_orders"],
        ["raw_targets", "constrained_plan"],
    ),
    "execution": (["constrained_plan"], ["logical_order_id", "child_order_ids"]),
}


def available_templates() -> tuple[str, ...]:
    return tuple(_PATHS)


def template_source(template_id: str) -> str:
    """Read a maintained template; the caller must execute only in its evaluator."""
    try:
        relative = _PATHS[template_id]
    except KeyError as error:
        raise ValueError("Unknown controlled research template") from error
    return (_ROOT / relative).read_text(encoding="utf-8")


def _inspect(source: str):
    if not isinstance(source, str) or len(source.encode()) > 262144:
        raise ValueError("Template source must fit 256 KiB")
    tree = ast.parse(source)
    metadata = None
    locations = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(name, ast.Name) and name.id == "RESEARCH_TEMPLATE"
            for name in node.targets
        ):
            if metadata is not None:
                raise ValueError("Duplicate template declaration")
            metadata = ast.literal_eval(node.value)
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    locations[f"{node.name}.{member.name}"] = (
                        member.lineno,
                        member.end_lineno,
                    )
    if not isinstance(metadata, dict) or set(metadata) != {
        "template_id",
        "strategy_symbol",
        "mode",
        "parameters",
        "rules",
    }:
        raise ValueError("Missing or invalid literal template metadata")
    if metadata["mode"] != "native" or metadata["template_id"] not in _IDS:
        raise ValueError("Unsupported template declaration")
    if not isinstance(metadata["parameters"], dict) or not isinstance(
        metadata["rules"], list
    ):
        raise TypeError("Invalid template parameter/rule metadata")
    return metadata, locations


def template_spec(
    template_id: str,
    *,
    source: str | None = None,
    expected_code_sha256: str | None = None,
    parameters: dict | None = None,
) -> StrategySpec:
    """Build hash-bound anchors without eval, exec, import or model calls.

    Context-bound parameters must be from the selected immutable version's native
    contract. Edits to code never silently reuse a controlled graph or old anchors.
    """
    bundled = template_source(template_id)
    source = bundled if source is None else source
    code_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if expected_code_sha256 is not None and code_hash != expected_code_sha256:
        raise ValueError("Source differs from the selected immutable code hash")
    metadata, locations = _inspect(source)
    if _IDS[metadata["template_id"]] != template_id:
        raise ValueError("Source declares a different template")
    values = native_contract(
        template_id, parameters={**metadata["parameters"], **(parameters or {})}
    ).parameter_values()
    controlled = source == bundled
    rules = []
    for item in metadata["rules"]:
        if not isinstance(item, dict) or set(item) != {
            "stage_id",
            "symbol",
            "description",
        }:
            raise ValueError("Invalid literal rule declaration")
        stage, symbol = item["stage_id"], item["symbol"]
        if stage not in _CATEGORIES or symbol not in locations:
            raise ValueError("Rule source symbol or stage is unavailable")
        start, end = locations[symbol]
        inputs, outputs = _STAGE_IO[stage]
        rules.append(
            Rule(
                rule_id=stage,
                category=_CATEGORIES[stage],
                title=stage.replace("_", " ").title(),
                description=item["description"],
                parameters=values,
                units=_UNITS,
                inputs=inputs,
                outputs=outputs,
                source_ref=SourceRef(
                    code_sha256=code_hash,
                    file="strategy.py",
                    symbol=symbol,
                    line_start=start,
                    line_end=end,
                ),
                source="static_inference",
                stage_id=stage,
                mapping="controlled" if controlled else "inferred",
            )
        )
    edges = (
        (
            [
                ("features", "signal"),
                ("features", "liquidity"),
                ("signal", "allocation"),
                ("liquidity", "allocation"),
                ("allocation", "execution"),
            ]
            if template_id == "daily_timing"
            else [
                ("universe", "features"),
                ("features", "liquidity"),
                ("features", "ranking"),
                ("liquidity", "ranking"),
                ("ranking", "allocation"),
                ("allocation", "execution"),
            ]
        )
        if controlled
        else []
    )
    return StrategySpec(
        code_sha256=code_hash,
        template_id=template_id if controlled else None,
        rules=rules,
        edges=edges,
        coverage="complete" if controlled else "partial",
        missing_reasons=[]
        if controlled
        else [
            "Source differs from the maintained controlled template; literal rule declarations are inferred descriptions, not verified executable semantics."
        ],
        revision=1,
    )


def native_contract(
    template_id: str, *, parameters: dict | None = None
) -> NativeStrategyContract:
    """Bounded parameter declarations for immutable native versions, not execution."""
    metadata, _ = _inspect(template_source(template_id))
    defaults = metadata["parameters"]
    overrides = {} if parameters is None else dict(parameters)
    if set(overrides) - set(defaults):
        raise ValueError("Unknown controlled template parameter")
    values = {**defaults, **overrides}
    declarations = []
    for name, default in defaults.items():
        value = values[name]
        if type(value) is not type(default) and not (
            type(default) is float and type(value) is int
        ):
            raise ValueError("Controlled template parameter has the wrong JSON type")
        if type(default) is bool:
            item = {"name": name, "kind": "boolean", "value": value}
        else:
            minimum = (
                2
                if name == "lookback"
                else 1
                if name in {"fast_window", "slow_window", "top_n", "max_positions"}
                else 0
            )
            maximum = (
                252
                if type(default) is int
                else 1e12
                if name == "min_average_dollar_volume"
                else 1
            )
            item = {
                "name": name,
                "kind": "integer" if type(default) is int else "number",
                "value": value,
                "minimum": float(minimum),
                "maximum": float(maximum),
            }
        declarations.append(item)
    lookback = (
        values["lookback"]
        if template_id == "cross_sectional"
        else values["slow_window"]
    )
    if template_id == "daily_timing" and values["fast_window"] >= values["slow_window"]:
        raise ValueError("fast_window must be less than slow_window")
    return NativeStrategyContract.model_validate(
        {
            "lookback_bars": lookback,
            "warmup_bars": lookback,
            "requires_shorting": False,
            "required_fields": ["open", "high", "low", "close", "volume"],
            "parameters": declarations,
        }
    )
