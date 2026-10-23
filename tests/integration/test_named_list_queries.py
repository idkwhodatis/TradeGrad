"""Real migrated PostgreSQL owner/filter/order/page behavior; collect only locally."""

import hashlib

import pytest
from sqlalchemy.orm import Session

from backend.app.models import Candidate
from tests.integration import test_data_universes as universes
from tests.integration import test_research_lifecycle as lifecycle

pytestmark = pytest.mark.postgres
register_listing = universes.register_listing
research_payload = lifecycle.research_payload
submit_research = lifecycle.submit_research
research_attempt = lifecycle.research_attempt


def test_listing_filters_and_sort_apply_before_page_without_foreign_count(
    api, owner_headers, other_headers, owner, other, register_listing
):
    for symbol in ("B6AAA", "B6BBB", "B6CCC"):
        register_listing(owner, symbol)
    register_listing(other, "B6ZZZ")
    response = api.get(
        "/api/v1/data/listings",
        headers=owner_headers,
        params={
            "search": "B6",
            "mic": "xnas",
            "currency": "usd",
            "sort_by": "symbol",
            "sort_direction": "desc",
            "offset": 1,
            "limit": 1,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 3
    assert [row["symbol"] for row in response.json()["data"]] == ["B6BBB"]
    foreign = api.get(
        "/api/v1/data/listings", headers=other_headers, params={"search": "B6"}
    ).json()
    assert foreign["count"] == 1 and foreign["data"][0]["symbol"] == "B6ZZZ"
    literal = api.get(
        "/api/v1/data/listings", headers=owner_headers, params={"search": "%"}
    ).json()
    assert literal["count"] == 0


def test_candidate_sort_filter_before_page_and_owner_lookup(
    api, owner_headers, other_headers, migrated_engine, research_attempt
):
    attempt = research_attempt
    with Session(migrated_engine) as session:
        for iteration in range(4):
            code = f"# Inert PG fixture {iteration}\n"
            session.add(
                Candidate(
                    run_id=attempt.run_id,
                    attempt_id=attempt.id,
                    generation=attempt.generation,
                    candidate_key=f"b6-candidate-{iteration}",
                    kind="seed" if iteration == 0 else "generated",
                    iteration=iteration,
                    repair_index=0,
                    parent_id=None,
                    code=code,
                    code_sha256=hashlib.sha256(code.encode()).hexdigest(),
                )
            )
        session.commit()
    path = f"/api/v1/research-runs/{attempt.run_id}/candidates"
    response = api.get(
        path,
        headers=owner_headers,
        params={
            "kind": "generated",
            "min_iteration": 1,
            "search": "b6-",
            "sort_by": "iteration",
            "sort_direction": "desc",
            "offset": 1,
            "limit": 1,
        },
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["count"] == 3 and response.json()["data"][0]["iteration"] == 2
    )
    assert (
        api.get(path, headers=other_headers, params={"search": "b6"}).status_code == 404
    )


@pytest.mark.parametrize(
    "query",
    [
        {"sort_by": "DROP TABLE users"},
        {"sort_direction": "random"},
        {"offset": 100001},
        {"search": "x" * 129},
    ],
)
def test_listing_query_allowlist_is_http_enforced(api, owner_headers, query):
    assert (
        api.get(
            "/api/v1/data/listings", headers=owner_headers, params=query
        ).status_code
        == 422
    )
