"""Append-only native versions, with PostgreSQL atomic owner-scoped replay.

A deterministic UUID reserves each (owner, Idempotency-Key) pair within this
operation without adding mutable idempotency state. ON CONFLICT waits for a
concurrent winner; the following READ COMMITTED query observes that committed
row. Exact field comparison then distinguishes a replay from conflicting input.
No strategy module is imported, compiled for execution, or run in this process.
"""

import ast
import hashlib
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from backend.app.models import StrategyVersion
from backend.app.services import DomainError
from backend.app.strategy_schemas import StrategyCreate

# Fixed operation namespace, not a user ID or an authorization token.
STRATEGY_SUBMISSION_NAMESPACE = UUID("1ecffca9-d9df-4f49-8764-5730b651329a")


def submission_id(owner_id: UUID, key: str) -> UUID:
    if (
        not key
        or len(key) > 128
        or any(ord(char) < 33 or ord(char) > 126 for char in key)
    ):
        raise DomainError(
            "invalid_idempotency_key",
            "Idempotency-Key must be 1 to 128 printable ASCII characters without spaces",
            422,
        )
    return uuid5(STRATEGY_SUBMISSION_NAMESPACE, f"{owner_id}:{key}")


def validate_syntax(code: str) -> None:
    """Parsing is not execution, sandbox validation, or a safety proof."""
    try:
        # Parse the exact file bytes so a UTF-8 BOM and Python encoding header
        # have normal Python file semantics; persisted source stays untouched.
        ast.parse(code.encode("utf-8"), filename="<strategy-upload>", mode="exec")
    except SyntaxError as exc:
        raise DomainError(
            "strategy_syntax_error",
            "Python syntax could not be parsed. Fix the source before saving a new version.",
            422,
            {"line": exc.lineno, "column": exc.offset, "reason": exc.msg[:200]},
        ) from None
    except (ValueError, RecursionError):
        raise DomainError(
            "strategy_syntax_error",
            "Python source is invalid or too deeply nested to parse.",
            422,
        ) from None


def same_submission(
    item: StrategyVersion, owner_id: UUID, body: StrategyCreate
) -> bool:
    return (
        item.owner_id == owner_id
        and item.name == body.name
        and item.task == body.task
        and item.mode == "native_akquant"
        and item.parent_id == body.parent_id
        and item.code == body.code
        and item.content_sha256 == hashlib.sha256(body.code.encode("utf-8")).hexdigest()
    )


def create_strategy_version(
    session: Session, owner_id: UUID, body: StrategyCreate, idempotency_key: str
) -> tuple[StrategyVersion, bool]:
    """Caller commits. Never mutate an existing version, including original seeds."""
    strategy_id = submission_id(owner_id, idempotency_key)
    existing = session.scalar(
        select(StrategyVersion).where(
            StrategyVersion.id == strategy_id, StrategyVersion.owner_id == owner_id
        )
    )
    if existing is not None:
        if not same_submission(existing, owner_id, body):
            raise DomainError(
                "idempotency_conflict",
                "This Idempotency-Key was already used for different strategy input.",
            )
        return existing, False

    if body.parent_id is not None:
        parent = session.scalar(
            select(StrategyVersion).where(
                StrategyVersion.id == body.parent_id,
                StrategyVersion.owner_id == owner_id,
            )
        )
        if parent is None:
            raise DomainError("not_found", "Parent strategy version not found", 404)
        if parent.task != body.task or parent.mode != "native_akquant":
            raise DomainError(
                "incompatible_parent",
                "A derived version must keep its parent's task and native_akquant mode.",
                422,
            )

    validate_syntax(body.code)
    statement = (
        insert(StrategyVersion)
        .values(
            id=strategy_id,
            owner_id=owner_id,
            name=body.name,
            task=body.task,
            mode="native_akquant",
            code=body.code,
            content_sha256=hashlib.sha256(body.code.encode("utf-8")).hexdigest(),
            parent_id=body.parent_id,
        )
        .on_conflict_do_nothing(index_elements=[StrategyVersion.id])
        .returning(StrategyVersion.id)
    )
    created = session.scalar(statement) is not None
    # This must be a separate query, not a same-statement snapshot/CTE. PostgreSQL
    # may have waited on an uncommitted conflicting insert above.
    item = session.scalar(
        select(StrategyVersion).where(
            StrategyVersion.id == strategy_id, StrategyVersion.owner_id == owner_id
        )
    )
    if item is None or not same_submission(item, owner_id, body):
        raise DomainError(
            "idempotency_conflict",
            "This Idempotency-Key was already used for different strategy input.",
        )
    return item, created
