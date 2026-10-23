"""V1 workspace management; authorization is rechecked under transaction locks."""

from importlib.metadata import PackageNotFoundError, version
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.core.security import get_password_hash
from backend.app.models import User, UserWorkspaceSettings, utcnow
from backend.app.schemas import Page, UserPublic
from backend.app.services import DomainError, advisory_lock
from backend.app.settings_schemas import (
    ManagedUserCreate,
    ManagedUserUpdate,
    QuotaPublic,
    QuotaUpdate,
    SecretReferencePublic,
    SecretReferenceUpdate,
    SelfProfileUpdate,
    SettingsStatus,
    WorkspaceDefaults,
)
from backend.app.workspace_policy import (
    active_counts,
    admitted_model_settings,
    workspace_record,
)

router = APIRouter()


def require_admin(session, actor_id):
    # The same lock serializes demotions, deactivations and account creation.
    advisory_lock(session, "settings:users")
    actor = session.get(User, actor_id, populate_existing=True)
    if not actor or not actor.is_active or not actor.is_superuser:
        raise DomainError("forbidden", "Active administrator permission required", 403)
    return actor


def owner_locks(session, owner_id):
    advisory_lock(session, f"submit:{owner_id}")
    advisory_lock(session, f"data-submit:{owner_id}")


def target_user(session, target_id):
    target = session.get(User, target_id, populate_existing=True)
    if not target:
        raise DomainError("not_found", "User not found", 404)
    return target


def writable_record(session, owner_id):
    record = workspace_record(session, owner_id)
    if record is None:
        record = UserWorkspaceSettings(
            owner_id=owner_id, defaults={}, disabled_model_profiles=[]
        )
        session.add(record)
    record.updated_at = utcnow()
    return record


def public_quota(session, owner_id, settings):
    record = workspace_record(session, owner_id)
    runs, imports = active_counts(session, owner_id)
    # Inactive accounts still need administratively visible limits.
    run_limit = getattr(record, "max_active_runs", None)
    import_limit = getattr(record, "max_active_imports", None)
    return QuotaPublic(
        user_id=owner_id,
        max_active_runs=run_limit,
        max_active_imports=import_limit,
        effective_active_runs=min(settings.max_active_runs_per_user, run_limit)
        if run_limit is not None
        else settings.max_active_runs_per_user,
        effective_active_imports=min(settings.max_active_imports_per_user, import_limit)
        if import_limit is not None
        else settings.max_active_imports_per_user,
        deployment_active_runs=settings.max_active_runs_per_user,
        deployment_active_imports=settings.max_active_imports_per_user,
        active_runs=runs,
        active_imports=imports,
    )


def profile_references(session, owner_id, settings):
    record = workspace_record(session, owner_id)
    disabled = set(record.disabled_model_profiles if record else [])
    return [
        SecretReferencePublic(
            id=p.id,
            label=p.label,
            provider=p.provider,
            model=p.model,
            enabled=p.id not in disabled,
            deployment_enabled=settings.model_profiles_enabled,
            synthetic=p.synthetic,
        )
        for p in settings.model_profiles
        if owner_id in p.owner_ids
    ]


@router.post(
    "/settings/profile", response_model=UserPublic, operation_id="update_self_profile"
)
def update_self_profile(
    body: SelfProfileUpdate, user: CurrentUser, session: SessionDep
):
    owner_locks(session, user.id)
    current = target_user(session, user.id)
    if not current.is_active:
        raise DomainError("unauthorized", "Invalid or inactive user", 401)
    current.full_name = body.full_name
    session.commit()
    return current


@router.get(
    "/settings/defaults",
    response_model=WorkspaceDefaults,
    operation_id="read_workspace_defaults",
)
def read_defaults(user: CurrentUser, session: SessionDep):
    record = workspace_record(session, user.id)
    return WorkspaceDefaults.model_validate(record.defaults if record else {})


@router.post(
    "/settings/defaults",
    response_model=WorkspaceDefaults,
    operation_id="update_workspace_defaults",
)
def update_defaults(
    body: WorkspaceDefaults, user: CurrentUser, session: SessionDep, request: Request
):
    from packages.tradegrad_adapter.model_profiles import available_profiles

    owner_locks(session, user.id)
    if not target_user(session, user.id).is_active:
        raise DomainError("unauthorized", "Invalid or inactive user", 401)
    settings = admitted_model_settings(session, user.id, request.app.state.settings)
    if body.model_profile_id not in {
        p["id"] for p in available_profiles(settings, user.id)
    }:
        raise DomainError(
            "model_profile_unavailable",
            "Choose a currently admitted model profile",
            422,
        )
    record = writable_record(session, user.id)
    record.defaults = body.model_dump(mode="json")
    session.commit()
    return body


@router.get(
    "/settings/quota", response_model=QuotaPublic, operation_id="read_workspace_quota"
)
def read_quota(user: CurrentUser, session: SessionDep, request: Request):
    return public_quota(session, user.id, request.app.state.settings)


@router.get(
    "/settings/status",
    response_model=SettingsStatus,
    operation_id="read_settings_status",
)
def read_status(user: CurrentUser, session: SessionDep, request: Request):
    from backend.app.operations_receipts import NOTE, receipt_evidence

    settings = request.app.state.settings
    revision = session.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one()
    evidence = (
        receipt_evidence(
            settings.operations_backup_receipts,
            application_ref=settings.operations_application_ref,
            schema_revision=revision,
            now=utcnow(),
            max_age_seconds=settings.operations_receipt_max_age_seconds,
        )
        if user.is_superuser
        else []
    )
    dependencies = {}
    for name in ("fastapi", "sqlalchemy", "psycopg", "pydantic", "procrastinate"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = "not_installed_in_api_environment"
    return SettingsStatus(
        permissions=["self_profile", "self_defaults", "owner_resources"]
        + (
            ["manage_users", "restrict_quotas", "restrict_existing_secret_references"]
            if user.is_superuser
            else []
        ),
        dependencies=dependencies,
        schema_revision=revision,
        backup_status=("observations_registered" if evidence else "not_reported")
        if user.is_superuser
        else "restricted",
        backup_evidence=evidence,
        backup_note=NOTE
        if user.is_superuser
        else "Backup command observations are visible only to administrators. No backup archive is exposed by the API.",
    )


@router.get(
    "/settings/users",
    response_model=Page[UserPublic],
    operation_id="list_managed_users",
)
def list_users(
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    require_admin(session, user.id)
    return {
        "data": session.scalars(
            select(User).order_by(User.email, User.id).offset(offset).limit(limit)
        ).all(),
        "count": session.scalar(select(func.count()).select_from(User)),
    }


@router.post(
    "/settings/users",
    response_model=UserPublic,
    status_code=201,
    operation_id="create_managed_user",
)
def create_user(body: ManagedUserCreate, user: CurrentUser, session: SessionDep):
    require_admin(session, user.id)
    email = str(body.email).lower().strip()
    if session.scalar(select(User.id).where(User.email == email)):
        raise DomainError(
            "user_exists", "An account already exists for this email", 409
        )
    created = User(
        email=email,
        hashed_password=get_password_hash(body.password),
        full_name=body.full_name,
        is_active=body.is_active,
        is_superuser=body.is_superuser,
    )
    session.add(created)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise DomainError(
            "user_exists", "An account already exists for this email", 409
        ) from None
    return created


@router.post(
    "/settings/users/{user_id}",
    response_model=UserPublic,
    operation_id="update_managed_user",
)
def update_user(
    user_id: UUID, body: ManagedUserUpdate, user: CurrentUser, session: SessionDep
):
    require_admin(session, user.id)
    owner_locks(session, user_id)
    target = target_user(session, user_id)
    if (
        target.is_active
        and target.is_superuser
        and not (body.is_active and body.is_superuser)
    ):
        count = session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.is_active.is_(True), User.is_superuser.is_(True))
        )
        if count <= 1:
            raise DomainError(
                "last_active_admin", "Keep at least one active administrator", 409
            )
    target.full_name, target.is_active, target.is_superuser = (
        body.full_name,
        body.is_active,
        body.is_superuser,
    )
    session.commit()
    return target


@router.get(
    "/settings/users/{user_id}/quota",
    response_model=QuotaPublic,
    operation_id="read_managed_quota",
)
def read_managed_quota(
    user_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    require_admin(session, user.id)
    target_user(session, user_id)
    return public_quota(session, user_id, request.app.state.settings)


@router.post(
    "/settings/users/{user_id}/quota",
    response_model=QuotaPublic,
    operation_id="update_managed_quota",
)
def update_quota(
    user_id: UUID,
    body: QuotaUpdate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
):
    require_admin(session, user.id)
    owner_locks(session, user_id)
    target_user(session, user_id)
    settings = request.app.state.settings
    if (
        body.max_active_runs is not None
        and body.max_active_runs > settings.max_active_runs_per_user
    ) or (
        body.max_active_imports is not None
        and body.max_active_imports > settings.max_active_imports_per_user
    ):
        raise DomainError(
            "quota_exceeds_deployment",
            "User quotas cannot exceed deployment limits",
            422,
        )
    record = writable_record(session, user_id)
    record.max_active_runs, record.max_active_imports = (
        body.max_active_runs,
        body.max_active_imports,
    )
    session.commit()
    return public_quota(session, user_id, settings)


@router.get(
    "/settings/users/{user_id}/secret-references",
    response_model=list[SecretReferencePublic],
    operation_id="list_managed_secret_references",
)
def list_secret_references(
    user_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    require_admin(session, user.id)
    target_user(session, user_id)
    return profile_references(session, user_id, request.app.state.settings)


@router.post(
    "/settings/users/{user_id}/secret-references/{profile_id}",
    response_model=list[SecretReferencePublic],
    operation_id="update_managed_secret_reference",
)
def update_secret_reference(
    user_id: UUID,
    profile_id: str,
    body: SecretReferenceUpdate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
):
    require_admin(session, user.id)
    owner_locks(session, user_id)
    target_user(session, user_id)
    settings = request.app.state.settings
    if profile_id not in {
        p.id for p in settings.model_profiles if user_id in p.owner_ids
    }:
        raise DomainError(
            "not_found", "Existing allowlisted secret reference not found", 404
        )
    record = writable_record(session, user_id)
    disabled = set(record.disabled_model_profiles)
    disabled.discard(profile_id) if body.enabled else disabled.add(profile_id)
    record.disabled_model_profiles = sorted(disabled)
    session.commit()
    return profile_references(session, user_id, settings)
