"""Owner-first exact note revision API; all content reads re-admit evidence."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request, Response

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.research_note_schemas import (
    NoteStatus,
    ResearchNoteCreate,
    ResearchNoteEdit,
    ResearchNoteRevision,
    ResearchNoteSummary,
)
from backend.app.research_note_services import (
    create_note,
    create_revision,
    list_notes,
    list_revisions,
    markdown,
    read_revision,
)
from backend.app.schemas import Page

router = APIRouter(tags=["research-notes"])
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]
Revision = Annotated[int, Path(ge=1, le=10000)]
Hash = Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")]
Offset = Annotated[int, Query(ge=0, le=100000)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.post(
    "/research-notes",
    response_model=ResearchNoteRevision,
    status_code=201,
    operation_id="create_research_note",
)
def create(
    body: ResearchNoteCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    response = create_note(
        session, user.id, body, idempotency_key, request.app.state.settings
    )
    session.commit()
    return response


@router.get(
    "/research-notes",
    response_model=Page[ResearchNoteSummary],
    operation_id="list_research_notes",
)
def listing(
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    q: str | None = Query(None, max_length=200),
    status: NoteStatus | None = None,
    tag: str | None = Query(None, max_length=80),
    offset: Offset = 0,
    limit: Limit = 50,
):
    response = list_notes(
        session,
        user.id,
        request.app.state.settings,
        q=q,
        status=status,
        tag=tag,
        offset=offset,
        limit=limit,
    )
    session.commit()
    return response


@router.get(
    "/research-notes/{note_id}",
    response_model=ResearchNoteRevision,
    operation_id="read_research_note",
)
def latest(note_id: UUID, user: CurrentUser, session: SessionDep, request: Request):
    response = read_revision(session, note_id, user.id, request.app.state.settings)
    session.commit()
    return response


@router.get(
    "/research-notes/{note_id}/revisions",
    response_model=Page[ResearchNoteSummary],
    operation_id="list_research_note_revisions",
)
def history(
    note_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    offset: Offset = 0,
    limit: Limit = 50,
):
    response = list_revisions(
        session,
        note_id,
        user.id,
        request.app.state.settings,
        offset=offset,
        limit=limit,
    )
    session.commit()
    return response


@router.post(
    "/research-notes/{note_id}/revisions",
    response_model=ResearchNoteRevision,
    status_code=201,
    operation_id="create_research_note_revision",
)
def revise(
    note_id: UUID,
    body: ResearchNoteEdit,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    response = create_revision(
        session, note_id, user.id, body, idempotency_key, request.app.state.settings
    )
    session.commit()
    return response


@router.get(
    "/research-notes/{note_id}/revisions/{revision}",
    response_model=ResearchNoteRevision,
    operation_id="read_research_note_revision",
)
def exact(
    note_id: UUID,
    revision: Revision,
    revision_sha256: Hash,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
):
    response = read_revision(
        session,
        note_id,
        user.id,
        request.app.state.settings,
        revision=revision,
        revision_sha256=revision_sha256,
    )
    session.commit()
    return response


@router.get(
    "/research-notes/{note_id}/revisions/{revision}/export",
    response_class=Response,
    responses={200: {"content": {"text/markdown": {"schema": {"type": "string"}}}}},
    operation_id="export_research_note_revision",
)
def export(
    note_id: UUID,
    revision: Revision,
    revision_sha256: Hash,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
):
    value = read_revision(
        session,
        note_id,
        user.id,
        request.app.state.settings,
        revision=revision,
        revision_sha256=revision_sha256,
        purpose="result_download",
    )
    content = markdown(value)
    session.commit()
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="research-note-{note_id}-r{revision}.md"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
