"""Real PostgreSQL HTTP replay admission, ownership, immutable bytes and exposure.

Persisted observations are explicit synthetic boundary fixtures, not engine or
Playwright certification. A required selector fails closed without PostgreSQL.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import DataUsageExposure, ExecutionAttempt, ResearchRun
from backend.tests.test_replay_services import duplicate_observations
from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.recorder import Recorder
from tests.integration import test_research_lifecycle as lifecycle
from tests.integration import test_result_exploration_http as prices
from tests.integration import test_strategy_packages as packages
from tests.integration import test_trace_api as traces

base_research_payload = prices.research_payload
submit_research = lifecycle.submit_research
research_attempt = lifecycle.research_attempt
publish_trace = traces.publish_trace
publish_initial = lifecycle.publish_initial
complete_candidate = lifecycle.complete_candidate
completed_source = packages.completed_source
frozen_selection = packages.frozen_selection
package_reference = packages.package_reference
pytestmark = pytest.mark.postgres


@pytest.fixture
def research_payload(base_research_payload, request):
    return {
        **base_research_payload,
        "research_capture": getattr(request, "param", {"mode": "research"}),
    }


@pytest.fixture
def published_replay(publish_trace, monkeypatch):
    def capture(*, code_hash, begin):
        recorder = Recorder(
            code_sha256=code_hash, config=CaptureConfig(mode="research")
        )
        for seq in range(2):
            moment = datetime.combine(begin, datetime.min.time(), tzinfo=UTC)
            recorder.begin_decision(
                data_event_id="frozen:daily",
                event_seq=seq,
                scope="portfolio",
                callback_phase="on_bar",
                information_cutoff=moment,
                decision_time=moment,
                market_timezone="Asia/Shanghai",
            )
            recorder.emit(
                "execution",
                {
                    **duplicate_observations(code_hash, moment),
                    "order_links": [
                        {
                            "logical_order_id": f"intent-{seq}",
                            "child_order_ids": ["order-1"] if seq == 0 else [],
                            "fill_ids": [],
                            "instrument_id": "target_symbol",
                            "submitted_at": None,
                            "fill_times": [],
                            "status": "accepted",
                            "source": "strategy_emitted",
                        }
                    ],
                },
            )
            recorder.end_decision()
        return recorder

    monkeypatch.setattr(traces, "captured", capture)
    return publish_trace()


def paths(value):
    base = f"/api/v1/evaluations/{value.result.evaluation.id}"
    decision = value.recorder.records[0].decision_id
    return [
        base + "/replay-instruments",
        base + f"/decisions/{decision}/market",
        base + f"/decisions/{decision}/execution",
    ]


def params(value):
    return {
        "execution_attempt_id": str(value.result.evaluation.attempt_id),
        "result_manifest_sha256": value.result.artifact.sha256,
        "trace_manifest_sha256": value.registrations[-1].sha256,
        "instrument_id": "target_symbol",
    }


def test_replay_required_postgres_gate():
    if os.environ.get("TRADEGRAD_REPLAY_POSTGRES_REQUIRED") == "1":
        assert os.environ.get("TEST_DATABASE_URL"), (
            "Required replay HTTP acceptance needs real PostgreSQL TEST_DATABASE_URL"
        )


def test_replay_exact_leaf_owner_source_and_exposure_before_success(
    api,
    owner_headers,
    other_headers,
    published_replay,
    migrated_engine,
):
    value = published_replay
    with Session(migrated_engine) as session:
        before = session.scalar(
            select(func.count())
            .select_from(DataUsageExposure)
            .where(DataUsageExposure.purpose == "result_preview")
        )
    for path in paths(value):
        assert api.get(path, params=params(value)).status_code == 401
        assert (
            api.get(path, headers=other_headers, params=params(value)).status_code
            == 404
        )
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DataUsageExposure)
                .where(DataUsageExposure.purpose == "result_preview")
            )
            == before
        )
    bodies = []
    for path in paths(value):
        response = api.get(path, headers=owner_headers, params=params(value))
        assert response.status_code == 200, response.text
        body = response.json()
        bodies.append(body)
        assert body["context"]["evaluation_id"] == str(value.result.evaluation.id)
        assert body["context"]["execution_attempt_id"] == str(
            value.result.evaluation.attempt_id
        )
        assert body["context"]["result_manifest_sha256"] == value.result.artifact.sha256
        assert (
            body["context"]["trace_manifest_sha256"] == value.registrations[-1].sha256
        )
    instruments, market, execution = bodies
    assert instruments["items"][0]["instrument_id"] == "target_symbol"
    assert instruments["items"][0]["configured_evaluation_member"] is True
    assert instruments["source_association"]["strategy_basis"] == "saved_seed"
    assert value.result.evaluation.candidate_id is None
    assert market["status"] == "available" and market["bars"][0]["close"] == 10
    assert market["session_date"] == "2024-02-01" and market["total"] == 1
    assert [row["record_kind"] for row in execution["items"]] == [
        "logical_order",
        "child_order",
        "fill",
    ]
    assert execution["items"][-1]["record"]["order_id"] == "order-1"
    assert execution["items"][-1]["association_basis"] == "child_order_id"
    second = value.recorder.records[1].decision_id
    second_result = api.get(
        paths(value)[2].replace(value.recorder.records[0].decision_id, second),
        headers=owner_headers,
        params=params(value),
    )
    assert second_result.status_code == 200, second_result.text
    assert second_result.json()["total"] == 1  # Same date is not execution linkage.
    with Session(migrated_engine) as session:
        exposures = session.scalars(
            select(DataUsageExposure).where(
                DataUsageExposure.purpose == "result_preview"
            )
        ).all()
        assert len(exposures) == before + 1
        assert exposures[-1].provenance["evaluation_id"] == str(
            value.result.evaluation.id
        )


@pytest.mark.parametrize("route", range(3))
@pytest.mark.parametrize(
    "pin", ["execution_attempt_id", "result_manifest_sha256", "trace_manifest_sha256"]
)
def test_replay_pins_never_retarget(api, owner_headers, published_replay, route, pin):
    value = published_replay
    query = {
        **params(value),
        pin: str(uuid4()) if pin == "execution_attempt_id" else "f" * 64,
    }
    response = api.get(paths(value)[route], headers=owner_headers, params=query)
    assert (
        response.status_code == 409 and response.json()["code"] == "trace_pin_mismatch"
    )


@pytest.mark.parametrize("route", range(3))
@pytest.mark.parametrize("query", [{"limit": 501}, {"offset": 100001}, {"limit": 0}])
def test_replay_http_page_limits(api, owner_headers, published_replay, route, query):
    value = published_replay
    response = api.get(
        paths(value)[route], headers=owner_headers, params={**params(value), **query}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("route", range(3))
def test_replay_corrupt_result_never_exposes_related_data(
    api, owner_headers, published_replay, settings, route
):
    value = published_replay
    (settings.storage_root / value.result.artifact.storage_key).write_bytes(
        value.result.content + b" "
    )
    response = api.get(paths(value)[route], headers=owner_headers, params=params(value))
    assert response.status_code == 409


@pytest.mark.parametrize("fault", ["prices", "universe", "trace"])
def test_replay_changed_frozen_files_are_not_reconstructed(
    api, owner_headers, published_replay, settings, fault
):
    value = published_replay
    dataset = value.result.dataset
    if fault == "trace":
        path = next(
            item.path for item in value.artifacts if "-trace-events-" in item.name
        )
    elif fault == "universe":
        name = next(
            name for name in dataset.manifest["files"] if name.startswith("universe/")
        )
        path = settings.storage_root / dataset.storage_key / name
    else:
        listing = next(iter(dataset.manifest["preview_files"]))
        path = (
            settings.storage_root
            / dataset.storage_key
            / dataset.manifest["preview_files"][listing][0]["path"]
        )
    path.write_bytes(path.read_bytes() + b"tampered")
    response = api.get(paths(value)[1], headers=owner_headers, params=params(value))
    assert response.status_code == 409
    assert str(settings.storage_root) not in response.text


def test_replay_missing_trace_preserves_unknown_not_empty(
    api, owner_headers, publish_trace
):
    value = publish_trace(collected=False)
    base = f"/api/v1/evaluations/{value.result.evaluation.id}/decisions/uncollected"
    for suffix, field in [("/market", "bars"), ("/execution", "items")]:
        response = api.get(
            base + suffix,
            headers=owner_headers,
            params={"instrument_id": "target_symbol"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert (
            body["status"] == "not_collected"
            and body[field] is None
            and body["total"] is None
        )


@pytest.mark.parametrize("fault", ["pending", "retry", "unfinished_attempt"])
@pytest.mark.parametrize(
    "suffix",
    ["/replay-instruments", "/decisions/absent/market", "/decisions/absent/execution"],
)
def test_replay_final_admission_fence_precedes_missing_trace(
    api, owner_headers, package_reference, migrated_engine, fault, suffix
):
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, package_reference.run_id)
        attempt = session.get(ExecutionAttempt, run.active_attempt_id)
        if fault == "pending":
            run.status, run.result = "running", None
        elif fault == "retry":
            run.generation += 1
        else:
            attempt.finished_at = None
        session.commit()
    response = api.get(
        f"/api/v1/evaluations/{package_reference.evaluation_id}{suffix}",
        headers=owner_headers,
        params={"instrument_id": "target_symbol"},
    )
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("research_payload", [{"mode": "off"}], indirect=True)
def test_registered_off_capture_is_pinned_not_historical_absence(
    api, owner_headers, publish_trace, monkeypatch
):
    def off(*, code_hash, begin):
        return Recorder(code_sha256=code_hash, config=CaptureConfig(mode="off"))

    monkeypatch.setattr(traces, "captured", off)
    value = publish_trace()
    base = f"/api/v1/evaluations/{value.result.evaluation.id}"
    expected_hash = value.registrations[-1].sha256
    for suffix in [
        "/replay-instruments",
        "/decisions/unobserved/market",
        "/decisions/unobserved/execution",
    ]:
        response = api.get(base + suffix, headers=owner_headers, params=params(value))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["context"]["status"] == "not_collected"
        assert body["context"]["trace_manifest_sha256"] == expected_hash
        assert body["context"]["binding"]["evaluation_id"] == str(
            value.result.evaluation.id
        )
        assert body["context"]["missing_reason"]
        if suffix.endswith(("market", "execution")):
            assert body["status"] == "not_collected" and body["total"] is None
        wrong = api.get(
            base + suffix,
            headers=owner_headers,
            params={**params(value), "trace_manifest_sha256": "f" * 64},
        )
        assert wrong.status_code == 409 and wrong.json()["code"] == "trace_pin_mismatch"


def test_duplicate_local_ids_keep_saved_row_identity_in_http_pages(
    api, owner_headers, published_replay
):
    value = published_replay
    base = paths(value)[2].removesuffix("/execution")
    detail_pages = []
    candidate_pages = []
    for offset in (0, 1):
        query = {**params(value), "stage_id": "execution", "offset": offset, "limit": 1}
        detail = api.get(base, headers=owner_headers, params=query)
        candidate = api.get(base + "/candidates", headers=owner_headers, params=query)
        assert detail.status_code == candidate.status_code == 200
        detail_pages.append(detail.json())
        candidate_pages.append(candidate.json())
    for kind in ("features", "predicates"):
        rows = [page[kind]["items"][0] for page in detail_pages]
        assert [row["ordinal"] for row in rows] == [1, 2]
        assert [row["observation"]["source_ref"]["line_start"] for row in rows] == [
            11,
            12,
        ]
        assert all(
            row["observation"]["instrument_id"] == "target_symbol" for row in rows
        )
    assert [
        page["features"]["items"][0]["observation"]["value"] for page in detail_pages
    ] == [0, 3]
    assert [
        page["predicates"]["items"][0]["observation"]["state"] for page in detail_pages
    ] == ["false", "true"]
    assert [page["items"][0]["ordinal"] for page in candidate_pages] == [1, 2]
    assert [page["items"][0]["observation"]["score"] for page in candidate_pages] == [
        0,
        3,
    ]


def test_http_exact_ordinal_restores_second_duplicate_without_old_page(
    api, owner_headers, published_replay
):
    value = published_replay
    base = paths(value)[2].removesuffix("/execution")
    query = {**params(value), "stage_id": "execution", "record_ordinal": 2}
    for suffix in ["", "/candidates"]:
        response = api.get(base + suffix, headers=owner_headers, params=query)
        assert response.status_code == 200, response.text
        page = response.json()["features"] if not suffix else response.json()
        assert page["total"] == 1 and len(page["items"]) == 1
        row = page["items"][0]
        assert (
            row["ordinal"] == 2 and row["observation"]["source_ref"]["line_start"] == 12
        )
        assert row["observation"]["value" if not suffix else "score"] == 3


@pytest.mark.parametrize(
    "query",
    [
        {"record_ordinal": 0},
        {"record_ordinal": -1, "stage_id": "execution"},
        {"record_ordinal": "1 OR true", "stage_id": "execution"},
    ],
)
def test_http_ordinal_requires_valid_scope(api, owner_headers, published_replay, query):
    value = published_replay
    base = paths(value)[2].removesuffix("/execution")
    for suffix in ["", "/candidates"]:
        response = api.get(
            base + suffix, headers=owner_headers, params={**params(value), **query}
        )
        assert response.status_code == 422, response.text
