"""Database-owned restrictions. Never extend deployment capabilities or mutate configs.

Admissions must hold their existing per-owner submit/data-submit xact lock.
Management takes both in the same order, so quota updates cannot race admissions.
"""

from sqlalchemy import func, select

from backend.app.models import DataImport, ResearchRun, User, UserWorkspaceSettings


def workspace_record(session, owner_id):
    return session.get(UserWorkspaceSettings, owner_id, populate_existing=True)


def quota_limit(session, owner_id, settings, kind="runs"):
    from backend.app.services import DomainError

    user = session.get(User, owner_id, populate_existing=True)
    if not user or not user.is_active:
        raise DomainError("unauthorized", "Invalid or inactive user", 401)
    record = workspace_record(session, owner_id)
    configured = getattr(settings, f"max_active_{kind}_per_user", 4)
    override = getattr(record, f"max_active_{kind}", None)
    return min(configured, override) if override is not None else configured


def active_counts(session, owner_id):
    from backend.app.data_services import TERMINAL_IMPORT
    from backend.app.services import TERMINAL

    return (
        session.scalar(
            select(func.count())
            .select_from(ResearchRun)
            .where(
                ResearchRun.owner_id == owner_id, ResearchRun.status.not_in(TERMINAL)
            )
        ),
        session.scalar(
            select(func.count())
            .select_from(DataImport)
            .where(
                DataImport.owner_id == owner_id,
                DataImport.status.not_in(TERMINAL_IMPORT),
            )
        ),
    )


def admitted_model_settings(session, owner_id, settings):
    record = workspace_record(session, owner_id)
    disabled = set(record.disabled_model_profiles if record else [])
    return settings.model_copy(
        update={
            "model_profiles": tuple(
                profile
                for profile in settings.model_profiles
                if profile.id not in disabled
            )
        }
    )


def enforce_model_policy(session, owner_id, model):
    from backend.app.services import DomainError

    user = session.get(User, owner_id, populate_existing=True)
    if not user or not user.is_active:
        raise DomainError("unauthorized", "Invalid or inactive user", 401)
    record = workspace_record(session, owner_id)
    if (
        model.provider != "fixture"
        and record
        and model.profile_id in record.disabled_model_profiles
    ):
        raise DomainError(
            "model_profile_unavailable",
            "Operator model profile is disabled for this account",
            422,
        )
