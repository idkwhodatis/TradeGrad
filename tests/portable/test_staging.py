"""Pure input projections with inert source. No policy, AKQuant or calendar execution."""

import hashlib
from uuid import UUID

import pytest

from packages.strategy_sdk import (
    DataRequirements,
    EngineProfile,
    PriceAssumptions,
    StateCodec,
    StateField,
    StateSchema,
    StrategyManifest,
    UniverseAssumptions,
    canonical_json,
    hash_json,
)
from packages.tradegrad_adapter.config import canonical_bytes
from packages.tradegrad_adapter.portable_config import PortableRunConfig
from packages.tradegrad_adapter.portable_windows import (
    preflight_portable_dataset,
    stage_portable_dataset_window,
)
from runtimes.evaluator.portable_runner import validate_inputs
from runtimes.portable_engine.runtime import load_inputs, profile_sha256
from workers.research.portable_staging import (
    stage_portable_input,
    validate_portable_binding,
)

SOURCE = "raise AssertionError('POLICY SOURCE MUST NEVER RUN ON HOST')\n"


def fixture(market="US", kind="event"):
    cn = market == "CN"
    ids = (
        [str(UUID(int=10))]
        if kind == "event"
        else [str(UUID(int=10)), str(UUID(int=11))]
    )
    days = [
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        "2024-01-09",
        "2024-01-10",
    ]
    next_days = days[1:] + ["2024-01-11"]
    calendar, mic, currency, timezone = (
        ("XSHG", "XSHE", "CNY", "Asia/Shanghai")
        if cn
        else ("XNYS", "XNAS", "USD", "America/New_York")
    )
    sessions = [
        {
            "session_date": day,
            "open_time": f"{day}T{'01:30' if cn else '14:30'}:00Z",
            "close_time": f"{day}T{'07:00' if cn else '21:00'}:00Z",
            "next_open_time": f"{nxt}T{'01:30' if cn else '14:30'}:00Z",
        }
        for day, nxt in zip(days, next_days)
    ]
    listings = [
        {
            "listing_id": id_,
            "instrument_id": id_,
            "symbol": ("000001" if cn else "AAPL")
            if index == 0
            else ("600000" if cn else "MSFT"),
            "mic": mic,
            "currency": currency,
            "calendar": calendar,
            "timezone": timezone,
            "asset_type": "equity",
            "listed_on": None,
            "delisted_on": None,
        }
        for index, id_ in enumerate(ids)
    ]
    bars = [
        {
            "listing_id": id_,
            "session_date": day,
            "open": 30.0,
            "high": 32.0,
            "low": 29.0,
            "close": 31.0,
            "volume": 10000.0,
            "currency": currency,
            "volume_unit": "shares",
            "flags": [],
        }
        for id_ in ids
        for day in days
    ]
    artifacts = {
        "sessions.json": {
            "schema_version": "marketdata.session-schedule.v1",
            "generator": {"name": "exchange_calendars", "version": "4.13.2"},
            "start": days[0],
            "end": "2024-01-11",
            "listing_calendars": {
                id_: {
                    "mic": mic,
                    "calendar": calendar,
                    "calendar_basis": "declared_proxy",
                    "note": f"{mic} uses {calendar} proxy; equivalence not certified.",
                }
                for id_ in ids
            },
            "calendars": {calendar: {"status": "complete", "sessions": sessions}},
        },
        "corporate-action-coverage.json": {
            "schema_version": "marketdata.corporate-action-coverage.v1",
            "start": days[0],
            "end": "2024-01-11",
            "listings": [
                {
                    "listing_id": id_,
                    "status": "known_none",
                    "provenance_ids": [id_],
                    "evidence": "synthetic_by_construction",
                }
                for id_ in ids
            ],
            "is_synthetic": True,
            "generator_version": "portable.synthetic.daily.v1",
        },
        "actions.json": [],
        "preview.json": bars,
    }
    blobs = {name: canonical_json(value) for name, value in artifacts.items()}
    manifest = {
        "request": {"start": days[0], "end": "2024-01-11", "interval": "1d"},
        "listings": listings,
        "price_semantics": {
            "split_basis": "unadjusted",
            "dividend_basis": "unadjusted",
            "algorithm_version": "portable.synthetic.daily.v1",
        },
        "universe": {"state": "user_fixed"},
        "files": {},
        "artifact_files": {
            name: hashlib.sha256(value).hexdigest() for name, value in blobs.items()
        },
        "provenance": [
            {
                "provenance_id": id_,
                "connector": "portable_fixture",
                "provider": "synthetic",
                "evidence": "fixture",
                "endpoint": "portable.synthetic.daily.v1",
                "requested_params": {
                    "listing_id": id_,
                    "start": "2024-01-02",
                    "end": "2024-01-11",
                },
                "effective_params": {
                    "is_synthetic": True,
                    "price_basis": "unadjusted",
                    "corporate_actions": "none_by_construction",
                    "generator_version": "portable.synthetic.daily.v1",
                },
            }
            for id_ in ids
        ],
    }
    schema = StateSchema(
        name="trend" if kind == "event" else "momentum",
        version=1,
        fields=(StateField(name="last_session", kind="string"),),
    )
    params = {"quantity": 100 if cn else 1} if kind == "event" else {"top_n": 1}
    policy = StrategyManifest(
        strategy_kind=kind,
        strategy_id=str(UUID(int=20)),
        code_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
        parameters_sha256=hash_json(params),
        market=market,
        currency=currency,
        calendar=calendar,
        timezone=timezone,
        data=DataRequirements(lookback_bars=2, warmup_bars=2, max_listings=100),
        universe=UniverseAssumptions(
            semantics="user_fixed",
            snapshot_sha256="0" * 64,
            survivorship_bias="possible",
        ),
        prices=PriceAssumptions(actions_snapshot_sha256="0" * 64),
        state_schema=schema,
        engine_profile=EngineProfile(
            profile_sha256=profile_sha256(market, 1e6), environment_sha256="f" * 64
        ),
    )
    config = PortableRunConfig.model_validate(
        {
            "task": f"{market.lower()}_{'time' if kind == 'event' else 'cross'}",
            "strategy_kind": kind,
            "dataset": {
                "id": str(UUID(int=1)),
                "sha256": hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
            },
            "seed_strategy": {"id": str(UUID(int=2)), "sha256": policy.code_sha256},
            "seed_manifest": {
                "id": f"{UUID(int=2)}/manifest",
                "sha256": hash_json(policy),
            },
            "seed_parameters": {
                "id": f"{UUID(int=2)}/parameters",
                "sha256": hash_json(params),
            },
            "universe": {"id": str(UUID(int=3)), "sha256": "e" * 64},
            "windows": {
                "train": {"start": "2024-01-04", "end": "2024-01-09"},
                "validation": {"start": "2024-01-09", "end": "2024-01-10"},
                "holdout": {"start": "2024-01-10", "end": "2024-01-11"},
            },
            "warmup": {"sessions": 2},
            "initial_state": StateCodec(schema)
            .encode({"last_session": ""})
            .model_dump(mode="json"),
            "environment_digest": "f" * 64,
        }
    )
    return config, manifest, blobs, policy, params


def repin(config, manifest, blobs):
    manifest["artifact_files"] = {
        name: hashlib.sha256(value).hexdigest() for name, value in blobs.items()
    }
    data = config.model_dump(mode="json")
    data["dataset"]["sha256"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    return PortableRunConfig.model_validate(data)


@pytest.mark.parametrize("market", ["CN", "US"])
@pytest.mark.parametrize("kind", ["event", "portfolio"])
def test_exact_runner_abi_and_safe_policy_mounts(tmp_path, market, kind):
    config, dataset, blobs, policy, params = fixture(market, kind)
    source = tmp_path / "dataset"
    source.mkdir()
    for name, data in blobs.items():
        (source / name).write_bytes(data)
    result = stage_portable_input(
        config=config,
        manifest=dataset,
        storage_root=tmp_path,
        dataset_storage_key="dataset",
        code=SOURCE,
        policy_manifest=policy,
        parameters=params,
        target=tmp_path / "input",
        max_bytes=4_000_000,
        split="train",
    )
    roots = validate_inputs(result.engine_root, result.policy_root, config.sha256)
    assert roots[2] == result.request
    assert {p.name for p in result.engine_root.iterdir()} == {
        "request.json",
        "market.json",
    }
    assert {p.name for p in result.policy_root.iterdir()} == {
        "policy.py",
        "parameters.json",
        "manifest.json",
    }
    policy_text = (result.policy_root / "manifest.json").read_text()
    assert (
        config.sha256 not in policy_text
        and result.report.market_data_sha256 not in policy_text
    )
    assert result.report.is_synthetic and not result.report.point_in_time_proven
    assert any("proxy" in v for v in result.report.warnings)
    # Trusted pure parser only. This verifier stub does not claim actual calendar certification.
    inputs = load_inputs(
        (result.engine_root / "request.json").read_bytes(),
        (result.engine_root / "market.json").read_bytes(),
        calendar_verifier=lambda *_: None,
    )
    assert inputs.manifest.strategy_kind == kind
    assert inputs.sessions[-1].session_date.isoformat() == "2024-01-08"
    assert all(b.session_date.isoformat() < "2024-01-09" for b in inputs.bars)
    assert (
        len([s for s in inputs.sessions if s.session_date.isoformat() < "2024-01-04"])
        == 2
    )
    with pytest.raises(FileExistsError):
        stage_portable_input(
            config=config,
            manifest=dataset,
            storage_root=tmp_path,
            dataset_storage_key="dataset",
            code=SOURCE,
            policy_manifest=policy,
            parameters=params,
            target=tmp_path / "input",
            max_bytes=4_000_000,
            split="train",
        )


def test_holdout_and_arbitrary_windows_never_admitted():
    config, manifest, blobs, *_ = fixture()
    for split, window in [
        ("holdout", None),
        ("train", {"split": "train", "start": "2024-01-04", "end": "2024-01-11"}),
        ("train", {"split": "holdout", "start": "2024-01-10", "end": "2024-01-11"}),
    ]:
        with pytest.raises(ValueError):
            preflight_portable_dataset(
                config, split, manifest, blobs.__getitem__, evaluation_window=window
            )


@pytest.mark.parametrize(
    "change",
    [
        "legacy",
        "adjusted",
        "unknown_actions",
        "actions",
        "bar_gap",
        "warmup_gap",
        "clock_gap",
        "proxy_missing",
        "unknown_units",
        "unknown_flags",
        "future_universe",
    ],
)
def test_fail_closed_semantics(change):
    import json

    config, manifest, blobs, *_ = fixture()
    if change == "legacy":
        del blobs["sessions.json"]
    elif change == "adjusted":
        manifest["price_semantics"]["split_basis"] = "adjusted"
    elif change == "unknown_actions":
        value = json.loads(blobs["corporate-action-coverage.json"])
        value["listings"][0]["status"] = "unknown"
        blobs["corporate-action-coverage.json"] = canonical_json(value)
    elif change == "actions":
        blobs["actions.json"] = canonical_json(
            [{"session_date": "2024-01-04", "action_type": "dividend"}]
        )
    elif change in {"bar_gap", "warmup_gap", "unknown_units", "unknown_flags"}:
        value = json.loads(blobs["preview.json"])
        if change == "bar_gap":
            value.pop(2)
        elif change == "warmup_gap":
            value.pop(0)
        elif change == "unknown_units":
            value[0]["volume_unit"] = "unknown"
        else:
            value[0]["flags"] = ["suspension_unknown"]
        blobs["preview.json"] = canonical_json(value)
    elif change in {"clock_gap", "proxy_missing"}:
        value = json.loads(blobs["sessions.json"])
        if change == "clock_gap":
            value["calendars"]["XNYS"]["sessions"].pop(2)
        else:
            value["listing_calendars"][str(UUID(int=10))]["calendar_basis"] = (
                "exchange_calendar"
            )
        blobs["sessions.json"] = canonical_json(value)
    else:
        manifest["universe"]["state"] = "point_in_time_verified"
    config = repin(config, manifest, blobs)
    with pytest.raises(ValueError):
        preflight_portable_dataset(config, "train", manifest, blobs.__getitem__)


def test_future_prices_do_not_change_staged_market_or_safe_policy_projection():
    import json

    config, manifest, blobs, *_ = fixture()
    first = stage_portable_dataset_window(config, "train", manifest, blobs.__getitem__)
    values = json.loads(blobs["preview.json"])
    for value in values:
        if value["session_date"] >= "2024-01-09":
            value.update(open=99.0, high=100.0, low=98.0, close=99.0)
    blobs["preview.json"] = canonical_json(values)
    config = repin(config, manifest, blobs)
    second = stage_portable_dataset_window(config, "train", manifest, blobs.__getitem__)
    assert first.content == second.content


def test_binding_rejects_changed_parameters_manifest_state_and_warmup():
    config, _, _, policy, params = fixture()
    assert validate_portable_binding(config, policy, params) == policy
    with pytest.raises(ValueError):
        validate_portable_binding(config, policy, {"quantity": 2})
    with pytest.raises(ValueError):
        validate_portable_binding(
            config, policy.model_copy(update={"code_sha256": "0" * 64}), params
        )
    values = config.model_dump(mode="json")
    values["warmup"]["sessions"] = 1
    with pytest.raises(ValueError):
        validate_portable_binding(
            PortableRunConfig.model_validate(values), policy, params
        )


def frozen_final_values():
    from packages.tradegrad_adapter.portable_final_config import (
        PortableFinalEvaluationConfig,
    )

    source, dataset, blobs, policy, parameters = fixture()
    values = source.model_dump(mode="json")
    values["execution_mode"] = "optimization"
    values["optimizer"]["max_iterations"] = 1
    source = PortableRunConfig.model_validate(values)
    code = SOURCE + "# frozen selected business source\n"
    selected = policy.model_dump(mode="json")
    selected["code_sha256"] = hashlib.sha256(code.encode()).hexdigest()
    selected_id = str(UUID(int=40))
    final = PortableFinalEvaluationConfig(
        selection_id=UUID(int=41),
        split_version_id=UUID(int=42),
        split_sha256="d" * 64,
        selected_strategy={"id": selected_id, "sha256": selected["code_sha256"]},
        selected_manifest={
            "id": f"{selected_id}/manifest",
            "sha256": hash_json(selected),
        },
        selected_parameters={
            "id": f"{selected_id}/parameters",
            "sha256": hash_json(parameters),
        },
        source_config=source,
        source_config_sha256=source.sha256,
        holdout=source.windows.holdout,
        environment_digest=source.environment_digest,
        max_runtime_seconds=30,
    )
    return final, dataset, blobs, selected, parameters, code


def test_final_staging_is_separate_exact_holdout_and_final_identity(tmp_path):
    from workers.research.portable_final_staging import (
        stage_portable_final_dataset_window,
        stage_portable_final_input,
    )

    final, dataset, blobs, selected, params, code = frozen_final_values()
    projected = stage_portable_final_dataset_window(final, dataset, blobs.__getitem__)
    assert projected.report.split == "holdout"
    assert projected.report.window == final.holdout
    assert [s["session_date"] for s in projected.market["sessions"]] == [
        "2024-01-08",
        "2024-01-09",
        "2024-01-10",
    ]
    source = tmp_path / "dataset"
    source.mkdir()
    for name, raw in blobs.items():
        (source / name).write_bytes(raw)
    staged = stage_portable_final_input(
        config=final,
        manifest=dataset,
        storage_root=tmp_path,
        dataset_storage_key="dataset",
        code=code,
        policy_manifest=selected,
        parameters=params,
        target=tmp_path / "input",
        max_bytes=4_000_000,
    )
    validate_inputs(staged.engine_root, staged.policy_root, final.sha256)
    assert (
        staged.request["input_identity"]["run_config_sha256"]
        != final.source_config.sha256
    )
    assert (staged.policy_root / "policy.py").read_text() == code
    with pytest.raises(ValueError):
        stage_portable_dataset_window(
            final.source_config, "holdout", dataset, blobs.__getitem__
        )


def test_final_selection_cannot_change_semantics_or_choose_another_window():
    from packages.tradegrad_adapter.config import DateWindow
    from packages.tradegrad_adapter.portable_final_config import (
        PortableFinalEvaluationConfig,
    )
    from workers.research.portable_final_staging import (
        stage_portable_final_dataset_window,
        validate_portable_final_binding,
    )

    final, dataset, blobs, selected, params, _ = frozen_final_values()
    assert (
        validate_portable_final_binding(final, selected, params).code_sha256
        == final.selected_strategy.sha256
    )
    changed = dict(selected)
    changed["strategy_id"] = str(UUID(int=99))
    values = final.model_dump(mode="json")
    values["selected_manifest"]["sha256"] = hash_json(changed)
    rebound = PortableFinalEvaluationConfig.model_validate(values)
    with pytest.raises(ValueError, match="seed manifest"):
        validate_portable_final_binding(rebound, changed, params)
    forged = final.model_copy(
        update={"holdout": DateWindow(start="2024-01-09", end="2024-01-11")}
    )
    with pytest.raises(ValueError, match="Holdout"):
        stage_portable_final_dataset_window(forged, dataset, blobs.__getitem__)
    with pytest.raises(ValueError, match="parameters"):
        validate_portable_final_binding(final, selected, {"quantity": 2})
