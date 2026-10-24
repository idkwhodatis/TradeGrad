"""Pure DTO/source projections; not PostgreSQL or HTTP integration evidence."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from backend.app.models import Evaluation, ResearchRun
from backend.app.services import DomainError
from backend.app.workbench_services import (
    SourceResource,
    _evaluation_clause,
    _spec_public,
)
from packages.research_trace.specification import (
    describe_source,
    source_sha256,
    spec_sha256,
)
from packages.research_trace.templates import native_contract, template_source


def resource_and_row(*, mode="native_akquant", controlled=True):
    source = template_source("daily_timing")
    parameters = native_contract("daily_timing").parameter_values()
    spec = describe_source(
        source,
        source_sha256(source),
        parameters=parameters,
        allow_controlled=controlled,
    )
    item = SimpleNamespace(id=uuid4(), code=source, mode=mode)
    resource = SourceResource(
        "strategy_version", item, item, None, source_sha256(source), parameters
    )
    row = SimpleNamespace(
        code_sha256=spec.code_sha256,
        revision=1,
        spec_sha256=spec_sha256(spec),
        spec=spec.model_dump(mode="json"),
        created_at=datetime.now(UTC),
        origin="static",
        change_note=None,
    )
    return resource, row


def test_controlled_persisted_projection_and_portable_mode_guard():
    resource, row = resource_and_row()
    result = _spec_public(row, resource)
    assert result.spec.template_id == "daily_timing" and result.spec.edges
    portable, wrong = resource_and_row(mode="portable")
    with pytest.raises(DomainError) as error:
        _spec_public(wrong, portable)
    assert error.value.code == "spec_integrity"
    portable, generic = resource_and_row(mode="portable", controlled=False)
    assert _spec_public(generic, portable).spec.template_id is None


@pytest.mark.parametrize("field", ["spec_sha256", "code_sha256", "revision"])
def test_persisted_revision_projection_rejects_tamper(field):
    resource, row = resource_and_row()
    setattr(row, field, 2 if field == "revision" else "0" * 64)
    with pytest.raises(DomainError) as error:
        _spec_public(row, resource)
    assert error.value.code == "spec_integrity"


def test_candidate_leaf_selector_has_cross_run_fk_but_same_run_legacy_key():
    identifier, run_id, owner_id = uuid4(), uuid4(), uuid4()
    resource = SimpleNamespace(
        kind="candidate",
        code_sha256="a" * 64,
        id=identifier,
        run=SimpleNamespace(id=run_id),
        item=SimpleNamespace(candidate_key="candidate-0001"),
    )
    query = (
        select(Evaluation)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .where(_evaluation_clause(resource), ResearchRun.owner_id == owner_id)
    )
    compiled = str(
        query.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert f"research_candidates.id = '{identifier}'" in compiled
    assert (
        "research_evaluations.candidate_id = research_candidates.id OR research_evaluations.candidate_id IS NULL AND research_evaluations.run_id = research_candidates.run_id AND research_evaluations.candidate_key = research_candidates.candidate_key"
        in compiled
    )
    assert (
        "research_evaluations.code_sha256 = research_candidates.code_sha256" in compiled
    )
    assert f"research_runs.owner_id = '{owner_id}'" in compiled


def test_strategy_selector_includes_selected_final_fk_without_absorbing_optimizer_candidates():
    resource = SimpleNamespace(
        kind="strategy_version", code_sha256="a" * 64, id=uuid4()
    )
    query = (
        select(Evaluation)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .where(_evaluation_clause(resource))
    )
    compiled = str(
        query.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "research_candidates.kind = 'seed'" in compiled
    assert (
        "research_evaluations.candidate_key IN ('candidate-0000', 'seed')" in compiled
    )
    assert (
        "final_selections.source_candidate_id = research_evaluations.candidate_id"
        in compiled
    )
    assert "final_selections.strategy_version_id = strategy_versions.id" in compiled
    assert (
        "research_runs.strategy_version_id" in compiled
        and "research_evaluations.code_sha256" in compiled
    )
    # Nested EXISTS must correlate to this exact outer leaf, not scan another
    # evaluation/run whose unrelated seed or final happens to match.
    assert "FROM research_candidates, research_evaluations" not in compiled
    assert "FROM final_selections, research_runs" not in compiled
