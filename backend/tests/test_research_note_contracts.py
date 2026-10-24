"""Pure note contract/security seams, not database or HTTP acceptance."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.comparison_schemas import ComparisonSideCreate
from backend.app.research_note_evidence import EvidenceResolver
from backend.app.research_note_schemas import (
    NoteControlledArm,
    NoteDiagnosticEvidence,
    NoteEvaluationEvidence,
    NoteRestore,
    NoteSourceEvidence,
    ResearchNoteContent,
    ResearchNoteCreate,
    ResearchNoteEdit,
    ResearchNoteRevision,
)
from backend.app.research_note_selection import reference_selection, restore_href
from backend.app.research_note_services import (
    _failure,
    _inventory,
    _resolve,
    markdown,
    revision_hash,
    search_text,
    validate_revision,
)
from backend.app.services import DomainError
from packages.research_trace.contracts import digest


def source(**changes):
    return NoteSourceEvidence.model_validate(
        dict(
            kind="source",
            source_kind="strategy_version",
            source_id=str(uuid4()),
            code_sha256="a" * 64,
            spec_revision=1,
            spec_sha256="b" * 64,
            **changes,
        )
    )


def leaf(ref=None, **changes):
    ref = ref or source()
    return ComparisonSideCreate.model_validate(
        dict(
            evaluation_id=str(uuid4()),
            execution_attempt_id=str(uuid4()),
            result_manifest_sha256="c" * 64,
            trace_manifest_sha256="d" * 64,
            source_kind=ref.source_kind,
            source_id=ref.source_id,
            code_sha256=ref.code_sha256,
            spec_revision=ref.spec_revision,
            spec_sha256=ref.spec_sha256,
            **changes,
        )
    )


def snapshot(ref):
    return {
        "strategy_version_id": str(ref.source_id),
        "source_kind": ref.source_kind,
        "source_id": str(ref.source_id),
        "code_sha256": ref.code_sha256,
    }


def row(content=None):
    content = content or ResearchNoteContent(
        title="Human hypothesis", body="profit never auto-validates"
    )
    value = SimpleNamespace(
        note_id=uuid4(),
        revision=1,
        content=content.model_dump(mode="json"),
        evidence_snapshot=[],
        admission_snapshot=[],
        change_note=None,
        pins_sha256=digest([]),
        title=content.title,
        tags=list(content.tags),
        status=content.status,
        search_text=search_text(content),
    )
    value.revision_sha256 = revision_hash(
        value.note_id,
        value.revision,
        value.content,
        value.evidence_snapshot,
        value.change_note,
        value.pins_sha256,
        value.admission_snapshot,
    )
    return value


@pytest.mark.parametrize(
    "kind", ["comparison", "controlled", "diagnostic", "evaluation", "decision"]
)
def test_discriminated_evidence_rejects_source_identifiers_for_other_kind(kind):
    value = source().model_dump(mode="json") | {"kind": kind}
    with pytest.raises(ValidationError):
        ResearchNoteContent(title="x", evidence=[value])


@pytest.mark.parametrize(
    "extra",
    ["comparison_id", "controlled_experiment_id", "evaluation_id", "run_id", "html"],
)
def test_source_cannot_smuggle_other_identity(extra):
    with pytest.raises(ValidationError):
        source(**{extra: str(uuid4())})


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"tags": ["liquidity", "LIQUIDITY"]},
        {"tags": [" "]},
        {"tags": ["x" * 81]},
        {"body": "x" * 32001},
        {"status": "validated"},
        {"evidence": [source().model_dump(mode="json")] * 33},
        {
            "hypothesis": "中" * 8000,
            "motivation": "中" * 8000,
            "rules": "中" * 8000,
            "sources": "中" * 8000,
        },
    ],
)
def test_bounded_content(changes):
    with pytest.raises(ValidationError):
        ResearchNoteContent.model_validate({"title": "x", **changes})


def test_profit_is_never_a_status_transition():
    for text in ("Profit 100%", "A large positive Sharpe", "Losses in all tests"):
        assert ResearchNoteContent(title=text, body=text).status == "pending"
    assert (
        ResearchNoteContent(
            title="Human decision", status="supported_under_current_tests"
        ).status
        == "supported_under_current_tests"
    )


def test_unpaired_spec_and_anchor_retarget_rejected():
    value = source().model_dump(mode="json")
    for patch in (
        {"spec_sha256": None},
        {"spec_revision": None},
        {
            "source_ref": {
                "file": "strategy.py",
                "symbol": "f",
                "line_start": 1,
                "line_end": 2,
                "code_sha256": "f" * 64,
            }
        },
    ):
        with pytest.raises(ValidationError):
            NoteSourceEvidence.model_validate(value | patch)


def test_decision_requires_exact_trace_and_observation_pair():
    value = {"kind": "decision", "leaf": leaf(), "decision_id": "d1"}
    for patch in (
        {"decision_id": None},
        {"record_id": "x"},
        {"record_ordinal": 0},
        {"record_kind": "feature"},
    ):
        with pytest.raises(ValidationError):
            NoteEvaluationEvidence.model_validate(value | patch)
    with pytest.raises(ValidationError):
        NoteEvaluationEvidence(
            kind="decision",
            leaf=leaf().model_copy(update={"trace_manifest_sha256": None}),
            decision_id="d1",
        )


def test_control_failed_arm_has_no_zero_or_fake_result():
    value = {
        "ordinal": 0,
        "run_id": uuid4(),
        "status": "failed",
        "execution_attempt_id": uuid4(),
        "generation": 1,
        "result": None,
    }
    assert NoteControlledArm(**value).result is None
    with pytest.raises(ValidationError):
        NoteControlledArm(**(value | {"status": "succeeded"}))
    with pytest.raises(ValidationError):
        NoteControlledArm(**(value | {"result": leaf()}))


@pytest.mark.parametrize(
    "selection",
    [
        {"tab": "description"},
        {"code_sha256": "a" * 64},
        {"code_sha256": "a" * 64, "spec_revision": 1},
    ],
)
def test_restore_never_loads_latest_from_partial_primary(selection):
    ref = source()
    with pytest.raises(DomainError, match="complete exact source"):
        restore_href(
            NoteRestore(strategy_version_id=ref.source_id, selection=selection),
            [ref],
            [snapshot(ref)],
        )


def test_restore_rule_plus_decision_are_separate_coherent_refs():
    ref = source(rule_id="liquidity")
    snap = snapshot(ref) | {"rule_stage_id": "filter"}
    decision = NoteEvaluationEvidence(
        kind="decision",
        leaf=leaf(ref),
        decision_id="d1",
        instrument_id="B",
        stage_id="filter",
        record_kind="candidate",
        record_id="B",
        record_ordinal=3,
    )
    _, rule_selection = reference_selection(ref, snap)
    _, decision_selection = reference_selection(decision, snapshot(ref))
    selection = rule_selection | decision_selection
    restored = restore_href(
        NoteRestore(strategy_version_id=ref.source_id, selection=selection),
        [ref, decision],
        [snap, snapshot(ref)],
    )
    assert (
        "decision_id=d1" in restored
        and "rule_id=liquidity" in restored
        and "record_ordinal=3" in restored
    )


def test_restore_rejects_same_leaf_cross_decision_record_mix():
    ref = source()
    pin = leaf(ref)
    refs = [
        NoteEvaluationEvidence(
            kind="decision",
            leaf=pin,
            decision_id=f"d{i}",
            stage_id="filter",
            record_kind="candidate",
            record_id=code,
            instrument_id=code,
            record_ordinal=i,
        )
        for i, code in [(1, "A"), (2, "B")]
    ]
    _, selection = reference_selection(refs[0], snapshot(ref))
    selection["record_id"], selection["record_ordinal"] = "B", 2
    with pytest.raises(DomainError):
        restore_href(
            NoteRestore(strategy_version_id=ref.source_id, selection=selection),
            refs,
            [snapshot(ref)] * 2,
        )


def test_restore_rejects_source_root_and_spec_revision_mix():
    one, two = (
        source(),
        source().model_copy(update={"code_sha256": "e" * 64, "spec_sha256": "f" * 64}),
    )
    _, selection = reference_selection(two, snapshot(two))
    with pytest.raises(DomainError):
        restore_href(
            NoteRestore(strategy_version_id=one.source_id, selection=selection),
            [one, two],
            [snapshot(one), snapshot(two)],
        )


@pytest.mark.parametrize(
    "selection",
    [
        {"note_id": str(uuid4())},
        {"tab": "javascript:evil"},
        {"record_ordinal": -1},
        {"record_ordinal": 2**54},
        {"market_start": "2026-02-31"},
        {"unknown": "x"},
        {"score_min": float("inf")},
    ],
)
def test_restore_is_flat_bounded_known_state(selection):
    with pytest.raises(ValidationError):
        NoteRestore(strategy_version_id=uuid4(), selection=selection)


@pytest.mark.parametrize(
    "field",
    [
        "title",
        "tags",
        "status",
        "search_text",
        "content",
        "pins_sha256",
        "evidence_snapshot",
    ],
)
def test_revision_hash_and_materialized_fields_reject_tamper(field):
    value = row()
    setattr(
        value,
        field,
        {"title": "changed"}
        if field == "content"
        else []
        if field == "evidence_snapshot"
        else "changed",
    )
    if field == "evidence_snapshot":
        value.evidence_snapshot = [{"forged": True}]
    with pytest.raises(DomainError) as error:
        validate_revision(value)
    assert error.value.code == "note_integrity"


def test_inventory_deduplicates_but_never_accepts_hash_conflict():
    pin = {
        "resource_kind": "artifact",
        "resource_id": str(uuid4()),
        "sha256": "a" * 64,
        "details": {"size_bytes": 1},
    }
    assert _inventory([pin, pin]) == [pin]
    with pytest.raises(DomainError):
        _inventory([pin, pin | {"sha256": "b" * 64}])


@pytest.mark.parametrize(
    "code",
    [
        "admission_required",
        "final_binding_mismatch",
        "invalid_final_config",
        "independent_integrity",
        "result_unavailable",
        "note_admission_unavailable",
    ],
)
def test_admission_denials_are_not_missing_links(code):
    with pytest.raises(DomainError) as error:
        _failure(DomainError(code, "denied", 409))
    assert error.value.code == code


def test_all_saved_sides_admitted_before_any_missing_resolution():
    ref = source()
    resolver = Mock()
    resolver.admit_snapshot.side_effect = DomainError(
        "admission_required", "right denied"
    )
    with pytest.raises(DomainError):
        _resolve(
            ResearchNoteContent(title="Private outcome", evidence=[ref]),
            [{"left_missing": True}],
            resolver,
        )
    resolver.resolve.assert_not_called()


def test_missing_reference_keeps_content_but_releases_no_link_or_binding():
    ref = source()
    resolver = Mock()
    resolver.resolve.side_effect = DomainError("artifact_missing", "private path")
    states, values = _resolve(
        ResearchNoteContent(title="Old note", evidence=[ref]), [snapshot(ref)], resolver
    )
    assert (
        states[0].state == "missing"
        and states[0].href is None
        and states[0].bindings == []
    )
    assert "private path" not in states[0].reason and values == [None]


def test_markdown_exports_literal_prose_and_immutable_inventory():
    content = ResearchNoteContent(
        title="<script>alert(1)</script>",
        body="[click](javascript:alert(1)) ![image](data:evil)",
        tags=["<img>"],
        counterexamples="No causal conclusion",
    )
    value = ResearchNoteRevision(
        id=uuid4(),
        note_id=uuid4(),
        revision=1,
        revision_sha256="a" * 64,
        created_at=datetime.now(UTC),
        change_note=None,
        content=content,
        evidence=[],
        restore_href=None,
        retained_pin_count=0,
    )
    result = markdown(value)
    assert (
        "<script>" not in result
        and "<img>" not in result
        and "[click](javascript:" not in result
    )
    assert (
        "counterexamples" in result.lower()
        and "not automatic validation" in result
        and "a" * 64 in result
    )


def test_note_models_are_append_only_and_revision_cas_is_explicit():
    value = ResearchNoteCreate(content=ResearchNoteContent(title="x"))
    with pytest.raises(ValidationError):
        value.content.title = "different"
    with pytest.raises(ValidationError):
        ResearchNoteEdit(content=value.content, base_revision=1)


def test_exact_pin_resolver_memoization_is_instance_local(monkeypatch):
    monkeypatch.setattr(
        "backend.app.research_note_evidence.advisory_lock", lambda *a: None
    )
    ref = source()
    resolver = EvidenceResolver(Mock(), uuid4(), SimpleNamespace())
    resolver.source = Mock(return_value=(snapshot(ref), []))
    first = resolver.resolve(ref)
    assert resolver.resolve(ref) is first
    resolver.source.assert_called_once()
    other = EvidenceResolver(Mock(), uuid4(), SimpleNamespace())
    assert other.resolved == {}


def metadata_resolver(
    monkeypatch, *, schema="tradegrad.final-evaluation.v1", generation=1
):
    from backend.app.models import ResearchRun

    monkeypatch.setattr(
        "backend.app.research_note_evidence.advisory_lock", lambda *a: None
    )
    owner_id, run_id, evaluation_id = uuid4(), uuid4(), uuid4()
    fields = {column.name: None for column in ResearchRun.__table__.columns}
    fields.update(
        id=run_id,
        owner_id=owner_id,
        generation=generation,
        config={"schema_version": schema},
        request_sha256="a" * 64,
    )
    run = SimpleNamespace(**fields)
    payload = {"result_bundle": {"sha256": "b" * 64}}
    evaluation = SimpleNamespace(
        id=evaluation_id,
        run_id=run_id,
        attempt_id=uuid4(),
        generation=1,
        code_sha256="c" * 64,
        payload=payload,
        payload_sha256=digest(payload),
    )
    session = Mock()
    session.scalar.side_effect = [run, evaluation, SimpleNamespace()]
    resolver = EvidenceResolver(session, owner_id, SimpleNamespace())
    snap = {
        "binding": {
            "run_id": str(run_id),
            "evaluation_id": str(evaluation_id),
            "execution_attempt_id": str(evaluation.attempt_id),
            "code_sha256": evaluation.code_sha256,
            "result_manifest_sha256": "b" * 64,
        },
        "evaluation_generation": 1,
        "run_schema_version": schema,
        "run_config_sha256": "a" * 64,
    }
    return resolver, snap, run


def test_metadata_admission_never_projects_away_current_final_generation(monkeypatch):
    resolver, snap, run = metadata_resolver(monkeypatch, generation=2)
    seen = []
    monkeypatch.setattr(
        "backend.app.final_evaluation_views.validate_final_terminal", lambda *a: None
    )
    monkeypatch.setattr(
        "backend.app.final_evaluation_services.validate_final_binding",
        lambda *a: (None, None, SimpleNamespace(), None, None),
    )

    def admission(session, actual_run, evaluation, owner, **kwargs):
        seen.append(actual_run)
        if evaluation.generation != actual_run.generation:
            raise DomainError("admission_required", "Generation mismatch")

    monkeypatch.setattr(
        "backend.app.final_result_admission.require_final_result_admission", admission
    )
    with pytest.raises(DomainError) as error:
        resolver.admit_snapshot(snap)
    assert error.value.code == "note_admission_unavailable" and seen == [run]


def test_metadata_admission_catches_terminal_denial_before_search_filter(monkeypatch):
    resolver, snap, _ = metadata_resolver(monkeypatch)
    monkeypatch.setattr(
        "backend.app.final_evaluation_views.validate_final_terminal",
        Mock(side_effect=DomainError("result_unavailable", "not terminal")),
    )
    with pytest.raises(DomainError) as error:
        resolver.admit_snapshot(snap)
    assert error.value.code == "note_admission_unavailable"


def test_saved_final_schema_cannot_be_downgraded_to_skip_admission(monkeypatch):
    resolver, snap, run = metadata_resolver(monkeypatch)
    run.config = {"schema_version": "unsupported-history"}
    with pytest.raises(DomainError) as error:
        resolver.admit_snapshot(snap)
    assert error.value.code == "note_admission_unavailable"


def test_source_request_cache_resets_after_success_and_failure():
    from backend.app.independent_services import _source_cache
    from backend.app.research_note_services import _source_request

    @_source_request
    def exercise(fail):
        assert _source_cache.get() == {}
        _source_cache.get()["private"] = "transaction"
        if fail:
            raise ValueError("test")
        return 3

    assert _source_cache.get() is None
    assert exercise(False) == 3 and _source_cache.get() is None
    with pytest.raises(ValueError):
        exercise(True)
    assert _source_cache.get() is None


def test_inherited_admission_snapshot_is_hashed_even_if_refs_removed():
    value = row()
    value.admission_snapshot = [{"binding": {"run_id": str(uuid4())}}]
    with pytest.raises(DomainError):
        validate_revision(value)


def test_note_write_builds_revision_and_empty_inventory_without_hidden_execution(
    monkeypatch,
):
    """Typed service seam, deliberately not a substitute for mandatory PG tests."""
    from backend.app import research_note_services as services
    from backend.app.models import ResearchNoteRevision as RevisionRow

    monkeypatch.setattr(services, "advisory_lock", lambda *a: None)
    monkeypatch.setattr(
        "backend.app.research_note_evidence.advisory_lock", lambda *a: None
    )
    session = Mock()
    session.scalar.return_value = None
    captured = []

    def add(value):
        captured.append(value)
        if isinstance(value, RevisionRow):
            value.created_at = datetime.now(UTC)

    session.add.side_effect = add
    monkeypatch.setattr(
        services,
        "revision_public",
        lambda session, row, resolver: validate_revision(row),
    )
    result = services.create_note(
        session,
        uuid4(),
        ResearchNoteCreate(content=ResearchNoteContent(title="No execution")),
        "bounded-key",
        SimpleNamespace(),
    )
    assert result.title == "No execution"
    revision = next(item for item in captured if isinstance(item, RevisionRow))
    assert (
        revision.evidence_snapshot == []
        and revision.admission_snapshot == []
        and revision.pins_sha256 == digest([])
    )


@pytest.mark.parametrize("missing", ["run", "evaluation", "attempt"])
def test_protected_metadata_requires_original_authority(monkeypatch, missing):
    resolver, snap, run = metadata_resolver(monkeypatch)
    if missing == "run":
        resolver.session.scalar.side_effect = [None]
    elif missing == "evaluation":
        resolver.session.scalar.side_effect = [run, None]
    else:
        values = list(resolver.session.scalar.side_effect)
        values[-1] = None
        resolver.session.scalar.side_effect = values
    with pytest.raises(DomainError) as error:
        resolver.admit_snapshot(snap)
    assert error.value.code == "note_admission_unavailable"


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_attempt_id", str(uuid4())),
        ("result_manifest_sha256", "e" * 64),
        ("code_sha256", "e" * 64),
        ("evaluation_generation", 2),
    ],
)
def test_protected_metadata_cannot_adopt_coherent_different_execution(
    monkeypatch, field, value
):
    resolver, snap, _ = metadata_resolver(monkeypatch)
    (snap if field == "evaluation_generation" else snap["binding"])[field] = value
    with pytest.raises(DomainError) as error:
        resolver.admit_snapshot(snap)
    assert error.value.code == "note_admission_unavailable"


def test_restore_rejects_bool_before_numeric_coercion():
    with pytest.raises(ValidationError):
        NoteRestore(strategy_version_id=uuid4(), selection={"spec_revision": True})


def test_empty_history_page_admits_latest_inherited_scope(monkeypatch):
    from backend.app import research_note_services as services

    latest = row()
    session = Mock()
    session.scalar.side_effect = [SimpleNamespace(id=latest.note_id), latest]
    resolver = Mock()
    resolver.admit_snapshot.side_effect = DomainError(
        "note_admission_unavailable", "denied", 409
    )
    monkeypatch.setattr(services, "EvidenceResolver", lambda *a: resolver)
    with pytest.raises(DomainError):
        services.list_revisions(
            session, latest.note_id, uuid4(), SimpleNamespace(), offset=100000
        )
    session.scalars.assert_not_called()


def test_restore_diagnostic_is_bound_to_original_primary_leaf():
    src = source()
    first, second = leaf(src), leaf(src)
    evaluation = NoteEvaluationEvidence(kind="evaluation", leaf=first)
    diagnostic = NoteDiagnosticEvidence(
        kind="diagnostic",
        run_id=uuid4(),
        config_sha256="a" * 64,
        report_sha256="b" * 64,
        result=leaf(src),
    )
    snaps = [
        snapshot(src),
        snapshot(src) | {"original_pin": second.model_dump(mode="json")},
    ]
    _, primary = reference_selection(evaluation, snaps[0])
    _, derived = reference_selection(diagnostic, snaps[1])
    selection = primary | {
        key: value for key, value in derived.items() if key.startswith("diagnostic_")
    }
    with pytest.raises(DomainError, match="complete immutable"):
        restore_href(
            NoteRestore(strategy_version_id=src.source_id, selection=selection),
            [evaluation, diagnostic],
            snaps,
        )
    assert restore_href(
        NoteRestore(strategy_version_id=src.source_id, selection=derived),
        [evaluation, diagnostic],
        snaps,
    )


def test_streamed_revision_budget_stops_before_reading_next_large_row(monkeypatch):
    from backend.app import research_note_services as services

    monkeypatch.setattr(services, "MAX_READ_BYTES", 1)
    seen = []

    def rows():
        seen.append(1)
        yield (row(),)
        seen.append(2)
        yield (row(),)

    session = Mock()
    session.execute.return_value = rows()
    resolver = SimpleNamespace(check_budget=lambda: None, revision_read_bytes=0)
    with pytest.raises(DomainError) as error:
        list(services._bounded_rows(session, Mock(), resolver, maximum=1000))
    assert error.value.code == "note_corpus_capacity" and seen == [1]


def test_streamed_revision_budget_is_shared_across_corpus_and_page(monkeypatch):
    from backend.app import research_note_services as services

    value = row()
    session = Mock()
    session.execute.side_effect = [[(value,)], [(value,)]]
    resolver = SimpleNamespace(check_budget=lambda: None, revision_read_bytes=0)
    assert list(services._bounded_rows(session, Mock(), resolver, maximum=1))
    monkeypatch.setattr(services, "MAX_READ_BYTES", resolver.revision_read_bytes)
    with pytest.raises(DomainError):
        list(services._bounded_rows(session, Mock(), resolver, maximum=1))
