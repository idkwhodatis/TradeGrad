"""Real PostgreSQL/HTTP authoring/preflight acceptance. Collect only without services."""

import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from backend.tests.test_strategy_authoring_contracts import declaration, portable_body
from tests.integration import test_research_lifecycle as lifecycle

research_payload = lifecycle.research_payload

pytestmark = pytest.mark.postgres


def test_native_declaration_replay_conflict_and_preflight(
    api, owner_headers, research_payload
):
    body = {
        "name": "Declared custom native",
        "task": "cn_time",
        "code": "raise AssertionError('INERT SOURCE')\n",
        "manifest": declaration(),
    }
    key = uuid4().hex
    first = api.post(
        "/api/v1/strategy-versions",
        headers={**owner_headers, "Idempotency-Key": key},
        json=body,
    )
    assert first.status_code == 201, first.text
    version = first.json()
    assert version["parameters"] == {"allocation": 0.2}
    assert (
        api.post(
            "/api/v1/strategy-versions",
            headers={**owner_headers, "Idempotency-Key": key},
            json=body,
        ).json()
        == version
    )
    changed = copy.deepcopy(body)
    changed["manifest"]["parameters"][0]["value"] = 0.8
    assert (
        api.post(
            "/api/v1/strategy-versions",
            headers={**owner_headers, "Idempotency-Key": key},
            json=changed,
        ).status_code
        == 409
    )
    config = copy.deepcopy(research_payload)
    config["seed_strategy"] = {"id": version["id"], "sha256": version["content_sha256"]}
    config["strategy_contract"] = version["manifest"]
    response = api.post(
        "/api/v1/research-runs/preflight", headers=owner_headers, json=config
    )
    assert response.status_code == 422 and "amount" in response.text
    del config["strategy_contract"]
    response = api.post(
        "/api/v1/research-runs/preflight", headers=owner_headers, json=config
    )
    assert (
        response.status_code == 422
        and response.json()["code"] == "strategy_contract_mismatch"
    )


def test_portable_source_owner_immutability_and_exact_idempotent_race(
    api, owner_headers, other_headers
):
    body, key, barrier = portable_body(), uuid4().hex, Barrier(3)

    def create(_):
        barrier.wait(timeout=10)
        return api.post(
            "/api/v1/portable-strategy-versions",
            headers={**owner_headers, "Idempotency-Key": key},
            json=body,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(create, range(3)))
    assert sorted(response.status_code for response in responses) == [200, 200, 201]
    item = responses[0].json()
    assert all(response.json() == item for response in responses)
    assert item["code"] == body["code"]
    assert (
        api.get(
            f"/api/v1/portable-strategies/{item['id']}", headers=other_headers
        ).status_code
        == 404
    )
    assert (
        api.get(
            f"/api/v1/portable-strategies/{item['id']}/source-draft",
            headers=other_headers,
        ).status_code
        == 404
    )
    child = copy.deepcopy(body)
    child.update(parent_id=item["id"], code=body["code"] + "# immutable child\n")
    response = api.post(
        "/api/v1/portable-strategy-versions",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        json=child,
    )
    assert response.status_code == 201 and response.json()["parent_id"] == item["id"]
    assert (
        api.get(
            f"/api/v1/portable-strategies/{item['id']}", headers=owner_headers
        ).json()
        == item
    )
    conflict = api.post(
        "/api/v1/portable-strategy-versions",
        headers={**owner_headers, "Idempotency-Key": key},
        json=child,
    )
    assert conflict.status_code == 409
    denied = api.post(
        "/api/v1/portable-strategy-versions",
        headers={**other_headers, "Idempotency-Key": uuid4().hex},
        json=child,
    )
    assert denied.status_code == 404
    for method in ("patch", "put", "delete"):
        assert (
            getattr(api, method)(
                f"/api/v1/portable-strategies/{item['id']}", headers=owner_headers
            ).status_code
            == 405
        )
