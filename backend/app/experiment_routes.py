"""Owner-scoped immutable experiment, split, freeze and exposure APIs."""

from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.experiment_schemas import (
    DataUsageExposurePublic,
    ExperimentCreate,
    ExperimentPublic,
    ExposureSnapshot,
    FinalSelectionCreate,
    FinalSelectionPublic,
    SplitVersionCreate,
    SplitVersionPublic,
)
from backend.app.experiment_services import (
    create_experiment,
    create_split_version,
    freeze_final_selection,
    get_exposure_snapshot,
    owned_resource,
)
from backend.app.models import (
    DataUsageExposure,
    Experiment,
    FinalSelection,
    SplitVersion,
)
from backend.app.schemas import Page

router = APIRouter()


def _page(session, model, owner_id, offset, limit, clause=None):
    query = model.owner_id == owner_id
    if clause is not None:
        query &= clause
    return {
        "data": session.scalars(
            select(model)
            .where(query)
            .order_by(model.created_at.desc(), model.id)
            .offset(offset)
            .limit(limit)
        ).all(),
        "count": session.scalar(select(func.count()).select_from(model).where(query)),
    }


@router.post(
    "/experiments",
    response_model=ExperimentPublic,
    status_code=201,
    operation_id="create_experiment",
)
def create(body: ExperimentCreate, user: CurrentUser, session: SessionDep):
    item = create_experiment(session, user.id, body)
    session.commit()
    return item


@router.get(
    "/experiments",
    response_model=Page[ExperimentPublic],
    operation_id="list_experiments",
)
def experiments(
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return _page(session, Experiment, user.id, offset, limit)


@router.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentPublic,
    operation_id="read_experiment",
)
def experiment(experiment_id: UUID, user: CurrentUser, session: SessionDep):
    return owned_resource(session, Experiment, experiment_id, user.id)


@router.post(
    "/split-versions",
    response_model=SplitVersionPublic,
    status_code=201,
    operation_id="create_split_version",
)
def create_split(body: SplitVersionCreate, user: CurrentUser, session: SessionDep):
    item = create_split_version(session, user.id, body)
    session.commit()
    return item


@router.get(
    "/split-versions",
    response_model=Page[SplitVersionPublic],
    operation_id="list_split_versions",
)
def splits(
    user: CurrentUser,
    session: SessionDep,
    experiment_id: UUID | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    if experiment_id:
        owned_resource(session, Experiment, experiment_id, user.id)
    return _page(
        session,
        SplitVersion,
        user.id,
        offset,
        limit,
        SplitVersion.experiment_id == experiment_id if experiment_id else None,
    )


@router.get(
    "/split-versions/{split_version_id}",
    response_model=SplitVersionPublic,
    operation_id="read_split_version",
)
def split(split_version_id: UUID, user: CurrentUser, session: SessionDep):
    return owned_resource(session, SplitVersion, split_version_id, user.id)


@router.get(
    "/split-versions/{split_version_id}/exposure",
    response_model=ExposureSnapshot,
    operation_id="read_split_exposure",
)
def split_exposure(split_version_id: UUID, user: CurrentUser, session: SessionDep):
    item = owned_resource(session, SplitVersion, split_version_id, user.id)
    return get_exposure_snapshot(session, user.id, item)


@router.post(
    "/final-selections",
    response_model=FinalSelectionPublic,
    status_code=201,
    operation_id="freeze_final_selection",
)
def freeze(
    body: FinalSelectionCreate, user: CurrentUser, session: SessionDep, request: Request
):
    item = freeze_final_selection(session, user.id, body, request.app.state.settings)
    session.commit()
    return item


@router.get(
    "/final-selections",
    response_model=Page[FinalSelectionPublic],
    operation_id="list_final_selections",
)
def selections(
    user: CurrentUser,
    session: SessionDep,
    split_version_id: UUID | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    if split_version_id:
        owned_resource(session, SplitVersion, split_version_id, user.id)
    return _page(
        session,
        FinalSelection,
        user.id,
        offset,
        limit,
        FinalSelection.split_version_id == split_version_id
        if split_version_id
        else None,
    )


@router.get(
    "/final-selections/{selection_id}",
    response_model=FinalSelectionPublic,
    operation_id="read_final_selection",
)
def selection(selection_id: UUID, user: CurrentUser, session: SessionDep):
    return owned_resource(session, FinalSelection, selection_id, user.id)


@router.get(
    "/data-usage-exposures",
    response_model=Page[DataUsageExposurePublic],
    operation_id="list_data_usage_exposures",
)
def exposures(
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return _page(session, DataUsageExposure, user.id, offset, limit)
