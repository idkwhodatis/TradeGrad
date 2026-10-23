"""Pure projections and inert source checks; never execute an SDK or child."""

import ast
import json
import secrets
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from script import probe_v1_live_sources as probe


def test_unknown_exception_names_and_all_messages_are_dropped():
    secret = secrets.token_urlsafe(32)
    error = type(secret, (Exception,), {})(secret)
    error.response = SimpleNamespace(status_code=secret, headers={"private": secret})
    value = probe.exception_projection(error)
    assert value == {
        "status": "exception",
        "exception_chain": [{"exception_class": "other", "http_status": None}],
    }
    assert secret not in json.dumps(value)


@pytest.mark.parametrize(
    "status,expected",
    [
        (403, 403),
        (429, 429),
        (503, 503),
        (0, None),
        (600, None),
        (True, None),
        ("403", None),
    ],
)
def test_only_valid_integer_http_status_is_projected(status, expected):
    error = ValueError("private message")
    error.response = SimpleNamespace(status_code=status)
    assert (
        probe.exception_projection(error)["exception_chain"][0]["http_status"]
        == expected
    )


def test_nested_error_projection_is_bounded_and_cycle_safe():
    errors = [ValueError("private") for _ in range(10)]
    for left, right in pairwise(errors):
        left.__cause__ = right
    assert len(probe.exception_projection(errors[0])["exception_chain"]) == 4
    errors[0].__cause__ = errors[0]
    assert len(probe.exception_projection(errors[0])["exception_chain"]) == 1


@pytest.mark.parametrize("case", tuple(probe.CASES))
def test_exception_projection_roundtrips_without_extra_fields(case):
    value = probe.exception_projection(ValueError("not exported"))
    assert probe.validate_projection(case, value) == value
    assert not probe.succeeded(case, value)
    value["message"] = "private"
    with pytest.raises(ValueError):
        probe.validate_projection(case, value)


def test_success_projections_never_export_prices_names_or_metadata_values():
    private = secrets.token_hex(32)
    ak = probe.success_projection(
        "akshare_resolution",
        {
            "table": {
                "columns": ["code", "name"],
                "data": [["600000", private], ["000001", private]],
            }
        },
    )
    yahoo = probe.success_projection(
        "yahoo_aapl_resolution",
        {
            "quotes": [{"symbol": "AAPL", "shortname": private}],
            "metadata": {
                "exchangeName": "NMS",
                "currency": "USD",
                "instrumentType": "EQUITY",
                "exchangeTimezoneName": "America/New_York",
                "secret": private,
            },
        },
    )
    obb = probe.success_projection(
        "openbb_aapl_history", SimpleNamespace(results=[{"close": private}])
    )
    for case, value in [
        ("akshare_resolution", ak),
        ("yahoo_aapl_resolution", yahoo),
        ("openbb_aapl_history", obb),
    ]:
        assert private not in json.dumps(value)
        assert probe.validate_projection(case, value) == value
        assert probe.succeeded(case, value)


@pytest.mark.parametrize(
    "case,value",
    [
        (
            "akshare_resolution",
            {
                "status": "returned",
                "row_count": 1,
                "requested_symbols_found": 0,
                "expected_symbols": 2,
            },
        ),
        ("openbb_aapl_history", {"status": "returned", "row_count": 0}),
        (
            "yahoo_aapl_resolution",
            {
                "status": "returned",
                "resolution_predicates": dict.fromkeys(
                    [
                        "exact_quote",
                        "supported_exchange",
                        "usd",
                        "equity",
                        "new_york_timezone",
                    ],
                    False,
                ),
            },
        ),
    ],
)
def test_empty_or_unsupported_return_is_not_success(case, value):
    assert probe.validate_projection(case, value) == value
    assert not probe.succeeded(case, value)


@pytest.mark.parametrize(
    "value",
    [
        {"status": "returned", "row_count": True},
        {"status": "returned", "row_count": -1},
        {"status": "returned", "row_count": 100001},
        {"status": "returned", "row_count": 1, "payload": "private"},
        {
            "status": "exception",
            "exception_chain": [{"exception_class": "private", "http_status": 403}],
        },
        {
            "status": "exception",
            "exception_chain": [
                {"exception_class": "HTTPError", "http_status": "private"}
            ],
        },
    ],
)
def test_untrusted_child_envelope_rejects_arbitrary_fields(value):
    with pytest.raises(ValueError):
        probe.validate_projection("openbb_aapl_history", value)


def test_hosted_main_optin_refuses_before_sdk_or_process_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "sys.argv", ["probe", "--report", str(tmp_path / "not-created.json")]
    )
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ValueError, match="opt-in"):
        probe.main()
    assert not list(tmp_path.iterdir())


def test_fixed_cases_owned_process_boundary_and_no_acceptance_promotion():
    source = Path(probe.__file__).read_text()
    assert len(probe.CASES) == 4
    assert probe.CASE_SECONDS == 60 and probe.TOTAL_SECONDS == 300
    assert "owner = ProcessOwner(deadline)" in source
    assert "env = sdk_environment(home)" in source
    assert "finally:\n                clean = owner.cleanup()" in source
    assert "maximum=16384" in source
    assert '"automatic_retries": 0' in source
    assert '"provider_fallback": False' in source
    assert '"snapshot_or_seed_acceptance": False' in source
    assert '"applies_only_to_case": "openbb_aapl_history"' in source
    tree = ast.parse(source)
    assert not any(
        isinstance(n, ast.Attribute) and n.attr in {"killpg", "kill"}
        for n in ast.walk(tree)
    )


def test_cancellation_and_cleanup_share_one_owner_and_total_budget():
    source = Path(probe.__file__).read_text()
    tree = ast.parse(source)
    run = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    constructors = [
        node
        for node in ast.walk(run)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "ProcessOwner"
    ]
    assert len(constructors) == 1
    loop = next(
        node
        for node in ast.walk(run)
        if isinstance(node, ast.For) and ast.unparse(node.iter) == "CASES.items()"
    )
    assert not any(node is constructors[0] for node in ast.walk(loop))
    assert (
        "deadline = time.monotonic() + TOTAL_SECONDS - CLEANUP_RESERVE_SECONDS"
        in source
    )
    assert "if not clean or owner.interrupted:" in source
    assert "if time.monotonic() >= deadline or owner.interrupted:" in source
    assert probe.CLEANUP_RESERVE_SECONDS == 20
    assert '"unattempted_cases"' in source


def test_yahoo_exchange_projection_matches_actual_adapter_without_importing_sdk():
    adapter = ast.parse(Path("packages/marketdata/providers/yfinance.py").read_text())
    assignment = next(
        node
        for node in adapter.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_EXCHANGES"
            for target in node.targets
        )
    )
    accepted = set(ast.literal_eval(assignment.value))
    for exchange in accepted | {"PCX", "BTS", "unsupported"}:
        result = probe.success_projection(
            "yahoo_aapl_resolution",
            {
                "quotes": [{"symbol": "AAPL"}],
                "metadata": {"exchangeName": exchange},
            },
        )
        assert result["resolution_predicates"]["supported_exchange"] is (
            exchange in accepted
        )
