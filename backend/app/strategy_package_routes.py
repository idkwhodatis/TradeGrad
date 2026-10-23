"""Authenticated immutable selection export and checksum-verified private download."""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.strategy_package_schemas import (
    StrategyPackageCreate,
    StrategyPackagePublic,
)
from backend.app.strategy_package_services import (
    create_strategy_package,
    get_owned_package,
    read_owned_package,
)

router = APIRouter()


@router.post(
    "/final-selections/{selection_id}/packages",
    response_model=StrategyPackagePublic,
    status_code=201,
    responses={200: {"model": StrategyPackagePublic}},
    operation_id="create_strategy_package",
)
def create_package(
    selection_id: UUID,
    body: StrategyPackageCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    response: Response,
):
    item, created = create_strategy_package(
        session, user.id, selection_id, body, request.app.state.settings
    )
    session.commit()
    response.status_code = 201 if created else 200
    return item


@router.get(
    "/strategy-packages/{package_id}",
    response_model=StrategyPackagePublic,
    operation_id="get_strategy_package",
)
def package_detail(package_id: UUID, user: CurrentUser, session: SessionDep):
    return get_owned_package(session, user.id, package_id)


@router.get(
    "/strategy-packages/{package_id}/download",
    operation_id="download_strategy_package",
    responses={200: {"content": {"application/zip": {}}}},
)
def package_download(
    package_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    item = get_owned_package(session, user.id, package_id)
    content = read_owned_package(session, user.id, item, request.app.state.settings)
    session.commit()
    return Response(
        content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="strategy-package-{item.id}.zip"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{item.archive_sha256}"',
        },
    )
