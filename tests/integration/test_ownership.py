"""Authenticated HTTP boundaries, including replay streams and file downloads."""

import hashlib
import json
import secrets
from datetime import timedelta
from uuid import UUID

import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.postgres


@pytest.fixture
def completed_artifact(migrated_engine, settings, claimed_attempt):
    from backend.app.models import Artifact
    from backend.app.services import finish_attempt

    attempt = claimed_attempt
    content = b'{"schema_version":1,"metrics":{"total_return":0.1}}\n'
    storage_key = f"runs/{attempt.run_id}/attempts/{attempt.id}/result.json"
    path = settings.storage_root / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    metadata = {
        "name": "result.json",
        "media_type": "application/json",
        "storage_key": storage_key,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    with Session(migrated_engine, expire_on_commit=False) as session:
        finish_attempt(
            session,
            attempt.id,
            attempt.generation,
            attempt.lease_token,
            {"status": "succeeded", "exit_code": 0, "metrics": {"total_return": 0.1}},
            True,
            [metadata],
        )
        session.commit()
        artifact = session.scalar(
            select(Artifact).where(Artifact.attempt_id == attempt.id)
        )
        return artifact, path, content


def test_login_uses_password_verification_and_returns_no_credentials(api, owner):
    wrong = api.post(
        "/api/v1/login/access-token",
        data={"username": owner.user.email, "password": secrets.token_urlsafe(32)},
    )
    absent = api.post(
        "/api/v1/login/access-token",
        data={"username": "absent@example.com", "password": owner.password},
    )
    assert wrong.status_code == absent.status_code == 401
    assert wrong.json()["code"] == absent.json()["code"]
    correct = api.post(
        "/api/v1/login/access-token",
        data={"username": owner.user.email, "password": owner.password},
    )
    if correct.status_code != 200:
        pytest.fail("Generated test account could not authenticate", pytrace=False)
    token = correct.json()
    if not token.get("access_token") or token.get("token_type") != "bearer":
        pytest.fail("Authentication response has invalid token metadata", pytrace=False)
    if not isinstance(token.get("expires_in"), int) or token["expires_in"] <= 0:
        pytest.fail("Authentication response has invalid token expiry", pytrace=False)
    if owner.password in correct.text or owner.user.hashed_password in correct.text:
        pytest.fail("Authentication response exposed password material", pytrace=False)


@pytest.mark.parametrize("kind", ["expired", "wrong-signature", "missing-expiry"])
def test_invalid_tokens_are_rejected(api, settings, owner, kind):
    from backend.app.models import utcnow

    payload = {
        "sub": str(owner.user.id),
        "iat": utcnow() - timedelta(minutes=2),
        "exp": utcnow() + timedelta(minutes=2),
        "iss": "tradegrad",
        "aud": "tradegrad-api",
    }
    if kind == "expired":
        payload["exp"] = utcnow() - timedelta(seconds=1)
    elif kind == "missing-expiry":
        del payload["exp"]
    key = (
        secrets.token_urlsafe(48) if kind == "wrong-signature" else settings.secret_key
    )
    token = jwt.encode(payload, key, algorithm="HS256")
    response = api.get("/api/v1/datasets", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401, response.text


def test_disabling_user_invalidates_existing_access_token(
    api, migrated_engine, owner, owner_headers
):
    from backend.app.models import User

    with Session(migrated_engine) as session:
        session.get(User, owner.user.id).is_active = False
        session.commit()
    response = api.get("/api/v1/datasets", headers=owner_headers)
    assert response.status_code == 401, response.text


def test_list_endpoints_only_return_the_authenticated_owners_resources(
    api, owner, other, owner_headers, other_headers, submit_run
):
    response = submit_run()
    assert response.status_code in (201, 202), response.text
    for path, own_id, foreign_id in (
        ("/api/v1/datasets", owner.dataset.id, other.dataset.id),
        ("/api/v1/strategy-versions", owner.strategy.id, other.strategy.id),
    ):
        result = api.get(path, headers=owner_headers)
        assert result.status_code == 200, result.text
        ids = {item["id"] for item in result.json()["data"]}
        assert str(own_id) in ids
        assert str(foreign_id) not in ids
    result = api.get("/api/v1/runs", headers=other_headers)
    assert result.status_code == 200, result.text
    assert result.json()["data"] == []
    assert result.json()["count"] == 0


def test_foreign_resources_and_events_are_not_disclosed(
    api, owner, owner_headers, other_headers, queued_run
):
    paths = [
        f"/api/v1/datasets/{owner.dataset.id}",
        f"/api/v1/strategy-versions/{owner.strategy.id}",
        f"/api/v1/runs/{queued_run['id']}",
        f"/api/v1/runs/{queued_run['id']}/events?follow=false",
        f"/api/v1/runs/{queued_run['id']}/artifacts",
    ]
    for path in paths:
        own = api.get(path, headers=owner_headers)
        assert own.status_code == 200, own.text
        foreign = api.get(path, headers=other_headers)
        assert foreign.status_code == 404, (path, foreign.text)
        anonymous = api.get(path)
        assert anonymous.status_code == 401, (path, anonymous.text)


def test_foreign_owner_cannot_cancel_resume_or_submit_references(
    api, owner, other, other_headers, queued_run, run_payload, migrated_engine
):
    from backend.app.models import ResearchRun

    for action in ("cancel", "resume"):
        response = api.post(
            f"/api/v1/runs/{queued_run['id']}/{action}", headers=other_headers
        )
        assert response.status_code == 404, response.text
    for reference in ("dataset_version_id", "strategy_version_id"):
        payload = {
            **run_payload,
            "dataset_version_id": str(other.dataset.id),
            "strategy_version_id": str(other.strategy.id),
            reference: str(
                owner.dataset.id
                if reference == "dataset_version_id"
                else owner.strategy.id
            ),
        }
        response = api.post(
            "/api/v1/runs",
            headers={**other_headers, "Idempotency-Key": f"foreign-{reference}"},
            json=payload,
        )
        assert response.status_code == 404, response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 1
        assert session.get(ResearchRun, UUID(queued_run["id"])).status == "queued"


def test_artifact_list_and_download_enforce_ownership_and_exact_bytes(
    api, owner_headers, other_headers, completed_artifact
):
    artifact, _, content = completed_artifact
    listing = api.get(
        f"/api/v1/runs/{artifact.run_id}/artifacts", headers=owner_headers
    )
    assert listing.status_code == 200, listing.text
    records = listing.json()["data"]
    assert len(records) == 1
    assert records[0]["id"] == str(artifact.id)
    assert records[0]["sha256"] == hashlib.sha256(content).hexdigest()
    assert "storage_key" not in records[0]
    download = f"/api/v1/artifacts/{artifact.id}/download"
    response = api.get(download, headers=owner_headers)
    assert response.status_code == 200, response.text
    assert response.content == content
    assert api.get(download, headers=other_headers).status_code == 404
    assert api.get(download).status_code == 401


def test_corrupted_artifact_is_not_served_as_valid(
    api, owner_headers, completed_artifact
):
    artifact, path, _ = completed_artifact
    path.write_bytes(b"corrupted after publication")
    response = api.get(
        f"/api/v1/artifacts/{artifact.id}/download", headers=owner_headers
    )
    assert response.status_code == 409, response.text
    assert b"corrupted after publication" not in response.content


def test_artifact_storage_path_cannot_escape_root(
    api, owner_headers, completed_artifact, migrated_engine, settings
):
    from backend.app.models import Artifact

    artifact, _, _ = completed_artifact
    secret = b"outside-storage-private-data"
    outside = settings.storage_root.parent / "outside.txt"
    outside.write_bytes(secret)
    with Session(migrated_engine) as session:
        record = session.get(Artifact, artifact.id)
        record.storage_key = "../outside.txt"
        record.sha256 = hashlib.sha256(secret).hexdigest()
        record.size_bytes = len(secret)
        session.commit()
    response = api.get(
        f"/api/v1/artifacts/{artifact.id}/download", headers=owner_headers
    )
    assert response.status_code == 500, response.text
    assert response.json()["code"] == "unsafe_storage_path"
    assert secret not in response.content


def test_sse_replay_has_ordered_ids_and_resumes_after_last_event(
    api, owner_headers, queued_run
):
    cancel = api.post(f"/api/v1/runs/{queued_run['id']}/cancel", headers=owner_headers)
    assert cancel.status_code == 202, cancel.text
    path = f"/api/v1/runs/{queued_run['id']}/events?follow=false"
    response = api.get(path, headers=owner_headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")

    def events(text):
        parsed = []
        for block in text.replace("\r\n", "\n").strip().split("\n\n"):
            fields = dict(
                line.split(":", 1)
                for line in block.splitlines()
                if ":" in line and not line.startswith(":")
            )
            if "data" in fields:
                parsed.append((int(fields["id"].strip()), json.loads(fields["data"])))
        return parsed

    original = events(response.text)
    assert [event_id for event_id, _ in original] == [1, 2]
    assert all(data["schema_version"] == 1 for _, data in original)
    assert all(data["run_id"] == queued_run["id"] for _, data in original)
    assert [data["seq"] for _, data in original] == [1, 2]
    replay = api.get(path, headers={**owner_headers, "Last-Event-ID": "1"})
    assert replay.status_code == 200, replay.text
    assert events(replay.text) == original[1:]
    exhausted = api.get(path, headers={**owner_headers, "Last-Event-ID": "2"})
    assert exhausted.status_code == 200, exhausted.text
    assert events(exhausted.text) == []


@pytest.mark.parametrize("cursor", ["-1", "not-an-event", "1:2", "9" * 30])
def test_sse_invalid_resume_cursor_is_rejected(api, owner_headers, queued_run, cursor):
    response = api.get(
        f"/api/v1/runs/{queued_run['id']}/events?follow=false",
        headers={**owner_headers, "Last-Event-ID": cursor},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_event_cursor"


def test_mutation_without_authentication_never_creates_a_run(
    api, run_payload, migrated_engine
):
    from backend.app.models import OutboxEvent, ResearchRun

    response = api.post(
        "/api/v1/runs", headers={"Idempotency-Key": "anonymous"}, json=run_payload
    )
    assert response.status_code == 401, response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0
