"""Append-only owner-scoped portable source admission; never execute candidate code."""

import hashlib
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backend.app.models import StrategyVersion
from backend.app.services import DomainError
from backend.app.strategy_services import submission_id, validate_syntax
from packages.strategy_sdk import StateCodec, canonical_json, hash_json, parse_manifest
from packages.tradegrad_adapter.portable_prompts import validate_user_policy_source

NAMESPACE = UUID("038d4946-eb55-4c83-bb74-c5785c0a1f20")


def normalized_source(body, identifier):
    validate_syntax(body.code)
    manifest = body.manifest
    if manifest.authoring is None:
        raise DomainError(
            "portable_declaration_required",
            "Custom source requires authoring.parameter_schema and a checksum-verified authoring.initial_state matching state_schema.",
            422,
        )
    try:
        validate_user_policy_source(body.code, manifest.strategy_kind)
        StateCodec(manifest.authoring.parameter_schema).encode(body.parameters)
        StateCodec(manifest.state_schema).decode(manifest.authoring.initial_state)
        expected_task = f"{manifest.market.lower()}_{'cross' if manifest.strategy_kind == 'portfolio' else 'time'}"
        if body.task != expected_task:
            raise ValueError("Task must match manifest market and strategy kind")
        value = manifest.model_dump(mode="json") | {
            "strategy_id": str(identifier),
            "code_sha256": hashlib.sha256(body.code.encode("utf-8")).hexdigest(),
            "parameters_sha256": hash_json(body.parameters),
            "reference_result_sha256": None,
        }
        return parse_manifest(canonical_json(value)).model_dump(mode="json")
    except (ValueError, TypeError, RecursionError) as error:
        raise DomainError(
            "portable_declaration_invalid", str(error)[:1000], 422
        ) from None


def create_portable_source(session, owner_id, body, key):
    # Validate the shared key syntax, while using a distinct operation namespace.
    identifier = uuid5(NAMESPACE, str(submission_id(owner_id, key)))
    manifest = normalized_source(body, identifier)
    values = {
        "id": identifier,
        "owner_id": owner_id,
        "name": body.name,
        "task": body.task,
        "mode": "portable",
        "code": body.code,
        "parent_id": body.parent_id,
        "content_sha256": manifest["code_sha256"],
        "manifest": manifest,
        "parameters": body.parameters,
    }
    if body.parent_id is not None:
        parent = session.scalar(
            select(StrategyVersion).where(
                StrategyVersion.id == body.parent_id,
                StrategyVersion.owner_id == owner_id,
            )
        )
        if parent is None:
            raise DomainError("not_found", "Parent portable strategy not found", 404)
        if parent.mode != "portable" or parent.task != body.task:
            raise DomainError(
                "incompatible_parent",
                "Derived portable versions must preserve mode and task",
                422,
            )
    created = (
        session.scalar(
            insert(StrategyVersion)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[StrategyVersion.id])
            .returning(StrategyVersion.id)
        )
        is not None
    )
    item = session.scalar(
        select(StrategyVersion).where(
            StrategyVersion.id == identifier, StrategyVersion.owner_id == owner_id
        )
    )
    if item is None or any(
        getattr(item, name) != value for name, value in values.items()
    ):
        raise DomainError(
            "idempotency_conflict",
            "This key already names a different immutable portable source submission",
        )
    return item, created


def portable_source_draft(item):
    """Owner-private editable copy, not a write or a runtime certification."""
    from backend.app.portable_services import reference_state
    from packages.strategy_sdk import StateField, StateSchema
    from packages.strategy_sdk.contracts import UserPolicyDeclaration

    manifest = parse_manifest(canonical_json(item.manifest))
    value = manifest.model_dump(mode="json")
    if manifest.authoring is None:
        schema, state = reference_state(manifest.strategy_kind)
        if schema != manifest.state_schema:
            raise DomainError(
                "portable_declaration_required",
                "Supply an explicit initial state for this policy schema",
                422,
            )
        name = "quantity" if manifest.strategy_kind == "event" else "top_n"
        value["authoring"] = UserPolicyDeclaration(
            parameter_schema=StateSchema(
                name="parameters",
                version=1,
                fields=(StateField(name=name, kind="integer"),),
            ),
            initial_state=state,
        ).model_dump(mode="json")
    return {
        "name": item.name[:235] + " (new version)",
        "task": item.task,
        "code": item.code,
        "parent_id": item.id,
        "manifest": value,
        "parameters": item.parameters,
    }
