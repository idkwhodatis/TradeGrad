"""Strict data-only portable result boundary; no SDK/pandas/AKQuant imports.

A valid shape is not execution certification. The supervisor must additionally
verify trusted-image provenance, source-free engine mounts, input identity and
confirmed termination of both containers before persisting a bundle.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

MAX_WIRE_BYTES = 8 * 1024 * 1024
VERSION = "tradegrad.portable-result.v1"
TRUST = "isolated_policy_trusted_akquant_engine"


def _keys(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError(f"invalid portable {label} fields")


def _number(value, *, minimum=None):
    try:
        valid = (
            type(value) in {int, float}
            and math.isfinite(value)
            and (minimum is None or value >= minimum)
        )
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        raise ValueError("invalid finite portable number")
    return value


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("invalid portable checksum")
    return value


def _text(value):
    if type(value) is not str or not value:
        raise ValueError("missing portable string")
    return value


def _timestamp(value):
    if type(value) is not str:
        raise ValueError("invalid portable timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid portable timestamp") from exc
    if result.tzinfo is None:
        raise ValueError("portable timestamp must carry timezone")
    return result


def _day(value):
    if type(value) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("invalid portable session date")
    return date.fromisoformat(value)


def _tree(
    value, *, max_nodes=500_000, max_depth=24, max_items=100_000, max_string=65_536
):
    count = 0

    def visit(v, depth=0):
        nonlocal count
        count += 1
        if count > max_nodes or depth > max_depth:
            raise ValueError("portable result complexity exceeded")
        if v is None or type(v) is bool:
            return
        if type(v) in {float, int}:
            _number(v)
            if type(v) is int and abs(v) > 2**53 - 1:
                raise ValueError("large integers must use lossless decimal strings")
        elif type(v) is str:
            if len(v) > max_string:
                raise ValueError("portable result string exceeded")
            v.encode("utf-8", errors="strict")
        elif type(v) in {list, dict}:
            if len(v) > max_items:
                raise ValueError("portable result collection exceeded")
            if type(v) is dict:
                if any(type(k) is not str for k in v):
                    raise ValueError("portable result keys must be strings")
                for k, item in v.items():
                    visit(k, depth + 1)
                    visit(item, depth + 1)
            else:
                for item in v:
                    visit(item, depth + 1)
        else:
            raise ValueError("only plain JSON result values accepted")

    visit(value)


def canonical_json_bytes(value) -> bytes:
    _tree(value)
    result = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if len(result) > MAX_WIRE_BYTES:
        raise ValueError("portable result byte budget exceeded")
    return result


def _sha(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _same(actual, expected, label):
    if not math.isclose(
        _number(actual), _number(expected), rel_tol=1e-10, abs_tol=1e-7
    ):
        raise ValueError(f"portable {label} does not reconcile")


def _native_time(row, ns="native_timestamp_ns", at="native_at"):
    value = row[ns]
    if type(value) is not str or re.fullmatch(r"[0-9]{1,20}", value) is None:
        raise ValueError("native nanosecond timestamp must be a decimal string")
    parsed = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=int(value) // 1000
    )
    if parsed != _timestamp(row[at]):
        raise ValueError("native timestamp was rewritten")
    return parsed


def _state(value):
    _keys(
        value,
        {
            "codec_version",
            "schema_name",
            "schema_version",
            "schema_sha256",
            "payload_json",
            "payload_sha256",
        },
        "state",
    )
    if (
        value["codec_version"] != "state-json.v1"
        or type(value["schema_version"]) is not int
        or value["schema_version"] < 1
    ):
        raise ValueError("unsupported portable state")
    _text(value["schema_name"])
    _hash(value["schema_sha256"])
    payload = parse_portable_json(value["payload_json"].encode())
    if (
        type(payload) is not dict
        or canonical_json_bytes(payload).decode() != value["payload_json"]
        or _sha(payload) != _hash(value["payload_sha256"])
    ):
        raise ValueError("portable state checksum mismatch")


def parse_portable_json(data: bytes):
    if type(data) is not bytes or not data or len(data) > MAX_WIRE_BYTES:
        raise ValueError("invalid portable JSON transport")

    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate portable JSON field")
            obj[key] = value
        return obj

    try:
        value = json.loads(
            data,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("nonfinite portable JSON")
            ),
        )
        _tree(value)
        return value
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid portable JSON") from exc


def _state_schema(schema):
    _keys(schema, {"name", "version", "fields"}, "state schema")
    _text(schema["name"])
    if (
        type(schema["version"]) is not int
        or not 1 <= schema["version"] <= 1_000_000
        or type(schema["fields"]) is not list
        or len(schema["fields"]) > 64
    ):
        raise ValueError("invalid state schema")
    names = set()
    for item in schema["fields"]:
        _keys(item, {"name", "kind", "required"}, "state field")
        if (
            _text(item["name"]) in names
            or item["kind"]
            not in {"string", "integer", "number", "boolean", "object", "array", "null"}
            or type(item["required"]) is not bool
        ):
            raise ValueError("invalid state field")
        names.add(item["name"])
    for name in (schema["name"], *names):
        if len(name) > 96 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", name) is None:
            raise ValueError("invalid state schema name")


def _authoring(value, schema):
    """Validate inert user declarations without importing the policy SDK/runtime."""
    _keys(
        value,
        {"source_kind", "parameter_schema", "initial_state", "evidence"},
        "authoring declaration",
    )
    if (
        value["source_kind"] != "user_authored"
        or value["evidence"] != "user_declared_not_code_verified"
    ):
        raise ValueError("unsupported portable authoring declaration")
    _state_schema(value["parameter_schema"])
    state = value["initial_state"]
    _state(state)
    if (state["schema_name"], state["schema_version"], state["schema_sha256"]) != (
        schema["name"],
        schema["version"],
        _sha(schema),
    ):
        raise ValueError("portable authoring initial-state schema mismatch")
    payload = parse_portable_json(state["payload_json"].encode())
    if len(state["payload_json"].encode()) > 65_536:
        raise ValueError("portable authoring initial-state byte limit exceeded")
    _tree(payload, max_nodes=8192, max_depth=12, max_items=1024, max_string=8192)
    fields = {field["name"]: field for field in schema["fields"]}
    types = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "object": (dict,),
        "array": (list,),
        "null": (type(None),),
    }
    if set(payload) - set(fields) or any(
        (name not in payload and field["required"])
        or (name in payload and type(payload[name]) not in types[field["kind"]])
        for name, field in fields.items()
    ):
        raise ValueError("portable authoring initial-state fields mismatch")


def _manifest_details(manifest):
    expected = {
        "US": ("USD", "XNYS", "America/New_York"),
        "CN": ("CNY", "XSHG", "Asia/Shanghai"),
    }
    if (
        (manifest["currency"], manifest["calendar"], manifest["timezone"])
        != expected[manifest["market"]]
        or manifest["asset_type"] != "equity"
        or manifest["decision_timing"] != "after_close"
        or manifest["execution_timing"] != "NextOpen"
    ):
        raise ValueError("unsupported portable market/timing")
    data = manifest["data"]
    _keys(
        data,
        {
            "interval",
            "lookback_bars",
            "warmup_bars",
            "history_order",
            "bar_complete",
            "snapshot_rule",
            "missing_bar_policy",
            "max_listings",
        },
        "data requirements",
    )
    constants = {
        "interval": "1d",
        "history_order": "oldest_first",
        "bar_complete": "exchange_session_closed",
        "snapshot_rule": "all_effective_listings_complete",
        "missing_bar_policy": "reject_snapshot",
    }
    if (
        any(data[k] != v for k, v in constants.items())
        or any(
            type(data[k]) is not int
            for k in ("lookback_bars", "warmup_bars", "max_listings")
        )
        or not 1 <= data["warmup_bars"] <= data["lookback_bars"] <= 252
        or not 1 <= data["max_listings"] <= 100
    ):
        raise ValueError("unsupported portable data requirements")
    universe = manifest["universe"]
    _keys(universe, {"semantics", "snapshot_sha256", "survivorship_bias"}, "universe")
    _hash(universe["snapshot_sha256"])
    if universe["semantics"] not in {
        "user_fixed",
        "current_snapshot",
        "historical_membership",
        "point_in_time_verified",
    } or universe["survivorship_bias"] != (
        "membership_evidence_required"
        if universe["semantics"] == "point_in_time_verified"
        else "possible"
    ):
        raise ValueError("invalid universe semantics")
    prices = manifest["prices"]
    _keys(
        prices,
        {"price_basis", "corporate_actions", "actions_snapshot_sha256"},
        "prices",
    )
    if prices != {
        "price_basis": "unadjusted",
        "corporate_actions": "reject_window_with_actions",
        "actions_snapshot_sha256": _sha([]),
    }:
        raise ValueError("unsupported portable price/corporate-action semantics")
    caps = manifest["capabilities"]
    _keys(
        caps,
        {
            "order_types",
            "time_in_force",
            "quantity_unit",
            "short",
            "leverage",
            "account_mode",
            "fractional_shares",
            "oco",
            "trailing_stop",
            "close_today",
        },
        "capabilities",
    )
    if (
        type(caps["order_types"]) is not list
        or not caps["order_types"]
        or len(set(caps["order_types"])) != len(caps["order_types"])
        or not set(caps["order_types"]) <= {"market", "limit"}
        or caps["time_in_force"] != ["GTC"]
        or caps["quantity_unit"] != "shares"
        or caps["account_mode"] != "cash_long_only"
        or type(caps["leverage"]) is not int
        or caps["leverage"] != 1
        or any(
            caps[k] is not False
            for k in (
                "short",
                "fractional_shares",
                "oco",
                "trailing_stop",
                "close_today",
            )
        )
    ):
        raise ValueError("unsupported portable capability")
    _state_schema(manifest["state_schema"])
    if "authoring" in manifest:
        _authoring(manifest["authoring"], manifest["state_schema"])
    ep = manifest["engine_profile"]
    _keys(
        ep,
        {
            "engine",
            "version",
            "profile_sha256",
            "environment_sha256",
            "profile_version",
            "engine_settlement_mode",
            "application_settlement_enforcement",
        },
        "engine profile",
    )
    if (
        (ep["engine"], ep["version"]) != ("akquant", "0.3.27")
        or ep["profile_version"] != "akquant.portable.cash-daily.controller-t1.v3"
        or ep["engine_settlement_mode"] != "T+0"
        or ep["application_settlement_enforcement"]
        != "trusted_controller_daily_next_open_inventory.v1"
    ):
        raise ValueError("unsupported portable engine profile")
    _hash(ep["profile_sha256"])
    _hash(ep["environment_sha256"])
    if manifest["reference_result_sha256"] is not None:
        _hash(manifest["reference_result_sha256"])


def _decision(value, next_open):
    timing = {"signal_id", "effective_at", "expires_at"}
    if value.get("kind") == "target_portfolio":
        _keys(
            value,
            timing
            | {
                "kind",
                "action",
                "basis",
                "targets",
                "omitted_positions",
                "rebalance_rule",
                "scores",
            },
            "target decision",
        )
        if (
            value["action"] not in {"rebalance", "hold", "liquidate", "no_decision"}
            or value["basis"]
            not in {"target_weight", "target_quantity", "target_notional"}
            or value["omitted_positions"] not in {"preserve", "liquidate"}
            or value["rebalance_rule"] != "next_open"
        ):
            raise ValueError("unsupported target decision")
        for key in ("targets", "scores"):
            if type(value[key]) is not list or len(value[key]) > 100:
                raise ValueError("invalid target collection")
            seen = set()
            for item in value[key]:
                _keys(item, {"listing_id", "value"}, "target/score")
                if _text(item["listing_id"]) in seen:
                    raise ValueError("duplicate target listing")
                seen.add(item["listing_id"])
                _number(item["value"], minimum=0 if key == "targets" else None)
        if (
            (value["action"] == "rebalance") != bool(value["targets"])
            or (
                value["action"] in {"hold", "no_decision"}
                and value["omitted_positions"] != "preserve"
            )
            or (
                value["action"] == "liquidate"
                and value["omitted_positions"] != "liquidate"
            )
        ):
            raise ValueError("ambiguous empty target semantics")
        if (
            value["basis"] == "target_weight"
            and sum(t["value"] for t in value["targets"]) > 1 + 1e-12
        ):
            raise ValueError("leveraged target weights")
        intents = [value]
    else:
        _keys(value, {"kind", "intents"}, "event decision")
        if (
            value["kind"] != "event"
            or type(value["intents"]) is not list
            or len(value["intents"]) > 100
        ):
            raise ValueError("invalid event intents")
        intents = value["intents"]
        for item in intents:
            if item.get("kind") == "cancel":
                _keys(
                    item, timing | {"kind", "order_id", "listing_id"}, "cancel intent"
                )
                _text(item["order_id"])
            else:
                _keys(
                    item,
                    timing
                    | {
                        "kind",
                        "listing_id",
                        "side",
                        "quantity",
                        "quantity_unit",
                        "order_type",
                        "limit_price",
                        "stop_price",
                        "time_in_force",
                        "reduce_only",
                        "position_effect",
                    },
                    "order intent",
                )
                if (
                    item["kind"] != "order"
                    or item["side"] not in {"buy", "sell"}
                    or item["order_type"] not in {"market", "limit"}
                    or item["time_in_force"] != "GTC"
                    or item["quantity_unit"] != "shares"
                    or item["stop_price"] is not None
                    or item["reduce_only"] is not (item["side"] == "sell")
                    or item["position_effect"]
                    != ("close" if item["side"] == "sell" else "open")
                ):
                    raise ValueError("unsupported event order")
                if (item["limit_price"] is None) != (item["order_type"] == "market"):
                    raise ValueError("invalid event limit")
                if item["limit_price"] is not None:
                    _number(item["limit_price"], minimum=1e-12)
                qty = _number(item["quantity"], minimum=1)
                if qty != int(qty):
                    raise ValueError("fractional event order")
            _text(item["listing_id"])
    ids = set()
    for item in intents:
        if (
            _text(item["signal_id"]) in ids
            or _timestamp(item["effective_at"]) != _timestamp(next_open)
            or item["expires_at"] is not None
        ):
            raise ValueError("invalid signal identity/timing")
        ids.add(item["signal_id"])
    return ids


def _manifest(manifest, identity):
    _keys(
        manifest,
        {
            "contract_version",
            "strategy_kind",
            "strategy_id",
            "code_sha256",
            "parameters_sha256",
            "market",
            "asset_type",
            "currency",
            "calendar",
            "timezone",
            "data",
            "decision_timing",
            "execution_timing",
            "universe",
            "prices",
            "capabilities",
            "state_schema",
            "engine_profile",
            "reference_result_sha256",
            "conformance",
            "external_framework_equivalence",
        }
        | ({"authoring"} if "authoring" in manifest else set()),
        "manifest",
    )
    if (
        _sha(manifest) != identity["manifest_sha256"]
        or manifest["code_sha256"] != identity["strategy_sha256"]
        or manifest["parameters_sha256"] != identity["parameters_sha256"]
    ):
        raise ValueError("portable manifest identity mismatch")
    if (
        manifest["contract_version"] != "portable.v1"
        or manifest["strategy_kind"] not in {"portfolio", "event"}
        or manifest["market"] not in {"CN", "US"}
        or manifest["conformance"] != "interface_only"
        or manifest["external_framework_equivalence"] != "not_certified"
    ):
        raise ValueError("unsupported portable manifest")
    _manifest_details(manifest)


def validate_portable_result(bundle):
    if type(bundle) is bytes:
        bundle = parse_portable_json(bundle)
    canonical_json_bytes(bundle)
    _keys(
        bundle,
        {
            "schema_version",
            "evaluation_mode",
            "result_trust",
            "ledger_origin",
            "runtime_certified",
            "identity",
            "manifest",
            "window",
            "engine",
            "account_series",
            "positions",
            "orders",
            "fills",
            "closed_trades",
            "native_metrics",
            "decision_trace",
            "logical_orders",
            "final_state",
            "summary",
        }
        | ({"benchmark"} if "benchmark" in bundle else set()),
        "result",
    )
    if (
        bundle["schema_version"],
        bundle["evaluation_mode"],
        bundle["result_trust"],
        bundle["ledger_origin"],
        bundle["runtime_certified"],
    ) != (VERSION, "portable", TRUST, "trusted_engine", False) or bundle[
        "runtime_certified"
    ] is not False:
        raise ValueError("unsupported portable provenance/certification")
    identity = bundle["identity"]
    _keys(
        identity,
        {
            "strategy_sha256",
            "parameters_sha256",
            "manifest_sha256",
            "run_config_sha256",
            "market_data_sha256",
        },
        "identity",
    )
    for value in identity.values():
        _hash(value)
    manifest = bundle["manifest"]
    _manifest(manifest, identity)
    timezone = ZoneInfo(manifest["timezone"])
    _keys(bundle["window"], {"start", "end", "interval"}, "window")
    start, end = _day(bundle["window"]["start"]), _day(bundle["window"]["end"])
    if end <= start or bundle["window"]["interval"] != "half_open":
        raise ValueError("invalid portable window")

    def active(ts):
        if not start <= ts.astimezone(timezone).date() < end:
            raise ValueError("native record outside active window")

    engine = bundle["engine"]
    _keys(
        engine,
        {
            "name",
            "version",
            "profile",
            "resolved_config",
            "resolved_execution_policy",
            "timestamp_semantics",
            "forced_final_liquidation",
        },
        "engine",
    )
    if (
        engine["name"] != "akquant"
        or engine["version"] != "0.3.27"
        or engine["forced_final_liquidation"] is not False
    ):
        raise ValueError("unsupported portable engine")
    if (
        type(engine["resolved_config"]) is not dict
        or type(engine["resolved_execution_policy"]) is not dict
        or not engine["resolved_config"]
        or not engine["resolved_execution_policy"]
    ):
        raise ValueError("missing native resolved configuration")
    _text(engine["timestamp_semantics"])
    resolved = engine["resolved_execution_policy"]
    _keys(
        resolved,
        {"price_basis", "bar_offset", "temporal", "source"},
        "resolved fill policy",
    )
    if (resolved["price_basis"], resolved["bar_offset"], resolved["temporal"]) != (
        "open",
        1,
        "same_cycle",
    ):
        raise ValueError("native resolved fill policy is not NextOpen")
    _text(resolved["source"])

    if _sha(engine["profile"]) != manifest["engine_profile"]["profile_sha256"]:
        raise ValueError("portable engine profile mismatch")
    config = engine["resolved_config"]
    _keys(
        config,
        {
            "slippage",
            "volume_limit_pct",
            "commission_policy",
            "stamp_tax_rate",
            "transfer_fee_rate",
            "min_commission",
            "t_plus_one",
            "timezone",
            "history_depth",
            "fill_policy",
        },
        "resolved native configuration",
    )
    expected_fill = {"price_basis": "open", "bar_offset": 1, "temporal": "same_cycle"}
    if (
        config["fill_policy"] != expected_fill
        or config["timezone"] != manifest["timezone"]
        or config["t_plus_one"] is not False
        or config["history_depth"] != manifest["data"]["lookback_bars"]
    ):
        raise ValueError(
            "native resolved configuration differs from requested execution"
        )
    profile = engine["profile"]
    _keys(
        profile,
        {
            "profile_version",
            "engine",
            "engine_version",
            "market",
            "initial_cash",
            "commission_rate",
            "stamp_tax_rate",
            "transfer_fee_rate",
            "min_commission",
            "slippage",
            "volume_limit_pct",
            "t_plus_one",
            "lot_size",
            "timezone",
            "risk_config",
            "fill_policy",
            "time_in_force",
            "sizing_price",
            "sizing_equity",
            "cash_budget",
            "sell_budget",
            "engine_settlement_mode",
            "application_settlement_enforcement",
            "application_t_plus_one",
            "initial_positions",
            "policy_callback",
            "intraday_policy",
            "external_funding",
            "corporate_actions",
            "limit_execution",
            "sellability_observation",
            "unsettled_next_open_sell",
            "pending_rebalance",
            "final_liquidation",
        },
        "reviewed engine profile",
    )
    fixed_profile = {
        "profile_version": "akquant.portable.cash-daily.controller-t1.v3",
        "engine_settlement_mode": "T+0",
        "application_settlement_enforcement": "trusted_controller_daily_next_open_inventory.v1",
        "application_t_plus_one": manifest["market"] == "CN",
        "initial_positions": "flat",
        "policy_callback": "complete_daily_cross_section_after_close",
        "intraday_policy": "unsupported",
        "external_funding": "unsupported",
        "corporate_actions": "reject_window_with_actions",
        "limit_execution": "reject_non_open_or_limit_breaching_native_fill",
        "engine": "akquant",
        "engine_version": "0.3.27",
        "market": manifest["market"],
        "timezone": manifest["timezone"],
        "commission_rate": 0.0005,
        "stamp_tax_rate": 0.001 if manifest["market"] == "CN" else 0.0,
        "transfer_fee_rate": 0.0,
        "min_commission": 0.0,
        "slippage": {"type": "percent", "value": 0.0005},
        "volume_limit_pct": 0.25,
        "sizing_price": "decision_close",
        "sizing_equity": "decision_close_native_equity",
        "cash_budget": "available_cash_without_anticipated_sale_proceeds",
        "sell_budget": "decision_close_holdings_less_pending_sells",
        "sellability_observation": "next_open_unreserved_holdings",
        "unsettled_next_open_sell": "admit_after_close_with_controller_t1",
        "pending_rebalance": "skip_until_all_native_legs_terminal",
    }
    if any(profile[k] != v for k, v in fixed_profile.items()):
        raise ValueError("unreviewed execution costs/profile")
    for key in (
        "slippage",
        "volume_limit_pct",
        "stamp_tax_rate",
        "transfer_fee_rate",
        "min_commission",
    ):
        if config[key] != profile[key]:
            raise ValueError("native resolved execution costs differ from profile")
    if config["commission_policy"] != {
        "type": "percent",
        "value": profile["commission_rate"],
    }:
        raise ValueError("native resolved commission differs from profile")
    if (
        profile.get("fill_policy") != "NextOpen"
        or profile.get("time_in_force") != "GTC"
        or profile.get("final_liquidation") is not False
        or profile.get("t_plus_one") is not False
        or profile.get("application_t_plus_one") is not (manifest["market"] == "CN")
        or profile.get("lot_size") != (100 if manifest["market"] == "CN" else 1)
        or profile.get("risk_config")
        != {"account_mode": "cash", "enable_short_sell": False}
    ):
        raise ValueError("unsupported portable execution semantics")
    for name in (
        "account_series",
        "positions",
        "orders",
        "fills",
        "closed_trades",
        "native_metrics",
        "decision_trace",
        "logical_orders",
    ):
        if type(bundle[name]) is not list:
            raise ValueError("portable collection must be an array")
    if (
        not bundle["account_series"]
        or not bundle["decision_trace"]
        or not bundle["native_metrics"]
    ):
        raise ValueError("missing native result observations")
    if "benchmark" in bundle:
        from .benchmarks import validate_comparison

        validate_comparison(bundle["benchmark"], bundle)
    summary = bundle["summary"]
    _keys(
        summary,
        {
            "initial_cash",
            "final_equity",
            "total_return_pct",
            "max_drawdown_pct",
            "execution_count",
            "closed_trade_count",
            "total_commission",
        },
        "summary",
    )
    initial = _number(summary["initial_cash"], minimum=1e-12)
    _same(initial, profile["initial_cash"], "initial cash")
    previous, peak = None, initial
    for row in bundle["account_series"]:
        _keys(
            row,
            {
                "native_timestamp_ns",
                "native_at",
                "session_date",
                "equity",
                "cash",
                "drawdown",
            },
            "account row",
        )
        ts = _native_time(row)
        active(ts)
        if _day(row["session_date"]) != ts.astimezone(timezone).date() or (
            previous is not None and int(row["native_timestamp_ns"]) <= previous
        ):
            raise ValueError("native account timestamps not strictly ordered")
        _number(row["cash"], minimum=-1e-7)
        peak = max(peak, _number(row["equity"], minimum=0))
        _same(row["drawdown"], 1 - row["equity"] / peak, "drawdown")
        previous = int(row["native_timestamp_ns"])
    _same(
        summary["final_equity"], bundle["account_series"][-1]["equity"], "final equity"
    )
    _same(
        summary["total_return_pct"],
        (summary["final_equity"] / initial - 1) * 100,
        "return",
    )
    _same(
        summary["max_drawdown_pct"],
        max(r["drawdown"] for r in bundle["account_series"]) * 100,
        "maximum drawdown",
    )
    for row in bundle["positions"]:
        _keys(
            row,
            {
                "native_timestamp_ns",
                "native_at",
                "listing_id",
                "quantity",
                "short_quantity",
                "close",
                "market_value",
                "entry_price",
                "unrealized_pnl",
            },
            "position",
        )
        active(_native_time(row))
        _text(row["listing_id"])
        for name in ("quantity", "close", "market_value", "entry_price"):
            _number(row[name], minimum=0)
        if row["short_quantity"] != 0:
            raise ValueError("cash long-only result contains shorts")
        _number(row["unrealized_pnl"])
    logical, native_ids = {}, set()
    for row in bundle["logical_orders"]:
        _keys(
            row,
            {
                "logical_order_id",
                "signal_id",
                "listing_id",
                "side",
                "quantity",
                "order_type",
                "limit_price",
                "decision_time",
                "decision_session",
                "effective_at",
                "native_order_ids",
                "filled_quantity",
                "terminal",
            },
            "logical order",
        )
        key = _hash(row["logical_order_id"])
        if (
            key in logical
            or type(row["native_order_ids"]) is not list
            or not row["native_order_ids"]
            or type(row["terminal"]) is not bool
        ):
            raise ValueError("duplicate/invalid logical order")
        for oid in row["native_order_ids"]:
            if _text(oid) in native_ids:
                raise ValueError("duplicate native order leg")
            native_ids.add(oid)
        if _timestamp(row["decision_time"]).astimezone(timezone).date() != _day(
            row["decision_session"]
        ) or _timestamp(row["effective_at"]) <= _timestamp(row["decision_time"]):
            raise ValueError("invalid logical decision timing")
        active(_timestamp(row["decision_time"]))
        if (
            row["side"] not in {"buy", "sell"}
            or row["order_type"] not in {"market", "limit"}
            or (row["limit_price"] is None) != (row["order_type"] == "market")
        ):
            raise ValueError("unsupported logical order")
        if row["limit_price"] is not None:
            _number(row["limit_price"], minimum=1e-12)
        qty = _number(row["quantity"], minimum=1)
        if qty % profile["lot_size"] or not 0 <= _number(row["filled_quantity"]) <= qty:
            raise ValueError("invalid market lot/fill quantity")
        _text(row["signal_id"])
        _text(row["listing_id"])
        logical[key] = row
    orders = {}
    for row in bundle["orders"]:
        _keys(
            row,
            {
                "order_id",
                "logical_order_id",
                "listing_id",
                "side",
                "quantity",
                "filled_quantity",
                "order_type",
                "limit_price",
                "time_in_force",
                "status",
                "reject_reason",
                "commission",
                "native_created_ns",
                "native_created_at",
                "native_updated_ns",
                "native_updated_at",
                "decision_time",
                "effective_at",
            },
            "native order",
        )
        if row["order_id"] in orders or row["logical_order_id"] not in logical:
            raise ValueError("unattributed or duplicate native order")
        rec = logical[row["logical_order_id"]]
        if (
            row["order_id"] not in rec["native_order_ids"]
            or any(
                row[k] != rec[k]
                for k in (
                    "listing_id",
                    "side",
                    "order_type",
                    "limit_price",
                    "decision_time",
                    "effective_at",
                )
            )
            or row["time_in_force"] != "GTC"
        ):
            raise ValueError("native order differs from reviewed intent")
        created = _native_time(row, "native_created_ns", "native_created_at")
        updated = _native_time(row, "native_updated_ns", "native_updated_at")
        active(created)
        active(updated)
        if created != _timestamp(rec["decision_time"]) or updated < created:
            raise ValueError("native order timestamp differs from decision")
        qty = _number(row["quantity"], minimum=1)
        if (
            not 0 <= _number(row["filled_quantity"]) <= qty
            or row["status"]
            not in {
                "accepted",
                "partial_fill",
                "filled",
                "cancelled",
                "rejected",
                "expired",
            }
            or type(row["reject_reason"]) is not str
        ):
            raise ValueError("invalid native order status/quantity")
        _number(row["commission"], minimum=0)
        orders[row["order_id"]] = row
    if set(orders) != native_ids:
        raise ValueError("missing receipt legs in native order history")
    fill_ids, fill_totals, held, bought_today = set(), {}, {}, {}
    for row in sorted(bundle["fills"], key=lambda v: int(v["native_timestamp_ns"])):
        _keys(
            row,
            {
                "native_timestamp_ns",
                "native_at",
                "fill_id",
                "order_id",
                "logical_order_id",
                "listing_id",
                "side",
                "quantity",
                "price",
                "commission",
                "session_date",
                "economic_reference_time",
                "economic_reference",
                "decision_session",
            },
            "fill",
        )
        ts = _native_time(row)
        active(ts)
        if _text(row["fill_id"]) in fill_ids or row["order_id"] not in orders:
            raise ValueError("duplicate or unattributed fill")
        fill_ids.add(row["fill_id"])
        order = orders[row["order_id"]]
        rec = logical[order["logical_order_id"]]
        if (
            any(row[k] != order[k] for k in ("logical_order_id", "listing_id", "side"))
            or row["decision_session"] != rec["decision_session"]
        ):
            raise ValueError("fill/order attribution mismatch")
        session = _day(row["session_date"])
        reference = _timestamp(row["economic_reference_time"])
        if (
            session != ts.astimezone(timezone).date()
            or session <= _day(rec["decision_session"])
            or reference < _timestamp(rec["effective_at"])
            or reference > ts
            or reference.astimezone(timezone).date() != session
            or row["economic_reference"]
            != "session_open_with_native_matching_and_slippage"
        ):
            raise ValueError("fill violates strictly-later NextOpen semantics")
        qty = _number(row["quantity"], minimum=1)
        if qty % profile["lot_size"]:
            raise ValueError("native fill violates lot size")
        price = _number(row["price"], minimum=1e-12)
        if rec["order_type"] == "limit" and (
            (row["side"] == "buy" and price > rec["limit_price"])
            or (row["side"] == "sell" and price < rec["limit_price"])
        ):
            raise ValueError("native fill breaches declared limit price")
        _number(row["commission"], minimum=0)
        fill_totals[row["order_id"]] = fill_totals.get(row["order_id"], 0) + qty
        key = row["listing_id"]
        if row["side"] == "buy":
            held[key] = held.get(key, 0) + qty
            bought_today[(key, session)] = bought_today.get((key, session), 0) + qty
        else:
            available = held.get(key, 0) - (
                bought_today.get((key, session), 0) if manifest["market"] == "CN" else 0
            )
            if qty > available + 1e-8:
                raise ValueError(
                    "native fill violates controller long-only/T+1 inventory invariant"
                )
            held[key] -= qty
    for key, order in orders.items():
        _same(order["filled_quantity"], fill_totals.get(key, 0), "native fills")
    for rec in logical.values():
        legs = [orders[key] for key in rec["native_order_ids"]]
        _same(
            rec["quantity"], sum(o["quantity"] for o in legs), "receipt leg quantities"
        )
        _same(
            rec["filled_quantity"],
            sum(o["filled_quantity"] for o in legs),
            "logical fills",
        )
        if rec["terminal"] != all(
            o["status"] in {"filled", "cancelled", "rejected", "expired"} for o in legs
        ):
            raise ValueError("unresolved order falsely marked terminal")
    for row in bundle["closed_trades"]:
        _keys(
            row,
            {
                "listing_id",
                "entry_native_ns",
                "entry_native_at",
                "exit_native_ns",
                "exit_native_at",
                "quantity",
                "entry_price",
                "exit_price",
                "pnl",
                "net_pnl",
                "commission",
            },
            "closed trade",
        )
        entry = _native_time(row, "entry_native_ns", "entry_native_at")
        exit = _native_time(row, "exit_native_ns", "exit_native_at")
        active(entry)
        active(exit)
        if exit < entry:
            raise ValueError("closed trade exit precedes entry")
        for key in ("quantity", "entry_price", "exit_price"):
            _number(row[key], minimum=1e-12)
        for key in ("pnl", "net_pnl", "commission"):
            _number(row[key])
        _same(row["net_pnl"], row["pnl"] - row["commission"], "closed trade fees")
    if (
        type(summary["execution_count"]) is not int
        or summary["execution_count"] != len(bundle["fills"])
        or type(summary["closed_trade_count"]) is not int
        or summary["closed_trade_count"] != len(bundle["closed_trades"])
    ):
        raise ValueError("portable summary count mismatch")
    _same(
        summary["total_commission"],
        sum(f["commission"] for f in bundle["fills"]),
        "aggregate commissions",
    )
    metric_names = set()
    for metric in bundle["native_metrics"]:
        _keys(metric, {"name", "value", "unit", "unavailable_reason"}, "native metric")
        if _text(metric["name"]) in metric_names or metric["unit"] not in {
            "native_engine_defined",
            "native_timestamp",
            "nanoseconds",
        }:
            raise ValueError("invalid native metric definition")
        metric_names.add(metric["name"])
        if metric["value"] is None:
            _text(metric["unavailable_reason"])
        elif metric["unit"] == "native_engine_defined":
            _number(metric["value"])
        elif type(metric["value"]) is not str:
            raise ValueError("native time metric must be lossless string")
        if metric["value"] is not None and metric["unavailable_reason"] is not None:
            raise ValueError("available metric has unavailable reason")
    previous_state, traced, sessions, all_signals = None, [], set(), set()
    for sequence, row in enumerate(bundle["decision_trace"]):
        _keys(
            row,
            {
                "sequence",
                "session_date",
                "decision_time",
                "next_open_time",
                "warmup",
                "snapshot_sha256",
                "observation_sha256",
                "reply_sha256",
                "state_before_sha256",
                "state_after_sha256",
                "decision",
                "logical_order_ids",
                "outcome",
            },
            "decision trace",
        )
        session = _day(row["session_date"])
        if (
            type(row["sequence"]) is not int
            or row["sequence"] != sequence
            or session in sessions
            or type(row["warmup"]) is not bool
        ):
            raise ValueError("invalid decision sequence")
        sessions.add(session)
        if _timestamp(row["decision_time"]).astimezone(
            timezone
        ).date() != session or _timestamp(row["next_open_time"]) <= _timestamp(
            row["decision_time"]
        ):
            raise ValueError("invalid decision trace timing")
        for key in (
            "snapshot_sha256",
            "observation_sha256",
            "reply_sha256",
            "state_before_sha256",
            "state_after_sha256",
        ):
            _hash(row[key])
        if previous_state is not None and row["state_before_sha256"] != previous_state:
            raise ValueError("broken portable state chain")
        previous_state = row["state_after_sha256"]
        decision = row["decision"]
        if (
            type(decision) is not dict
            or decision.get("kind") not in {"event", "target_portfolio"}
            or type(row["logical_order_ids"]) is not list
        ):
            raise ValueError("invalid portable decision trace")
        signal_ids = _decision(decision, row["next_open_time"])
        if signal_ids & all_signals:
            raise ValueError("replayed signal in decision trace")
        all_signals.update(signal_ids)
        if row["warmup"] and (
            row["logical_order_ids"]
            or row["outcome"] != "warmup_state_only"
            or (decision["kind"] == "event" and decision.get("intents"))
            or (
                decision["kind"] == "target_portfolio"
                and decision.get("action") != "no_decision"
            )
        ):
            raise ValueError("warmup trace attempted trading")
        if row["outcome"] not in {
            "no_orders",
            "submitted",
            "cancel_requested",
            "skipped_pending_rebalance",
            "warmup_state_only",
        }:
            raise ValueError("invalid portable decision outcome")
        for key in row["logical_order_ids"]:
            if (
                key not in logical
                or logical[key]["decision_session"] != row["session_date"]
            ):
                raise ValueError("unattributed logical decision")
        traced.extend(row["logical_order_ids"])
    if len(traced) != len(set(traced)) or set(traced) != set(logical):
        raise ValueError("incomplete logical decision trace")
    _state(bundle["final_state"])
    if bundle["final_state"]["payload_sha256"] != previous_state or bundle[
        "final_state"
    ]["schema_sha256"] != _sha(manifest["state_schema"]):
        raise ValueError("final portable state does not match trace/schema")
    return bundle


# Explicit alias for callers choosing the mode before invoking a validator.
validate_result_bundle = validate_portable_result
