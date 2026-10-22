"""Real PostgreSQL acceptance: immutable authoring, owner boundaries and races.

Collect locally without a server. These tests never substitute SQLite or fake
sessions, and must be executed against the migrated PostgreSQL release stack.
"""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import StrategyVersion

pytestmark = pytest.mark.postgres
PATH = "/api/v1/strategy-versions"


def payload(**changes):
    return {
        "name": "Uploaded native version",
        "task": "cn_time",
        "code": '# Unicode 原样\r\nvalue = "é"\r\n',
        "parent_id": None,
        **changes,
    }


def create(api, headers, body=None, key=None):
    return api.post(
        PATH,
        headers={**headers, "Idempotency-Key": key or uuid4().hex},
        json=body or payload(),
    )


def test_create_replay_conflict_and_exact_utf8(
    api, owner_headers, migrated_engine, owner
):
    key, source = uuid4().hex, payload()
    first = create(api, owner_headers, source, key)
    assert first.status_code == 201, first.text
    item = first.json()
    assert item["mode"] == "native_akquant"
    assert item["code"] == source["code"]
    assert item["content_sha256"] == sha256(source["code"].encode("utf-8")).hexdigest()
    replay = create(api, owner_headers, source, key)
    assert replay.status_code == 200 and replay.json() == item
    for changes in [
        {"name": "Changed"},
        {"task": "us_time"},
        {"code": source["code"] + "\n"},
        {"parent_id": str(owner.strategy.id)},
    ]:
        conflict = create(api, owner_headers, {**source, **changes}, key)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_conflict"
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(StrategyVersion)
                .where(StrategyVersion.owner_id == owner.user.id)
            )
            == 2
        )


def test_parent_and_owner_access_are_checked_without_mutating_original(
    api, owner_headers, other_headers, owner, other
):
    original = api.get(f"{PATH}/{owner.strategy.id}", headers=owner_headers).json()
    created = create(api, owner_headers, payload(parent_id=str(owner.strategy.id)))
    assert created.status_code == 201
    child = created.json()
    assert child["parent_id"] == str(owner.strategy.id)
    assert child["id"] != str(owner.strategy.id)
    assert (
        api.get(f"{PATH}/{owner.strategy.id}", headers=owner_headers).json() == original
    )
    assert (
        create(
            api, owner_headers, payload(parent_id=str(other.strategy.id))
        ).status_code
        == 404
    )
    assert (
        create(api, owner_headers, payload(parent_id=str(uuid4()))).status_code == 404
    )
    incompatible = create(
        api, owner_headers, payload(task="us_time", parent_id=str(owner.strategy.id))
    )
    assert incompatible.status_code == 422
    assert incompatible.json()["code"] == "incompatible_parent"
    assert api.get(f"{PATH}/{child['id']}", headers=other_headers).status_code == 404
    other_list = api.get(PATH, headers=other_headers).json()
    assert child["id"] not in {item["id"] for item in other_list["data"]}
    for method in ("patch", "put", "delete"):
        assert (
            getattr(api, method)(
                f"{PATH}/{child['id']}", headers=owner_headers
            ).status_code
            == 405
        )


def test_same_key_is_scoped_to_owner_and_distinct_keys_create_new_versions(
    api, owner_headers, other_headers
):
    key = uuid4().hex
    first = create(api, owner_headers, key=key)
    other = create(api, other_headers, key=key)
    duplicate_content = create(api, owner_headers)
    assert (
        first.status_code == other.status_code == duplicate_content.status_code == 201
    )
    assert (
        len({first.json()["id"], other.json()["id"], duplicate_content.json()["id"]})
        == 3
    )


def test_parallel_identical_submissions_commit_exactly_one_version(
    api, owner_headers, owner, migrated_engine
):
    key, barrier = uuid4().hex, Barrier(4)

    def request(_):
        barrier.wait(timeout=10)
        return create(api, owner_headers, key=key)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(request, range(4)))
    assert sorted(response.status_code for response in responses) == [
        200,
        200,
        200,
        201,
    ]
    assert len({response.json()["id"] for response in responses}) == 1
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(StrategyVersion)
                .where(StrategyVersion.owner_id == owner.user.id)
            )
            == 2
        )


def test_parallel_conflicting_submissions_cannot_overwrite_winner(api, owner_headers):
    key, barrier = uuid4().hex, Barrier(2)

    def request(index):
        barrier.wait(timeout=10)
        return create(api, owner_headers, payload(name=f"Concurrent {index}"), key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(request, range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(
        response.json() for response in responses if response.status_code == 201
    )
    assert api.get(f"{PATH}/{winner['id']}", headers=owner_headers).json() == winner


def test_upload_only_parses_and_syntax_failure_does_not_consume_key(
    api, owner_headers, tmp_path
):
    marker = tmp_path / "never-executed"
    code = f"import nonexistent_uploaded_module\nopen({str(marker)!r}, 'w').write('executed')\n"
    assert create(api, owner_headers, payload(code=code)).status_code == 201
    assert not marker.exists()
    key = uuid4().hex
    failed = create(api, owner_headers, payload(code="def broken(\n"), key)
    assert (
        failed.status_code == 422 and failed.json()["code"] == "strategy_syntax_error"
    )
    assert create(api, owner_headers, key=key).status_code == 201


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"code": "#" + "中" * 50000},
        {"code": "\x00"},
        {"mode": "portable"},
        {"owner_id": str(uuid4())},
    ],
)
def test_invalid_body_has_no_write(api, owner_headers, change):
    before = api.get(PATH, headers=owner_headers).json()["count"]
    assert create(api, owner_headers, payload(**change)).status_code == 422
    assert api.get(PATH, headers=owner_headers).json()["count"] == before


def test_create_requires_authentication_and_idempotency_header(api, owner_headers):
    assert (
        api.post(
            PATH, json=payload(), headers={"Idempotency-Key": uuid4().hex}
        ).status_code
        == 401
    )
    assert api.post(PATH, json=payload(), headers=owner_headers).status_code == 422
