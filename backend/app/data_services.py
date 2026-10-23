"""Durable PostgreSQL M2 control plane. SDKs and normalization stay in data workers."""

import hashlib
import json
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from backend.app.models import (
    DataImport,
    DataOutbox,
    DatasetVersion,
    Instrument,
    Listing,
    OwnerListing,
    UniverseVersion,
    VendorMapping,
    utcnow,
)
from backend.app.services import DomainError, FenceLost, advisory_lock
from packages.marketdata.contracts import Listing as ListingContract

TERMINAL_IMPORT = {"ready", "partial", "failed", "cancelled"}


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def owned_import(session, import_id, owner_id, lock=False):
    query = select(DataImport).where(
        DataImport.id == import_id, DataImport.owner_id == owner_id
    )
    if lock:
        query = query.with_for_update()
    item = session.scalar(query)
    if item is None:
        raise DomainError("not_found", "Data import not found", 404)
    return item


def owned_dataset(session, dataset_id, owner_id):
    item = session.scalar(
        select(DatasetVersion).where(
            DatasetVersion.id == dataset_id, DatasetVersion.owner_id == owner_id
        )
    )
    if item is None:
        raise DomainError("not_found", "Dataset version not found", 404)
    return item


def listing_contracts(session, owner_id, ids):
    rows = session.scalars(
        select(Listing)
        .join(OwnerListing, OwnerListing.listing_id == Listing.id)
        .where(
            OwnerListing.owner_id == owner_id,
            Listing.id.in_([UUID(str(i)) for i in ids]),
        )
    ).all()
    if len(rows) != len(set(map(str, ids))):
        raise DomainError("not_found", "One or more listings not found", 404)
    by_id = {
        str(row.id): ListingContract.model_validate(row.metadata_json) for row in rows
    }
    return [by_id[str(i)] for i in ids]


def persist_listings(session, owner_id, listings):
    # Serialize registry merges. Identity is venue/security scoped, not ticker globally.
    for item in listings:
        iid, lid = UUID(item.instrument_id), UUID(item.listing_id)
        advisory_lock(session, "listing:" + str(lid))
        if session.get(Instrument, iid) is None:
            session.add(
                Instrument(
                    id=iid,
                    asset_type=item.asset_type,
                    identity_status=item.identity_status,
                )
            )
            session.flush()
        stored = session.get(Listing, lid)
        if stored is None:
            stored = Listing(
                id=lid,
                instrument_id=iid,
                symbol=item.symbol,
                mic=item.mic,
                currency=item.currency,
                timezone=item.timezone,
                calendar=item.calendar,
                metadata_json=item.model_dump(mode="json"),
            )
            session.add(stored)
            session.flush()
        elif (
            stored.instrument_id,
            stored.mic,
            stored.currency,
            stored.timezone,
            stored.calendar,
        ) != (iid, item.mic, item.currency, item.timezone, item.calendar):
            raise DomainError(
                "listing_identity_conflict",
                "Provider listing disagrees with the persisted registry",
                422,
            )
        else:
            metadata = dict(stored.metadata_json)
            mappings = {canonical(m).decode(): m for m in metadata["vendor_symbols"]}
            mappings.update(
                {
                    canonical(m.model_dump(mode="json")).decode(): m.model_dump(
                        mode="json"
                    )
                    for m in item.vendor_symbols
                }
            )
            metadata["vendor_symbols"] = list(mappings.values())
            stored.metadata_json = metadata
        for mapping in item.vendor_symbols:
            session.execute(
                insert(VendorMapping)
                .values(
                    id=uuid5(
                        NAMESPACE_URL,
                        str(lid)
                        + ":"
                        + canonical(mapping.model_dump(mode="json")).decode(),
                    ),
                    listing_id=lid,
                    vendor=mapping.vendor,
                    symbol=mapping.symbol,
                    valid_from=mapping.valid_from,
                    valid_to=mapping.valid_to,
                )
                .on_conflict_do_nothing(index_elements=[VendorMapping.id])
            )
        session.execute(
            insert(OwnerListing)
            .values(owner_id=owner_id, listing_id=lid)
            .on_conflict_do_nothing()
        )
    session.flush()


def submit_import(
    session,
    owner_id,
    body,
    idempotency_key,
    settings,
    kind="import",
    parent_version_id=None,
    upload=None,
):
    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key)
    ):
        raise DomainError(
            "invalid_idempotency_key",
            "Idempotency-Key must be printable ASCII without spaces",
            422,
        )
    request = body.model_dump(mode="json")
    if upload is not None:
        request["upload"] = upload
    fingerprint = digest(
        {
            "kind": kind,
            "request": request,
            "parent_version_id": str(parent_version_id) if parent_version_id else None,
        }
    )
    advisory_lock(session, "data-submit:" + str(owner_id))
    previous = session.scalar(
        select(DataImport).where(
            DataImport.owner_id == owner_id,
            DataImport.idempotency_key == idempotency_key,
        )
    )
    if previous:
        if previous.request_sha256 != fingerprint:
            raise DomainError(
                "idempotency_conflict",
                "Idempotency-Key was used for a different request",
            )
        return previous
    if parent_version_id:
        owned_dataset(session, parent_version_id, owner_id)
    provider = request.get("provider", "upload")
    if kind != "upload":
        if provider == "portable_fixture":
            if settings.data_provider_mode != "fixture":
                raise DomainError(
                    "fixture_provider_disabled",
                    "Synthetic portable data is available only in fixture mode",
                    422,
                )
            if kind == "import" and not (
                "2024-01-02"
                <= request["start_date"]
                < request["end_date"]
                <= "2025-01-01"
            ):
                raise DomainError(
                    "fixture_window_unavailable",
                    "Portable synthetic fixture supports only [2024-01-02, 2025-01-01)",
                    422,
                )
        elif (provider == "akshare") != (request["market"] == "CN"):
            raise DomainError(
                "unsupported_market",
                "Provider does not support the selected market",
                422,
            )
        for symbol in request.get("symbols", []):
            if (
                len(symbol) > 80
                or not symbol.strip()
                or any(ord(c) < 32 for c in symbol)
            ):
                raise DomainError(
                    "invalid_symbol", "Symbols must be bounded printable strings", 422
                )
        modes = {
            "akshare": {"provider_native", "raw", "qfq", "hfq"},
            "yfinance": {"provider_native"},
            "openbb": {"provider_native"},
            "portable_fixture": {"raw"},
        }
        if (
            kind != "resolve"
            and request.get("price_mode", "provider_native") not in modes[provider]
        ):
            raise DomainError(
                "unsupported_price_mode",
                "Provider does not support this price mode",
                422,
            )
    ids = request.get("listing_ids", [])
    if ids:
        listings = listing_contracts(session, owner_id, ids)
        for listing in listings:
            if (request["market"] == "CN") != (listing.mic in {"XSHG", "XSHE"}):
                raise DomainError(
                    "listing_market_mismatch",
                    "Listing does not match selected market",
                    422,
                )
    universe_id = request.get("universe_version_id")
    if universe_id:
        universe = session.scalar(
            select(UniverseVersion).where(
                UniverseVersion.id == UUID(universe_id),
                UniverseVersion.owner_id == owner_id,
            )
        )
        if universe is None:
            raise DomainError("not_found", "Universe version not found", 404)
        if (
            universe.state == "current_snapshot"
            and request["start_date"] < universe.as_of.date().isoformat()
        ):
            raise DomainError(
                "universe_not_historical",
                "Current snapshot membership cannot be applied before its as_of date; choose user_fixed or evidenced historical membership",
                422,
            )
        if not ids or set(ids) != {m["listing_id"] for m in universe.members}:
            raise DomainError(
                "universe_mismatch",
                "Resolved listing IDs must exactly match the selected universe",
                422,
            )
    active = session.scalar(
        select(func.count())
        .select_from(DataImport)
        .where(
            DataImport.owner_id == owner_id, DataImport.status.not_in(TERMINAL_IMPORT)
        )
    )
    if active >= settings.max_active_imports_per_user:
        raise DomainError("import_quota", "Active import limit reached", 429)
    from packages.marketdata.contracts import (
        FIXTURE_VERSION,
        PORTABLE_FIXTURE_VERSION,
        SCHEMA_VERSION,
        TRANSFORM_VERSION,
    )

    request["_execution"] = {
        "provider_mode": settings.data_provider_mode,
        "block_days": settings.data_block_days,
        "schema_version": SCHEMA_VERSION,
        "transform_version": TRANSFORM_VERSION,
        "worker_contract": "data-import.v1",
        "fixture_version": (
            PORTABLE_FIXTURE_VERSION
            if provider == "portable_fixture"
            else FIXTURE_VERSION
        )
        if settings.data_provider_mode == "fixture"
        else None,
    }
    item = DataImport(
        id=uuid4(),
        owner_id=owner_id,
        kind=kind,
        provider=provider,
        idempotency_key=idempotency_key,
        request_sha256=fingerprint,
        request=request,
        listing_ids=ids,
        status="requested",
        generation=1,
        progress={},
        warnings=[],
        parent_version_id=parent_version_id,
    )
    session.add(item)
    session.flush()
    session.add(DataOutbox(import_id=item.id, generation=1))
    session.flush()
    return item


def cancel_import(session, import_id, owner_id):
    item = owned_import(session, import_id, owner_id, True)
    if item.status in TERMINAL_IMPORT:
        return item
    item.cancel_requested = True
    item.status = "cancelled"
    item.lease_token = None
    item.lease_expires_at = None
    item.updated_at = utcnow()
    # Every worker write is fenced. In-flight network reads may finish, but cannot publish.
    return item


def retry_import(session, import_id, owner_id, settings):
    advisory_lock(session, "data-submit:" + str(owner_id))
    item = owned_import(session, import_id, owner_id, True)
    if item.status not in {"failed", "partial", "cancelled"}:
        raise DomainError(
            "import_not_retryable",
            "Only failed, partial or cancelled imports can retry",
        )
    active = session.scalar(
        select(func.count())
        .select_from(DataImport)
        .where(
            DataImport.owner_id == owner_id, DataImport.status.not_in(TERMINAL_IMPORT)
        )
    )
    if active >= settings.max_active_imports_per_user:
        raise DomainError("import_quota", "Active import limit reached", 429)
    item.generation += 1
    item.status = "requested"
    item.cancel_requested = False
    item.lease_token = None
    item.lease_expires_at = None
    item.error = None
    item.updated_at = utcnow()
    session.add(DataOutbox(import_id=item.id, generation=item.generation))
    session.flush()
    return item


def claim_import(session, import_id, generation, settings):
    item = session.scalar(
        select(DataImport).where(DataImport.id == import_id).with_for_update()
    )
    if (
        item is None
        or item.generation != generation
        or item.status != "requested"
        or item.cancel_requested
    ):
        return None
    token = uuid4()
    item.lease_token = token
    item.lease_expires_at = utcnow() + timedelta(seconds=settings.data_lease_seconds)
    item.status = "fetching"
    item.updated_at = utcnow()
    session.flush()
    return token


def fence_import(session, import_id, generation, token, settings, renew=True):
    item = session.scalar(
        select(DataImport).where(DataImport.id == import_id).with_for_update()
    )
    if (
        item is None
        or item.generation != generation
        or item.lease_token != token
        or item.cancel_requested
        or item.status in TERMINAL_IMPORT
        or item.lease_expires_at is None
        or item.lease_expires_at <= utcnow()
    ):
        raise FenceLost()
    if renew:
        item.lease_expires_at = utcnow() + timedelta(
            seconds=settings.data_lease_seconds
        )
    item.updated_at = utcnow()
    return item


def reconcile_imports(session, settings):
    items = session.scalars(
        select(DataImport)
        .where(
            DataImport.status.not_in(TERMINAL_IMPORT | {"requested"}),
            DataImport.lease_expires_at <= utcnow(),
        )
        .with_for_update(skip_locked=True)
        .limit(100)
    ).all()
    for item in items:
        item.generation += 1
        item.status = "requested"
        item.lease_token = None
        item.lease_expires_at = None
        item.updated_at = utcnow()
        item.progress = {**item.progress, "recovered_after_worker_loss": True}
        session.add(DataOutbox(import_id=item.id, generation=item.generation))
    session.flush()
    return len(items)


def create_universe(session, owner_id, body):
    listing_contracts(session, owner_id, [m.listing_id for m in body.members])
    value = body.model_dump(mode="json")
    value["evidence"] = [
        {**record, "verification_status": "user_supplied_unverified"}
        for record in value["evidence"]
    ]
    # Deduplicate a lost-response retry without overwriting an immutable version.
    fingerprint = digest(value)
    advisory_lock(session, "universe:" + str(owner_id) + ":" + fingerprint)
    previous = session.scalar(
        select(UniverseVersion).where(
            UniverseVersion.owner_id == owner_id,
            UniverseVersion.content_sha256 == fingerprint,
        )
    )
    if previous is not None:
        return previous
    # Client evidence is retained, never upgraded to independently verified PIT.
    item = UniverseVersion(
        owner_id=owner_id,
        name=body.name,
        state=body.state,
        as_of=body.as_of,
        members=value["members"],
        evidence=value["evidence"],
        content_sha256=fingerprint,
        definition=value,
    )
    session.add(item)
    session.flush()
    return item
