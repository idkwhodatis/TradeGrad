# ruff: noqa: SIM117
"""Mandatory real PostgreSQL I6 API/immutability/ownership gates.

Frozen source/trace fixtures are explicitly synthetic boundary evidence; no
SQLite or mocked-session fallback, model/provider calls or browser claims.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from stat import S_IMODE
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.app.models import (
    Artifact,
    ResearchNote,
    ResearchNoteRevision,
)
from tests.integration import test_comparison_api as comparisons
from tests.integration.test_strategy_versions import create, payload

base_research_payload = comparisons.base_research_payload
research_payload = comparisons.research_payload
submit_research = comparisons.submit_research
research_attempt = comparisons.research_attempt
published_pair = comparisons.published_pair
queued_comparison = comparisons.queued_comparison
completed_comparison = comparisons.completed_comparison
pytestmark = pytest.mark.postgres
PATH = "/api/v1/research-notes"


def test_notes_required_postgres_gate():
    if os.environ.get("TRADEGRAD_NOTES_POSTGRES_REQUIRED") == "1":
        assert os.environ.get("TEST_DATABASE_URL"), (
            "Required research-note acceptance needs actual PostgreSQL TEST_DATABASE_URL"
        )


def submit(api, headers, content, *, key=None):
    return api.post(
        PATH,
        headers=headers | {"Idempotency-Key": key or uuid4().hex},
        json={"content": content},
    )


def exact_url(value):
    return f"{PATH}/{value['note_id']}/revisions/{value['revision']}?revision_sha256={value['revision_sha256']}"


def edit(api, headers, value, *, changes=None, key=None):
    return api.post(
        f"{PATH}/{value['note_id']}/revisions",
        headers=headers | {"Idempotency-Key": key or uuid4().hex},
        json={
            "base_revision": value["revision"],
            "base_revision_sha256": value["revision_sha256"],
            "content": value["content"] | (changes or {}),
            "change_note": "Human revision",
        },
    )


@pytest.fixture
def note_source(api, owner_headers):
    response = create(api, owner_headers, payload(code="def alpha():\n    return 1\n"))
    assert response.status_code == 201, response.text
    strategy = response.json()
    response = api.get(
        f"/api/v1/strategy-versions/{strategy['id']}/spec", headers=owner_headers
    )
    assert response.status_code == 200, response.text
    spec = response.json()
    evidence = {
        "kind": "source",
        "source_kind": "strategy_version",
        "source_id": strategy["id"],
        "code_sha256": spec["code_sha256"],
        "spec_revision": spec["revision"],
        "spec_sha256": spec["spec_sha256"],
    }
    content = {
        "title": "Liquidity is a hypothesis",
        "tags": ["liquidity", "研究"],
        "body": "Need a counterexample before a conclusion. Literal 100%_test",
        "hypothesis": "Remove illiquid names",
        "status": "pending",
        "evidence": [evidence],
        "restore": {
            "strategy_version_id": strategy["id"],
            "selection": {
                "tab": "description",
                "code_sha256": spec["code_sha256"],
                "spec_revision": spec["revision"],
                "spec_sha256": spec["spec_sha256"],
            },
        },
    }
    return content


@pytest.fixture
def saved_note(api, owner_headers, note_source):
    response = submit(api, owner_headers, note_source)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_retry_conflict_exact_history_export_and_literal_search(
    api, owner_headers, note_source, migrated_engine
):
    key = uuid4().hex
    response = submit(api, owner_headers, note_source, key=key)
    assert response.status_code == 201, response.text
    first = response.json()
    assert first["revision"] == 1 and first["content"]["status"] == "pending"
    assert first["retained_pin_count"] >= 2 and first["restore_href"]
    assert submit(api, owner_headers, note_source, key=key).json() == first
    assert (
        submit(
            api, owner_headers, note_source | {"title": "Different"}, key=key
        ).status_code
        == 409
    )
    second = edit(
        api,
        owner_headers,
        first,
        changes={
            "status": "mixed",
            "counterexamples": "Lower turnover did not improve this period",
        },
    ).json()
    assert second["revision"] == 2
    assert api.get(exact_url(first), headers=owner_headers).json() == first
    assert (
        api.get(f"{PATH}/{first['note_id']}", headers=owner_headers).json()["revision"]
        == 2
    )
    assert (
        api.get(
            f"{PATH}/{first['note_id']}/revisions?limit=1", headers=owner_headers
        ).json()["count"]
        == 2
    )
    for query in ("q=Liquidity", "q=100%25_test", "tag=liquidity", "status=mixed"):
        result = api.get(f"{PATH}?{query}", headers=owner_headers)
        assert result.status_code == 200 and result.json()["count"] == 1, result.text
    assert (
        api.get(f"{PATH}?q=does-not-occur", headers=owner_headers).json()["count"] == 0
    )
    export = api.get(exact_url(first).replace("?", "/export?"), headers=owner_headers)
    assert (
        export.status_code == 200 and "text/markdown" in export.headers["content-type"]
    )
    assert (
        first["revision_sha256"] in export.text
        and note_source["evidence"][0]["spec_sha256"] in export.text
    )
    with Session(migrated_engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ResearchNoteRevision)) == 2
        )


def test_concurrent_edit_cas_and_lost_response_retry(api, owner_headers, saved_note):
    key = uuid4().hex
    first = edit(api, owner_headers, saved_note, changes={"title": "first"}, key=key)
    assert first.status_code == 201
    assert (
        edit(api, owner_headers, saved_note, changes={"title": "first"}, key=key).json()
        == first.json()
    )
    assert (
        edit(api, owner_headers, saved_note, changes={"title": "stale"}).status_code
        == 409
    )
    current = first.json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(
                lambda name: edit(api, owner_headers, current, changes={"title": name}),
                ["parallel-a", "parallel-b"],
            )
        )
    assert sorted(reply.status_code for reply in replies) == [201, 409]


@pytest.mark.parametrize("endpoint", ["latest", "exact", "history", "export", "edit"])
def test_note_owner_is_checked_before_revision_or_evidence_shape(
    api, owner_headers, other_headers, saved_note, endpoint
):
    identifier = saved_note["note_id"]
    url = {
        "latest": f"{PATH}/{identifier}",
        "exact": exact_url(saved_note),
        "history": f"{PATH}/{identifier}/revisions",
        "export": exact_url(saved_note).replace("?", "/export?"),
        "edit": None,
    }[endpoint]
    response = (
        edit(api, other_headers, saved_note)
        if endpoint == "edit"
        else api.get(url, headers=other_headers)
    )
    assert response.status_code == 404 and "Liquidity" not in response.text
    assert api.get(PATH, headers=other_headers).json()["count"] == 0


def test_foreign_reference_rejected_and_no_note_inserted(
    api, owner_headers, other, note_source, migrated_engine
):
    content = note_source | {
        "restore": None,
        "evidence": [
            note_source["evidence"][0] | {"source_id": str(other.strategy.id)}
        ],
    }
    response = submit(api, owner_headers, content)
    assert response.status_code == 404
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchNote)) == 0


@pytest.mark.parametrize("target", ["note", "revision", "pin"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_database_revisions_and_retention_inventory_are_append_only(
    migrated_engine, saved_note, target, operation
):
    table = {
        "note": "research_notes",
        "revision": "research_note_revisions",
        "pin": "research_note_evidence_pins",
    }[target]
    sql = (
        f"UPDATE {table} SET owner_id=owner_id"
        if operation == "UPDATE"
        else f"DELETE FROM {table}"
    )
    with migrated_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            with connection.begin():
                connection.execute(text(sql))


def test_exact_revision_hash_is_required_and_never_latest(
    api, owner_headers, saved_note
):
    url = f"{PATH}/{saved_note['note_id']}/revisions/1"
    assert api.get(url, headers=owner_headers).status_code == 422
    response = api.get(url + "?revision_sha256=" + "f" * 64, headers=owner_headers)
    assert (
        response.status_code == 409
        and response.json()["code"] == "note_revision_pin_mismatch"
    )


def test_immutable_spec_revision_is_not_replaced_by_later_description(
    api, owner_headers, saved_note
):
    ref = saved_note["content"]["evidence"][0]
    endpoint = f"/api/v1/strategy-versions/{ref['source_id']}/spec"
    spec = api.get(endpoint, headers=owner_headers).json()
    rule = spec["spec"]["rules"][0]
    updated = api.post(
        endpoint + "/revisions",
        headers=owner_headers | {"Idempotency-Key": uuid4().hex},
        json={
            "code_sha256": ref["code_sha256"],
            "base_revision": ref["spec_revision"],
            "base_spec_sha256": ref["spec_sha256"],
            "rules": [
                {
                    "rule_id": rule["rule_id"],
                    "title": "New human title",
                    "description": "New explanation, old notes remain frozen",
                }
            ],
        },
    )
    assert updated.status_code in {200, 201}, updated.text
    value = api.get(exact_url(saved_note), headers=owner_headers).json()
    assert value["content"]["evidence"][0]["spec_sha256"] == ref["spec_sha256"]
    assert "spec_revision=1" in value["restore_href"]


def test_metadata_writers_cannot_pass_cold_maintenance_barrier(
    migrated_engine, api, owner_headers, note_source
):
    from backend.app.core.maintenance import LOCK_PARAMETERS

    with migrated_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        assert connection.execute(
            text("SELECT pg_try_advisory_lock(:namespace,:resource)"), LOCK_PARAMETERS
        ).scalar_one()
        try:
            response = submit(api, owner_headers, note_source)
            assert response.status_code == 503, response.text
        finally:
            assert connection.execute(
                text("SELECT pg_advisory_unlock(:namespace,:resource)"), LOCK_PARAMETERS
            ).scalar_one()
    with Session(migrated_engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ResearchNoteRevision)) == 0
        )


def leaf_content(values):
    pin = comparisons.request_body(values)["left"]
    return {
        "title": "Saved physical outcome",
        "status": "pending",
        "body": "The original outcome is referenced, not copied or validated automatically",
        "evidence": [{"kind": "evaluation", "leaf": pin}],
    }


@pytest.mark.parametrize(
    "fault", ["missing_file", "corrupt_file", "removed_registration"]
)
def test_missing_tampered_originals_stay_visible_and_never_retarget(
    api, owner_headers, published_pair, settings, migrated_engine, fault
):
    response = submit(api, owner_headers, leaf_content(published_pair))
    assert response.status_code == 201, response.text
    saved = response.json()
    trace = published_pair[0].trace_artifact
    with Session(migrated_engine) as session:
        item = session.get(Artifact, trace.id)
        path = settings.storage_root / item.storage_key
        if fault == "removed_registration":
            session.delete(item)
            session.commit()
        elif fault == "missing_file":
            path.unlink()
        else:
            original_mode = S_IMODE(path.stat().st_mode)
            try:
                path.chmod(original_mode | 0o200)
                path.write_bytes(b"corrupt fixture bytes")
            finally:
                path.chmod(original_mode)
    response = api.get(exact_url(saved), headers=owner_headers)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["evidence"][0]["state"] in {"missing", "corrupt"}
    assert (
        value["evidence"][0]["href"] is None and value["evidence"][0]["bindings"] == []
    )
    revised = edit(
        api, owner_headers, value, changes={"title": "Broken original, preserved"}
    )
    assert revised.status_code == 201, revised.text
    assert revised.json()["content"]["evidence"] == saved["content"]["evidence"]
    assert revised.json()["retained_pin_count"] >= saved["retained_pin_count"]


def test_completed_comparison_pins_both_sides_and_actual_input(
    api, owner_headers, published_pair, completed_comparison
):
    identifier = completed_comparison["id"]
    result = api.get(
        f"/api/v1/comparisons/{identifier}/result", headers=owner_headers
    ).json()
    ref = {
        "kind": "comparison",
        "comparison_id": identifier,
        "input_sha256": result["input_sha256"],
        "manifest_sha256": result["manifest_sha256"],
        **comparisons.request_body(published_pair),
    }
    response = submit(
        api, owner_headers, {"title": "Two physical sources", "evidence": [ref]}
    )
    assert response.status_code == 201, response.text
    saved = response.json()
    assert len(saved["evidence"][0]["bindings"]) == 2
    forged = ref | {"input_sha256": "f" * 64}
    assert (
        submit(api, owner_headers, {"title": "wrong", "evidence": [forged]}).status_code
        == 409
    )
    forged = ref | {"instrument_id": "INVENTED", "section": "candidates"}
    assert (
        submit(api, owner_headers, {"title": "wrong", "evidence": [forged]}).status_code
        == 404
    )
