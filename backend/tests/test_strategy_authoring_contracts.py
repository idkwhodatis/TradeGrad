"""Pure inert declarations, hashes and projections only. Never load candidate code."""

import ast
import hashlib
import json
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.app.portable_authoring import normalized_source
from backend.app.portable_schemas import PortableSourceCreate
from backend.app.strategy_schemas import StrategyCreate
from packages.strategy_sdk import (
    StateCodec,
    StateField,
    StateSchema,
    canonical_json,
    hash_json,
    parse_manifest,
)
from packages.strategy_sdk.contracts import UserPolicyDeclaration
from packages.tradegrad_adapter.config import DateWindow, MarketConfig, RunConfig
from packages.tradegrad_adapter.native_contract import (
    NativeStrategyContract,
    native_context,
    validate_native_binding,
)
from packages.tradegrad_adapter.portable_prompts import (
    validate_policy_source,
    validate_user_policy_source,
)
from packages.tradegrad_adapter.windows import stage_csv_window

ROOT = Path(__file__).parents[2]
INERT = "raise AssertionError('MUST NEVER EXECUTE ON HOST')\nclass Policy: pass\n"


def declaration(**changes):
    return {
        "lookback_bars": 20,
        "warmup_bars": 20,
        "requires_shorting": False,
        "required_fields": ["open", "high", "low", "close", "volume", "amount"],
        "parameters": [
            {
                "name": "allocation",
                "kind": "number",
                "value": 0.2,
                "minimum": 0.1,
                "maximum": 0.9,
            }
        ],
        **changes,
    }


def native_config(**changes):
    data = json.loads(
        (ROOT / "tests/tradegrad/fixtures/sealed-native-config.json").read_bytes()
    )
    if changes.get("strategy_contract") is not None:
        data.update(
            profile="corrected_explicit_config",
            warmup={"sessions": 20},
            market_profile=MarketConfig.for_market(data["task"][:2].upper()).model_dump(
                mode="json"
            ),
        )
    data.update(changes)
    return RunConfig.model_validate(data)


def portable_body():
    schema = StateSchema(name="user_state", version=1, fields=())
    state = StateCodec(schema).encode({})
    parameters = {"threshold": 0.2}
    return {
        "name": "Custom user policy",
        "task": "us_time",
        "code": INERT,
        "parameters": parameters,
        "manifest": {
            "strategy_id": str(UUID(int=10)),
            "strategy_kind": "event",
            "code_sha256": "0" * 64,
            "parameters_sha256": "0" * 64,
            "market": "US",
            "currency": "USD",
            "calendar": "XNYS",
            "timezone": "America/New_York",
            "data": {"lookback_bars": 2, "warmup_bars": 2, "max_listings": 1},
            "universe": {
                "semantics": "user_fixed",
                "snapshot_sha256": "1" * 64,
                "survivorship_bias": "possible",
            },
            "prices": {"actions_snapshot_sha256": "2" * 64},
            "state_schema": schema.model_dump(mode="json"),
            "engine_profile": {
                "profile_sha256": "3" * 64,
                "environment_sha256": "4" * 64,
            },
            "authoring": UserPolicyDeclaration(
                parameter_schema=StateSchema(
                    name="user_parameters",
                    version=1,
                    fields=(StateField(name="threshold", kind="number"),),
                ),
                initial_state=state,
            ).model_dump(mode="json"),
        },
    }


def test_legacy_native_hash_and_source_bytes_unchanged():
    path = ROOT / "tests/tradegrad/fixtures/sealed-native-config.json"
    raw = path.read_bytes()
    value = native_config(strategy_contract=None)
    assert value.canonical_bytes() == raw
    assert value.sha256 == hashlib.sha256(raw).hexdigest()
    assert "strategy_contract" not in value.model_dump(mode="json")
    source = '# Unicode 原样\r\nx = "é"\r\n'
    assert (
        StrategyCreate(name="source", task="cn_time", code=source).code.encode()
        == source.encode()
    )


def test_declared_parameters_are_immutable_bounded_detached_and_hash_bound():
    contract = NativeStrategyContract.model_validate(declaration())
    with pytest.raises(ValidationError):
        contract.parameters[0].value = 0.8
    context = native_context(
        {"target_symbol": "target_symbol"}, contract.model_dump(mode="json")
    )
    assert context["tradegrad_parameters"] == {"allocation": 0.2}
    context["tradegrad_parameters"]["allocation"] = 0.8
    assert contract.parameter_values()["allocation"] == 0.2
    first = native_config(strategy_contract=contract)
    second = native_config(
        strategy_contract=declaration(
            parameters=[
                {
                    "name": "allocation",
                    "kind": "number",
                    "value": 0.8,
                    "minimum": 0.1,
                    "maximum": 0.9,
                }
            ]
        )
    )
    assert first.sha256 != second.sha256
    validate_native_binding(
        contract, contract.model_dump(mode="json"), {"allocation": 0.2}
    )
    with pytest.raises(ValueError, match="exact declaration"):
        validate_native_binding(
            contract, contract.model_dump(mode="json"), {"allocation": 0.8}
        )
    old = {"target_symbol": "target_symbol"}
    assert native_context(old, None) is old


@pytest.mark.parametrize(
    "parameter",
    [
        {"name": "x", "kind": "integer", "value": True, "minimum": 0, "maximum": 2},
        {"name": "x", "kind": "integer", "value": 3, "minimum": 0, "maximum": 2},
        {
            "name": "x",
            "kind": "number",
            "value": float("nan"),
            "minimum": 0,
            "maximum": 2,
        },
        {"name": "x", "kind": "number", "value": 1},
        {"name": "x", "kind": "integer", "value": 10**400, "minimum": 0, "maximum": 1},
        {
            "name": "x",
            "kind": "number",
            "value": 0.5,
            "minimum": 0,
            "maximum": 1,
            "choices": [0.5, 2],
        },
        {"name": "x", "kind": "string", "value": "x" * 257},
        {"name": "__class__", "kind": "string", "value": "x"},
        {"name": "x", "kind": "boolean", "value": 1},
    ],
)
def test_invalid_parameters_fail_closed(parameter):
    with pytest.raises(ValidationError):
        NativeStrategyContract.model_validate(declaration(parameters=[parameter]))


@pytest.mark.parametrize(
    "changes",
    [
        {"lookback_bars": 253},
        {"required_fields": ["amount", "amount"]},
        {"requires_shorting": "false"},
        {"required_fields": ["future_close"]},
    ],
)
def test_invalid_declarations_fail_closed(changes):
    with pytest.raises(ValidationError):
        NativeStrategyContract.model_validate(declaration(**changes))


def test_warmup_and_shorting_claims_drive_config_not_task_defaults():
    with pytest.raises(ValueError, match="40 previous"):
        native_config(strategy_contract=declaration(lookback_bars=40))
    base = native_config(strategy_contract=declaration()).model_dump(mode="json")
    base["market_profile"]["risk_config"] = None
    base["strategy_contract"] = declaration()
    RunConfig.model_validate(base)  # US long-only custom strategy need not short.
    base["strategy_contract"]["requires_shorting"] = True
    with pytest.raises(ValueError, match="short-selling"):
        RunConfig.model_validate(base)
    base.update(
        profile="source_compat",
        warmup={"sessions": 0, "history_policy": "source_cold_start"},
    )
    with pytest.raises(ValueError, match="source_compat"):
        RunConfig.model_validate(base)


@pytest.mark.parametrize("amount", ["", "null", "NaN", "inf"])
def test_required_amount_must_be_non_null_finite(amount):
    raw = f"date,open,high,low,close,volume,amount\n2024-01-02,10,11,9,10,100,{amount}\n".encode()
    with pytest.raises(ValueError):
        stage_csv_window(
            raw,
            listing_id="A",
            window=DateWindow(start=date(2024, 1, 2), end=date(2024, 1, 3)),
            warmup_sessions=0,
            required_fields=("date", "amount"),
        )


def test_missing_amount_failure_identifies_action_and_field():
    from packages.tradegrad_adapter.config import DateWindow

    with pytest.raises(ValueError, match="amount.*publish a snapshot"):
        stage_csv_window(
            b"date,open,high,low,close,volume\n2024-01-02,10,11,9,10,100\n",
            listing_id="A",
            window=DateWindow(start="2024-01-02", end="2024-01-03"),
            warmup_sessions=0,
            required_fields=("date", "amount"),
        )


def test_custom_portable_source_is_inert_and_server_binds_identity():
    body = PortableSourceCreate.model_validate(portable_body())
    value = normalized_source(body, UUID(int=99))
    assert value["strategy_id"] == str(UUID(int=99))
    assert value["code_sha256"] == hashlib.sha256(INERT.encode()).hexdigest()
    assert value["parameters_sha256"] == hash_json(body.parameters)
    assert value["reference_result_sha256"] is None
    assert body.code == INERT
    assert parse_manifest(canonical_json(value)).authoring is not None
    validate_user_policy_source(INERT, "event")


@pytest.mark.parametrize(
    "mutation",
    ["parameter_type", "extra_parameter", "state_hash", "capability", "unknown"],
)
def test_portable_custom_declarations_remain_strict(mutation):
    value = portable_body()
    if mutation == "parameter_type":
        value["parameters"]["threshold"] = "0.2"
    if mutation == "extra_parameter":
        value["parameters"]["secret"] = "x"
    if mutation == "state_hash":
        value["manifest"]["authoring"]["initial_state"]["schema_sha256"] = "0" * 64
    if mutation == "capability":
        value["manifest"]["capabilities"] = {"short": True}
    if mutation == "unknown":
        value["manifest"]["trusted"] = True
    from backend.app.services import DomainError

    with pytest.raises((ValueError, DomainError)):
        normalized_source(PortableSourceCreate.model_validate(value), UUID(int=99))


def test_custom_optimization_preserves_own_scaffolding_not_reference_template():
    seed = "class Policy:\n    def custom(self):\n        # ### EDIT START\n        return 1\n        # ### EDIT END\n"
    validate_policy_source(seed.replace("return 1", "return 2"), "event", baseline=seed)
    with pytest.raises(ValueError, match="protected"):
        validate_policy_source(
            seed.replace("def custom", "def changed"), "event", baseline=seed
        )
    with pytest.raises(ValueError, match="edit region"):
        validate_policy_source(INERT, "event", baseline=INERT)


def test_evaluator_calls_contract_binding_without_host_execution():
    for name in ("evaluate.py", "evaluate_time.py"):
        tree = ast.parse((ROOT / "src" / name).read_text())
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "run_backtest"
        )
        assert "strategy_contract" in [arg.arg for arg in fn.args.kwonlyargs]
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "native_context"
            for node in ast.walk(fn)
        )


def test_production_native_staging_binds_fields_parameters_and_inert_source(tmp_path):
    from tests.tradegrad.test_config_windows import _bundle
    from workers.research.optimization import stage_research_input

    config, manifest, files = _bundle(second=False)
    value = config.model_dump(mode="json")
    value["strategy_contract"] = declaration(lookback_bars=3, warmup_bars=3)
    declared = RunConfig.model_validate(value)
    for name, content in files.items():
        path = tmp_path / "dataset" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    kwargs = {
        "config": declared,
        "manifest": manifest,
        "storage_root": tmp_path,
        "dataset_storage_key": "dataset",
        "code": INERT,
        "window": {"split": "train", "start": "2024-01-05", "end": "2024-01-08"},
        "target": tmp_path / "missing" / "input",
        "max_bytes": 100000,
    }
    with pytest.raises(ValueError, match="amount"):
        stage_research_input(**kwargs)
    value["strategy_contract"]["required_fields"].remove("amount")
    kwargs["config"] = RunConfig.model_validate(value)
    kwargs["target"] = tmp_path / "valid" / "input"
    path = stage_research_input(**kwargs)
    request = json.loads((path / "request.json").read_bytes())
    assert request["evaluation_config"]["strategy_contract"] == kwargs[
        "config"
    ].strategy_contract.model_dump(mode="json")
    assert (path / "strategy.py").read_text() == INERT
    assert (
        (path / "data/A.csv").read_bytes().splitlines()[-1].startswith(b"2024-01-07,")
    )


def test_native_declaration_budget_precedes_runner_request_limit():
    parameters = [
        {
            "name": f"value{i}",
            "kind": "string",
            "value": "中" * 256,
            "choices": ["中" * 256] * 32,
        }
        for i in range(32)
    ]
    with pytest.raises(ValueError, match="16 KiB"):
        NativeStrategyContract.model_validate(declaration(parameters=parameters))
