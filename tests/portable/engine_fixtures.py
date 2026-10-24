"""Trusted portable-engine inputs and replies shared with native subprocess tests.

Keep this module independent of pytest and collected test modules: the frozen
native evaluator intentionally has no test-runner dependencies. These helpers
only construct maintained test data and replies; they never import candidates
or execute AKQuant.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from uuid import UUID

from packages.strategy_sdk import (
    BarView,
    DataRequirements,
    EngineProfile,
    EventDecision,
    EventReply,
    ListingView,
    OrderIntent,
    PortfolioReply,
    PriceAssumptions,
    StateCodec,
    StateField,
    StateSchema,
    StrategyManifest,
    TargetPortfolio,
    UniverseAssumptions,
    canonical_json,
    hash_json,
)
from runtimes.portable_engine.runtime import Session, iso, load_inputs, reviewed_profile

L1, L2 = str(UUID(int=1)), str(UUID(int=2))
SCHEMA = StateSchema(
    name="engine-test", version=1, fields=(StateField(name="counter", kind="integer"),)
)


def fixture(market="US", kind="portfolio", listing_count=1, prewarm=False):
    cn = market == "CN"
    zone, calendar, currency, mic = (
        ("Asia/Shanghai", "XSHG", "CNY", "XSHG")
        if cn
        else ("America/New_York", "XNYS", "USD", "XNAS")
    )
    sessions = []
    for day in (2, 3, 6):
        close = datetime(2025, 1, day, 7 if cn else 21, tzinfo=UTC)
        opening = close.replace(hour=1 if cn else 14, minute=30)
        next_day = 3 if day == 2 else (6 if day == 3 else 7)
        sessions.append(
            Session(close.date(), opening, close, opening.replace(day=next_day))
        )
    listings = tuple(
        ListingView(
            listing_id=key,
            instrument_id=key,
            symbol="AAA" if key == L1 else "BBB",
            mic=mic,
            currency=currency,
            calendar=calendar,
            timezone=zone,
            effective_from=date(2020, 1, 1),
            known_at=datetime(2020, 1, 1, tzinfo=UTC),
            lot_size=100 if cn else 1,
        )
        for key in (L1, L2)[:listing_count]
    )
    bars = [
        BarView(
            listing_id=l.listing_id,
            session_date=s.session_date,
            completed_at=s.close_time,
            available_at=s.close_time,
            open=10.0,
            high=12.0,
            low=8.0,
            close=10.0,
            volume=1_000_000.0,
        )
        for s in sessions
        for l in listings
    ]
    data = {
        "listings": [l.model_dump(mode="json") for l in listings],
        "bars": [b.model_dump(mode="json") for b in bars],
        "sessions": [
            {
                "session_date": s.session_date.isoformat(),
                "open_time": iso(s.open_time),
                "close_time": iso(s.close_time),
                "next_open_time": iso(s.next_open_time),
            }
            for s in sessions
        ],
        "corporate_actions": [],
    }
    manifest = StrategyManifest(
        strategy_kind=kind,
        strategy_id=str(UUID(int=100)),
        code_sha256="a" * 64,
        parameters_sha256=hash_json({}),
        market=market,
        currency=currency,
        calendar=calendar,
        timezone=zone,
        data=DataRequirements(
            lookback_bars=3, warmup_bars=1, max_listings=listing_count
        ),
        universe=UniverseAssumptions(
            semantics="user_fixed",
            snapshot_sha256=hash_json(data["listings"]),
            survivorship_bias="possible",
        ),
        prices=PriceAssumptions(actions_snapshot_sha256=hash_json([])),
        state_schema=SCHEMA,
        engine_profile=EngineProfile(
            profile_sha256=hash_json(reviewed_profile(market, 10000.0)),
            environment_sha256="e" * 64,
        ),
    )
    request = {
        "schema_version": "portable-engine-request.v1",
        "manifest": manifest.model_dump(mode="json"),
        "window": {
            "start": "2025-01-03" if prewarm else "2025-01-02",
            "end": "2025-01-07",
        },
        "initial_cash": 10000.0,
        "initial_state": StateCodec(SCHEMA)
        .encode({"counter": 0})
        .model_dump(mode="json"),
        "input_identity": {
            "run_config_sha256": "b" * 64,
            "market_data_sha256": hashlib.sha256(canonical_json(data)).hexdigest(),
        },
    }
    return request, data


def inputs(market="US", kind="portfolio", listing_count=1, prewarm=False):
    request, data = fixture(market, kind, listing_count, prewarm)
    return load_inputs(
        canonical_json(request), canonical_json(data), calendar_verifier=lambda *_: None
    )


def changed_input(request, market):
    request["input_identity"]["market_data_sha256"] = hashlib.sha256(
        canonical_json(market)
    ).hexdigest()
    return load_inputs(
        canonical_json(request),
        canonical_json(market),
        calendar_verifier=lambda *_: None,
    )


def state_after(obs):
    return StateCodec(SCHEMA).encode(
        {"counter": StateCodec(SCHEMA).decode(obs.state)["counter"] + 1}
    )


def portfolio_reply(obs, action="hold", targets=(), basis="target_quantity"):
    action = "no_decision" if obs.warmup else action
    return PortfolioReply(
        decision=TargetPortfolio(
            signal_id=f"s:{obs.session_date}",
            effective_at=obs.next_open_time,
            expires_at=None,
            action=action,
            basis=basis,
            targets=targets if action == "rebalance" else (),
            omitted_positions="liquidate" if action == "liquidate" else "preserve",
        ),
        next_state=state_after(obs),
    ).model_dump(mode="json")


def event_reply(obs, intents=()):
    return EventReply(
        decision=EventDecision(intents=intents), next_state=state_after(obs)
    ).model_dump(mode="json")


def order(obs, side="buy", quantity=10.0, **changes):
    fields = {
        "signal_id": f"o:{obs.session_date}",
        "listing_id": L1,
        "side": side,
        "quantity": quantity,
        "order_type": "market",
        "reduce_only": side == "sell",
        "position_effect": "close" if side == "sell" else "open",
        "effective_at": obs.next_open_time,
        "expires_at": None,
        "time_in_force": "GTC",
    }
    fields.update(changes)
    return OrderIntent(**fields)
