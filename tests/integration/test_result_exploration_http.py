"""Real PostgreSQL + ASGI + checksum files. Collection only needs no services.

Synthetic persisted observations are deliberate; source execution and browser
acceptance live in separate opt-in system/Playwright gates, not this suite.
"""

import copy
import hashlib

import pytest
from sqlalchemy.orm import Session

from backend.app.models import DatasetVersion
from packages.tradegrad_adapter.config import canonical_bytes
from tests.integration import test_research_lifecycle as lifecycle
from tests.integration import test_research_view_http as views

pytestmark = pytest.mark.postgres
research_attempt = lifecycle.research_attempt
submit_research = lifecycle.submit_research
publish_view_result = views.publish_view_result


@pytest.fixture
def research_payload(migrated_engine, settings, owner):
    body = lifecycle.research_payload.__wrapped__(migrated_engine, settings, owner)
    with Session(migrated_engine) as session:
        dataset = session.get(DatasetVersion, owner.dataset.id)
        manifest = copy.deepcopy(dataset.manifest)
        listing = next(iter(manifest["evaluator_symbol_mapping"]))
        bars = [
            {
                "listing_id": listing,
                "session_date": "2024-02-01",
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.0,
                "volume": 100.0,
                "currency": "CNY",
                "volume_unit": "shares",
                "flags": ["synthetic_test"],
            }
        ]
        raw = canonical_bytes(bars)
        name = "preview-test.json"
        (settings.storage_root / dataset.storage_key / name).write_bytes(raw)
        manifest["files"][name] = hashlib.sha256(raw).hexdigest()
        manifest["preview_files"] = {
            listing: [
                {
                    "path": name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "start_date": "2024-02-01",
                    "end_date": "2024-02-02",
                }
            ]
        }
        dataset.manifest = manifest
        dataset.content_sha256 = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        (settings.storage_root / dataset.storage_key / "manifest.json").write_bytes(
            canonical_bytes(manifest)
        )
        body["dataset"]["sha256"] = dataset.content_sha256
        session.commit()
    return body


def test_replay_analysis_sort_download_and_owner_isolation(
    api, owner_headers, other_headers, publish_view_result
):
    result = publish_view_result()
    identifier = result.evaluation.id
    listing = next(iter(result.dataset.manifest["evaluator_symbol_mapping"]))
    base = f"/api/v1/research-evaluations/{identifier}"
    analysis = api.get(base + "/return-analysis", headers=owner_headers)
    assert analysis.status_code == 200, analysis.text
    assert analysis.json()["scope"] == "full_persisted_evaluation_portfolio"
    assert analysis.json()["months"][0]["status"] == "partial_period"
    replay = api.get(
        base + "/price-replay",
        headers=owner_headers,
        params={"listing_id": listing, "limit": 1},
    )
    assert replay.status_code == 200, replay.text
    assert len(replay.json()["bars"]) == 1
    assert replay.json()["markers"][0]["id"] == "fill:0"
    assert replay.json()["markers"][0]["on_price_bar"]
    assert replay.json()["indicators_status"] == "not_persisted"
    sorted_result = api.get(
        base + "/results",
        headers=owner_headers,
        params={
            "sections": "fills",
            "sort_by": "price",
            "sort_direction": "desc",
            "side": "buy",
            "limit": 1,
        },
    )
    assert sorted_result.status_code == 200, sorted_result.text
    assert sorted_result.json()["tables"]["fills"]["row_indices"] == [0]
    assert sorted_result.json()["metrics"] == result.bundle["metrics"]
    empty = api.get(
        base + "/results",
        headers=owner_headers,
        params={"sections": "fills", "side": "sell"},
    )
    assert empty.json()["tables"]["fills"]["filtered_rows"] == 0
    for suffix in ("/return-analysis", f"/price-replay?listing_id={listing}"):
        assert api.get(base + suffix, headers=other_headers).status_code == 404
        assert api.get(base + suffix).status_code == 401
    assert (
        api.get(base + "/results/download", headers=owner_headers).content
        == result.content
    )


@pytest.mark.parametrize("suffix", ["/return-analysis", "/price-replay"])
def test_corrupt_result_blocks_new_read_paths(
    api, owner_headers, publish_view_result, settings, suffix
):
    result = publish_view_result()
    path = settings.storage_root / result.artifact.storage_key
    path.write_bytes(result.content + b" ")
    listing = next(iter(result.dataset.manifest["evaluator_symbol_mapping"]))
    response = api.get(
        f"/api/v1/research-evaluations/{result.evaluation.id}{suffix}",
        headers=owner_headers,
        params={"listing_id": listing} if suffix.endswith("replay") else {},
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "query",
    [
        {"sort_by": "price desc; select 1"},
        {"sort_by": "__dict__"},
        {"order_status": "filled"},
        {"side": "BUY"},
        {"limit": 501},
    ],
)
def test_result_query_allowlist_rejects_instead_of_falling_back(
    api, owner_headers, publish_view_result, query
):
    result = publish_view_result()
    response = api.get(
        f"/api/v1/research-evaluations/{result.evaluation.id}/results",
        headers=owner_headers,
        params={"sections": "fills", **query},
    )
    assert response.status_code == 422
