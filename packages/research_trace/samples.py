"""Hand-checkable I0 inputs and expected observations, not a backtest engine.

The literal expectations below are independent of template calculations. Nothing
in this module generates returns, orders, fills or account trajectories. Execute
these frozen bars through AKQuant to obtain actual execution evidence. Contract
examples explicitly labelled hand_worked are not represented as engine output.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class FrozenInstrument:
    instrument_id: str
    closes: tuple[float, ...]
    volumes: tuple[float, ...]


@dataclass(frozen=True)
class FrozenTape:
    sample_id: str
    dates: tuple[str, ...]
    instruments: tuple[FrozenInstrument, ...]
    universe: tuple[tuple[str, tuple[str, ...]], ...]
    initial_cash: float = 10000.0
    market_timezone: str = "America/New_York"


DATES = (
    "2024-01-02",
    "2024-01-03",
    "2024-01-04",
    "2024-01-05",
    "2024-01-08",
    "2024-01-09",
    "2024-01-10",
    "2024-01-11",
)
CROSS_SECTION_TAPE = FrozenTape(
    sample_id="controlled-cross.hand-worked.v1",
    dates=DATES,
    instruments=(
        FrozenInstrument(
            "A", (10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 12.0), (1000.0,) * 8
        ),
        FrozenInstrument(
            "B", (10.0, 11.0, 12.0, 13.0, 12.0, 11.0, 10.0, 11.0), (1000.0,) * 8
        ),
        FrozenInstrument(
            "C", (10.0, 12.0, 15.0, 16.0, 15.0, 14.0, 13.0, 12.0), (10.0,) * 8
        ),
        FrozenInstrument(
            "D", (10.0, 10.2, 10.5, 10.5, 10.5, 10.5, 10.5, 10.5), (1000.0,) * 8
        ),
    ),
    universe=(("2024-01-02", ("A", "B", "C", "D")), ("2024-01-09", ("A", "C", "D"))),
)
TIMING_TAPE = FrozenTape(
    sample_id="controlled-timing.hand-worked.v1",
    dates=DATES,
    instruments=(
        FrozenInstrument(
            "target_symbol",
            (12.0, 11.0, 10.0, 11.0, 12.0, 10.0, 9.0, 8.0),
            (1000.0,) * 8,
        ),
    ),
    universe=(),
)


@dataclass(frozen=True)
class CandidateExpectation:
    instrument_id: str
    score: float
    average_dollar_volume: float
    eligible: bool
    rank: int | None
    selected: bool
    reason: str
    target_quantity: float


# Jan 4: oldest-to-newest, 3 completed daily bars, no holdings, $10,000
# cash/equity, lot size 1. B's 12/10 - 1 = .20 leads eligible names; C's
# 15/10 - 1 = .50 is higher but (10+12+15)*10/3 = $123 1/3 < $1,000.
# Raw B target = .8*$10,000/$12 = 666 2/3 shares; .4 weight cap and
# whole-share rounding produce 333. These are intents, not fill claims.
CROSS_FIRST_READY = (
    CandidateExpectation("A", 0.10, 10500.0, True, 2, False, "rank_not_selected", 0.0),
    CandidateExpectation("B", 0.20, 11000.0, True, 1, True, "selected", 333.0),
    CandidateExpectation(
        "C", 0.50, 123.33333333333333, False, None, False, "eligibility_filter", 0.0
    ),
    CandidateExpectation(
        "D", 0.05, 10233.333333333334, True, 3, False, "rank_not_selected", 0.0
    ),
)

# Same input slice, independently specified prior state. D has 100 shares
# worth $1,050 and rank 3, inside 1 + 2 retention buffer. It is kept while B
# is admitted. With $600 cash and $1,650 equity, reserve=$82.50, available
# cash=$517.50 and B's .4 weight cap=$660; the stricter gross budget
# below wins before cash, yielding 22 shares.
RETENTION_CASH_STATE = MappingProxyType(
    {"cash": 600.0, "equity": 1650.0, "D_quantity": 100.0}
)
RETENTION_CASH_EXPECTED = MappingProxyType(
    {
        # D retention does not override the .4 per-name cap: floor(660/10.5)=62.
        "D_target_quantity": 62.0,
        "D_reason": "holding_retained",
        "B_target_quantity": 22.0,
        # Gross budget is conservative: .8*1650 - old retained value 1050 =270;
        # floor(270/12)=22. No unfilled D sale proceeds are counted as cash.
        "B_constraints": ("risk_cap",),
    }
)
CASH_ONLY_STATE = MappingProxyType(
    {"cash": 10000.0, "equity": 10000.0, "cash_reserve": 0.99}
)
# Independently controlled cash-reserve variant: .99*$10,000 leaves $100;
# floor($100/$12)=8 shares. No hypothetical missing holdings are required.
CASH_ONLY_EXPECTED = MappingProxyType(
    {"B_target_quantity": 8.0, "B_constraints": ("risk_cap", "cash_reserve")}
)

# Jan 4: fast=(11+10)/2=10.5, slow=(12+11+10)/3=11, so no entry.
# Jan 8: fast=(11+12)/2=11.5, slow=(10+11+12)/3=11, entry true;
# floor(.4*10000/12)=333 shares before any native fill.
TIMING_EXPECTED = MappingProxyType(
    {
        "2024-01-04": MappingProxyType(
            {
                "fast_mean": 10.5,
                "slow_mean": 11.0,
                "entry_state": "false",
                "reason": "timing_not_triggered",
                "target_quantity": 0.0,
            }
        ),
        "2024-01-08": MappingProxyType(
            {
                "fast_mean": 11.5,
                "slow_mean": 11.0,
                "entry_state": "true",
                "reason": "selected",
                "target_quantity": 333.0,
            }
        ),
    }
)


@dataclass(frozen=True)
class ConditionExample:
    state: str
    operands: tuple[float, ...] | None
    threshold: float | None
    missing_reason: str | None
    meaning: str


# Contract examples, not fabricated engine observations. In real execution,
# not_sampled is produced by the capture policy, not by skipping computation.
MISSING_STATE_EXAMPLES = (
    ConditionExample(
        "false",
        (10.5,),
        11.0,
        None,
        "The timing condition was evaluated and did not trigger.",
    ),
    ConditionExample(
        "not_evaluated",
        None,
        None,
        "warmup_insufficient",
        "The entry comparison was short-circuited during warmup.",
    ),
    ConditionExample(
        "unknown",
        None,
        1000.0,
        "input_missing",
        "The liquidity input was unavailable, not below threshold.",
    ),
    ConditionExample(
        "not_sampled",
        None,
        None,
        "detail_scope_excluded",
        "The strategy still computed the full pool; this detail was not retained.",
    ),
)

# Portfolio identity includes sequence, not only date, timestamp, or symbol.
SAME_TIMESTAMP_EVENTS = (
    (
        "2024-01-04T21:00:00+00:00",
        "daily_slice:1704402000000000000",
        "on_cross_section",
        1,
    ),
    (
        "2024-01-04T21:00:00+00:00",
        "daily_slice:1704402000000000000",
        "on_cross_section",
        2,
    ),
)

# Frozen fixture provenance. A source change requires a deliberate fixture review,
# not silently regenerating expected observations from the changed implementation.
CONTROLLED_SOURCE_SHA256 = MappingProxyType(
    {
        "cross_sectional": "3de9608c98cb3827a3253dbdc1c3974149b52b869fa403600945695a3a5bfed7",
        "daily_timing": "5d53e60265dd28874d922c6dc4a5792c4d55b79b33ac1ac3196823207ac4459e",
    }
)
