"""Actual PostgreSQL HTTP I2 acceptance, never SQLite or stub-session evidence.

Candidate/evaluation rows are explicitly synthetic registration fixtures. Runtime
truth and artifact admission remain covered by the independent trace gate.
"""

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.app.models import (
    Candidate,
    CandidateBookmark,
    Evaluation,
    ResearchRun,
    StrategySpecRevision,
)
from backend.app.research_services import _candidate
from tests.integration import test_research_lifecycle as lifecycle
from tests.integration.test_strategy_versions import create, payload

research_payload = lifecycle.research_payload
submit_research = lifecycle.submit_research
research_attempt = lifecycle.research_attempt
pytestmark = pytest.mark.postgres


def test_workbench_required_postgres_gate():
    if os.environ.get("TRADEGRAD_WORKBENCH_POSTGRES_REQUIRED") == "1":
        assert os.environ.get("TEST_DATABASE_URL"), (
            "Required workbench HTTP acceptance needs actual PostgreSQL TEST_DATABASE_URL"
        )


def base(identifier, kind="strategy-versions"):
    return f"/api/v1/{kind}/{identifier}"


def revision_body(value, **changes):
    return {
        "code_sha256": value["code_sha256"],
        "base_revision": value["revision"],
        "base_spec_sha256": value["spec_sha256"],
        "rules": [
            {
                "rule_id": value["spec"]["rules"][0]["rule_id"],
                "title": "Human-reviewed title",
                "description": "Reviewed prose, not observed execution.",
            }
        ],
        **changes,
    }


def revise(api, headers, url, body, key=None):
    return api.post(
        url + "/spec/revisions",
        headers={**headers, "Idempotency-Key": key or uuid4().hex},
        json=body,
    )


@pytest.fixture
def candidates(migrated_engine, research_attempt, owner):
    with Session(migrated_engine, expire_on_commit=False) as session:
        run = session.get(ResearchRun, research_attempt.run_id)
        rows = []
        for key, parent, kind, iteration, repair_index, references in [
            ("candidate-0000", None, "seed", 0, 0, None),
            ("candidate-0001", 0, "generated", 1, 0, None),
            ("candidate-0002", 0, "generated", 2, 0, ["candidate-0001"]),
            ("candidate-0002-repair-01", 2, "repair", 2, 1, None),
        ]:
            code = owner.strategy.code + ("" if kind == "seed" else f"\n# {key}\n")
            feedback = "Consider the persisted reference; it is not the code parent."
            context = (
                None
                if references is None
                else {
                    "schema_version": "tradegrad.candidate-context.v1",
                    "reference_candidate_keys": references,
                    "feedback": feedback,
                    "feedback_sha256": hashlib.sha256(feedback.encode()).hexdigest(),
                    "feedback_truncated": False,
                    "revision_mode": "small",
                    "generation_status": "generated",
                    "model_call_ids": [],
                }
            )
            row = _candidate(
                session,
                run,
                research_attempt,
                {
                    "candidate_id": key,
                    "parent_id": rows[parent].candidate_key
                    if parent is not None
                    else None,
                    "kind": kind,
                    "iteration": iteration,
                    "repair_index": repair_index,
                    "code": code,
                    "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                    **({"lineage_context": context} if context else {}),
                },
            )
            rows.append(row)
        failed = Evaluation(
            run_id=run.id,
            attempt_id=research_attempt.id,
            generation=1,
            candidate_id=rows[2].id,
            candidate_key=rows[2].candidate_key,
            code_sha256=rows[2].code_sha256,
            split="train",
            window={"start": "2024-02-01", "end": "2024-02-10"},
            window_sha256="a" * 64,
            image_digest="sha256:" + "a" * 64,
            sandbox_id="synthetic-workbench-" + uuid4().hex,
            status="failed",
            sandbox_stopped=True,
            error="Synthetic evaluation failure, not zero profit",
            finished_at=datetime.now(UTC),
        )
        session.add(failed)
        session.commit()
        return SimpleNamespace(
            seed=rows[0],
            reference=rows[1],
            generated=rows[2],
            repair=rows[3],
            failed=failed,
        )


def test_catalog_creates_both_actual_controlled_versions(api, owner_headers):
    assert api.get("/api/v1/research-templates").status_code == 401
    catalog = api.get("/api/v1/research-templates", headers=owner_headers)
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["count"] == 2
    for template in catalog.json()["data"]:
        response = create(
            api,
            owner_headers,
            payload(
                name=template["name"],
                task="cn_" + template["task_suffix"],
                code=template["code"],
                manifest=template["manifest"],
            ),
        )
        assert response.status_code == 201, response.text
        result = api.get(base(response.json()["id"]) + "/spec", headers=owner_headers)
        assert result.status_code == 200, result.text
        value = result.json()
        assert value["code_sha256"] == template["code_sha256"]
        assert value["spec"]["template_id"] == template["template_id"]
        assert value["spec"]["edges"] and value["spec"]["coverage"] == "complete"
        assert all(
            rule["source_ref"]["code_sha256"] == template["code_sha256"]
            for rule in value["spec"]["rules"]
        )


def test_human_revision_is_append_only_pinned_and_idempotent(
    api, owner_headers, owner, migrated_engine
):
    url = base(owner.strategy.id)
    first = api.get(url + "/spec", headers=owner_headers).json()
    code = api.get(url, headers=owner_headers).json()
    key = uuid4().hex
    body = revision_body(first)
    changed = revise(api, owner_headers, url, body, key)
    assert changed.status_code == 201, changed.text
    assert changed.json()["revision"] == 2 and changed.json()["origin"] == "human"
    assert changed.json()["spec"]["rules"][0]["source"] == "human_explanation"
    replay = revise(api, owner_headers, url, body, key)
    assert replay.status_code == 200 and replay.json() == changed.json()
    assert api.get(url, headers=owner_headers).json() == code
    historical = api.get(
        url + "/spec",
        params={"revision": 1, "spec_sha256": first["spec_sha256"]},
        headers=owner_headers,
    )
    assert historical.status_code == 200 and historical.json() == first
    assert api.get(url + "/spec", headers=owner_headers).json() == changed.json()
    assert api.get(url + "/spec/revisions", headers=owner_headers).json()["count"] == 2
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(StrategySpecRevision)
                .where(StrategySpecRevision.strategy_version_id == owner.strategy.id)
            )
            == 2
        )


@pytest.mark.parametrize("change", ["hash", "code", "revision", "rule", "extra"])
def test_revision_rejects_stale_and_retargeted_input(api, owner_headers, owner, change):
    url = base(owner.strategy.id)
    first = api.get(url + "/spec", headers=owner_headers).json()
    body = revision_body(first)
    if change == "hash":
        body["base_spec_sha256"] = "0" * 64
    if change == "code":
        body["code_sha256"] = "0" * 64
    if change == "revision":
        body["base_revision"] += 1
    if change == "rule":
        body["rules"][0]["rule_id"] = "unmapped-invented"
    if change == "extra":
        body["rules"][0]["source_ref"] = first["spec"]["rules"][0]["source_ref"]
    response = revise(api, owner_headers, url, body)
    assert response.status_code in (409, 422), response.text
    assert api.get(url + "/spec/revisions", headers=owner_headers).json()["count"] == 1


def test_hash_pin_requires_both_fields_and_never_retargets(api, owner_headers, owner):
    url = base(owner.strategy.id)
    first = api.get(url + "/spec", headers=owner_headers).json()
    for params in [{"revision": 1}, {"spec_sha256": first["spec_sha256"]}]:
        assert (
            api.get(url + "/spec", params=params, headers=owner_headers).status_code
            == 422
        )
    assert (
        api.get(
            url + "/spec",
            params={"revision": 1, "spec_sha256": "0" * 64},
            headers=owner_headers,
        ).status_code
        == 409
    )
    assert (
        api.get(
            url + "/spec",
            params={"revision": 900, "spec_sha256": first["spec_sha256"]},
            headers=owner_headers,
        ).status_code
        == 404
    )


def test_new_derived_source_does_not_reuse_old_description(api, owner_headers, owner):
    parent = api.get(base(owner.strategy.id) + "/spec", headers=owner_headers).json()
    child = create(
        api,
        owner_headers,
        payload(
            parent_id=str(owner.strategy.id),
            code=owner.strategy.code + "\n# New source\n",
        ),
    ).json()
    new = api.get(base(child["id"]) + "/spec", headers=owner_headers).json()
    assert (
        new["resource_id"] != parent["resource_id"]
        and new["code_sha256"] != parent["code_sha256"]
    )
    assert new["spec_sha256"] != parent["spec_sha256"] and new["revision"] == 1
    lineage = api.get(base(child["id"]) + "/lineage", headers=owner_headers).json()
    assert lineage["parent"]["id"] == str(owner.strategy.id)


def test_candidate_actual_source_is_not_its_run_seed_and_foreign_reads_do_not_initialize(
    api, owner_headers, other_headers, owner, candidates, migrated_engine
):
    url = base(candidates.generated.id, "candidates")
    for suffix in ["/spec", "/spec/revisions", "/lineage", "/evaluations"]:
        assert api.get(url + suffix, headers=other_headers).status_code == 404
    with Session(migrated_engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(StrategySpecRevision)) == 0
        )
    value = api.get(url + "/spec", headers=owner_headers).json()
    assert value["candidate_id"] == str(candidates.generated.id)
    assert value["strategy_version_id"] == str(owner.strategy.id)
    assert (
        value["code_sha256"]
        == candidates.generated.code_sha256
        != owner.strategy.content_sha256
    )
    changed = revise(api, owner_headers, url, revision_body(value))
    assert changed.status_code == 201, changed.text
    with Session(migrated_engine) as session:
        assert (
            session.get(Candidate, candidates.generated.id).code
            == candidates.generated.code
        )


def test_lineage_retains_failed_parent_repair_and_distinct_algorithm_references(
    api, owner_headers, candidates
):
    url = base(candidates.generated.id, "candidates")
    response = api.get(url + "/lineage", headers=owner_headers)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["parent"]["id"] == str(candidates.seed.id)
    assert [item["candidate_id"] for item in value["reference_candidates"]] == [
        str(candidates.reference.id)
    ]
    assert (
        value["context"]["status"] == "available"
        and value["context"]["revision_mode"] == "small"
    )
    assert value["context"]["feedback"]
    assert (
        value["children"][0]["id"] == str(candidates.repair.id)
        and value["children"][0]["kind"] == "repair"
    )
    evaluation = value["focus"]["evaluations"][0]
    assert evaluation["evaluation_id"] == str(candidates.failed.id)
    assert (
        evaluation["status"] == "failed"
        and evaluation["result_manifest_sha256"] is None
    )
    assert "zero profit" in evaluation["error"]
    assert value["children"][0]["evaluations"] == []
    old = api.get(
        base(candidates.seed.id, "candidates") + "/lineage", headers=owner_headers
    ).json()
    assert (
        old["context"]["status"] == "not_collected"
        and old["reference_candidates"] == []
    )


def test_failed_or_regressed_candidate_can_be_bookmarked_durably_without_source_mutation(
    api, owner_headers, other_headers, candidates, migrated_engine, owner
):
    url = base(candidates.generated.id, "candidates")
    assert (
        api.post(
            url + "/bookmark", json={"bookmarked": True}, headers=other_headers
        ).status_code
        == 404
    )
    for _ in range(2):
        response = api.post(
            url + "/bookmark", json={"bookmarked": True}, headers=owner_headers
        )
        assert response.status_code == 200 and response.json()["bookmarked"]
    with Session(migrated_engine) as session:
        stored = session.get(
            CandidateBookmark, (owner.user.id, candidates.generated.id)
        )
        assert stored and stored.code_sha256 == candidates.generated.code_sha256
        assert (
            session.get(Candidate, candidates.generated.id).code
            == candidates.generated.code
        )
    assert api.get(url + "/lineage", headers=owner_headers).json()["focus"][
        "bookmarked"
    ]
    response = api.post(
        url + "/bookmark", json={"bookmarked": False}, headers=owner_headers
    )
    assert response.status_code == 200 and not response.json()["bookmarked"]


def test_exact_failed_leaf_and_child_pages_preserve_real_absence(
    api, owner_headers, other_headers, candidates
):
    url = f"/api/v1/evaluations/{candidates.failed.id}/workbench"
    assert api.get(url, headers=other_headers).status_code == 404
    value = api.get(url, headers=owner_headers).json()
    assert value["status"] == "failed" and value["evidence_status"] == "unavailable"
    assert (
        value["trace_manifest_sha256"] is None
        and value["result_manifest_sha256"] is None
    )
    root = base(candidates.seed.id, "candidates") + "/lineage"
    first = api.get(
        root, params={"offset": 0, "limit": 1}, headers=owner_headers
    ).json()
    second = api.get(
        root, params={"offset": 1, "limit": 1}, headers=owner_headers
    ).json()
    assert first["child_count"] == 2 and len(first["children"]) == 1
    assert first["children"][0]["id"] != second["children"][0]["id"]
    assert (
        api.get(root, params={"limit": 101}, headers=owner_headers).status_code == 422
    )


def test_concurrent_description_revisions_are_serialized_without_lost_history(
    api, owner_headers, owner
):
    url = base(owner.strategy.id)
    first = api.get(url + "/spec", headers=owner_headers).json()
    body = revision_body(first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: revise(api, owner_headers, url, body), range(2))
        )
    assert sorted(row.status_code for row in results) == [201, 409]
    assert api.get(url + "/spec/revisions", headers=owner_headers).json()["count"] == 2


def test_database_itself_rejects_description_mutation_and_wrong_owner_hash(
    api, owner_headers, owner, other, migrated_engine
):
    value = api.get(base(owner.strategy.id) + "/spec", headers=owner_headers).json()
    for sql in [
        "UPDATE strategy_spec_revisions SET change_note = 'mutated'",
        "DELETE FROM strategy_spec_revisions",
    ]:
        with pytest.raises(DBAPIError), migrated_engine.begin() as connection:
            connection.execute(text(sql))
    with pytest.raises(DBAPIError), Session(migrated_engine) as session:
        session.add(
            StrategySpecRevision(
                id=uuid4(),
                owner_id=other.user.id,
                strategy_version_id=owner.strategy.id,
                candidate_id=None,
                code_sha256=value["code_sha256"],
                revision=2,
                spec_sha256=value["spec_sha256"],
                spec={**value["spec"], "revision": 2},
                origin="human",
            )
        )
        session.commit()
    assert (
        api.get(base(owner.strategy.id) + "/spec", headers=owner_headers).json()
        == value
    )


# These fixtures perform real PostgreSQL selection/final admission and persist
# explicitly synthetic artifact bytes; they do not claim native engine execution.
from tests.integration import test_strategy_packages as package_fixtures

publish_initial = lifecycle.publish_initial
complete_candidate = lifecycle.complete_candidate
completed_source = package_fixtures.completed_source
frozen_selection = package_fixtures.frozen_selection
package_reference = package_fixtures.package_reference


def test_native_final_leaf_is_discoverable_from_candidate_and_frozen_version(
    api, owner_headers, other_headers, package_reference
):
    selected = package_reference.selection
    for kind, resource_id in [
        ("candidates", selected.source_candidate_id),
        ("strategy-versions", selected.strategy_version_id),
    ]:
        response = api.get(
            base(resource_id, kind) + "/evaluations", headers=owner_headers
        )
        assert response.status_code == 200, response.text
        leaves = response.json()["data"]
        leaf = next(
            row
            for row in leaves
            if row["evaluation_id"] == str(package_reference.evaluation_id)
        )
        assert leaf["status"] == "succeeded" and leaf["split"] == "holdout"
        assert (
            leaf["run_kind"] == "native_final"
            and leaf["final_test_id"] == leaf["run_id"]
        )
        assert leaf["strategy_version_id"] == str(selected.strategy_version_id)
        assert leaf["candidate_id"] == str(selected.source_candidate_id)
        assert leaf["source_association"]["candidate_id"] == str(
            selected.source_candidate_id
        )
        assert leaf["source_association"]["strategy_version_id"] == str(
            selected.strategy_version_id
        )
        assert leaf["source_association"]["strategy_basis"] == "selected_final"
        assert leaf["result_manifest_sha256"] is not None
        assert (
            api.get(
                base(resource_id, kind) + "/evaluations", headers=other_headers
            ).status_code
            == 404
        )
    lineage = api.get(
        base(selected.source_candidate_id, "candidates") + "/lineage",
        headers=owner_headers,
    ).json()
    assert lineage["focus"]["is_recorded_best"] is True
    assert str(selected.id) in lineage["focus"]["final_selection_ids"]
    assert lineage["focus"]["final_selection_count"] == 1
    assert lineage["focus"]["aggregate_status"] == "not_loaded"


def test_source_only_seed_context_does_not_fabricate_result_preview_exposure(
    api, owner_headers, migrated_engine, research_attempt, owner
):
    from backend.app.models import DataUsageExposure

    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, research_attempt.run_id)
        seed = _candidate(
            session,
            run,
            research_attempt,
            {
                "candidate_id": "candidate-0000",
                "parent_id": None,
                "kind": "seed",
                "iteration": 0,
                "repair_index": 0,
                "code": owner.strategy.code,
                "code_sha256": owner.strategy.content_sha256,
                "lineage_context": {
                    "schema_version": "tradegrad.candidate-context.v1",
                    "reference_candidate_keys": [],
                    "feedback": None,
                    "feedback_sha256": None,
                    "feedback_truncated": False,
                    "revision_mode": None,
                    "generation_status": "seed",
                    "model_call_ids": [],
                },
            },
        )
        identifier = seed.id
        before = session.scalar(
            select(func.count())
            .select_from(DataUsageExposure)
            .where(DataUsageExposure.purpose == "result_preview")
        )
        session.commit()
    response = api.get(
        base(identifier, "candidates") + "/lineage", headers=owner_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["context"]["status"] == "available"
    assert response.json()["focus"]["is_recorded_best"] is None
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DataUsageExposure)
                .where(DataUsageExposure.purpose == "result_preview")
            )
            == before
        )


def test_database_bookmark_guard_rejects_cross_owner_and_wrong_hash(
    migrated_engine, owner, other, candidates
):
    for owner_id, code_hash in [
        (other.user.id, candidates.generated.code_sha256),
        (owner.user.id, "0" * 64),
    ]:
        with pytest.raises(DBAPIError), Session(migrated_engine) as session:
            session.add(
                CandidateBookmark(
                    owner_id=owner_id,
                    candidate_id=candidates.generated.id,
                    code_sha256=code_hash,
                )
            )
            session.commit()


def test_migration_refuses_to_drop_generation_context_without_descriptions(
    migrated_engine, candidates
):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "backend/alembic.ini"))
    config.set_main_option("script_location", str(root / "backend/alembic"))
    with migrated_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM strategy_spec_revisions")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM candidate_bookmarks")
            ).scalar_one()
            == 0
        )
    with pytest.raises(DBAPIError), migrated_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "006_provider_connections")
    with migrated_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "007_research_specs"
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM research_candidates WHERE lineage_context IS NOT NULL"
                )
            ).scalar_one()
            == 1
        )


def _interrupted_leaf(session, attempt, *, key, code_hash, candidate_id=None):
    row = Evaluation(
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        generation=attempt.generation,
        candidate_id=candidate_id,
        candidate_key=key,
        code_sha256=code_hash,
        split="train",
        window={"start": "2024-02-01", "end": "2024-02-10"},
        window_sha256="b" * 64,
        image_digest="sha256:" + "a" * 64,
        sandbox_id="workbench-interrupted-" + uuid4().hex,
        status="interrupted",
        sandbox_stopped=True,
        error=None,
        finished_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row.id


@pytest.mark.parametrize("raw_fk", [False, True], ids=["null-fk", "registered-fk"])
def test_same_code_generated_leaf_has_candidate_association_but_never_saved_seed_association(
    api, owner_headers, migrated_engine, owner, candidates, research_attempt, raw_fk
):
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, research_attempt.run_id)
        child = _candidate(
            session,
            run,
            research_attempt,
            {
                "candidate_id": "candidate-0003",
                "parent_id": candidates.seed.candidate_key,
                "kind": "generated",
                "iteration": 3,
                "repair_index": 0,
                "code": owner.strategy.code,
                "code_sha256": owner.strategy.content_sha256,
            },
        )
        child_id = child.id
        leaf_id = _interrupted_leaf(
            session,
            research_attempt,
            key=child.candidate_key,
            code_hash=child.code_sha256,
            candidate_id=child.id if raw_fk else None,
        )
        session.commit()
    exact = api.get(f"/api/v1/evaluations/{leaf_id}/workbench", headers=owner_headers)
    assert exact.status_code == 200, exact.text
    value = exact.json()
    assert value["candidate_id"] == (str(child_id) if raw_fk else None)
    association = value["source_association"]
    assert association["candidate_id"] == str(child_id)
    assert association["candidate_basis"] == (
        "registered_fk" if raw_fk else "same_run_key_hash"
    )
    assert (
        association["strategy_version_id"] is None
        and association["strategy_basis"] == "unavailable"
    )
    listed = api.get(
        base(child_id, "candidates") + "/evaluations", headers=owner_headers
    ).json()
    assert (
        next(row for row in listed["data"] if row["evaluation_id"] == str(leaf_id))
        == value
    )
    saved = api.get(
        base(owner.strategy.id) + "/evaluations", headers=owner_headers
    ).json()
    assert str(leaf_id) not in {row["evaluation_id"] for row in saved["data"]}
    with Session(migrated_engine) as session:
        assert session.get(Evaluation, leaf_id).candidate_id == (
            child_id if raw_fk else None
        )


def test_interrupted_null_fk_candidate_resolves_only_exact_key_run_and_hash(
    api, owner_headers, migrated_engine, candidates, research_attempt
):
    with Session(migrated_engine) as session:
        matched = _interrupted_leaf(
            session,
            research_attempt,
            key=candidates.generated.candidate_key,
            code_hash=candidates.generated.code_sha256,
        )
        wrong_key = _interrupted_leaf(
            session,
            research_attempt,
            key="candidate-9999",
            code_hash=candidates.generated.code_sha256,
        )
        wrong_hash = _interrupted_leaf(
            session,
            research_attempt,
            key=candidates.generated.candidate_key,
            code_hash="f" * 64,
        )
        session.commit()
    for identifier, expected in [
        (matched, str(candidates.generated.id)),
        (wrong_key, None),
        (wrong_hash, None),
    ]:
        value = api.get(
            f"/api/v1/evaluations/{identifier}/workbench", headers=owner_headers
        ).json()
        assert value["candidate_id"] is None
        assert value["source_association"]["candidate_id"] == expected
    listed = api.get(
        base(candidates.generated.id, "candidates") + "/evaluations",
        headers=owner_headers,
    ).json()
    ids = {row["evaluation_id"] for row in listed["data"]}
    assert (
        str(matched) in ids and str(wrong_key) not in ids and str(wrong_hash) not in ids
    )


@pytest.mark.parametrize(
    "key,associated", [("candidate-0000", True), ("candidate-0099", False)]
)
def test_unpublished_seed_registration_has_source_only_association_not_an_invented_candidate(
    api, owner_headers, migrated_engine, owner, research_attempt, key, associated
):
    with Session(migrated_engine) as session:
        leaf_id = _interrupted_leaf(
            session, research_attempt, key=key, code_hash=owner.strategy.content_sha256
        )
        session.commit()
    direct = api.get(
        f"/api/v1/evaluations/{leaf_id}/workbench", headers=owner_headers
    ).json()
    association = direct["source_association"]
    assert direct["candidate_id"] is None and association["candidate_id"] is None
    assert association["candidate_basis"] == "unavailable"
    assert association["strategy_version_id"] == (
        str(owner.strategy.id) if associated else None
    )
    assert association["strategy_basis"] == (
        "saved_seed" if associated else "unavailable"
    )
    page = api.get(
        base(owner.strategy.id) + "/evaluations", headers=owner_headers
    ).json()
    assert (
        str(leaf_id) in {row["evaluation_id"] for row in page["data"]}
    ) is associated
