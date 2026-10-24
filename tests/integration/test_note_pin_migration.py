"""Required real-PG regression for the 010 OLD-alias defect and 011 repair.

Synthetic frozen artifact fixtures exercise real migrations, triggers and note
services. No mocked-session, SQLite, browser or native-engine claim is made.
"""

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.app.models import (
    Artifact,
    ResearchNote,
    ResearchNoteEvidencePin,
    ResearchNoteRevision,
)
from tests.integration import test_research_notes_api as notes

base_research_payload = notes.base_research_payload
research_payload = notes.research_payload
submit_research = notes.submit_research
research_attempt = notes.research_attempt
published_pair = notes.published_pair
pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]


def test_note_pin_migration_required_postgres_gate():
    if os.environ.get("TRADEGRAD_NOTES_POSTGRES_REQUIRED") == "1":
        assert os.environ.get("TEST_DATABASE_URL"), (
            "Required note trigger migration acceptance needs actual PostgreSQL"
        )


def migrate(engine, direction, revision):
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/alembic"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, direction)(config, revision)


def stored_notes(engine):
    with engine.connect() as connection:
        return {
            model.__tablename__: [
                dict(row)
                for row in connection.execute(
                    select(model.__table__).order_by(model.id)
                ).mappings()
            ]
            for model in (ResearchNote, ResearchNoteRevision, ResearchNoteEvidencePin)
        }


def probe_pin(engine, values, *, sqlstate=None):
    """Rollback every probe, even success; never mutate hashed note inventories."""
    if sqlstate is not None:
        with pytest.raises(DBAPIError) as failure:
            probe_pin(engine, values)
        assert failure.value.orig.sqlstate == sqlstate
        return
    with Session(engine) as session:
        session.add(ResearchNoteEvidencePin(id=uuid4(), **values))
        session.flush()


def test_010_ambiguity_and_011_repair_preserve_pin_admission_and_history(
    migrated_engine, api, owner, other, owner_headers, other_headers, published_pair
):
    # Prepare populated history under the repaired head, then restore the exact
    # 010 function. This tests upgrade preservation without weakening its known
    # error via a session-wide variable_conflict override or disabled triggers.
    response = notes.submit(api, owner_headers, notes.leaf_content(published_pair))
    assert response.status_code == 201, response.text
    first = response.json()
    response = notes.submit(api, owner_headers, {"title": "Rollback-only pin probes"})
    assert response.status_code == 201, response.text
    probe = response.json()
    response = notes.submit(
        api, other_headers, {"title": "Foreign artifact pin probes"}
    )
    assert response.status_code == 201, response.text
    foreign_probe = response.json()
    trace_id = published_pair[0].trace_artifact.id
    with Session(migrated_engine) as session:
        pin = session.scalar(
            select(ResearchNoteEvidencePin).where(
                ResearchNoteEvidencePin.revision_id == UUID(first["id"]),
                ResearchNoteEvidencePin.resource_id == trace_id,
                ResearchNoteEvidencePin.resource_kind == "artifact",
            )
        )
        assert pin is not None
        values = {
            "revision_id": UUID(probe["id"]),
            "owner_id": owner.user.id,
            "resource_kind": pin.resource_kind,
            "resource_id": pin.resource_id,
            "sha256": pin.sha256,
            "details": dict(pin.details),
        }
    before = stored_notes(migrated_engine)
    tables = set(inspect(migrated_engine).get_table_names())
    migrate(migrated_engine, "downgrade", "010_research_notes")
    # Even a non-artifact insert prepares the complete IF expression and sees
    # old.revision_id as both the table alias and the implicit OLD record.
    source_values = values | {
        "resource_kind": "strategy_version",
        "resource_id": owner.strategy.id,
        "sha256": owner.strategy.content_sha256,
        "details": {},
    }
    probe_pin(migrated_engine, source_values, sqlstate="42702")
    assert stored_notes(migrated_engine) == before
    migrate(migrated_engine, "upgrade", "011_note_pin_trigger")
    assert set(inspect(migrated_engine).get_table_names()) == tables
    assert stored_notes(migrated_engine) == before
    assert api.get(notes.exact_url(first), headers=owner_headers).json() == first

    # Original matching registrations remain valid. All existing owner, hash and
    # artifact metadata predicates must still reject incompatible registrations.
    probe_pin(migrated_engine, source_values)
    probe_pin(migrated_engine, values)
    probe_pin(migrated_engine, values | {"owner_id": other.user.id}, sqlstate="P0001")
    probe_pin(
        migrated_engine,
        values | {"revision_id": UUID(foreign_probe["id"]), "owner_id": other.user.id},
        sqlstate="P0001",
    )
    wrong_hash = "0" * 64 if values["sha256"] != "0" * 64 else "1" * 64
    probe_pin(migrated_engine, values | {"sha256": wrong_hash}, sqlstate="P0001")
    for field, replacement in (
        ("storage_key", values["details"]["storage_key"] + ".different"),
        ("size_bytes", values["details"]["size_bytes"] + 1),
        ("name", values["details"]["name"] + ".different"),
        ("run_id", str(uuid4())),
        ("attempt_id", str(uuid4())),
        ("media_type", "application/x-mismatched-fixture"),
    ):
        probe_pin(
            migrated_engine,
            values | {"details": values["details"] | {field: replacement}},
            sqlstate="P0001",
        )
    assert stored_notes(migrated_engine) == before

    # A removed registration may be retained only by a later revision of that
    # exact same note. Another note cannot borrow the historical registration.
    with Session(migrated_engine) as session:
        session.delete(session.get(Artifact, trace_id))
        session.commit()
    probe_pin(migrated_engine, values, sqlstate="P0001")
    response = notes.edit(
        api, owner_headers, first, changes={"title": "Original evidence retained"}
    )
    assert response.status_code == 201, response.text
    second = response.json()
    assert second["revision"] == 2
    assert second["retained_pin_count"] == first["retained_pin_count"]
    assert second["content"]["evidence"] == first["content"]["evidence"]
    # The evaluation still declares its trace manifest, so its missing database
    # registration is an integrity failure, not an originally absent trace.
    assert second["evidence"][0]["state"] == "corrupt"
    probe_pin(
        migrated_engine,
        values | {"revision_id": UUID(first["id"])},
        sqlstate="P0001",
    )
    probe_pin(
        migrated_engine,
        values | {"revision_id": UUID(second["id"]), "sha256": wrong_hash},
        sqlstate="P0001",
    )
    after = stored_notes(migrated_engine)
    for table, prior_rows in before.items():
        by_id = {row["id"]: row for row in after[table]}
        assert all(by_id[row["id"]] == row for row in prior_rows)
    with Session(migrated_engine) as session:
        retained = session.scalar(
            select(ResearchNoteEvidencePin).where(
                ResearchNoteEvidencePin.revision_id == UUID(second["id"]),
                ResearchNoteEvidencePin.resource_id == trace_id,
            )
        )
        assert retained.sha256 == values["sha256"]
        assert retained.details == values["details"]
