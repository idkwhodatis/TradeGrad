"""Real pinned calendar and synthetic fixture tests, never paid/live providers."""

import hashlib
import json
from datetime import UTC, date, datetime

import pytest

from packages.marketdata.calendars import session_schedule
from packages.marketdata.contracts import (
    BarsRequest,
    PriceSemantics,
    ProviderResult,
)
from packages.marketdata.portable_fixture import (
    GENERATOR_VERSION,
    PortableFixtureProvider,
    portable_fixture_result,
)
from packages.marketdata.providers.registry import get_provider
from packages.marketdata.storage import verify_bundle, write_bundle
from packages.tradegrad_adapter.config import canonical_bytes


def request(symbol="AAPL", start="2024-01-02", end="2024-02-01"):
    listing = PortableFixtureProvider().resolve_instruments([symbol]).data[0]
    return BarsRequest(
        listings=[listing],
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        as_of=datetime(2025, 1, 2, tzinfo=UTC),
        price_mode="raw",
    )


def test_schedule_uses_actual_dst_early_close_holidays_and_next_open():
    ny = session_schedule(request(start="2024-11-27", end="2024-12-03"))
    sessions = ny["calendars"]["XNYS"]["sessions"]
    assert [s["session_date"] for s in sessions] == [
        "2024-11-27",
        "2024-11-29",
        "2024-12-02",
    ]
    assert sessions[0]["next_open_time"] == "2024-11-29T14:30:00Z"
    assert sessions[1]["close_time"] == "2024-11-29T18:00:00Z"
    assert sessions[1]["next_open_time"] == "2024-12-02T14:30:00Z"
    assert (
        sessions[-1]["next_open_time"] == "2024-12-03T14:30:00Z"
    )  # outside requested bars
    assert ny["generator"] == {"name": "exchange_calendars", "version": "4.13.2"}
    binding = next(iter(ny["listing_calendars"].values()))
    assert binding["mic"] == "XNAS" and binding["calendar"] == "XNYS"
    assert (
        binding["calendar_basis"] == "declared_proxy"
        and "not certified" in binding["note"]
    )
    dst = session_schedule(request(start="2024-03-08", end="2024-03-12"))["calendars"][
        "XNYS"
    ]["sessions"]
    assert dst[0]["open_time"] == "2024-03-08T14:30:00Z"
    assert dst[0]["next_open_time"] == "2024-03-11T13:30:00Z"
    assert dst[1]["open_time"] == "2024-03-11T13:30:00Z"
    cn = session_schedule(request("000001", "2024-02-08", "2024-02-20"))
    assert [s["session_date"] for s in cn["calendars"]["XSHG"]["sessions"]] == [
        "2024-02-08",
        "2024-02-19",
    ]
    assert (
        cn["calendars"]["XSHG"]["sessions"][0]["next_open_time"]
        == "2024-02-19T01:30:00Z"
    )


@pytest.mark.parametrize("symbol", ["AAPL", "MSFT", "600000", "000001"])
def test_raw_synthetic_fixture_bundle_pins_actual_sessions_and_action_evidence(
    tmp_path, symbol
):
    req = request(symbol)
    result = portable_fixture_result(req)
    manifest = write_bundle(tmp_path / "data", req, result)
    verify_bundle(tmp_path / "data", manifest)
    assert manifest["ready"]
    assert (
        result.price_semantics.split_basis
        == result.price_semantics.dividend_basis
        == "unadjusted"
    )
    assert result.price_semantics.algorithm_version == GENERATOR_VERSION
    assert result.actions == []
    assert all(p.evidence == "fixture" for p in result.provenance)
    clocks = json.loads((tmp_path / "data/sessions.json").read_bytes())
    actions = json.loads(
        (tmp_path / "data/corporate-action-coverage.json").read_bytes()
    )
    assert actions["is_synthetic"] is True
    assert actions["listings"][0]["status"] == "known_none"
    assert set(manifest["artifact_files"]) >= {
        "sessions.json",
        "corporate-action-coverage.json",
    }
    assert (
        hashlib.sha256((tmp_path / "data/sessions.json").read_bytes()).hexdigest()
        == manifest["artifact_files"]["sessions.json"]
    )
    # Missing price rows cannot remove or alter the independent calendar schedule.
    sparse = result.model_copy(update={"data": result.data[2:]})
    sparse_manifest = write_bundle(tmp_path / "sparse", req, sparse)
    assert (
        sparse_manifest["artifact_files"]["sessions.json"]
        == manifest["artifact_files"]["sessions.json"]
    )
    assert clocks["start"] == str(req.start)


def test_unknown_actions_never_promoted_by_empty_array(tmp_path):
    req = request()
    generated = portable_fixture_result(req)
    unknown = ProviderResult(
        data=generated.data,
        coverage=generated.coverage,
        price_semantics=PriceSemantics(
            native_mode="raw", split_basis="unadjusted", dividend_basis="unadjusted"
        ),
    )
    write_bundle(tmp_path / "unknown", req, unknown)
    value = json.loads(
        (tmp_path / "unknown/corporate-action-coverage.json").read_bytes()
    )
    assert value["is_synthetic"] is False
    assert value["listings"][0]["status"] == "unknown"


def test_fixture_blocks_overlap_exactly_and_cannot_run_live():
    full = portable_fixture_result(request())
    tail = portable_fixture_result(request(start="2024-01-10"))
    assert [
        bar for bar in full.data if bar.session_date >= date(2024, 1, 10)
    ] == tail.data
    assert get_provider("portable_fixture", mode="fixture").mode == "fixture"
    with pytest.raises(ValueError, match="live"):
        get_provider("portable_fixture", mode="live")
    with pytest.raises(ValueError, match="covers"):
        portable_fixture_result(request(start="2023-12-01"))
    with pytest.raises(ValueError, match="raw"):
        portable_fixture_result(request().model_copy(update={"price_mode": "qfq"}))


def test_entire_generated_bundle_stages_into_engine_abi_without_executing_code(
    tmp_path,
):
    from uuid import UUID

    from packages.strategy_sdk import (
        DataRequirements,
        EngineProfile,
        PriceAssumptions,
        StateCodec,
        StateField,
        StateSchema,
        StrategyManifest,
        UniverseAssumptions,
        hash_json,
    )
    from packages.tradegrad_adapter.portable_config import PortableRunConfig
    from runtimes.evaluator.portable_runner import validate_inputs
    from runtimes.portable_engine.runtime import load_inputs, profile_sha256
    from workers.research.portable_staging import stage_portable_input

    for symbol, market, kind in [
        ("AAPL", "US", "event"),
        ("600000", "CN", "event"),
        ("MSFT", "US", "portfolio"),
        ("000001", "CN", "portfolio"),
    ]:
        req = request(symbol)
        directory = tmp_path / symbol
        dataset = write_bundle(directory, req, portable_fixture_result(req))
        schema = StateSchema(
            name="test",
            version=1,
            fields=(StateField(name="last_session", kind="string"),),
        )
        params = (
            {"quantity": 100 if market == "CN" else 1}
            if kind == "event"
            else {"top_n": 1}
        )
        code = "raise AssertionError('NEVER EXECUTE ON HOST')\n"
        policy = StrategyManifest(
            strategy_kind=kind,
            strategy_id=str(UUID(int=20)),
            code_sha256=hashlib.sha256(code.encode()).hexdigest(),
            parameters_sha256=hash_json(params),
            market=market,
            currency=req.listings[0].currency,
            calendar=req.listings[0].calendar,
            timezone=req.listings[0].timezone,
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
        ref = lambda value: {"id": str(UUID(int=3)), "sha256": value}
        config = PortableRunConfig(
            task=f"{market.lower()}_{'time' if kind == 'event' else 'cross'}",
            strategy_kind=kind,
            dataset=ref(hashlib.sha256(canonical_bytes(dataset)).hexdigest()),
            seed_strategy=ref(policy.code_sha256),
            seed_manifest=ref(hash_json(policy)),
            seed_parameters=ref(hash_json(params)),
            universe=ref("e" * 64),
            windows={"train": {"start": "2024-01-08", "end": "2024-01-15"}},
            warmup={"sessions": 2},
            initial_state=StateCodec(schema).encode({"last_session": ""}),
            environment_digest="f" * 64,
        )
        staged = stage_portable_input(
            config=config,
            manifest=dataset,
            storage_root=tmp_path,
            dataset_storage_key=symbol,
            code=code,
            policy_manifest=policy,
            parameters=params,
            target=tmp_path / f"{symbol}-input",
            max_bytes=32_000_000,
            split="train",
        )
        validate_inputs(staged.engine_root, staged.policy_root, config.sha256)
        # load_inputs is a trusted parser + real calendar check only; no AKQuant
        # import/execute or policy import. This does not certify container isolation.
        inputs = load_inputs(
            (staged.engine_root / "request.json").read_bytes(),
            (staged.engine_root / "market.json").read_bytes(),
        )
        assert inputs.profile["risk_config"] == {
            "account_mode": "cash",
            "enable_short_sell": False,
        }
