"""No historical timestamp is upgraded to a newly corrected daily label."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.trace_services import timestamp_evidence


def result(*, profile="corrected_explicit_config", mode=None, explanation=None):
    return SimpleNamespace(
        bundle={
            "identity": {"profile": profile},
            "evaluation_mode": mode,
            "engine": {
                "name": "akquant",
                "series_semantics": {
                    "timestamps": explanation or "ISO 8601 native offsets"
                },
            },
        }
    )


def test_new_native_daily_labels_disclose_cutoff_and_lookahead_limits():
    evidence = timestamp_evidence(
        result(
            explanation="Date-only daily CSV inputs use market-local midnight session labels, not observed exchange-close or data-availability instants."
        )
    )
    assert evidence["timestamp_semantics"] == "native_daily_session_labels"
    for required in (
        "information-cutoff",
        "market-local midnight session labels",
        "not observed",
        "Same-session OHLC",
        "absence of look-ahead",
        "not rewritten",
    ):
        assert required in evidence["timestamp_caveat"]


@pytest.mark.parametrize("profile", ["corrected_explicit_config", "source_compat"])
def test_old_native_offsets_are_not_relabelled_as_fixed_midnight(profile):
    evidence = timestamp_evidence(result(profile=profile))
    assert evidence["timestamp_semantics"] == "native_recorded_unqualified"
    assert "retained exactly as recorded" in evidence["timestamp_caveat"]
    assert "market-local midnight" not in evidence["timestamp_caveat"]


def test_portable_keeps_recorded_engine_instant_semantics_without_native_caveat():
    assert timestamp_evidence(result(mode="portable")) == {
        "timestamp_semantics": "portable_engine_instants",
        "timestamp_caveat": None,
    }


def test_missing_engine_metadata_has_unknown_not_invented_close_semantics():
    evidence = timestamp_evidence(SimpleNamespace(bundle={}))
    assert evidence["timestamp_semantics"] == "unknown"
    assert (
        "no actual data-availability instant is inferred"
        in evidence["timestamp_caveat"]
    )


def test_comparison_read_time_projection_qualifies_each_side_without_manifest_rewrite(
    monkeypatch,
):
    # Explicitly fake DTO assembly only. Actual ownership, manifests and storage
    # admission have their separate PostgreSQL/Parquet contracts.
    from backend.app import comparison_services

    monkeypatch.setattr(
        comparison_services, "ComparisonResult", lambda **values: values
    )
    manifest = {"historical_manifest": "unchanged"}
    owned = SimpleNamespace(
        comparison=SimpleNamespace(id=str(uuid4()), input_sha256="a" * 64),
        artifact=SimpleNamespace(id=str(uuid4()), sha256="b" * 64),
        manifest=manifest,
        sides=[
            SimpleNamespace(
                source={"side": "left"}, trace=SimpleNamespace(result=result())
            ),
            SimpleNamespace(
                source={"side": "right"},
                trace=SimpleNamespace(result=result(mode="portable")),
            ),
        ],
    )
    projected = comparison_services.comparison_result_view(owned)
    assert projected["manifest"] is manifest
    assert (
        projected["left_timestamp_evidence"]["timestamp_semantics"]
        == "native_recorded_unqualified"
    )
    assert (
        projected["right_timestamp_evidence"]["timestamp_semantics"]
        == "portable_engine_instants"
    )
    assert projected["right_timestamp_evidence"]["timestamp_caveat"] is None
