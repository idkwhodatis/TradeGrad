"""HTTP and persistence tests for run submission and idempotency."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.postgres


def test_submission_atomically_persists_run_event_and_outbox(
    submit_run, migrated_engine, owner
):
    from backend.app.models import OutboxEvent, ResearchRun, RunEvent

    response = submit_run(key="initial-submission")
    assert response.status_code in (201, 202), response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["generation"] == 1
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, UUID(body["id"]))
        assert run is not None
        assert run.owner_id == owner.user.id
        assert run.idempotency_key == "initial-submission"
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run.id)
        ).all()
        outbox = session.scalars(
            select(OutboxEvent).where(OutboxEvent.run_id == run.id)
        ).all()
        assert len(events) == 1
        assert events[0].seq == 1
        assert len(outbox) == 1
        assert outbox[0].generation == 1
        assert outbox[0].dispatched_at is None
        assert outbox[0].job_id is None


def test_replayed_request_is_same_run_without_duplicate_events_or_outbox(
    submit_run, migrated_engine
):
    from backend.app.models import OutboxEvent, ResearchRun, RunEvent

    first = submit_run(key="retry-after-response-loss")
    second = submit_run(key="retry-after-response-loss")
    assert first.status_code in (201, 202), first.text
    assert second.status_code in (200, 201, 202), second.text
    assert first.json()["id"] == second.json()["id"]
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert session.scalar(select(func.count()).select_from(RunEvent)) == 1


def test_same_key_with_different_payload_conflicts_without_mutation(
    submit_run, run_payload, migrated_engine
):
    from backend.app.models import OutboxEvent, ResearchRun, RunEvent

    first = submit_run(key="conflicting-request")
    assert first.status_code in (201, 202), first.text
    changed = {**run_payload, "end_date": "2024-03-01"}
    second = submit_run(key="conflicting-request", payload=changed)
    assert second.status_code == 409, second.text
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, UUID(first.json()["id"]))
        assert run.config["end_date"] == run_payload["end_date"]
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert session.scalar(select(func.count()).select_from(RunEvent)) == 1


def test_concurrent_identical_submissions_commit_only_one_run(
    submit_run, migrated_engine
):
    from backend.app.models import OutboxEvent, ResearchRun, RunEvent

    barrier = Barrier(6)

    def submit(_):
        barrier.wait(timeout=10)
        return submit_run(key="parallel-http-retries")

    with ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(submit, range(6)))
    assert all(item.status_code in (200, 201, 202) for item in responses), [
        (item.status_code, item.text) for item in responses
    ]
    assert len({item.json()["id"] for item in responses}) == 1
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert session.scalar(select(func.count()).select_from(RunEvent)) == 1


def test_idempotency_keys_are_scoped_to_user(
    submit_run, other, other_headers, run_payload, migrated_engine
):
    from backend.app.models import ResearchRun

    first = submit_run(key="same-key-different-owner")
    second = submit_run(
        key="same-key-different-owner",
        headers=other_headers,
        payload={
            **run_payload,
            "dataset_version_id": str(other.dataset.id),
            "strategy_version_id": str(other.strategy.id),
        },
    )
    assert first.status_code in (201, 202), first.text
    assert second.status_code in (201, 202), second.text
    assert first.json()["id"] != second.json()["id"]
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 2


@pytest.mark.parametrize("key", [None, "", "x" * 129])
def test_missing_empty_or_overlong_idempotency_keys_are_rejected(
    api, owner_headers, run_payload, key, migrated_engine
):
    from backend.app.models import OutboxEvent, ResearchRun

    headers = dict(owner_headers)
    if key is not None:
        headers["Idempotency-Key"] = key
    response = api.post("/api/v1/runs", headers=headers, json=run_payload)
    assert response.status_code in (400, 422), response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"start_date": "2024-04-01", "end_date": "2024-01-01"},
        {"start_date": "2024-01-01", "end_date": "2024-01-01"},
        {"task": "us_time"},
        {"start_date": "2023-12-31"},
        {"end_date": "2024-04-02"},
        {"owner_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
def test_invalid_runs_do_not_leave_partial_run_or_outbox(
    submit_run, run_payload, changes, migrated_engine
):
    from backend.app.models import OutboxEvent, ResearchRun, RunEvent

    response = submit_run(payload={**run_payload, **changes})
    assert response.status_code in (400, 422), response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert session.scalar(select(func.count()).select_from(RunEvent)) == 0


def test_concurrent_distinct_submissions_enforce_per_user_active_run_limit(
    submit_run, migrated_engine, settings
):
    from backend.app.models import OutboxEvent, ResearchRun

    count = settings.max_active_runs_per_user + 2
    barrier = Barrier(count)

    def submit(index):
        barrier.wait(timeout=10)
        return submit_run(key=f"quota-request-{index}")

    with ThreadPoolExecutor(max_workers=count) as executor:
        responses = list(executor.map(submit, range(count)))
    accepted = [response for response in responses if response.status_code == 202]
    rejected = [response for response in responses if response.status_code == 429]
    assert len(accepted) == settings.max_active_runs_per_user
    assert len(rejected) == 2
    assert all(response.json()["code"] == "run_quota" for response in rejected)
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == len(
            accepted
        )
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == len(
            accepted
        )


def test_idempotent_retry_still_works_when_active_quota_is_full(submit_run, settings):
    first = submit_run(key="retry-when-full")
    assert first.status_code == 202, first.text
    for index in range(settings.max_active_runs_per_user - 1):
        response = submit_run(key=f"fill-quota-{index}")
        assert response.status_code == 202, response.text
    retry = submit_run(key="retry-when-full")
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] == first.json()["id"]


def test_uncommitted_submission_rolls_back_run_event_and_outbox(
    migrated_engine, settings, owner, run_payload
):
    from backend.app.models import OutboxEvent, ResearchRun, RunEvent
    from backend.app.schemas import RunCreate
    from backend.app.services import submit_run

    with Session(migrated_engine) as session:
        run = submit_run(
            session,
            owner.user.id,
            RunCreate.model_validate(run_payload),
            "rolled-back",
            settings,
        )
        assert run.id is not None
        session.rollback()
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert session.scalar(select(func.count()).select_from(RunEvent)) == 0


@pytest.mark.parametrize("quota", [1, 2])
def test_resume_respects_configured_quota_without_partial_generation(
    api, submit_run, owner_headers, migrated_engine, settings, quota
):
    from backend.app.models import OutboxEvent, ResearchRun

    settings.max_active_runs_per_user = quota
    terminal = submit_run(key="terminal-before-quota")
    assert terminal.status_code == 202, terminal.text
    run_id = terminal.json()["id"]
    assert (
        api.post(f"/api/v1/runs/{run_id}/cancel", headers=owner_headers).status_code
        == 202
    )
    active = [submit_run(key=f"active-{index}") for index in range(quota)]
    assert all(response.status_code == 202 for response in active)
    rejected = api.post(f"/api/v1/runs/{run_id}/resume", headers=owner_headers)
    assert rejected.status_code == 429, rejected.text
    assert rejected.json()["code"] == "run_quota"
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, UUID(run_id))
        assert run.status == "cancelled"
        assert run.generation == 1
        assert (
            session.scalar(select(func.count()).select_from(OutboxEvent)) == quota + 1
        )
    freed = api.post(
        f"/api/v1/runs/{active[0].json()['id']}/cancel", headers=owner_headers
    )
    assert freed.status_code == 202, freed.text
    resumed = api.post(f"/api/v1/runs/{run_id}/resume", headers=owner_headers)
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["generation"] == 2


def test_resume_and_submission_compete_for_one_quota_slot(
    api, submit_run, owner_headers, settings, migrated_engine
):
    from backend.app.models import OutboxEvent, ResearchRun
    from backend.app.services import TERMINAL

    settings.max_active_runs_per_user = 1
    terminal = submit_run(key="race-terminal")
    assert terminal.status_code == 202, terminal.text
    run_id = terminal.json()["id"]
    assert (
        api.post(f"/api/v1/runs/{run_id}/cancel", headers=owner_headers).status_code
        == 202
    )
    barrier = Barrier(2)

    def compete(operation):
        barrier.wait(timeout=10)
        if operation == "resume":
            return api.post(f"/api/v1/runs/{run_id}/resume", headers=owner_headers)
        return submit_run(key="race-new-submission")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(compete, ["resume", "submit"]))
    assert sorted(response.status_code for response in results) == [202, 429], [
        (response.status_code, response.text) for response in results
    ]
    with Session(migrated_engine) as session:
        active_count = session.scalar(
            select(func.count())
            .select_from(ResearchRun)
            .where(ResearchRun.status.not_in(TERMINAL))
        )
        assert active_count == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 2
