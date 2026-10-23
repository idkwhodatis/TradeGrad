"""M2 routes use metadata and bounded verified JSON only; never provider SDKs."""

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy import func, select

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.core.atomic_storage import publish_bytes
from backend.app.data_schemas import (
    BlockPublic,
    CoveragePublic,
    ImportCreate,
    ImportDetail,
    ImportPublic,
    ListingPublic,
    PreviewPage,
    ProviderConnectionPublic,
    ProviderConnectionUpdate,
    ProviderProbeCreate,
    ProviderPublic,
    ResolveCreate,
    UniverseCreate,
    UniversePublic,
    UploadMapping,
)
from backend.app.data_services import (
    cancel_import,
    create_universe,
    owned_dataset,
    owned_import,
    retry_import,
    submit_import,
)
from backend.app.list_queries import listing_query, query_page
from backend.app.models import (
    DataBlock,
    DataImport,
    UniverseVersion,
)
from backend.app.schemas import Page
from backend.app.services import DomainError
from backend.app.storage import read_verified, safe_path

router = APIRouter()
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]


def paginate(session, model, owner_id, offset, limit):
    clause = model.owner_id == owner_id
    return {
        "data": session.scalars(
            select(model)
            .where(clause)
            .order_by(model.created_at.desc(), model.id)
            .offset(offset)
            .limit(limit)
        ).all(),
        "count": session.scalar(select(func.count()).select_from(model).where(clause)),
    }


@router.get(
    "/data/providers",
    response_model=list[ProviderPublic],
    operation_id="list_data_providers",
)
def providers(user: CurrentUser, request: Request, session: SessionDep):
    from packages.marketdata.catalog import provider_catalog

    catalog = [
        {k: v for k, v in item.items() if k not in {"provider", "intervals"}}
        | {"upstream": item["provider"]}
        for item in provider_catalog()
    ]
    mode = request.app.state.settings.data_provider_mode
    from backend.app.provider_connections import current_connections

    connections = current_connections(session, user.id)
    latest = {}
    for p in catalog:
        job = session.scalar(
            select(DataImport)
            .where(
                DataImport.owner_id == user.id,
                DataImport.provider == p["id"],
                DataImport.kind == "probe",
                DataImport.status.in_(["ready", "partial", "failed"]),
                DataImport.progress.has_key("probe_receipt"),
            )
            .order_by(DataImport.created_at.desc(), DataImport.id)
            .limit(1)
        )
        latest[p["id"]] = job.progress["probe_receipt"] if job else None
    return [
        {
            **p,
            "latest_test": latest[p["id"]],
            "connection": connections.get(p["id"]),
            "runtime_mode": mode,
            "fixture_notice": "Explicit synthetic SDK-schema fixture mode; not live provider evidence or investment data."
            if mode == "fixture"
            else None,
        }
        for p in catalog
        if p["id"] != "portable_fixture" or mode == "fixture"
    ]


@router.post(
    "/data/provider-connections/{provider}",
    response_model=ProviderConnectionPublic,
    operation_id="update_provider_connection",
)
def update_provider_connection(
    provider: str,
    body: ProviderConnectionUpdate,
    user: CurrentUser,
    session: SessionDep,
):
    from backend.app.provider_connections import update_connection

    value = update_connection(session, user.id, provider, body)
    session.commit()
    return value


@router.get(
    "/data/provider-connections/{provider}/revisions/{revision}",
    response_model=ProviderConnectionPublic,
    operation_id="read_provider_connection_revision",
)
def read_provider_connection_revision(
    provider: str, revision: int, user: CurrentUser, session: SessionDep
):
    from backend.app.provider_connections import read_revision

    return read_revision(session, user.id, provider, revision)


@router.post(
    "/data/providers/{provider}/test",
    response_model=ImportPublic,
    status_code=202,
    operation_id="test_data_provider",
)
def test_provider(
    provider: str,
    body: ProviderProbeCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    allowed = {
        "akshare": {"600000", "000001"},
        "yfinance": {"AAPL", "MSFT"},
        "openbb": {"AAPL", "MSFT"},
    }
    if provider not in allowed or body.symbol not in allowed[provider]:
        raise DomainError(
            "unsupported_probe",
            "Choose a supported public test symbol: AKShare 600000/000001 or Yahoo/Nasdaq AAPL/MSFT",
            422,
        )
    value = ImportCreate(
        provider=provider,
        market="CN" if provider == "akshare" else "US",
        name="Bounded provider connection test",
        symbols=[body.symbol],
        start_date=body.start_date,
        end_date=body.end_date,
        expected_connection_revision=body.expected_connection_revision,
    )
    item = submit_import(
        session,
        user.id,
        value,
        idempotency_key,
        request.app.state.settings,
        kind="probe",
    )
    session.commit()
    return item


@router.post(
    "/data/resolve",
    response_model=ImportPublic,
    status_code=202,
    operation_id="resolve_data_instruments",
)
def resolve(
    body: ResolveCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    item = submit_import(
        session,
        user.id,
        body,
        idempotency_key,
        request.app.state.settings,
        kind="resolve",
    )
    session.commit()
    return item


@router.get(
    "/data/listings",
    response_model=Page[ListingPublic],
    operation_id="list_data_listings",
)
def listings(
    user: CurrentUser,
    session: SessionDep,
    listing_ids: Annotated[list[UUID] | None, Query(max_length=100)] = None,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=100),
    sort_by: str = Query("symbol", max_length=32),
    sort_direction: str = Query("asc", max_length=4),
    search: str | None = Query(None, max_length=128),
    mic: str | None = Query(None, max_length=4),
    currency: str | None = Query(None, max_length=16),
):
    page = query_page(
        session,
        listing_query(
            user.id,
            listing_ids=listing_ids,
            sort_by=sort_by,
            sort_direction=sort_direction,
            search=search,
            mic=mic,
            currency=currency,
        ),
        offset,
        limit,
    )
    return {
        "data": [item.metadata_json for item in page["data"]],
        "count": page["count"],
    }


@router.post(
    "/data/imports",
    response_model=ImportPublic,
    status_code=202,
    operation_id="create_data_import",
)
def create_import(
    body: ImportCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    item = submit_import(
        session, user.id, body, idempotency_key, request.app.state.settings
    )
    session.commit()
    return item


@router.get(
    "/data/imports", response_model=Page[ImportPublic], operation_id="list_data_imports"
)
def imports(
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return paginate(session, DataImport, user.id, offset, limit)


@router.get(
    "/data/imports/{import_id}",
    response_model=ImportDetail,
    operation_id="read_data_import",
)
def read_import(
    import_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    block_offset: int = Query(0, ge=0, le=1_000_000),
    block_limit: int = Query(1000, ge=1, le=1000),
):

    item = owned_import(session, import_id, user.id)
    blocks = session.scalars(
        select(DataBlock)
        .where(DataBlock.import_id == import_id)
        .order_by(DataBlock.symbol, DataBlock.start_date, DataBlock.id)
        .offset(block_offset)
        .limit(block_limit)
    ).all()
    total = session.scalar(
        select(func.count())
        .select_from(DataBlock)
        .where(DataBlock.import_id == import_id)
    )
    return ImportDetail(
        **ImportPublic.model_validate(item).model_dump(),
        blocks=[BlockPublic.model_validate(b) for b in blocks],
        blocks_total=total,
        blocks_offset=block_offset,
        blocks_limit=block_limit,
    )


@router.post(
    "/data/imports/{import_id}/cancel",
    response_model=ImportPublic,
    status_code=202,
    operation_id="cancel_data_import",
)
def cancel(import_id: UUID, user: CurrentUser, session: SessionDep):
    item = cancel_import(session, import_id, user.id)
    session.commit()
    return item


@router.post(
    "/data/imports/{import_id}/retry",
    response_model=ImportPublic,
    status_code=202,
    operation_id="retry_data_import",
)
def retry(import_id: UUID, user: CurrentUser, session: SessionDep, request: Request):
    item = retry_import(session, import_id, user.id, request.app.state.settings)
    session.commit()
    return item


@router.post(
    "/data/universes",
    response_model=UniversePublic,
    status_code=201,
    operation_id="create_data_universe",
)
def universe_create(body: UniverseCreate, user: CurrentUser, session: SessionDep):
    item = create_universe(session, user.id, body)
    session.commit()
    return item


@router.get(
    "/data/universes",
    response_model=Page[UniversePublic],
    operation_id="list_data_universes",
)
def universe_list(
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return paginate(session, UniverseVersion, user.id, offset, limit)


@router.get(
    "/data/universes/{universe_id}",
    response_model=UniversePublic,
    operation_id="read_data_universe",
)
def universe_read(universe_id: UUID, user: CurrentUser, session: SessionDep):
    item = session.scalar(
        select(UniverseVersion).where(
            UniverseVersion.id == universe_id, UniverseVersion.owner_id == user.id
        )
    )
    if item is None:
        raise DomainError("not_found", "Universe version not found", 404)
    return item


@router.post(
    "/data/uploads/validate-mapping",
    response_model=UploadMapping,
    operation_id="validate_upload_mapping",
)
def validate_upload_mapping(body: UploadMapping, user: CurrentUser):
    return body


@router.post(
    "/data/uploads",
    response_model=ImportPublic,
    status_code=202,
    operation_id="upload_data_file",
)
async def upload(
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
    mapping: Annotated[str, Form(max_length=20000)],
    file: Annotated[UploadFile, File()],
):
    try:
        body = UploadMapping.model_validate_json(mapping)
    except (ValidationError, ValueError):
        raise DomainError(
            "invalid_upload_mapping",
            "Upload mapping is invalid; provide columns, dates, currency, units and price semantics",
            422,
        ) from None
    settings = request.app.state.settings
    extension = Path(file.filename or "").suffix.lower()
    if extension not in {".csv", ".parquet"}:
        raise DomainError(
            "invalid_upload_type", "Only CSV and Parquet files are supported", 422
        )
    content = await file.read(settings.max_upload_bytes + 1)
    if not content or len(content) > settings.max_upload_bytes:
        raise DomainError(
            "upload_size", "Upload is empty or exceeds the configured byte limit", 413
        )
    sha = hashlib.sha256(content).hexdigest()
    key = f"uploads/{user.id}/{sha}{extension}"
    target = safe_path(settings.storage_root, key)
    item = submit_import(
        session,
        user.id,
        body,
        idempotency_key,
        settings,
        kind="upload",
        upload={
            "storage_key": key,
            "sha256": sha,
            "format": extension[1:],
            "size_bytes": len(content),
        },
    )
    # Validate ownership, quota and idempotency before writing durable bytes.
    # The outbox cannot become visible until the complete object exists.
    publish_bytes(target, content, sha256=sha, durable_root=settings.storage_root)
    session.commit()
    return item


def repair_parent_reference(dataset, settings):
    from packages.marketdata.contracts import RepairSource

    manifest = dataset.manifest
    config = manifest.get("import_request")
    prices = manifest.get("price_semantics", {})
    if (
        dataset.source != "yfinance"
        or not config
        or config.get("price_mode") != "provider_native"
        or prices.get("algorithm_version") != "yahoo-native;repair=False"
    ):
        raise DomainError(
            "repair_unavailable",
            "Select an original, unrepaired Yahoo provider-native snapshot",
            422,
        )
    content = read_verified(
        settings.storage_root,
        f"{dataset.storage_key}/manifest.json",
        dataset.content_sha256,
        10_000_000,
    )
    if json.loads(content) != manifest:
        raise DomainError(
            "snapshot_integrity",
            "Parent manifest disagrees with its immutable artifact",
            409,
        )
    files = manifest.get("artifact_files", {})
    try:
        return RepairSource(
            dataset_version_id=dataset.id,
            manifest_sha256=dataset.content_sha256,
            content_hash=manifest["content_hash"],
            raw_sha256=files["raw.json"],
            provenance_sha256=files["provenance.json"],
        )
    except (KeyError, ValueError):
        raise DomainError(
            "repair_parent_incomplete",
            "Parent lacks immutable raw/provenance references",
            422,
        ) from None


@router.post(
    "/datasets/{dataset_id}/yahoo-repair",
    response_model=ImportPublic,
    status_code=202,
    operation_id="repair_yahoo_snapshot",
)
def repair_yahoo(
    dataset_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    dataset = owned_dataset(session, dataset_id, user.id)
    settings = request.app.state.settings
    reference = repair_parent_reference(dataset, settings)
    config = dict(dataset.manifest["import_request"])
    config.update(
        price_mode="repair",
        name=dataset.name[:235] + " · Yahoo repair",
        symbols=[],
        listing_ids=[item["listing_id"] for item in dataset.manifest["listings"]],
    )
    body = ImportCreate.model_validate(config)
    item = submit_import(
        session,
        user.id,
        body,
        idempotency_key,
        settings,
        parent_version_id=dataset.id,
        derivation=reference.model_dump(mode="json"),
    )
    session.commit()
    return item


@router.post(
    "/datasets/{dataset_id}/refresh",
    response_model=ImportPublic,
    status_code=202,
    operation_id="refresh_data_snapshot",
)
def refresh(
    dataset_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    dataset = owned_dataset(session, dataset_id, user.id)
    config = dataset.manifest.get("import_request")
    if not config or dataset.source == "upload" or config.get("price_mode") == "repair":
        raise DomainError(
            "refresh_unavailable",
            "This snapshot cannot be provider-refreshed; upload a new file or explicitly repair its original Yahoo parent instead",
            422,
        )
    body = ImportCreate.model_validate(config)
    item = submit_import(
        session,
        user.id,
        body,
        idempotency_key,
        request.app.state.settings,
        parent_version_id=dataset.id,
    )
    session.commit()
    return item


@router.get(
    "/datasets/{dataset_id}/coverage",
    response_model=CoveragePublic,
    operation_id="read_data_coverage",
)
def coverage(dataset_id: UUID, user: CurrentUser, session: SessionDep):
    item = owned_dataset(session, dataset_id, user.id)
    manifest = item.manifest
    return {
        "dataset_version_id": item.id,
        "coverage": manifest.get("coverage", []),
        "quality": manifest.get("quality", {}),
        "provenance": manifest.get("provenance", []),
        "universe": manifest.get("universe", {"state": "user_fixed"}),
    }


@router.get(
    "/datasets/{dataset_id}/preview",
    response_model=PreviewPage,
    operation_id="preview_data_snapshot",
)
def preview(
    dataset_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    listing_id: UUID,
    start_date: date | None = None,
    end_date: date | None = None,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    item = owned_dataset(session, dataset_id, user.id)
    if start_date and end_date and start_date >= end_date:
        raise DomainError(
            "invalid_dates", "Preview end date must follow start date", 422
        )
    refs = item.manifest.get("preview_files", {}).get(str(listing_id))
    if refs is None:
        raise DomainError("not_found", "Listing not present in snapshot", 404)
    rows = []
    # Maximum ten-year import and monthly checkpoints bound this scan; no SQL/path inputs.
    for ref in refs:
        if start_date and ref["end_date"] <= str(start_date):
            continue
        if end_date and ref["start_date"] >= str(end_date):
            continue
        raw = read_verified(
            request.app.state.settings.storage_root,
            f"{item.storage_key}/{ref['path']}",
            ref["sha256"],
            2_000_000,
        )
        for row in json.loads(raw):
            if row["listing_id"] != str(listing_id):
                raise DomainError(
                    "snapshot_integrity", "Preview listing identity mismatch"
                )
            if (start_date is None or row["session_date"] >= str(start_date)) and (
                end_date is None or row["session_date"] < str(end_date)
            ):
                rows.append(row)
    rows.sort(key=lambda r: r["session_date"])
    page = rows[offset : offset + limit]
    if page:
        from backend.app.experiment_services import record_dataset_access
        from packages.tradegrad_adapter.config import DateWindow

        visible = DateWindow(
            start=date.fromisoformat(page[0]["session_date"]),
            end=date.fromisoformat(page[-1]["session_date"]) + timedelta(days=1),
        )
        key = f"dataset-preview:{item.id}:{listing_id}:{visible.start}:{visible.end}"
        record_dataset_access(
            session,
            user.id,
            item,
            purpose="dataset_preview",
            source_key=key,
            window=visible,
        )
        session.commit()
    return {
        "data": page,
        "count": len(rows),
        "offset": offset,
        "limit": limit,
        "listing_id": str(listing_id),
    }
