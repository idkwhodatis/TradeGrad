"""Owner-local append-only configuration. Changes affect only future submissions."""

from sqlalchemy import select

from backend.app.models import ProviderConnection, utcnow
from backend.app.services import DomainError, advisory_lock
from packages.marketdata.connections import (
    PUBLIC_PROVIDERS,
    ConnectionSnapshot,
    make_snapshot,
)


def current_connections(session, owner_id):
    rows = session.scalars(
        select(ProviderConnection)
        .where(ProviderConnection.owner_id == owner_id)
        .order_by(ProviderConnection.provider, ProviderConnection.revision.desc())
        .distinct(ProviderConnection.provider)
    ).all()
    by_provider = {row.provider: row for row in rows}
    return {
        provider: {
            **(
                by_provider[provider].config
                if provider in by_provider
                else make_snapshot(owner_id, provider).model_dump(mode="json")
            ),
            "created_at": by_provider[provider].created_at
            if provider in by_provider
            else None,
        }
        for provider in PUBLIC_PROVIDERS
    }


def _append(session, owner_id, current, **changes):
    snapshot = make_snapshot(
        owner_id,
        current["provider"],
        current["revision"] + 1,
        **{
            key: changes.get(key, current[key])
            for key in ("enabled", "is_default", "default_price_mode")
        },
    )
    row = ProviderConnection(
        connection_id=snapshot.connection_id,
        owner_id=owner_id,
        provider=snapshot.provider,
        revision=snapshot.revision,
        config=snapshot.model_dump(mode="json"),
        config_sha256=snapshot.config_sha256,
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return {**row.config, "created_at": row.created_at}


def update_connection(session, owner_id, provider, body):
    if provider not in PUBLIC_PROVIDERS:
        raise DomainError("not_found", "Public provider connection not found", 404)
    advisory_lock(session, "provider-connections:" + str(owner_id))
    connections = current_connections(session, owner_id)
    current = connections[provider]
    if body.expected_revision != current["revision"]:
        raise DomainError(
            "provider_connection_conflict",
            "Connection changed. Reload the latest settings before saving.",
            409,
        )
    values = body.model_dump(exclude={"expected_revision"})
    try:
        make_snapshot(owner_id, provider, current["revision"] + 1, **values)
    except ValueError:
        raise DomainError(
            "invalid_provider_connection",
            "Choose a supported public default price mode; disabled connections cannot be defaults.",
            422,
        ) from None
    if current["revision"] and all(
        current[key] == value for key, value in values.items()
    ):
        return current
    # Owner-wide serialization makes default replacement atomic with admission.
    if body.is_default:
        for other, config in connections.items():
            if (
                other != provider
                and (other == "akshare") == (provider == "akshare")
                and config["is_default"]
            ):
                _append(session, owner_id, config, is_default=False)
    return _append(session, owner_id, current, **values)


def read_revision(session, owner_id, provider, revision):
    item = session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.owner_id == owner_id,
            ProviderConnection.provider == provider,
            ProviderConnection.revision == revision,
        )
    )
    if item is None:
        raise DomainError("not_found", "Provider connection revision not found", 404)
    return {**item.config, "created_at": item.created_at}


def bind_connection(session, owner_id, request, kind):
    """Select defaults and freeze a persisted revision in this transaction."""
    advisory_lock(session, "provider-connections:" + str(owner_id))
    connections = current_connections(session, owner_id)
    provider = request.get("provider")
    if provider is None:
        choices = [
            p
            for p, c in connections.items()
            if c["enabled"]
            and c["is_default"]
            and (p == "akshare") == (request["market"] == "CN")
        ]
        if len(choices) != 1:
            raise DomainError(
                "provider_default_unavailable",
                "Choose an enabled default provider for this market in Data Sources.",
                422,
            )
        provider = choices[0]
    if provider not in PUBLIC_PROVIDERS:
        return request
    current = connections[provider]
    expected = request.get("expected_connection_revision")
    if expected is not None and expected != current["revision"]:
        raise DomainError(
            "provider_connection_conflict",
            "Connection changed. Reload Data Sources before submitting.",
            409,
        )
    if not current["enabled"]:
        raise DomainError(
            "provider_connection_disabled",
            "Enable this provider in Data Sources before creating a new job.",
            422,
        )
    if current["revision"] == 0:
        current = _append(session, owner_id, current)
    snapshot = ConnectionSnapshot.model_validate(
        {k: v for k, v in current.items() if k != "created_at"}
    )
    result = {
        **request,
        "provider": provider,
        "_provider_connection": snapshot.model_dump(mode="json"),
    }
    if kind != "resolve" and result.get("price_mode") is None:
        result["price_mode"] = snapshot.default_price_mode
    return result
