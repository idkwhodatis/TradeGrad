"""Actual PostgreSQL ownership/HTTP/artifact gates; synthetic result bytes only.

A missing TEST_DATABASE_URL is an explicit skip, never SQLite or mocked database
acceptance. Native execution and browser/Docker acceptance are separate suites.
"""

import copy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import (
    DatasetVersion,
    ExecutionAttempt,
    ModelCall,
    OutboxEvent,
    ResearchRun,
    RunEvent,
)
from backend.app.research_services import (
    begin_evaluation,
    complete_evaluation,
    payload_hash,
)
from backend.tests.research_view_fixtures import synthetic_result
from packages.tradegrad_adapter.config import RunConfig, evaluation_windows
from tests.integration import test_research_lifecycle as lifecycle

research_attempt = lifecycle.research_attempt
research_payload = lifecycle.research_payload
submit_research = lifecycle.submit_research

pytestmark = pytest.mark.postgres


@pytest.fixture
def publish_view_result(
    migrated_engine, settings, owner, research_payload, research_attempt
):
    def publish():
        config = RunConfig.model_validate(research_payload)
        attempt = research_attempt
        with Session(migrated_engine, expire_on_commit=False) as session:
            item = begin_evaluation(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                candidate_key="candidate-0000",
                code_sha256=config.seed_strategy.sha256,
                window=evaluation_windows(config)[0],
            )
            dataset = session.get(DatasetVersion, UUID(config.dataset.id))
            result = synthetic_result(
                config,
                owner_id=owner.user.id,
                run_id=attempt.run_id,
                attempt_id=attempt.id,
                evaluation_id=item.id,
                dataset_manifest=dataset.manifest,
            )
            session.commit()
        path = settings.storage_root / result.artifact.storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.content)
        # Callers inspect the immutable artifact after this session has closed.
        with Session(migrated_engine, expire_on_commit=False) as session:
            session.add(result.artifact)
            session.flush()
            completed = complete_evaluation(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                item.id,
                outcome=result.evaluation.payload,
                sandbox_stopped=True,
            )
            assert completed.status == "succeeded"
            session.commit()
        return result

    return publish


def counts(engine):
    with Session(engine) as session:
        return [
            session.scalar(select(func.count()).select_from(model))
            for model in (ResearchRun, ExecutionAttempt, RunEvent, OutboxEvent)
        ]


def test_preflight_is_owner_scoped_and_creates_no_run_attempt_event_or_outbox(
    api, owner_headers, other_headers, research_payload, migrated_engine
):
    before = counts(migrated_engine)
    response = api.post(
        "/api/v1/research-runs/preflight", headers=owner_headers, json=research_payload
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert body["config_sha256"] == RunConfig.model_validate(research_payload).sha256
    assert body["evaluation_windows"] == list(
        evaluation_windows(RunConfig.model_validate(research_payload))
    )
    assert all(window["split"] != "holdout" for window in body["evaluation_windows"])
    assert counts(migrated_engine) == before
    foreign = api.post(
        "/api/v1/research-runs/preflight", headers=other_headers, json=research_payload
    )
    assert foreign.status_code == 404
    assert counts(migrated_engine) == before


def test_preflight_does_not_reserve_or_consume_active_run_quota(
    api, owner_headers, research_payload, submit_research, settings, migrated_engine
):
    settings.max_active_runs_per_user = 1
    assert submit_research().status_code == 202
    before = counts(migrated_engine)
    response = api.post(
        "/api/v1/research-runs/preflight", headers=owner_headers, json=research_payload
    )
    assert response.status_code == 200
    assert counts(migrated_engine) == before
    assert submit_research().status_code == 429


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_dataset",
        "wrong_hash",
        "warmup",
        "overlap",
        "online_model",
        "wrong_environment",
    ],
)
def test_preflight_and_submit_share_fail_closed_validation(
    api, owner_headers, research_payload, submit_research, mutation, migrated_engine
):
    payload = copy.deepcopy(research_payload)
    if mutation == "missing_dataset":
        payload["dataset"]["id"] = str(uuid4())
    elif mutation == "wrong_hash":
        payload["dataset"]["sha256"] = "0" * 64
    elif mutation == "warmup":
        payload["warmup"]["sessions"] = 1
    elif mutation == "overlap":
        payload["windows"]["validation"]["start"] = payload["windows"]["train"]["start"]
    elif mutation == "online_model":
        payload["model"].update(
            provider="openai",
            model="unavailable-model",
            credential_ref="server-reference",
        )
    else:
        payload["environment_digest"] = "0" * 64
    before = counts(migrated_engine)
    preflight = api.post(
        "/api/v1/research-runs/preflight", headers=owner_headers, json=payload
    )
    submitted = submit_research(payload=payload)
    assert preflight.status_code == submitted.status_code
    assert preflight.json()["code"] == submitted.json()["code"]
    assert counts(migrated_engine) == before


def test_persisted_results_are_bounded_owner_scoped_and_download_exact_bytes(
    api, owner_headers, other_headers, publish_view_result
):
    result = publish_view_result()
    base = f"/api/v1/research-evaluations/{result.evaluation.id}"
    response = api.get(
        base + "/results",
        headers=owner_headers,
        params=[
            ("sections", "equity"),
            ("sections", "fills"),
            ("max_points", 4),
            ("limit", 1),
        ],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tables"]["equity"]["returned_rows"] <= 4
    assert body["tables"]["equity"]["sampling"] == "min_max"
    assert body["tables"]["fills"]["returned_rows"] == 1
    assert body["metrics"][0]["value"] == 1.123456789123
    assert body["artifact_sha256"] == result.artifact.sha256
    assert body["capabilities"]["trusted_ledger"] is False
    downloaded = api.get(base + "/results/download", headers=owner_headers)
    assert downloaded.status_code == 200 and downloaded.content == result.content
    for suffix in ["/results", "/results/download", "/report"]:
        denied = api.get(base + suffix, headers=other_headers)
        assert denied.status_code == 404
        assert str(result.evaluation.id) not in denied.text
    assert (
        api.get(
            f"/api/v1/research-runs/{result.run.id}/usage", headers=other_headers
        ).status_code
        == 404
    )


def test_result_date_listing_bounds_and_safety_headers(
    api, owner_headers, publish_view_result
):
    result = publish_view_result()
    base = f"/api/v1/research-evaluations/{result.evaluation.id}"
    listing_id = next(iter(result.dataset.manifest["evaluator_symbol_mapping"]))
    response = api.get(
        base + "/results",
        headers=owner_headers,
        params=[
            ("sections", "equity"),
            ("sections", "positions"),
            ("listing_id", listing_id),
            ("start", "2024-02-01"),
            ("end", "2024-02-03"),
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json()["tables"]["equity"]["filtered_rows"] == 2
    assert response.json()["tables"]["positions"]["filter_status"] == "applied"
    for params in [
        {"listing_id": str(uuid4())},
        {"start": "2024-02-10", "end": "2024-02-01"},
        {"max_points": 5001},
        {"limit": 501},
        {"offset": 100001},
        {"sections": "unknown"},
    ]:
        assert (
            api.get(base + "/results", headers=owner_headers, params=params).status_code
            == 422
        )
    report = api.get(base + "/report", headers=owner_headers)
    assert report.status_code == 200
    assert "attachment;" in report.headers["content-disposition"]
    assert report.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in report.headers["content-security-policy"]
    assert "default-src 'none'" in report.headers["content-security-policy"]
    assert report.headers["cache-control"] == "private, no-store"
    assert b"<script" not in report.content
    assert result.artifact.sha256.encode() in report.content


def test_corrupted_persisted_artifact_never_reaches_any_projection(
    api, owner_headers, publish_view_result, settings
):
    result = publish_view_result()
    path = settings.storage_root / result.artifact.storage_key
    path.write_bytes(result.content + b"corruption")
    base = f"/api/v1/research-evaluations/{result.evaluation.id}"
    for suffix in ["/results", "/results/download", "/report"]:
        response = api.get(base + suffix, headers=owner_headers)
        assert response.status_code == 409
        assert response.json()["code"] == "artifact_integrity"


def test_compare_preserves_order_rejects_foreign_and_never_ranks(
    api, owner_headers, other_headers, publish_view_result
):
    first, second = publish_view_result(), publish_view_result()
    ids = [str(second.evaluation.id), str(first.evaluation.id)]
    response = api.post(
        "/api/v1/research-evaluations/compare",
        headers=owner_headers,
        json={"evaluation_ids": ids},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert [item["evaluation_id"] for item in data["evaluations"]] == ids
    assert data["ranking"] is None
    assert (
        data["comparable"] is False
    )  # This synthetic dataset declares no price semantics.
    assert (
        api.post(
            "/api/v1/research-evaluations/compare",
            headers=other_headers,
            json={"evaluation_ids": ids},
        ).status_code
        == 404
    )
    for invalid in [ids[:1], ids + ids, [ids[0], ids[0]], ids + [str(uuid4())] * 3]:
        assert (
            api.post(
                "/api/v1/research-evaluations/compare",
                headers=owner_headers,
                json={"evaluation_ids": invalid},
            ).status_code
            == 422
        )
    assert (
        api.post(
            "/api/v1/research-evaluations/compare",
            headers=owner_headers,
            json={"evaluation_ids": [ids[0], str(uuid4())]},
        ).status_code
        == 404
    )


def test_usage_sums_all_persisted_calls_not_a_page_and_does_not_invent_cost(
    api, owner_headers, publish_view_result, migrated_engine
):
    result = publish_view_result()
    with Session(migrated_engine) as session:
        for index in range(201):
            known = index < 200
            usage = {
                "input_tokens": 3 if known else None,
                "output_tokens": 4 if known else None,
                "charged_tokens": 7 if known else 9,
                "usage_source": "provider_reported" if known else "reservation",
                "price": None,
            }
            payload = {"synthetic": True, "usage": usage}
            session.add(
                ModelCall(
                    id=uuid4(),
                    run_id=result.run.id,
                    attempt_id=result.evaluation.attempt_id,
                    generation=1,
                    logical_call_id=f"fixture-call-{index}",
                    provider="fixture",
                    model="deterministic-fixture-v1",
                    prompt_version="source_prompts.v1",
                    status="succeeded",
                    adopted=known,
                    request_sha256="a" * 64,
                    response_sha256=None,
                    response_artifact=None,
                    usage=usage,
                    latency_seconds=0.0,
                    error_category=None,
                    payload=payload,
                    payload_sha256=payload_hash(payload),
                )
            )
        session.commit()
    response = api.get(
        f"/api/v1/research-runs/{result.run.id}/usage", headers=owner_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        body["calls"] == 201
        and body["known_usage_calls"] == 200
        and body["unknown_usage_calls"] == 1
    )
    assert body["input_tokens"] is None and body["output_tokens"] is None
    assert body["charged_tokens"] == 200 * 7 + 9
    assert body["cost"] is None and body["cost_status"] == "unavailable"
