"""Actual portable engine audit edges plus inert target/pre-state contract tests.

These execute the trusted fixture adapter, not an arbitrary policy or Docker
isolation harness. They never certify a whole portable replay from these parts.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from packages.research_trace.contracts import canonical_bytes
from packages.research_trace.diagnostic_parity import (
    _account_audit_complete,
    _account_trajectory,
    _audit_equity,
    _check,
    _graph_values,
    _graphs,
    _portable_targets,
    _prestate,
    _timestamp_ns,
)
from packages.research_trace.portable import INTERNALS_MISSING
from tests.portable.test_runtime_observation import ACTUAL_ENGINE_SCRIPT, ROOT


def _actual_portable(scenario):
    interpreter = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    if not interpreter:
        if os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1":
            pytest.fail("Required actual portable parity test needs native interpreter")
        pytest.skip("Actual portable engine evidence requires pinned AKQuant")
    # Keep exact real engine IDs to test audit/result topology. The audit-on/off
    # fixture normalizes only its economic comparison output, not audit IDs.
    script = ACTUAL_ENGINE_SCRIPT.replace(
        "'bundle':normalized(bundle)", "'bundle':bundle"
    )
    assert script != ACTUAL_ENGINE_SCRIPT
    result = subprocess.run(
        [interpreter, "-c", script, "audit", scenario],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "TRADEGRAD_PORTABLE_ENGINE_CONTAINER": "1",
            "TRADEGRAD_DATA_WORKERS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    value = json.loads(
        next(
            line
            for line in result.stdout.decode().splitlines()
            if line.startswith("PORTABLE_AUDIT_RESULT:")
        ).split(":", 1)[1]
    )
    assert value["error"] is None
    return SimpleNamespace(
        bundle=value["bundle"], runtime_observation={"audit": value["audit"]}
    )


@pytest.fixture(
    scope="module", params=["filled", "partial", "cancel", "reject", "zero"]
)
def actual_portable(request):
    return _actual_portable(request.param)


@pytest.fixture(scope="module")
def actual_portable_pair():
    # Separate AKQuant processes, changing prices and two actual held assets.
    # No policy is imported here: these are trusted fixture replies, not proof
    # of the separate hosted original-policy/RNG replay admission boundary.
    return _actual_portable("multi_asset"), _actual_portable("multi_asset")


def test_actual_fresh_process_multi_asset_account_and_order_evidence_matches(
    actual_portable_pair,
):
    a, b = actual_portable_pair
    before = [canonical_bytes(v.runtime_observation) for v in (a, b)]
    accounts = [_account_trajectory(v, v.runtime_observation["audit"]) for v in (a, b)]
    assert _check("account_trajectory", *accounts, sequence=True).status == "matched"
    for value, account in zip((a, b), accounts, strict=True):
        assert len({row["listing_id"] for row in value.bundle["fills"]}) == 2
        assert _account_audit_complete(value, value.runtime_observation["audit"])
        assert len(account["audit_equity"]) == sum(
            event["event_type"] == "equity"
            for event in value.runtime_observation["audit"]["events"]
        )
        assert len(account["audit_equity"]) > len(account["series"])
    for kind in ("submitted_intents", "ordered_status_history", "fills"):
        values = [_graph_values(_graphs(v), kind) for v in (a, b)]
        assert _check(kind, *values).status == "matched"
    assert [canonical_bytes(v.runtime_observation) for v in (a, b)] == before


def _inert_equity(**payload):
    return {
        "events": [
            {
                "event_type": "equity",
                "ts": 1735938000000000000,
                "symbol": None,
                "payload": {
                    "cash": "4373.6158328095004405219405917",
                    "equity": "10015.437832809500397065146870",
                    "margin": "5641.8219999999999565432062790",
                    "timestamp": "1735938000000000000",
                    **payload,
                },
            }
        ]
    }


def test_audit_decimal_text_uses_existing_amount_tolerances_without_mutation():
    # Values observed across real fresh AKQuant processes. This explicit data
    # regression remains deterministic even when a pair chooses the same native
    # aggregation order and does not reproduce the last-decimal variation.
    a = _inert_equity()
    b = _inert_equity(equity="10015.437832809500397065146871")
    before = canonical_bytes(a), canonical_bytes(b)
    assert _check("account_trajectory", a, b, sequence=True).status == "mismatch"
    assert (
        _check(
            "account_trajectory", _audit_equity(a), _audit_equity(b), sequence=True
        ).status
        == "matched"
    )
    assert (canonical_bytes(a), canonical_bytes(b)) == before


@pytest.mark.parametrize("field", ["cash", "equity"])
@pytest.mark.parametrize(
    "delta,expected", [(0.00000005, "matched"), (0.00001, "mismatch")]
)
def test_audit_amounts_respect_unchanged_tolerance_boundary(field, delta, expected):
    a = _audit_equity(_inert_equity(**{field: "1.0"}))
    b = _audit_equity(_inert_equity(**{field: str(1.0 + delta)}))
    assert _check("account_trajectory", a, b, sequence=True).status == expected


@pytest.mark.parametrize("field", ["cash", "equity"])
def test_integer_audit_amounts_retain_exact_integer_comparison(field):
    a = _audit_equity(_inert_equity(**{field: 1_000_000_000_000}))
    b = _audit_equity(_inert_equity(**{field: 1_000_000_000_001}))
    assert _check("account_trajectory", a, b, sequence=True).status == "mismatch"


@pytest.mark.parametrize(
    "field,changed",
    [
        ("margin", "5641.8219999999999565432062791"),
        ("timestamp", "1735938000000000001"),
        ("quantity", "1.0000000000000000000000000001"),
        ("unknown_amount", "1.0000000000000000000000000001"),
    ],
)
def test_audit_projection_never_coerces_other_fields(field, changed):
    a = _inert_equity()
    if field not in a["events"][0]["payload"]:
        a["events"][0]["payload"][field] = "1.0"
    b = copy.deepcopy(a)
    b["events"][0]["payload"][field] = changed
    assert (
        _check(
            "account_trajectory", _audit_equity(a), _audit_equity(b), sequence=True
        ).status
        == "mismatch"
    )


@pytest.mark.parametrize("field", ["cash", "equity"])
@pytest.mark.parametrize(
    "amount",
    [None, True, "bad", "NaN", "Infinity", "-Infinity", "1e9999", "", " 1", "1_0"],
)
def test_malformed_audit_amounts_cannot_become_equal_missing_evidence(field, amount):
    assert _audit_equity(_inert_equity(**{field: amount})) is None


@pytest.mark.parametrize("field", ["cash", "equity"])
def test_absent_audit_amount_is_not_fabricated(field):
    audit = _inert_equity()
    del audit["events"][0]["payload"][field]
    assert _audit_equity(audit) is None


@pytest.mark.parametrize(
    "change",
    ["amount", "drop", "duplicate", "order", "nanosecond", "symbol", "drawdown"],
)
def test_actual_account_trajectory_keeps_every_strict_dimension(
    actual_portable_pair, change
):
    value = actual_portable_pair[0]
    a = _account_trajectory(value, value.runtime_observation["audit"])
    b = copy.deepcopy(a)
    events = b["audit_equity"]
    if change == "amount":
        # Alter an intermediate same-timestamp observation only, not the final
        # daily account row. Coverage alone must not hide this difference.
        events[0]["payload"]["equity"] += 1.0
    elif change == "drop":
        events.pop(0)
    elif change == "duplicate":
        events.insert(0, copy.deepcopy(events[0]))
    elif change == "order":
        events[0], events[-1] = events[-1], events[0]
    elif change == "nanosecond":
        events[0]["ts"] += 1
    elif change == "symbol":
        events[0]["symbol"] = "other"
    else:
        b["series"][0]["drawdown"] += 1e-15
    assert _check("account_trajectory", a, b, sequence=True).status == "mismatch"


def test_real_portable_order_status_parent_fill_and_account_coverage(actual_portable):
    graph = _graphs(actual_portable)
    assert graph is not None
    assert len(graph) == len(actual_portable.bundle["orders"])
    assert _account_audit_complete(
        actual_portable, actual_portable.runtime_observation["audit"]
    )
    for kind in ("submitted_intents", "ordered_status_history", "fills"):
        values = _graph_values(graph, kind)
        assert _check(kind, values, values).status == "matched"


def test_missing_actual_equity_receipts_never_pass(actual_portable):
    audit = copy.deepcopy(actual_portable.runtime_observation["audit"])
    audit["events"] = [e for e in audit["events"] if e["event_type"] != "equity"]
    assert not _account_audit_complete(actual_portable, audit)


def test_actual_portable_topology_cannot_be_guessed(actual_portable):
    value = copy.deepcopy(actual_portable)
    if not value.bundle["orders"]:
        assert _graphs(value) == {}
        return
    value.bundle["logical_orders"][0]["native_order_ids"] = ["missing-child"]
    assert _graphs(value) is None


def test_actual_portable_fill_id_and_nanosecond_are_required(actual_portable):
    if not actual_portable.bundle["fills"]:
        return
    for field in ("trade_id", "ts"):
        value = copy.deepcopy(actual_portable)
        event = next(
            e
            for e in value.runtime_observation["audit"]["events"]
            if e["event_type"] == "trade"
        )
        if field == "trade_id":
            event["payload"][field] = "unregistered"
        else:
            event[field] += 1
        assert _graphs(value) is None


def test_exact_fixed_config_not_economic_tolerance():
    assert (
        _check(
            "fixed_conditions", {"weight": 1.0}, {"weight": 1.000000001}, sequence=True
        ).status
        == "mismatch"
    )
    assert _timestamp_ns("2024-01-02T09:30:00.000000123+08:00") == 1704159000000000123


def target_row(*, event=False):
    return {
        "stages": [
            {
                "stage_id": "portable.policy_reply",
                "coverage": "partial",
                "missing_reason": INTERNALS_MISSING,
                "raw_targets": None if event else [{"listing_id": "B", "value": 1}],
            },
            {
                "stage_id": "portable.order_intents"
                if event
                else "portable.controller_plan",
                "coverage": "complete",
                "constrained_plan": [{"listing_id": "B", "quantity": 1}],
            },
        ]
    }


@pytest.mark.parametrize("event", [False, True])
def test_portable_channels_and_listing_scope_are_explicit(event):
    row = target_row(event=event)
    target = _portable_targets(row, {"instrument_ids": ["B"]})
    assert target is not None and "B" in canonical_bytes(target).decode()
    filtered = _portable_targets(row, {"instrument_ids": ["C"]})
    assert filtered is not None and "B" not in canonical_bytes(filtered).decode()
    row["stages"][1]["constrained_plan"] = None
    assert _portable_targets(row, {}) is None


def test_portfolio_unknown_reply_and_missing_stage_are_not_empty_hold():
    row = target_row()
    row["stages"][0]["raw_targets"] = None
    assert _portable_targets(row, {}) is None
    assert (
        _portable_targets(target_row(), {"stage_ids": ["portable.policy_reply"]})
        is None
    )


def test_portable_open_logical_order_requires_explicit_child_links():
    state = {
        "coverage": "complete",
        "source": "adapter_observed",
        "missing_reason": None,
        "cash": 10,
        "equity": 10,
        "positions": [],
        "strategy_state": {},
        "open_orders": [
            {"order_id": "logical", "native_order_ids": ["native"], "quantity": 1}
        ],
    }
    row = {"pre_state": state}
    graph = {"native": {"order": {"quantity": 1}}}
    assert _prestate(row, graph) is not None
    state["open_orders"][0]["native_order_ids"] = ["other"]
    assert _prestate(row, graph) is None


@pytest.mark.parametrize("portable", [False, True])
def test_same_final_order_different_path_keeps_prestate_explicit_edge(portable):
    # Both sides keep the same total graph multiset, but choose a different
    # partially-filled order in the recorded pre-state. Final rows alone collide.
    graph = {
        "A": {
            "order": {"quantity": 2, "price": 10},
            "statuses": [{"status": "partial"}],
            "fills": [{"price": 9, "quantity": 1}, {"price": 11, "quantity": 1}],
            "audit_fills": [],
        },
        "B": {
            "order": {"quantity": 2, "price": 10},
            "statuses": [{"status": "partial"}],
            "fills": [{"price": 11, "quantity": 1}, {"price": 9, "quantity": 1}],
            "audit_fills": [],
        },
    }

    def record(child):
        order = (
            {"order_id": "logical", "native_order_ids": [child]}
            if portable
            else {"order_id": child}
        )
        return {
            "pre_state": {
                "coverage": "complete",
                "source": "adapter_observed",
                "positions": [],
                "open_orders": [order],
            }
        }

    assert _graph_values(graph, "fills") == _graph_values(graph, "fills")
    assert _prestate(record("A"), graph) != _prestate(record("B"), graph)
