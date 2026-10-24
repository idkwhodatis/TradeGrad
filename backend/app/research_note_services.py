"""Append-only notes, optimistic editing, literal search and exact Markdown export."""

from __future__ import annotations

import html
import json
import re
from functools import wraps
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from backend.app.models import (
    ResearchNote,
    ResearchNoteEvidencePin,
    ResearchNoteRevision,
)
from backend.app.research_note_evidence import MAX_PINS, EvidenceResolver
from backend.app.research_note_schemas import (
    NoteEvidenceResolution,
    ResearchNoteContent,
    ResearchNoteSummary,
)
from backend.app.research_note_schemas import (
    ResearchNoteRevision as RevisionPublic,
)
from backend.app.research_note_selection import restore_href
from backend.app.services import DomainError, advisory_lock
from packages.research_trace.contracts import canonical_bytes, digest

# Missing physical references remain readable. An admission/final contract denial
# is NOT a broken-link condition and must never disclose cached outcome prose.
MAX_READ_BYTES = 32 * 1024 * 1024

HARD_DENIALS = {
    "admission_required",
    "final_binding_mismatch",
    "invalid_final_config",
    "invalid_portable_config",
    "independent_integrity",
    "result_unavailable",
    "split_integrity",
    "invalid_split",
    "unauthorized",
    "forbidden",
}
MISSING_CODES = {"not_found", "artifact_missing", "trace_artifact_missing"}
UNAVAILABLE_CODES = {
    "trace_unavailable",
    "spec_unavailable",
    "comparison_unavailable",
    "diagnostic_parity_pending",
    "note_evidence_unavailable",
    "independent_source_depth",
}


def _source_request(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        from backend.app.independent_services import _source_cache

        token = _source_cache.set({}) if _source_cache.get() is None else None
        try:
            return function(*args, **kwargs)
        finally:
            if token is not None:
                _source_cache.reset(token)

    return wrapped


def integrity():
    return DomainError(
        "note_integrity",
        "Research note revision or retained evidence does not match its immutable hash",
        409,
    )


def owned_note(session, note_id, owner_id, *, lock=False):
    query = select(ResearchNote).where(
        ResearchNote.id == note_id, ResearchNote.owner_id == owner_id
    )
    item = session.scalar(query.with_for_update() if lock else query)
    if item is None:
        raise DomainError("not_found", "Research note not found", 404)
    return item


def revision_hash(
    note_id, revision, content, snapshots, change_note, pins_sha256, admission_snapshot
):
    return digest(
        {
            "schema_version": "tradegrad.research-note.v1",
            "note_id": str(note_id),
            "revision": revision,
            "content": content,
            "evidence_snapshot": snapshots,
            "change_note": change_note,
            "pins_sha256": pins_sha256,
            "admission_snapshot": admission_snapshot,
        }
    )


def search_text(content):
    return "\n".join(
        [
            content.title,
            *content.tags,
            *[
                getattr(content, key)
                for key in (
                    "body",
                    "hypothesis",
                    "motivation",
                    "rules",
                    "sources",
                    "behavior_evidence",
                    "control_results",
                    "counterexamples",
                    "applicability",
                    "next_steps",
                )
            ],
        ]
    )


def validate_revision(row):
    try:
        content = ResearchNoteContent.model_validate(row.content)
        if (
            row.revision_sha256
            != revision_hash(
                row.note_id,
                row.revision,
                row.content,
                row.evidence_snapshot,
                row.change_note,
                row.pins_sha256,
                row.admission_snapshot,
            )
            or len(content.evidence) != len(row.evidence_snapshot)
            or row.title != content.title
            or row.tags != list(content.tags)
            or row.status != content.status
            or row.search_text != search_text(content)
        ):
            raise ValueError("Revision contents differ")
        return content
    except (ValueError, TypeError, KeyError):
        raise integrity() from None


def _failure(error):
    if error.code in HARD_DENIALS or error.status_code in {401, 403}:
        raise error
    if error.code in MISSING_CODES:
        return (
            "missing",
            "Original evidence is missing or no longer accessible; no newer resource was substituted.",
        )
    if error.code in UNAVAILABLE_CODES:
        return (
            "unavailable",
            "Original evidence is unavailable for this exact selection; no newer result was substituted.",
        )
    if (
        error.code.endswith("integrity")
        or "pin_mismatch" in error.code
        or error.code in {"invalid_artifact", "unsafe_storage_path"}
    ):
        return (
            "corrupt",
            "Original evidence failed immutable integrity or pin verification; no evidence link is released.",
        )
    raise error


def _resolve(content, snapshots, resolver):
    resolver.admit_snapshot(snapshots)
    states, resolved = [], []
    for index, (ref, expected) in enumerate(
        zip(content.evidence, snapshots, strict=True)
    ):
        try:
            value = resolver.resolve(ref)
            if value.snapshot != expected:
                raise integrity()
            states.append(
                NoteEvidenceResolution(
                    index=index,
                    state="available",
                    reason=None,
                    bindings=value.bindings,
                    href=value.href,
                )
            )
            resolved.append(value)
        except DomainError as error:
            state, reason = _failure(error)
            states.append(
                NoteEvidenceResolution(index=index, state=state, reason=reason)
            )
            resolved.append(None)
    return states, resolved


@_source_request
def read_revision(
    session,
    note_id,
    owner_id,
    settings,
    *,
    revision=None,
    revision_sha256=None,
    resolver=None,
    purpose="result_preview",
):
    note = owned_note(session, note_id, owner_id)
    if (revision is None) != (revision_sha256 is None):
        raise DomainError(
            "invalid_note_pin",
            "Exact revision and revision hash must be supplied together",
            422,
        )
    query = select(ResearchNoteRevision).where(
        ResearchNoteRevision.note_id == note.id,
        ResearchNoteRevision.owner_id == owner_id,
    )
    if revision is not None:
        query = query.where(ResearchNoteRevision.revision == revision)
    row = session.scalar(query.order_by(ResearchNoteRevision.revision.desc()).limit(1))
    if row is None:
        raise DomainError("not_found", "Research note revision not found", 404)
    if revision_sha256 is not None and row.revision_sha256 != revision_sha256:
        raise DomainError(
            "note_revision_pin_mismatch",
            "Requested note revision hash differs; latest was not substituted",
            409,
        )
    resolver = resolver or EvidenceResolver(
        session, owner_id, settings, purpose=purpose
    )
    return revision_public(session, row, resolver)


def revision_public(session, row, resolver):
    content = validate_revision(row)
    retained = _inventory(_stored_pins(session, row))
    if digest(retained) != row.pins_sha256:
        raise integrity()
    resolver.admit_snapshot(row.admission_snapshot)
    states, _ = _resolve(content, row.evidence_snapshot, resolver)
    restored = (
        restore_href(content.restore, content.evidence, row.evidence_snapshot)
        if all(item.state == "available" for item in states)
        else None
    )
    return RevisionPublic(
        id=row.id,
        note_id=row.note_id,
        revision=row.revision,
        revision_sha256=row.revision_sha256,
        created_at=row.created_at,
        change_note=row.change_note,
        content=content,
        evidence=states,
        restore_href=restored,
        retained_pin_count=len(retained),
    )


def _inventory(pins):
    values = {}
    for pin in pins:
        key = (pin["resource_kind"], pin["resource_id"])
        if key in values and values[key] != pin:
            raise integrity()
        values[key] = pin
    if len(values) > MAX_PINS:
        raise DomainError(
            "note_evidence_capacity",
            "Note retention inventory exceeds 100000 unique registrations",
            422,
        )
    return [values[key] for key in sorted(values)]


def _stored_pins(session, row):
    return [
        {
            "resource_kind": pin.resource_kind,
            "resource_id": str(pin.resource_id),
            "sha256": pin.sha256,
            "details": pin.details,
        }
        for pin in session.scalars(
            select(ResearchNoteEvidencePin).where(
                ResearchNoteEvidencePin.revision_id == row.id,
                ResearchNoteEvidencePin.owner_id == row.owner_id,
            )
        ).all()
    ]


def _write(session, owner_id, body, key, settings, *, note_id=None):
    from uuid import UUID

    from backend.app.final_evaluation_services import _idempotency_key

    _idempotency_key(key)
    advisory_lock(session, f"submit:{owner_id}")
    if note_id is not None:
        note = owned_note(session, note_id, owner_id, lock=True)
    request_sha = digest(
        {
            "operation": "create" if note_id is None else "revise",
            "note_id": str(note_id) if note_id is not None else None,
            "body": body.model_dump(mode="json"),
        }
    )
    prior = session.scalar(
        select(ResearchNoteRevision).where(
            ResearchNoteRevision.owner_id == owner_id,
            ResearchNoteRevision.idempotency_key == key,
        )
    )
    resolver = EvidenceResolver(session, owner_id, settings)
    if prior is not None:
        if prior.request_sha256 != request_sha:
            raise DomainError(
                "idempotency_conflict",
                "This key already belongs to another note mutation",
                409,
            )
        return revision_public(session, prior, resolver)
    previous = None
    if note_id is None:
        note = ResearchNote(id=uuid4(), owner_id=owner_id)
        session.add(note)
        session.flush()
    else:
        previous = session.scalar(
            select(ResearchNoteRevision)
            .where(
                ResearchNoteRevision.note_id == note.id,
                ResearchNoteRevision.owner_id == owner_id,
            )
            .order_by(ResearchNoteRevision.revision.desc())
            .limit(1)
        )
        if previous is None:
            raise integrity()
        validate_revision(previous)
        if (
            previous.revision != body.base_revision
            or previous.revision_sha256 != body.base_revision_sha256
        ):
            raise DomainError(
                "note_revision_conflict",
                "Note changed since editing began; reopen the exact newer revision before retrying",
                409,
            )
        if previous.revision >= 10000:
            raise DomainError(
                "note_revision_capacity",
                "This note reached its 10000-revision limit",
                422,
            )
        # Even removing a final ref cannot bypass its existing outcome admission.
        revision_public(session, previous, resolver)
    snapshots, pins = [], []
    previous_refs = (
        {}
        if previous is None
        else {
            digest(ref): snapshot
            for ref, snapshot in zip(
                previous.content["evidence"], previous.evidence_snapshot, strict=True
            )
        }
    )
    copied_previous = False
    for ref in body.content.evidence:
        try:
            value = resolver.resolve(ref)
            snapshots.append(value.snapshot)
            pins.extend(value.pins)
        except DomainError as error:
            old = previous_refs.get(digest(ref))
            if old is None:
                raise
            _failure(error)  # admission denial is never a copy-previous fallback
            snapshots.append(old)
            if not copied_previous:
                # Conservative over-retention when preserving broken refs. Old
                # revisions already retain these; never release an old inventory.
                pins.extend(_stored_pins(session, previous))
                copied_previous = True
    restore_href(body.content.restore, body.content.evidence, snapshots)
    inherited = (
        {}
        if previous is None
        else {digest(value): value for value in previous.admission_snapshot}
    )
    inherited.update({digest(value): value for value in snapshots})
    admissions = [inherited[key] for key in sorted(inherited)]
    if len(admissions) > 256 or len(canonical_bytes(admissions)) > 4 * 1024**2:
        raise DomainError(
            "note_admission_capacity",
            "Note inherited evidence exceeds its 256-reference / 4 MiB budget",
            422,
        )
    inventory = _inventory(pins)
    pins_sha = digest(inventory)
    raw = body.content.model_dump(mode="json")
    number = 1 if previous is None else previous.revision + 1
    row = ResearchNoteRevision(
        id=uuid4(),
        note_id=note.id,
        owner_id=owner_id,
        revision=number,
        revision_sha256=revision_hash(
            note.id, number, raw, snapshots, body.change_note, pins_sha, admissions
        ),
        pins_sha256=pins_sha,
        content=raw,
        evidence_snapshot=snapshots,
        admission_snapshot=admissions,
        title=body.content.title,
        tags=list(body.content.tags),
        status=body.content.status,
        search_text=search_text(body.content),
        change_note=body.change_note,
        idempotency_key=key,
        request_sha256=request_sha,
    )
    session.add(row)
    session.flush()
    for pin in inventory:
        session.add(
            ResearchNoteEvidencePin(
                revision_id=row.id,
                owner_id=owner_id,
                resource_kind=pin["resource_kind"],
                resource_id=UUID(pin["resource_id"]),
                sha256=pin["sha256"],
                details=pin["details"],
            )
        )
    session.flush()
    return revision_public(session, row, resolver)


@_source_request
def create_note(session, owner_id, body, idempotency_key, settings):
    return _write(session, owner_id, body, idempotency_key, settings)


@_source_request
def create_revision(session, note_id, owner_id, body, idempotency_key, settings):
    return _write(session, owner_id, body, idempotency_key, settings, note_id=note_id)


def _summary(session, row, note, resolver):
    value = revision_public(session, row, resolver)
    return ResearchNoteSummary(
        id=note.id,
        revision=row.revision,
        revision_sha256=row.revision_sha256,
        title=value.content.title,
        tags=value.content.tags,
        status=value.content.status,
        evidence_count=len(value.evidence),
        created_at=note.created_at,
        updated_at=row.created_at,
        change_note=row.change_note,
    )


def _bounded_rows(session, query, resolver, *, maximum):
    """Stream at most one large JSON revision at a time under one request budget."""
    for count, values in enumerate(
        session.execute(query.execution_options(yield_per=1)), start=1
    ):
        resolver.check_budget()
        if count > maximum:
            raise DomainError(
                "note_corpus_capacity",
                "Note read exceeds its bounded revision count",
                422,
            )
        row = values[0]
        resolver.revision_read_bytes += len(
            canonical_bytes(
                {
                    "content": row.content,
                    "evidence_snapshot": row.evidence_snapshot,
                    "admission_snapshot": row.admission_snapshot,
                    "search_text": row.search_text,
                }
            )
        )
        if resolver.revision_read_bytes > MAX_READ_BYTES:
            raise DomainError(
                "note_corpus_capacity",
                "Note read exceeds its aggregate 32 MiB revision budget",
                422,
            )
        yield values


@_source_request
def list_notes(
    session, owner_id, settings, *, q=None, status=None, tag=None, offset=0, limit=50
):
    if not 0 <= offset <= 100000 or not 1 <= limit <= 100:
        raise DomainError("invalid_note_query", "Invalid note page bounds", 422)
    if q is not None and len(q) > 200 or tag is not None and len(tag) > 80:
        raise DomainError(
            "invalid_note_query", "Note search exceeds its bounded length", 422
        )
    newer = aliased(ResearchNoteRevision)
    query = (
        select(ResearchNoteRevision, ResearchNote)
        .join(ResearchNote, ResearchNote.id == ResearchNoteRevision.note_id)
        .where(
            ResearchNote.owner_id == owner_id,
            ResearchNoteRevision.owner_id == owner_id,
            ~select(newer.id)
            .where(
                newer.note_id == ResearchNoteRevision.note_id,
                newer.revision > ResearchNoteRevision.revision,
            )
            .exists(),
        )
    )
    resolver = EvidenceResolver(session, owner_id, settings)
    for row, _ in _bounded_rows(session, query.limit(1001), resolver, maximum=1000):
        validate_revision(row)
        resolver.admit_snapshot(row.admission_snapshot)
    if q and q.strip():
        # Literal substring, not SQL wildcards or a vector/semantic query.
        query = query.where(
            ResearchNoteRevision.search_text.icontains(q.strip(), autoescape=True)
        )
    if status:
        query = query.where(ResearchNoteRevision.status == status)
    if tag:
        query = query.where(ResearchNoteRevision.tags.contains([tag]))
    rows = _bounded_rows(
        session,
        query.order_by(ResearchNoteRevision.created_at.desc(), ResearchNote.id)
        .offset(offset)
        .limit(limit),
        resolver,
        maximum=limit,
    )
    data = [_summary(session, row, note, resolver) for row, note in rows]
    return {
        "data": data,
        "count": session.scalar(select(func.count()).select_from(query.subquery())),
    }


@_source_request
def list_revisions(session, note_id, owner_id, settings, *, offset=0, limit=50):
    note = owned_note(session, note_id, owner_id)
    if not 0 <= offset <= 100000 or not 1 <= limit <= 100:
        raise DomainError("invalid_note_query", "Invalid history page bounds", 422)
    query = select(ResearchNoteRevision).where(
        ResearchNoteRevision.note_id == note.id,
        ResearchNoteRevision.owner_id == owner_id,
    )
    # The latest revision inherits all prior outcome admission dependencies.
    # Check it before even an empty page/count can disclose history metadata.
    resolver = EvidenceResolver(session, owner_id, settings)
    latest = session.scalar(
        query.order_by(ResearchNoteRevision.revision.desc()).limit(1)
    )
    if latest is not None:
        validate_revision(latest)
        resolver.admit_snapshot(latest.admission_snapshot)
    rows = _bounded_rows(
        session,
        query.order_by(ResearchNoteRevision.revision.desc())
        .offset(offset)
        .limit(limit),
        resolver,
        maximum=limit,
    )
    return {
        "data": [_summary(session, row, note, resolver) for (row,) in rows],
        "count": session.scalar(select(func.count()).select_from(query.subquery())),
    }


def markdown(value):
    content = value.content

    def escaped(text):
        # Prose is literal user text, never active HTML, image embeds or URL syntax.
        return re.sub(
            r"([\\`*{}\[\]()#+.!_>|~-])", r"\\\1", html.escape(text, quote=True)
        )

    def heading(text):
        return escaped(text.replace("\n", " ").replace("\r", " "))

    lines = [
        f"# {heading(content.title)}",
        "",
        f"Note: {value.note_id}",
        f"Revision: {value.revision}",
        f"Revision SHA-256: {value.revision_sha256}",
        f"Status: {content.status} (human maintained; not automatic validation)",
        f"Tags: {escaped(', '.join(content.tags))}",
        "",
    ]
    for key, title in (
        ("body", "Notes"),
        ("hypothesis", "Hypothesis"),
        ("motivation", "Motivation"),
        ("rules", "Rules"),
        ("sources", "Sources"),
        ("behavior_evidence", "Behavior evidence"),
        ("control_results", "Control results"),
        ("counterexamples", "Counterexamples"),
        ("applicability", "Applicability and limitations"),
        ("next_steps", "Next steps"),
    ):
        lines.extend(
            [f"## {title}", "", escaped(getattr(content, key)) or "Not recorded.", ""]
        )
    lines.extend(
        [
            "## Immutable evidence inventory",
            "",
            "References are immutable identities, not a causal proof. Broken references never point to newer code or executions.",
            "",
        ]
    )
    for ref, resolved in zip(content.evidence, value.evidence, strict=True):
        lines.extend(
            [
                f"### {resolved.index + 1}. {heading(ref.label or ref.kind)}",
                "",
                f"Availability: {resolved.state}",
                resolved.reason
                or "Re-admitted against original ownership, final-result admission and immutable bytes.",
                "",
                "```json",
                json.dumps(ref.model_dump(mode="json"), indent=2, ensure_ascii=True)
                .replace("`", "\\u0060")
                .replace("<", "\\u003c")
                .replace(">", "\\u003e"),
                "```",
                "",
            ]
        )
        if resolved.bindings:
            lines.extend(
                [
                    "Physical execution bindings:",
                    "",
                    "```json",
                    json.dumps(
                        [
                            binding.model_dump(mode="json")
                            for binding in resolved.bindings
                        ],
                        indent=2,
                        ensure_ascii=False,
                    ),
                    "```",
                    "",
                ]
            )
        if resolved.href:
            lines.extend([f"Exact workbench path: {resolved.href}", ""])
    if value.restore_href:
        lines.extend([f"Exact saved view: {value.restore_href}", ""])
    result = "\n".join(lines)
    if len(result.encode()) > 512 * 1024:
        raise DomainError(
            "note_export_capacity",
            "Markdown export exceeds its 512 KiB byte budget",
            422,
        )
    return result
